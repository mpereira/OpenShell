# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""What every backend's injector shares: the start hook, the timeline events
of an operation, and the undo stack that the end of the flow and an abort
unwind."""

from __future__ import annotations

import asyncio
import contextlib
import os
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from chaos import envvars
from chaos.adapter import Adapter, Target, run_hook
from chaos.envvars import RunInfo
from chaos.inject import Candidate, select
from chaos.instances import InstanceManager
from chaos.model import Operation, RoleTarget, RunError
from chaos.timeline import Timeline

START_TIMEOUT = 300.0


@dataclass
class Undo:
    operation: str
    what: str
    run: Callable[[], Awaitable[None]]
    # Returns why the undo is not needed any more, such as the instance it
    # would act on being deleted, or None to run it.
    skip: Callable[[], str | None] | None = None


class Injector(ABC):
    def __init__(
        self,
        adapter: Adapter,
        run: RunInfo,
        timeline: Timeline,
        instances: InstanceManager,
    ) -> None:
        self.adapter = adapter
        self.run = run
        self.timeline = timeline
        self.instances = instances
        self.undo_stack: list[Undo] = []
        # Every pid a process target has had, for the end-of-run leak check.
        # Empty on backends whose targets are not host processes.
        self.pids: dict[str, list[int]] = {}

    def _run_env(self) -> dict[str, str]:
        return {**os.environ, **envvars.run_hook_env(self.run)}

    # Backend hooks.

    @abstractmethod
    async def _candidates(
        self, op: Operation
    ) -> tuple[str, Target, list[Candidate]]: ...

    @abstractmethod
    async def _apply(
        self, op: Operation, name: str, target: Target, picks: list[Candidate]
    ) -> None:
        """Apply the fault to the picks, emit `operation.applied`, and for a
        duration fault hold it and emit `operation.reverted`."""

    @abstractmethod
    async def ensure_up(self) -> None:
        """Abort path: start any adapter-level target that is down."""

    def _started(self, name: str, target: Target) -> dict[str, Any]:
        """Fields of `target.restarted` after the start hook succeeded."""
        return {}

    # Targets.

    async def start(self, name: str, target: Target, operation: str | None) -> None:
        """Run the target's start hook, then its after-recovery hook."""
        assert target.start is not None
        result = await run_hook(
            self.adapter,
            f"targets.{name}.start",
            target.start,
            envvars.run_hook_env(self.run),
            START_TIMEOUT,
        )
        if not result.ok:
            raise RunError(
                f"start {name} failed (exit {result.code}): {result.stderr.strip()[-2000:]}"
            )
        self.timeline.emit(
            "target.restarted",
            target=name,
            operation=operation,
            **self._started(name, target),
        )
        if target.after_recovery:
            after = await run_hook(
                self.adapter,
                f"targets.{name}.after-recovery",
                target.after_recovery,
                envvars.run_hook_env(self.run),
                START_TIMEOUT,
            )
            if not after.ok:
                raise RunError(
                    f"after-recovery {name} failed: {after.stderr.strip()[-2000:]}"
                )

    # Operations.

    def _emit(self, event: str, op: Operation, **fields) -> None:
        self.timeline.emit(event, operation=op.name, uses=op.uses, **fields)

    async def run_operation(self, op: Operation) -> None:
        """Run a built-in operation. Returns once a one-shot fault is applied
        and undone, or a duration fault is reverted."""
        try:
            name, target, candidates = await self._candidates(op)
            picks = select(op, self.run.seed, candidates)
        except RunError as err:
            self._emit("operation.error", op, error=str(err))
            raise
        self._emit(
            "operation.started",
            op,
            run=1,
            target=name,
            scope="instances" if isinstance(op.target, RoleTarget) else "target",
            instances=[p.instance for p in picks],
            ids=[p.id for p in picks],
        )
        # The fault can put an instance in a terminal state on purpose; held,
        # it is not finished until a poll after the fault reads it so again.
        names = {p.instance for p in picks}
        held = (
            [i for i in self.instances.all() if i.name in names]
            if isinstance(op.target, RoleTarget)
            else []
        )
        try:
            with self.instances.hold(held) if held else contextlib.nullcontext():
                await self._apply(op, name, target, picks)
        except RunError as err:
            self._emit("operation.error", op, error=str(err))
            raise
        self._emit("operation.completed", op, run=1, outcome="ok")

    async def _hold(
        self, op: Operation, undo: list[Undo], hold: float | None = None
    ) -> None:
        """Push undo entries, wait out the duration, then undo in reverse.

        If the wait is cancelled (abort), the entries stay on the stack for
        `undo_all`. An entry leaves the stack only once it has succeeded.
        """
        self.undo_stack.extend(e for e in undo if e not in self.undo_stack)
        await asyncio.sleep(hold if hold is not None else op.duration_seconds or 0)
        for entry in reversed(undo):
            await entry.run()
            self.undo_stack.remove(entry)

    async def undo_all(self, abort: bool = True) -> list[str]:
        """Undo every entry on the stack, newest first: the faults still in
        effect and the adapter operations that were applied. At the end of the
        flow only adapter operations are left; on abort, both. Returns errors.
        """
        errors = []
        while self.undo_stack:
            # Pop only after the attempt: a cancelled undo stays for the next
            # `undo_all`, and a failed one is recorded before it is dropped.
            entry = self.undo_stack[-1]
            try:
                skipped = entry.skip() if entry.skip is not None else None
                if skipped is None:
                    await entry.run()
                self.timeline.emit(
                    "operation.reverted",
                    operation=entry.operation,
                    undo=entry.what,
                    abort=abort,
                    **({"skipped": skipped} if skipped is not None else {}),
                )
            except RunError as err:
                errors.append(f"undo {entry.operation} ({entry.what}): {err}")
                self.timeline.emit(
                    "operation.error", operation=entry.operation, error=str(err)
                )
            self.undo_stack.pop()
        return errors
