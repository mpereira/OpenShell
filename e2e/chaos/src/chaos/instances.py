# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Workload instances: script wrapping and the create/status/collect/delete hooks."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from chaos import envvars
from chaos.adapter import Adapter, OptionSpec, run_hook
from chaos.envvars import InstanceInfo, RunInfo
from chaos.model import RunError, Workload
from chaos.timeline import Timeline

log = logging.getLogger("chaos")

# Runs the user script as `$1`, so the script is never spliced into other shell
# text. Records attempts and the exit code under the reserved `.chaos/`
# directory, which makes both independent of the platform.
WRAPPER = """\
out=$CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_OUTPUT_DIR
mkdir -p "$out/.chaos"
rm -f "$out/.chaos/exit_code"
date +%s >> "$out/.chaos/attempts"
sh -c "$1"
code=$?
echo "$code" > "$out/.chaos/exit_code"
exit "$code"
"""

STATES = {"Pending", "Running", "Succeeded", "Failed", "Unknown"}
TERMINAL = {"Succeeded", "Failed"}
STARTED = {"Running", "Succeeded", "Failed"}

POLL_INTERVAL = 2.0
# How long collect and delete keep retrying, e.g. across a gateway outage.
RETRY_WINDOW = 120.0
HOOK_TIMEOUT = 300.0


@dataclass
class Instance:
    info: InstanceInfo
    workload: Workload
    options: dict[str, str]
    run_file: Path
    wrapper_file: Path
    env_file: Path
    system_id: str | None = None
    create_attempted: bool = False
    state: str = "Pending"
    # The word after the state on the status hook's line, if any.
    reason: str | None = None
    # A status hook output that read as Unknown was logged once already.
    status_logged: bool = False
    started: bool = False
    collected: bool = False
    deleted: bool = False
    # Operations running on the instance now, and how many have finished. While
    # an operation runs, a terminal state can be one it causes on purpose, such
    # as the stop in a restart, so the instance is not finished on it.
    holds: int = 0
    releases: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def name(self) -> str:
        return self.info.name

    @property
    def present(self) -> bool:
        """May exist on the system: a create that failed or was cancelled
        can still have made it, and delete works by name."""
        return self.create_attempted and not self.deleted


def instance_dir(run_dir: Path, workload: str, instance: str) -> Path:
    return run_dir / "workloads" / workload / "instances" / instance


def data_file(run_dir: Path, name: str) -> Path:
    """Where the runner writes the experiment's data entry `name`."""
    return run_dir / "data" / name


def write_data(run_dir: Path, name: str, value: Any) -> None:
    """Text as it is, a number or boolean as JSON, anything else as YAML."""
    if isinstance(value, str):
        text = value
    elif isinstance(value, dict | list):
        text = yaml.safe_dump(value, sort_keys=False)
    else:
        text = json.dumps(value) + "\n"
    path = data_file(run_dir, name)
    path.parent.mkdir(exist_ok=True)
    path.write_text(text)


def data_paths(
    specs: dict[str, OptionSpec], options: dict[str, str], run_dir: Path
) -> dict[str, str]:
    """`options` with each data option's entry name replaced by its file path."""
    return {
        key: str(data_file(run_dir, value)) if specs[key].data and value else value
        for key, value in options.items()
    }


def read_exit_code(inst_dir: Path) -> int | None:
    path = inst_dir / "output" / ".chaos" / "exit_code"
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


