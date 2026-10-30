# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The flow engine: one asyncio task per node.

`parallel` runs children in a TaskGroup, so an error in one branch cancels its
siblings and surfaces here as the first `RunError`.
"""

from __future__ import annotations

import asyncio
import time
from typing import Protocol

from chaos.instances import POLL_INTERVAL, TERMINAL, InstanceManager
from chaos.model import (
    Delay,
    Experiment,
    Node,
    Operation,
    Parallel,
    Ref,
    Repeat,
    RunError,
    Serial,
    SignalWait,
    Wait,
    Workload,
    parse_duration,
)
from chaos.signals import Signals
from chaos.timeline import Timeline


class OperationRunner(Protocol):
    async def run_operation(self, op: Operation) -> None: ...


def first_error(group: BaseExceptionGroup) -> BaseException:
    """The error to report from a TaskGroup: the first RunError, else the first."""
    flat: list[BaseException] = []

    def walk(g: BaseExceptionGroup) -> None:
        for e in g.exceptions:
            if isinstance(e, BaseExceptionGroup):
                walk(e)
            else:
                flat.append(e)

    walk(group)
    return next((e for e in flat if isinstance(e, RunError)), flat[0])


async def gather_all(*coros) -> None:
    """Run coroutines together; on failure cancel the rest and raise the first error."""
    try:
        async with asyncio.TaskGroup() as tg:
            for coro in coros:
                tg.create_task(coro)
    except BaseExceptionGroup as group:
        raise first_error(group) from None


class Engine:
    def __init__(
        self,
        exp: Experiment,
        timeline: Timeline,
        instances: InstanceManager,
        operations: OperationRunner,
        signals: Signals | None = None,
    ) -> None:
        self.exp = exp
        self.timeline = timeline
        self.instances = instances
        self.operations = operations
        self.signals = signals or Signals()
        # The event each signal wait node last returned for, by node path.
        self.last_signal: dict[str, float] = {}
        # Set once every instance of a workload has been seen started, or the
        # workload node ended; in that case the workload is in `ended_unstarted`
        # when some instance never started, and a wait on it fails.
        self.started = {w.name: asyncio.Event() for w in exp.workloads}
        self.ended_unstarted: set[str] = set()

    async def run(self) -> None:
        await self.node(self.exp.flow)

    async def node(self, node: Node) -> None:
        label = _label(node)
        self.timeline.emit("node.started", node=node.path, **label)
        try:
            await self._dispatch(node)
        except asyncio.CancelledError:
            # A sibling in a `parallel` failed, or the run is aborting: the
            # timeline still closes this node.
            self.timeline.emit(
                "node.finished", node=node.path, status="cancelled", **label
            )
            raise
        self.timeline.emit("node.finished", node=node.path, **label)

    async def _dispatch(self, node: Node) -> None:
        if isinstance(node, Repeat):
            await self.repeat(node)
        elif isinstance(node, Serial):
            for child in node.children:
                await self.node(child)
        elif isinstance(node, Parallel):
            await gather_all(*(self.node(child) for child in node.children))
        elif isinstance(node, Delay):
            await asyncio.sleep(node.seconds)
        elif isinstance(node, Wait):
            await self.started[node.workload].wait()
            if node.workload in self.ended_unstarted:
                raise RunError(
                    f"{node.path}: {node.workload} ended before every instance started"
                )
        elif isinstance(node, SignalWait):
            await self.signal_wait(node)
        elif isinstance(node, Ref):
            if node.name in {w.name for w in self.exp.workloads}:
                await self.workload(self.exp.workload(node.name))
            else:
                await self.operations.run_operation(self.exp.operation(node.name))
        else:
            raise TypeError(f"unknown node {node!r}")

    async def repeat(self, node: Repeat) -> None:
        """Run the children in order, `times` times or until `for` has passed;
        a run that starts before then finishes."""
        loop = asyncio.get_running_loop()
        end = loop.time() + node.seconds if node.seconds is not None else None
        iteration = 0
        while (node.times is None or iteration < node.times) and (
            end is None or loop.time() < end
        ):
            iteration += 1
            self.timeline.emit("repeat.iteration", node=node.path, iteration=iteration)
            for child in node.children:
                await self.node(child)

    async def signal_wait(self, node: SignalWait) -> None:
        """Return at e + delay for the first signal event e newer than the one
        this node last returned for, with e + delay not in the past. The
        timeout bounds the wait for the event, not the delay after it."""
        began = time.monotonic()
        delay = node.delay_seconds
        try:
            at = await self.signals.next(
                node.signal,
                after=self.last_signal.get(node.path),
                not_before=began - delay,
                deadline=began + node.timeout_seconds,
            )
        except TimeoutError:
            seen = self.signals.events.get(node.signal) or []
            self.timeline.emit(
                "signal.timeout",
                node=node.path,
                signal=node.signal,
                timeout=node.timeout,
                last_t=round(seen[-1] - self.timeline.start, 3) if seen else None,
            )
            raise RunError(
                f"{node.path}: no {node.signal} signal within {node.timeout}"
            ) from None
        self.last_signal[node.path] = at
        await asyncio.sleep(max(0.0, at + delay - time.monotonic()))
        self.timeline.emit(
            "signal.waited",
            node=node.path,
            signal=node.signal,
            signal_t=round(at - self.timeline.start, 3),
            delay=node.delay or "0s",
        )

    async def workload(self, w: Workload) -> None:
        """Create every instance, poll until each is terminal or timed out, and
        collect and delete each one as soon as it is."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + parse_duration(w.timeout)
        insts = self.instances.prepare(w)
        try:
            await gather_all(*(self.instances.create(i) for i in insts))
            async with asyncio.TaskGroup() as finishing:
                pending = list(insts)
                while pending:
                    states = await asyncio.gather(
                        *(self.instances.poll(i) for i in pending)
                    )
                    if all(i.started for i in insts):
                        self.started[w.name].set()
                    timed_out = loop.time() >= deadline
                    still = []
                    for inst, state in zip(pending, states, strict=True):
                        if state in TERMINAL or timed_out:
                            if state not in TERMINAL:
                                self.timeline.emit(
                                    "instance.timed_out",
                                    workload=w.name,
                                    instance=inst.name,
                                    state=state,
                                )
                            finishing.create_task(self.instances.finish(inst))
                        else:
                            still.append(inst)
                    pending = still
                    if pending:
                        await asyncio.sleep(POLL_INTERVAL)
        except BaseExceptionGroup as group:
            raise first_error(group) from None
        finally:
            if not self.started[w.name].is_set():
                if not all(i.started for i in insts):
                    self.ended_unstarted.add(w.name)
                self.started[w.name].set()


def _label(node: Node) -> dict[str, str]:
    if isinstance(node, Ref):
        return {"name": node.name}
    if isinstance(node, Wait):
        return {"wait": node.workload, "condition": node.condition}
    if isinstance(node, SignalWait):
        return {"signal": node.signal}
    if isinstance(node, Delay):
        return {"delay": node.duration}
    if isinstance(node, Repeat):
        return {
            "repeat": f"{node.times} times" if node.times else f"for {node.duration}"
        }
    return {}
