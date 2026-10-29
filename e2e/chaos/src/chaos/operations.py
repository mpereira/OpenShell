# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Operations: the flow steps that act on the system under test.

A built-in operation goes to the injector, which applies it and undoes it. An
adapter operation runs the adapter's `run` hook for it and returns once the
hook reports the change in effect. Each run that changed something (outcome
`ok` or `failed`) pushes its `revert` hook onto the injector's undo stack, so
the runner reverts adapter operations with the faults, newest first, at the
end of the flow and on abort. An operation with `revert: none` is left as is.

Timeline: `operation.started` (with the instances it applies to), then one
`operation.completed` per instance (or one for `scope: global`) with the
hook's outcome, or `operation.error`. Each event carries `run`, which counts
the runs of the operation from 1 when a repeat runs it more than once. An
error ends the flow with ERROR; an outcome of `failed` is a result for the
checks, not an error. While the hook runs, the instances it applies to are
held: a terminal state they reach does not finish them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from chaos import envvars
from chaos.adapter import Adapter, resolve_options, run_hook
from chaos.envvars import RunInfo
from chaos.flow import gather_all
from chaos.inject import Candidate, select
from chaos.inject.base import Undo
from chaos.instances import Instance, InstanceManager, data_paths
from chaos.model import Operation, RunError
from chaos.timeline import Timeline

HOOK_TIMEOUT = 300.0
OUTCOMES = ("ok", "skipped", "failed")


class Injector(Protocol):
    undo_stack: list[Undo]

    async def run_operation(self, op: Operation) -> None: ...


def read_outcome(path: Path) -> tuple[str, str]:
    """The outcome a hook wrote: `<ok|skipped|failed> [reason]`. A hook that
    wrote nothing did not report, which is an error, never a success."""
    try:
        text = path.read_text().strip()
    except FileNotFoundError:
        text = ""
    if not text:
        raise RunError(f"{path.stem}: hook wrote no outcome")
    outcome, _, reason = text.partition(" ")
    if outcome not in OUTCOMES:
        raise RunError(
            f"{path.name}: outcome {outcome!r} is not one of {', '.join(OUTCOMES)}"
        )
    return outcome, " ".join(reason.split())