class InstanceManager:
    def __init__(self, run: RunInfo, adapter: Adapter, timeline: Timeline) -> None:
        self.run = run
        self.adapter = adapter
        self.timeline = timeline
        self.by_workload: dict[str, list[Instance]] = {}
        # Loop time by which cleanup must end; set by the runner's abort.
        self.deadline: float | None = None

    def all(self) -> list[Instance]:
        return [i for insts in self.by_workload.values() for i in insts]

    def prepare(self, workload: Workload) -> list[Instance]:
        """Write the per-workload launch files and build the instance list."""
        options = self.adapter.resolve_options(
            workload.options, f"workloads.{workload.name}.options"
        )
        options = data_paths(self.adapter.options, options, self.run.run_dir)
        wl_dir = self.run.run_dir / "workloads" / workload.name
        wl_dir.mkdir(parents=True, exist_ok=True)
        run_file = wl_dir / "run.sh"
        run_file.write_text(workload.run)
        wrapper_file = wl_dir / "wrapper.sh"
        wrapper_file.write_text(WRAPPER)

        instances = []
        for index in range(workload.instances):
            name = f"{workload.name}-{index}"
            inst_dir = instance_dir(self.run.run_dir, workload.name, name)
            (inst_dir / "output").mkdir(parents=True, exist_ok=True)
            (inst_dir / "logs").mkdir(exist_ok=True)
            info = InstanceInfo(
                workload=workload.name,
                count=workload.instances,
                index=index,
                name=name,
                dir=inst_dir,
                sandbox_output_dir=self.adapter.instance.output_dir,
            )
            env_file = inst_dir / "env"
            env = {**workload.env, **envvars.workload_run_env(self.run, info)}
            env_file.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
            instances.append(
                Instance(info, workload, options, run_file, wrapper_file, env_file)
            )
        self.by_workload[workload.name] = instances
        return instances

    @contextmanager
    def hold(self, insts: list[Instance]) -> Iterator[None]:
        """Keep `insts` from being finished on a terminal state while the
        block runs."""
        for inst in insts:
            inst.holds += 1
        try:
            yield
        finally:
            for inst in insts:
                inst.holds -= 1
                inst.releases += 1

    def hook_env(self, inst: Instance) -> dict[str, str]:
        return envvars.instance_hook_env(
            self.run,
            inst.info,
            inst.options,
            inst.run_file,
            inst.wrapper_file,
            inst.env_file,
            inst.system_id,
        )

    async def create(self, inst: Instance) -> None:
        inst.create_attempted = True
        result = await run_hook(
            self.adapter,
            "instance.create",
            self.adapter.instance.create,
            self.hook_env(inst),
        )
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if not result.ok or not lines:
            raise RunError(
                f"create {inst.name} failed (exit {result.code}): {result.stderr.strip()[-2000:]}"
            )
        inst.system_id = lines[-1]
        self.timeline.emit(
            "instance.created",
            workload=inst.workload.name,
            instance=inst.name,
            system_id=inst.system_id,
        )

    async def poll(self, inst: Instance) -> str:
        """Read the instance state; a failing status hook reads as Unknown.

        A terminal state read while an operation held the instance is recorded
        but returned as Unknown, so the caller polls again.
        """
        held, releases = inst.holds > 0, inst.releases
        result = await run_hook(
            self.adapter,
            "instance.status",
            self.adapter.instance.status,
            self.hook_env(inst),
            timeout=60,
        )
        # The hook prints `STATE` or `STATE reason` on its last line.
        words = (
            result.stdout.strip().splitlines()[-1].split()
            if result.ok and result.stdout.strip()
            else []
        )
        parsed = bool(words) and words[0] in STATES
        state = words[0] if parsed else "Unknown"
        reason = words[1] if parsed and len(words) > 1 else None
        if not parsed and not inst.status_logged:
            # Once per instance, so a hook bug is visible in harness.log.
            inst.status_logged = True
            log.warning(
                "status hook for %s read as Unknown: exit %d, stdout %r, stderr %r",
                inst.name,
                result.code,
                result.stdout.strip(),
                result.stderr.strip(),
            )
        if state != inst.state or reason != inst.reason:
            inst.state, inst.reason = state, reason
            self.timeline.emit(
                "instance.state",
                workload=inst.workload.name,
                instance=inst.name,
                state=state,
                **({"reason": reason} if reason else {}),
            )
        if state in STARTED:
            inst.started = True
        if state in TERMINAL and (held or inst.holds > 0 or inst.releases != releases):
            return "Unknown"
        return state

    def _timeout(self, default: float) -> float:
        """`default`, or less when the cleanup deadline is nearer."""
        if self.deadline is None:
            return default
        left = self.deadline - asyncio.get_running_loop().time()
        return max(1.0, min(default, left))

    async def _retry(self, inst: Instance, hook: str, what: str) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + RETRY_WINDOW
        while True:
            result = await run_hook(
                self.adapter,
                f"instance.{what}",
                hook,
                self.hook_env(inst),
                timeout=self._timeout(HOOK_TIMEOUT),
            )
            if result.ok:
                return
            if loop.time() >= deadline:
                raise RunError(
                    f"{what} {inst.name} failed (exit {result.code}): {result.stderr.strip()[-2000:]}"
                )
            await asyncio.sleep(POLL_INTERVAL)

    async def finish(self, inst: Instance) -> None:
        """Collect, then delete. Safe to call more than once. A failed collect
        still deletes the instance, then raises."""
        async with inst._lock:
            if not inst.present:
                return
            failed: RunError | None = None
            # Without an id the create failed; there is nothing to collect.
            if inst.system_id is not None and not inst.collected:
                try:
                    await self._retry(inst, self.adapter.instance.collect, "collect")
                except RunError as err:
                    failed = err
                else:
                    inst.collected = True
                    self.timeline.emit(
                        "instance.collected",
                        workload=inst.workload.name,
                        instance=inst.name,
                        exit_code=read_exit_code(inst.info.dir),
                    )
            await self._retry(inst, self.adapter.instance.delete, "delete")
            inst.deleted = True
            self.timeline.emit(
                "instance.deleted", workload=inst.workload.name, instance=inst.name
            )
            if failed is not None:
                raise failed

    async def delete_only(self, inst: Instance) -> None:
        """One delete attempt for the forced exit; no collect, no retries. The
        instance counts as deleted only when the hook exits 0."""
        if not inst.present:
            return
        result = await run_hook(
            self.adapter,
            "instance.delete",
            self.adapter.instance.delete,
            self.hook_env(inst),
            timeout=self._timeout(60),
        )
        if not result.ok:
            raise RunError(
                f"delete {inst.name} ({inst.system_id}) failed (exit {result.code}): "
                f"{result.stderr.strip()[-2000:]}"
            )
        inst.deleted = True
        self.timeline.emit(
            "instance.deleted", workload=inst.workload.name, instance=inst.name
        )
