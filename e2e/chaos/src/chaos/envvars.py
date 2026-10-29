# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The `CHAOS_*` variables, one builder per scope.

Each function returns exactly the variables the REFERENCE.md table lists for that
scope; a variable with no meaning in a scope is absent, not empty.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PREFIX = "CHAOS_EXPERIMENT"


def option_env_name(key: str) -> str:
    """The variable suffix of an option: `restart-policy` is `RESTART_POLICY`."""
    return key.upper().replace("-", "_")


@dataclass(frozen=True)
class RunInfo:
    experiment_id: str
    experiment_version: int
    adapter: str
    run_id: str
    seed: int
    run_dir: Path


@dataclass(frozen=True)
class InstanceInfo:
    workload: str
    count: int
    index: int
    name: str
    dir: Path  # host: workloads/<w>/instances/<i>
    sandbox_output_dir: str  # the adapter's instance.output-dir

    @property
    def host_output_dir(self) -> Path:
        return self.dir / "output"


def _run(run: RunInfo) -> dict[str, str]:
    return {
        f"{PREFIX}_ID": run.experiment_id,
        f"{PREFIX}_VERSION": str(run.experiment_version),
        f"{PREFIX}_ADAPTER": run.adapter,
        f"{PREFIX}_RUN_ID": run.run_id,
        f"{PREFIX}_RUN_SEED": str(run.seed),
    }


def _dirs(run: RunInfo) -> dict[str, str]:
    return {
        f"{PREFIX}_RUN_DIR": str(run.run_dir),
        f"{PREFIX}_DATA_DIR": str(run.run_dir / "data"),
    }


def _instance(inst: InstanceInfo) -> dict[str, str]:
    return {
        f"{PREFIX}_WORKLOAD_NAME": inst.workload,
        f"{PREFIX}_WORKLOAD_INSTANCE_COUNT": str(inst.count),
        f"{PREFIX}_WORKLOAD_INSTANCE_NAME": inst.name,
        f"{PREFIX}_WORKLOAD_INSTANCE_INDEX": str(inst.index),
    }


def workload_run_env(run: RunInfo, inst: InstanceInfo) -> dict[str, str]:
    """Variables the workload `run` script sees inside the sandbox."""
    return {
        **_run(run),
        **_instance(inst),
        f"{PREFIX}_WORKLOAD_INSTANCE_OUTPUT_DIR": inst.sandbox_output_dir,
    }


def workload_check_env(
    run: RunInfo, inst: InstanceInfo, check: str, exit_code: int | None
) -> dict[str, str]:
    """Variables a workload check sees on the host."""
    env = {
        **_run(run),
        **_dirs(run),
        **_instance(inst),
        f"{PREFIX}_WORKLOAD_INSTANCE_OUTPUT_DIR": str(inst.host_output_dir),
        f"{PREFIX}_WORKLOAD_INSTANCE_DIR": str(inst.dir),
        f"{PREFIX}_WORKLOAD_CHECK_NAME": check,
    }
    if exit_code is not None:
        env[f"{PREFIX}_WORKLOAD_INSTANCE_EXIT_CODE"] = str(exit_code)
    return env


def experiment_check_env(run: RunInfo, check: str) -> dict[str, str]:
    """Variables an experiment or adapter check sees on the host."""
    return {
        **_run(run),
        **_dirs(run),
        f"{PREFIX}_CHECK_NAME": check,
    }


def run_hook_env(run: RunInfo) -> dict[str, str]:
    """Variables a run-level adapter hook (collect, start) sees."""
    return {**_run(run), **_dirs(run)}


def instance_hook_env(
    run: RunInfo,
    inst: InstanceInfo,
    options: dict[str, str],
    run_file: Path,
    wrapper_file: Path,
    env_file: Path,
    system_id: str | None,
) -> dict[str, str]:
    """Variables an instance hook (create, status, collect, delete) sees.

    `OUTPUT_DIR` is the sandbox path, as in the workload; the host copy goes
    to `<INSTANCE_DIR>/output`.
    """
    env = {
        **workload_run_env(run, inst),
        **_dirs(run),
        f"{PREFIX}_WORKLOAD_INSTANCE_DIR": str(inst.dir),
        f"{PREFIX}_WORKLOAD_RUN_FILE": str(run_file),
        f"{PREFIX}_WORKLOAD_WRAPPER_FILE": str(wrapper_file),
        f"{PREFIX}_WORKLOAD_INSTANCE_ENV_FILE": str(env_file),
    }
    for key, value in options.items():
        env[f"{PREFIX}_WORKLOAD_OPTION_{option_env_name(key)}"] = value
    if system_id is not None:
        env[f"{PREFIX}_WORKLOAD_INSTANCE_SYSTEM_ID"] = system_id
    return env


def operation_env(
    run: RunInfo,
    name: str,
    uses: str,
    options: dict[str, str],
    instances_file: Path | None = None,
) -> dict[str, str]:
    """Variables an adapter operation hook sees, apart from `OUTCOME_FILE`.

    On top of the instance hook variables for an operation with a target, or
    the run hook variables plus `INSTANCES_FILE` for `scope: global`.
    """
    env = {
        f"{PREFIX}_OPERATION_NAME": name,
        f"{PREFIX}_OPERATION_USES": uses,
        f"{PREFIX}_OPERATION_SCOPE": "instances"
        if instances_file is None
        else "global",
    }
    for key, value in options.items():
        env[f"{PREFIX}_OPERATION_OPTION_{option_env_name(key)}"] = value
    if instances_file is None:
        return env
    return {
        **run_hook_env(run),
        **env,
        f"{PREFIX}_OPERATION_INSTANCES_FILE": str(instances_file),
    }