class Operations:
    def __init__(
        self,
        adapter: Adapter,
        run: RunInfo,
        timeline: Timeline,
        instances: InstanceManager,
        injector: Injector,
    ) -> None:
        self.adapter = adapter
        self.run = run
        self.timeline = timeline
        self.instances = instances
        self.injector = injector
        self.runs: dict[str, int] = {}

    @property
    def log_dir(self) -> Path:
        return self.run.run_dir / "logs" / "operations"

    async def run_operation(self, op: Operation) -> None:
        if op.builtin:
            await self.injector.run_operation(op)
            return
        run = self.runs[op.name] = self.runs.get(op.name, 0) + 1
        try:
            await self._apply(op, run)
        except RunError as err:
            self.timeline.emit(
                "operation.error",
                operation=op.name,
                uses=op.uses,
                run=run,
                error=str(err),
            )
            raise

    async def _apply(self, op: Operation, run: int) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        spec = self.adapter.operations[op.uses]
        options = resolve_options(
            spec.options, op.options, f"operations.{op.name}.options", op.uses
        )
        started = {
            "operation": op.name,
            "uses": op.uses,
            "run": run,
            "options": options,
        }
        options = data_paths(spec.options, options, self.run.run_dir)

        if op.target is None:
            # Every instance that is running now; the hook confirms the change
            # on each of them.
            running = [
                i
                for i in self.instances.all()
                if i.system_id is not None and not i.deleted and i.state == "Running"
            ]
            names = [i.name for i in running]
            instances_file = self.log_dir / f"{op.name}.instances"
            instances_file.write_text("".join(f"{n}\n" for n in names))
            self.timeline.emit(
                "operation.started", **started, scope="global", instances=names
            )
            env = envvars.operation_env(
                self.run, op.name, op.uses, options, instances_file
            )
            with self.instances.hold(running):
                await self._hook(op, env, op.name, run, None)
            return

        assert op.workload is not None
        by_name: dict[str, Instance] = {}
        candidates = []
        for inst in self.instances.by_workload.get(op.workload, []):
            if inst.system_id is None or inst.deleted:
                continue
            by_name[inst.name] = inst
            candidates.append(
                Candidate(
                    inst.name, inst.info.index, inst.workload.name, inst.system_id
                )
            )
        picks = [by_name[p.instance] for p in select(op, self.run.seed, candidates)]
        self.timeline.emit(
            "operation.started",
            **started,
            scope="instances",
            instances=[i.name for i in picks],
        )
        env = envvars.operation_env(self.run, op.name, op.uses, options)
        with self.instances.hold(picks):
            await gather_all(
                *(
                    self._hook(
                        op,
                        {**self.instances.hook_env(inst), **env},
                        f"{op.name}.{inst.name}",
                        run,
                        inst,
                    )
                    for inst in picks
                )
            )

    async def _hook(
        self,
        op: Operation,
        env: dict[str, str],
        log: str,
        run: int,
        instance: Instance | None,
    ) -> None:
        hook = f"operations.{op.uses}"
        spec = self.adapter.operations[op.uses]
        outcome_file = self.log_dir / f"{log}.outcome"
        outcome_file.unlink(missing_ok=True)
        # What `run` leaves for `revert`, per run of the operation.
        state_dir = self.log_dir / f"{log}.{run}.state"
        state_dir.mkdir(exist_ok=True)
        env = {
            **env,
            f"{envvars.PREFIX}_OPERATION_OUTCOME_FILE": str(outcome_file),
            f"{envvars.PREFIX}_OPERATION_STATE_DIR": str(state_dir),
        }
        result = await run_hook(self.adapter, hook, spec.run, env, timeout=HOOK_TIMEOUT)
        # Appended, so every run of a repeated operation keeps its output.
        self._log(log, f"run {run}", result.stdout + result.stderr)
        if not result.ok:
            tail = result.stderr.strip()[-2000:]
            raise RunError(f"operation {log}: {hook} exited {result.code}: {tail}")
        outcome, reason = read_outcome(outcome_file)
        if spec.reverts and outcome != "skipped":
            # `skipped` changed nothing. `failed` may have changed something.
            self.injector.undo_stack.append(self._undo(op, env, log, run, instance))
        fields = {"instance": instance.name} if instance is not None else {}
        self.timeline.emit(
            "operation.completed",
            operation=op.name,
            uses=op.uses,
            run=run,
            **fields,
            outcome=outcome,
            reason=reason,
        )

    def _log(self, log: str, header: str, output: str) -> None:
        with (self.log_dir / f"{log}.log").open("a") as f:
            f.write(f"--- {header}\n{output}")

    def _undo(
        self,
        op: Operation,
        env: dict[str, str],
        log: str,
        run: int,
        instance: Instance | None,
    ) -> Undo:
        """The undo entry that runs the operation's `revert` hook with the
        variables its `run` had, or is skipped once the instance is deleted."""
        hook = f"operations.{op.uses}.revert"
        script = self.adapter.operations[op.uses].revert
        what = f"revert {log} (run {run})"

        async def revert() -> None:
            result = await run_hook(
                self.adapter, hook, script, env, timeout=HOOK_TIMEOUT
            )
            self._log(log, f"revert run {run}", result.stdout + result.stderr)
            if not result.ok:
                tail = result.stderr.strip()[-2000:]
                raise RunError(f"{what}: {hook} exited {result.code}: {tail}")

        def skip() -> str | None:
            if instance is not None and instance.deleted:
                return f"{instance.name} is deleted"
            return None

        return Undo(op.name, what, revert, skip)
