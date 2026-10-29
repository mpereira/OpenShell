# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The REFERENCE.md variable tables are the oracle for the `envvars` builders."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from conftest import PACKAGE_DIR

from chaos import envvars
from chaos.envvars import InstanceInfo, RunInfo

ROW_RE = re.compile(r"^\| `(CHAOS_[A-Z_]+)` \|(.*)\|$")
COLUMNS = ["workload_run", "workload_check", "experiment_check"]

RUN = RunInfo(
    "exp", 3, "openshell-docker", "20260101T000000Z-abcd", 42, Path("/runs/exp/r")
)
INST = InstanceInfo(
    "counter",
    2,
    1,
    "counter-1",
    Path("/runs/exp/r/workloads/counter/instances/counter-1"),
    "/out",
)


def readme_table() -> dict[str, set[str]]:
    """Map each column of the variable table to the variables marked in it."""
    table: dict[str, set[str]] = {c: set() for c in COLUMNS}
    for line in (PACKAGE_DIR / "REFERENCE.md").read_text().splitlines():
        match = ROW_RE.match(line)
        if not match:
            continue
        cells = [c.strip() for c in match.group(2).split("|")]
        assert len(cells) == len(COLUMNS), line
        for column, cell in zip(COLUMNS, cells, strict=True):
            if cell.startswith("✓"):
                table[column].add(match.group(1))
    assert all(table.values()), "REFERENCE.md variable table not found"
    return table


def builders(exit_code: int | None) -> dict[str, dict[str, str]]:
    return {
        "workload_run": envvars.workload_run_env(RUN, INST),
        "workload_check": envvars.workload_check_env(RUN, INST, "c", exit_code),
        "experiment_check": envvars.experiment_check_env(RUN, "c"),
    }


@pytest.mark.parametrize("column", COLUMNS)
def test_scope_matches_reference(column):
    assert set(builders(0)[column]) == readme_table()[column]


def test_exit_code_unset_when_none_recorded():
    env = builders(None)["workload_check"]
    assert "CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_EXIT_CODE" not in env
    assert set(env) == readme_table()["workload_check"] - {
        "CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_EXIT_CODE"
    }


def test_output_dir_is_the_only_value_that_differs():
    run, check = builders(0)["workload_run"], builders(0)["workload_check"]
    differ = {k for k in run if run[k] != check[k]}
    assert differ == {"CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_OUTPUT_DIR"}
    assert run["CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_OUTPUT_DIR"] == "/out"
    assert check["CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_OUTPUT_DIR"] == str(
        INST.dir / "output"
    )


def test_values():
    env = builders(3)["workload_check"]
    assert env["CHAOS_EXPERIMENT_VERSION"] == "3"
    assert env["CHAOS_EXPERIMENT_RUN_SEED"] == "42"
    assert env["CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_COUNT"] == "2"
    assert env["CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_INDEX"] == "1"
    assert env["CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_EXIT_CODE"] == "3"
    assert env["CHAOS_EXPERIMENT_RUN_DIR"] == "/runs/exp/r"


def test_instance_hook_env():
    env = envvars.instance_hook_env(
        RUN,
        INST,
        {"restart-policy": "never"},
        Path("/run.sh"),
        Path("/wrap.sh"),
        Path("/env"),
        None,
    )
    expected = readme_table()["workload_run"] | {
        "CHAOS_EXPERIMENT_RUN_DIR",
        "CHAOS_EXPERIMENT_DATA_DIR",
        "CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_DIR",
        "CHAOS_EXPERIMENT_WORKLOAD_RUN_FILE",
        "CHAOS_EXPERIMENT_WORKLOAD_WRAPPER_FILE",
        "CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_ENV_FILE",
        "CHAOS_EXPERIMENT_WORKLOAD_OPTION_RESTART_POLICY",
    }
    assert set(env) == expected
    with_id = envvars.instance_hook_env(
        RUN, INST, {}, Path("/run.sh"), Path("/wrap.sh"), Path("/env"), "sb-1"
    )
    assert with_id["CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_SYSTEM_ID"] == "sb-1"


def test_operation_env_matches_reference():
    reference = (PACKAGE_DIR / "REFERENCE.md").read_text()
    documented = set(
        re.findall(r"^- `(CHAOS_EXPERIMENT_OPERATION_[A-Z_]+)`", reference, re.M)
    )
    # The runner adds the outcome file and the state dir to both.
    documented -= {
        "CHAOS_EXPERIMENT_OPERATION_OUTCOME_FILE",
        "CHAOS_EXPERIMENT_OPERATION_STATE_DIR",
    }
    options = {"policy": "/run/data/p"}
    targeted = envvars.operation_env(RUN, "a", "openshell-policy-set", options)
    option = {"CHAOS_EXPERIMENT_OPERATION_OPTION_POLICY"}
    assert (
        set(targeted)
        == documented - {"CHAOS_EXPERIMENT_OPERATION_INSTANCES_FILE"} | option
    )
    assert targeted["CHAOS_EXPERIMENT_OPERATION_SCOPE"] == "instances"
    assert targeted["CHAOS_EXPERIMENT_OPERATION_OPTION_POLICY"] == "/run/data/p"
    glob = envvars.operation_env(RUN, "a", "openshell-policy-set", options, Path("/i"))
    operations = {k for k in glob if k.startswith("CHAOS_EXPERIMENT_OPERATION_")}
    assert operations == documented | option
    assert glob["CHAOS_EXPERIMENT_OPERATION_SCOPE"] == "global"
    assert glob["CHAOS_EXPERIMENT_RUN_DIR"] == str(RUN.run_dir)
