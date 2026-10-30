# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Docker injector: the built-in operations, on host processes and containers.

A `process` target is a host process found through a pid file, with a `start`
hook that starts it again, as a supervisor or a Deployment would. A
`container` target is a set of Docker containers found by label.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from chaos.adapter import Target, expand, run_shell
from chaos.inject import Candidate
from chaos.inject.base import Injector, Undo
from chaos.model import Operation, RoleTarget, RunError

STOP_TIMEOUT = 10.0


async def _docker(*args: str, timeout: float = 60) -> str:
    result = await run_shell(["docker", *args], {}, timeout)
    if not result.ok:
        raise RunError(f"docker {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


def _alive(pid: int) -> bool:
    """True if pid exists and is not a zombie (a killed child of the e2e wrapper
    stays a zombie until the wrapper reaps it)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    stat = subprocess.run(
        ["ps", "-p", str(pid), "-o", "stat="],
        capture_output=True,
        text=True,
        check=False,
    )
    return (
        stat.returncode == 0
        and bool(stat.stdout.strip())
        and not stat.stdout.strip().startswith("Z")
    )


async def _wait_dead(pid: int, timeout: float) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while _alive(pid):
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.2)
    return True


def _signal(pid: int, sig: signal.Signals) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass
    except OSError as err:
        raise RunError(f"signal {sig.name} to pid {pid}: {err}") from None


class DockerInjector(Injector):
    # Process targets.

    def _pid_file(self, target: Target) -> Path:
        assert target.pid_file is not None
        return Path(expand(target.pid_file, self._run_env()))

    def _pid(self, target: Target) -> int | None:
        try:
            return int(self._pid_file(target).read_text().strip())
        except (OSError, ValueError):
            return None

    def _record_pid(self, name: str, pid: int | None) -> None:
        if pid is not None and pid not in self.pids.setdefault(name, []):
            self.pids[name].append(pid)

    def _started(self, name: str, target: Target) -> dict[str, Any]:
        """The start hook writes the new pid file."""
        pid = self._pid(target)
        self._record_pid(name, pid)
        return {"pid": pid}

    async def ensure_up(self) -> None:
        """Abort path: restart any process target that is down."""
        for name, target in self.adapter.targets.named.items():
            if target.type != "process":
                continue
            pid = self._pid(target)
            if pid is None or not _alive(pid):
                await self.start(name, target, operation=None)

    async def _kill(self, pid: int, grace: float) -> None:
        if grace > 0:
            _signal(pid, signal.SIGTERM)
            try:
                if await _wait_dead(pid, grace):
                    return
            except asyncio.CancelledError:
                # Aborted mid-grace: finish the kill so `ensure_up` sees the
                # target down and restarts it, instead of skipping a process
                # that exits right after.
                _signal(pid, signal.SIGKILL)
                await _wait_dead(pid, STOP_TIMEOUT)
                raise
        _signal(pid, signal.SIGKILL)
        if not await _wait_dead(pid, STOP_TIMEOUT):
            raise RunError(f"pid {pid} survived SIGKILL")

    # Selection.

    async def _candidates(self, op: Operation) -> tuple[str, Target, list[Candidate]]:
        if isinstance(op.target, RoleTarget):
            role = op.target.role
            target = self.adapter.role(role)
            candidates: list[Candidate] = []
            for inst in self.instances.by_workload.get(op.target.workload, []):
                if inst.system_id is None or inst.deleted:
                    continue
                env = {**os.environ, **self.instances.hook_env(inst)}
                for cid in await self._containers(target, env):
                    candidates.append(Candidate(inst.name, inst.info.index, role, cid))
            return role, target, candidates

        assert isinstance(op.target, str)
        name = op.target
        target = self.adapter.target(name)
        if target.type == "process":
            self._record_pid(name, pid := self._pid(target))
            alive = pid is not None and _alive(pid)
            return name, target, [Candidate(name, 0, name, str(pid))] if alive else []
        ids = await self._containers(target, self._run_env())
        return name, target, [Candidate(name, 0, name, cid) for cid in ids]

    async def _containers(self, target: Target, env: dict[str, str]) -> list[str]:
        filters = []
        for key, value in target.labels.items():
            key, value = expand(key, env), expand(value, env)
            if not key or not value:
                # An empty filter would match every container on the daemon.
                raise RunError(f"label selector {key}={value} expands to empty")
            filters += ["--filter", f"label={key}={value}"]
        if not filters:
            raise RunError("a container target needs at least one label")
        out = await _docker("ps", "--no-trunc", "--format", "{{.ID}}", *filters)
        return [line.strip() for line in out.splitlines() if line.strip()]

    # Operations.

    async def _apply(
        self, op: Operation, name: str, target: Target, picks: list[Candidate]
    ) -> None:
        if target.type == "process":
            await self._process_fault(op, name, target, picks)
        elif target.type == "container":
            await self._container_fault(op, picks)
        else:
            raise RunError(f"docker can't run {op.uses} on a {target.type} target")

    async def _process_fault(
        self, op: Operation, name: str, target: Target, picks: list[Candidate]
    ) -> None:
        pids = [int(p.id) for p in picks]
        restart = Undo(
            op.name, f"start {name}", lambda: self.start(name, target, op.name)
        )
        if op.uses == "restart":
            for pid in pids:
                await self._kill(pid, op.grace_seconds or 0)
            self._emit("operation.applied", op)
            await self._hold(op, [restart], hold=0)
        elif op.uses == "outage":
            for pid in pids:
                await self._kill(pid, 0)
            self._emit("operation.applied", op)
            await self._hold(op, [restart])
            self._emit("operation.reverted", op)
        elif op.uses == "freeze":
            for pid in pids:
                _signal(pid, signal.SIGSTOP)
            self._emit("operation.applied", op)
            undo = [Undo(op.name, f"SIGCONT {pid}", self._cont(pid)) for pid in pids]
            await self._hold(op, undo)
            self._emit("operation.reverted", op)
        else:
            raise RunError(f"{op.uses} is not supported on a process target")

    def _cont(self, pid: int) -> Callable[[], Awaitable[None]]:
        async def run() -> None:
            _signal(pid, signal.SIGCONT)

        return run

    async def _container_fault(self, op: Operation, picks: list[Candidate]) -> None:
        ids = [p.id for p in picks]
        if op.uses == "kill":
            for cid in ids:
                await _docker("kill", "-s", "KILL", cid)
            self._emit("operation.applied", op)
        elif op.uses == "restart-in-place":
            # SIGTERM, SIGKILL after the container's stop timeout (10s unless
            # it sets its own), then start the same container again: what
            # `docker restart` and a daemon restart do to a sandbox.
            for cid in ids:
                await _docker("restart", cid)
            self._emit("operation.applied", op)
        elif op.uses == "freeze":
            undo = [
                Undo(op.name, f"unpause {cid[:12]}", self._unpause(cid)) for cid in ids
            ]
            # On the stack before pausing, so an abort mid-pause still
            # unpauses; unpausing a running container is a no-op.
            self.undo_stack.extend(undo)
            for cid in ids:
                await _docker("pause", cid)
            self._emit("operation.applied", op)
            await self._hold(op, undo)
            self._emit("operation.reverted", op)
        else:
            raise RunError(f"{op.uses} is not supported on a container target")

    def _unpause(self, cid: str) -> Callable[[], Awaitable[None]]:
        async def run() -> None:
            result = await run_shell(["docker", "unpause", cid], {}, 60)
            if result.ok:
                return
            # A container that is running again or gone counts as reverted;
            # any other answer, such as an unreachable daemon, does not.
            state = await run_shell(
                ["docker", "inspect", "-f", "{{.State.Paused}}", cid], {}, 60
            )
            if state.ok and state.stdout.strip() == "false":
                return
            if not state.ok and "no such" in state.stderr.lower():
                return
            raise RunError(f"docker unpause {cid}: {result.stderr.strip()}")

        return run
