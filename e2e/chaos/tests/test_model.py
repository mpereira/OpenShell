# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from conftest import ADAPTERS, EXPERIMENTS, FIXTURES

from chaos.adapter import load_adapter, validate_adapter
from chaos.model import (
    ExperimentError,
    Operation,
    Parallel,
    Ref,
    Repeat,
    Serial,
    Wait,
    apply_overrides,
    load_experiment,
    parse_duration,
    parse_experiment,
    read_yaml,
)

# Each invalid fixture and a pattern its error must contain; the pattern names
# the offending field or flow node.
INVALID = {
    "invalid-api-version": r"apiVersion: Input should be 'chaos/v1alpha1'",
    "invalid-no-api-version": r"apiVersion: Field required",
    "invalid-unknown-key": r"colour: Extra inputs are not permitted",
    "invalid-missing-description": r"description: Field required",
    "invalid-chaos-env": r"workloads\.0\.env: .*'CHAOS_X' must not start with CHAOS_",
    "invalid-flow-missing-name": r"flow: 'f' is never used",
    "invalid-flow-repeated-name": r"flow\.serial\[1\]: 'w' appears more than once",
    "invalid-metric-name": r"metrics\.0: .*'rate\(x\[1m\]\)' is not a metric family name",
    "invalid-metric-entry-both": r"metrics\.0: .*set exactly one of family and expr",
    "invalid-metric-check-no-bound": r"checks\.0\.metric: .*set equals, at-least or at-most",
    "invalid-metric-check-undeclared": r"checks\.0\.metric: 'other_total' is not a family under metrics:",
    "invalid-wait-outside-parallel": r"flow\.serial\[1\]\.wait: wait on 'w' must be in a sibling branch",
    "invalid-no-checks": r"workloads\.0\.checks: List should have at least 1 item",
    "invalid-outage-no-duration": r"operations\.0: .*outage requires duration",
    "invalid-restart-duration": r"operations\.0: .*restart is one-shot and takes no duration",
    "invalid-role-fault-no-wait": r"operations\.f: must come after wait: \{workload: w, condition: started\}",
    "invalid-role-fault-wait-after": r"operations\.f: must come after wait: \{workload: w, condition: started\}",
    "invalid-data-name": r"data\.Bad_Name: want lowercase letters, digits, '-'",
    "invalid-operation-no-wait": r"operations\.a: must come after wait: \{workload: w, condition: started\}",
    "invalid-operation-global-select": r"operations\.0: .*scope global takes no select",
    "invalid-operation-no-targeting": r"operations\.0: .*set exactly one of target and scope",
    "invalid-operation-name-clash": r"duplicate workload/operation name 'w'",
    "invalid-expectation-issue": r"expectations\.0\.issue: .*must be a URL or look like <owner>/<repo>#<number>",
    "invalid-expectation-unknown-check": r"expectations\.0\.checks: no check 'w/exited-ok'",
    "invalid-expectation-shared-check": r"expectations\.1\.checks: 'w/ok' is in more than one expectation",
    "invalid-expectation-affected-experiment-check": r"expectations\.0: only workload checks run per instance",
    "invalid-expectation-affected-untargeted": r"expectations\.0\.instances: no operation targets w instances",
    "invalid-repeat-workload": r"flow\.repeat\.serial\[0\]: only adapter operations can be in a repeat, not 'w'",
    "invalid-repeat-shape": r"flow\.serial\[1\]\.repeat: want \{times: <n>, serial: \[\.\.\.\]\}",
}


def test_every_invalid_fixture_is_covered():
    assert {p.stem for p in FIXTURES.glob("invalid-*.yaml")} == set(INVALID)


@pytest.mark.parametrize("name", sorted(INVALID))
def test_invalid_fixture_is_rejected(name):
    with pytest.raises(ExperimentError, match=INVALID[name]):
        load_experiment(FIXTURES / f"{name}.yaml")


@pytest.mark.parametrize(
    "path", sorted(FIXTURES.glob("valid-*.yaml")), ids=lambda p: p.stem
)
def test_valid_fixture_loads(path):
    exp, _ = load_experiment(path)
    assert exp.id == path.stem


@pytest.mark.parametrize(
    "path", sorted(EXPERIMENTS.glob("*.yaml")), ids=lambda p: p.stem
)
def test_shipped_experiment_validates_against_its_adapter(path):
    exp, _ = load_experiment(path)
    validate_adapter(exp, load_adapter(ADAPTERS, exp.default_adapter))


def test_id_must_equal_file_name(tmp_path):
    path = tmp_path / "other-name.yaml"
    path.write_text((FIXTURES / "valid-minimal.yaml").read_text())
    with pytest.raises(ExperimentError, match="must equal the file name 'other-name'"):
        load_experiment(path)


def test_exit_code_check_only_in_workloads(tmp_path):
    raw = read_yaml(FIXTURES / "valid-minimal.yaml")
    raw["id"] = "exit-code-outside"
    raw["checks"] = [
        {"name": "e", "description": "d", "kind": "exit-code", "equals": 0}
    ]
    path = tmp_path / "exit-code-outside.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(
        ExperimentError, match=r"checks\.0: Input tag 'exit-code' .* 'shell', 'metric'"
    ):
        load_experiment(path)


def test_keys_are_kebab_case(tmp_path):
    raw = read_yaml(FIXTURES / "valid-minimal.yaml")
    raw["id"] = "snake-case-key"
    raw["requires_env"] = []
    path = tmp_path / "snake-case-key.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(
        ExperimentError, match=r"requires_env: Extra inputs are not permitted"
    ):
        load_experiment(path)


def test_operation_must_suit_the_target_type(tmp_path, adapters_dir):
    exp, _ = load_experiment(FIXTURES / "valid-freeze.yaml")
    validate_adapter(exp, load_adapter(adapters_dir(), "openshell-docker"))
    text = (FIXTURES / "valid-role-fault.yaml").read_text()
    path = tmp_path / "valid-role-fault.yaml"
    path.write_text(text.replace("kill", "restart"))
    exp, _ = load_experiment(path)
    with pytest.raises(
        ExperimentError,
        match=r"operations\.f: restart on a container target "
        r"\(role supervisor\) is not supported on docker",
    ):
        validate_adapter(exp, load_adapter(adapters_dir(), "openshell-docker"))


def test_flow_tree_has_paths():
    exp, _ = load_experiment(FIXTURES / "valid-role-fault.yaml")
    assert isinstance(exp.flow, Parallel)
    workload, branch = exp.flow.children
    assert isinstance(workload, Ref) and workload.path == "flow.parallel[0]"
    assert isinstance(branch, Serial)
    assert isinstance(branch.children[0], Wait) and branch.children[0].workload == "w"
    assert branch.children[1].path == "flow.parallel[1].serial[1]"


def test_repeat_parses_times_and_for(tmp_path):
    raw = read_yaml(FIXTURES / "valid-operations.yaml")
    raw["id"] = "rep"
    raw["flow"] = {
        "parallel": [
            "w",
            {
                "serial": [
                    {"wait": {"workload": "w", "condition": "started"}},
                    {"repeat": {"for": "1m", "serial": ["a", {"delay": "1s"}]}},
                    {"repeat": {"times": 2, "serial": ["g"]}},
                ]
            },
        ]
    }
    path = tmp_path / "rep.yaml"
    path.write_text(yaml.safe_dump(raw))
    exp, _ = load_experiment(path)
    _, branch = exp.flow.children
    timed, counted = branch.children[1:]
    assert isinstance(timed, Repeat) and (timed.seconds, timed.times) == (60, None)
    assert timed.children[0].path == "flow.parallel[1].serial[1].repeat.serial[0]"
    assert isinstance(counted, Repeat) and (counted.seconds, counted.times) == (None, 2)


@pytest.mark.parametrize(
    ("value", "seconds"), [("500ms", 0.5), ("10s", 10), ("6m", 360), ("1h", 3600)]
)
def test_parse_duration(value, seconds):
    assert parse_duration(value) == seconds


@pytest.mark.parametrize("value", ["10", "1.5s", "10 s", "1d", ""])
def test_parse_duration_rejects(value):
    with pytest.raises(ValueError, match="invalid duration"):
        parse_duration(value)


# --set overrides.


def test_override_addresses_list_items_by_name():
    exp, raw = load_experiment(
        EXPERIMENTS / "smoke.yaml",
        [
            "workloads.counter.instances=3",
            "workloads.counter.env.TICKS=7",
            "timeout=20m",
        ],
    )
    assert exp.workload("counter").instances == 3
    assert exp.workload("counter").env["TICKS"] == "7"
    assert exp.timeout == "20m"
    assert raw["workloads"][0]["instances"] == 3


def test_override_does_not_mutate_input():
    raw = {"workloads": [{"name": "w", "instances": 1}]}
    apply_overrides(raw, ["workloads.w.instances=2"])
    assert raw["workloads"][0]["instances"] == 1


def test_override_creates_missing_mappings():
    data = apply_overrides(
        {"workloads": [{"name": "w"}]}, ["workloads.w.options.image=x"]
    )
    assert data["workloads"][0]["options"] == {"image": "x"}


def test_override_empty_value_is_empty_string():
    assert apply_overrides({"a": "b"}, ["a="]) == {"a": ""}


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ("timeout", "want path=value"),
        ("=1", "want path=value"),
        ("workloads.nope.instances=2", "no item named 'nope'"),
        ("workloads.counter=2", "cannot replace a whole list item"),
        ("timeout.minutes=2", "timeout is not a mapping or list"),
    ],
)
def test_override_errors(override, message):
    with pytest.raises(ExperimentError, match=message):
        load_experiment(EXPERIMENTS / "smoke.yaml", [override])


def test_override_result_is_validated():
    with pytest.raises(ExperimentError, match=r"workloads\.0\.instances"):
        load_experiment(EXPERIMENTS / "smoke.yaml", ["workloads.counter.instances=0"])


def _with_preflight(tmp_path, names: list[str]):
    raw = read_yaml(FIXTURES / "valid-minimal.yaml")
    raw["id"] = "pf"
    raw["preflight"] = [
        {"name": n, "description": "d", "kind": "shell", "run": "true"} for n in names
    ]
    path = tmp_path / "pf.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


def test_preflight_names(tmp_path):
    with pytest.raises(ExperimentError, match="duplicate preflight check"):
        load_experiment(_with_preflight(tmp_path, ["a", "a"]))
    exp, _ = load_experiment(_with_preflight(tmp_path, ["docker-reachable"]))
    with pytest.raises(ExperimentError, match="already has a preflight check"):
        validate_adapter(exp, load_adapter(ADAPTERS, exp.default_adapter))


# Data and adapter operations.


def _operations_fixture(tmp_path, edit) -> Path:
    raw = read_yaml(FIXTURES / "valid-operations.yaml")
    raw["id"] = "act"
    edit(raw)
    path = tmp_path / "act.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


@pytest.mark.parametrize(
    ("edit", "error"),
    [
        (
            lambda raw: raw["workloads"][0]["options"].update(policy="nope"),
            r"workloads\.w\.options\.policy: no data entry named 'nope'",
        ),
        (
            lambda raw: raw["operations"][1]["options"].update(policy="nope"),
            r"operations\.g\.options\.policy: no data entry named 'nope'",
        ),
        (
            lambda raw: raw["operations"][1].update(options={}),
            r"operations\.g\.options: option policy is required",
        ),
        (
            lambda raw: raw["operations"][1]["options"].update(colour="red"),
            r"operations\.g\.options: unknown option 'colour' for openshell-policy-set",
        ),
    ],
)
def test_options_are_checked_against_the_adapter(tmp_path, edit, error):
    exp, _ = load_experiment(_operations_fixture(tmp_path, edit))
    with pytest.raises(ExperimentError, match=error):
        validate_adapter(exp, load_adapter(ADAPTERS, exp.default_adapter))


def test_adapter_needs_the_version_marker(adapters_dir):
    directory = adapters_dir()
    path = directory / "openshell-docker.yaml"
    path.write_text(path.read_text().replace("apiVersion: chaos/v1alpha1", ""))
    with pytest.raises(ExperimentError, match=r"apiVersion: Field required"):
        load_adapter(directory, "openshell-docker")


def test_limits_are_the_adapters(tmp_path, adapters_dir):
    """A `run` script or an `env` value over the adapter's limits is rejected
    against that adapter, with the limit named; the harness has no limit."""
    raw = read_yaml(FIXTURES / "valid-minimal.yaml")
    raw["workloads"][0]["run"] = "x" * 100
    raw["workloads"][0]["env"] = {"BIG": "y" * 50}
    path = tmp_path / "valid-minimal.yaml"
    path.write_text(yaml.safe_dump(raw))
    exp, _ = load_experiment(path)
    validate_adapter(exp, load_adapter(adapters_dir(), "openshell-docker"))
    limits = {"env-value-bytes": 50, "run-script-bytes": 99}
    adapter = load_adapter(adapters_dir(limits=limits), "openshell-docker")
    with pytest.raises(
        ExperimentError,
        match=r"workloads\.w\.run: 100 bytes is over limits\.run-script-bytes "
        r"\(99\) of openshell-docker",
    ):
        validate_adapter(exp, adapter)
    limits = {"env-value-bytes": 49, "run-script-bytes": 100}
    adapter = load_adapter(adapters_dir(limits=limits), "openshell-docker")
    with pytest.raises(
        ExperimentError,
        match=r"workloads\.w\.env\.BIG: 50 bytes is over limits\.env-value-bytes "
        r"\(49\) of openshell-docker",
    ):
        validate_adapter(exp, adapter)


def test_operation_must_be_in_the_adapter(adapters_dir):
    exp, _ = load_experiment(FIXTURES / "valid-operations.yaml")
    validate_adapter(exp, load_adapter(ADAPTERS, exp.default_adapter))
    options = {"policy": {"default": "", "data": True}}
    adapter = load_adapter(adapters_dir(options=options), "openshell-docker")
    with pytest.raises(
        ExperimentError,
        match=r"operations\.a: openshell-docker has no operation 'openshell-policy-set'",
    ):
        validate_adapter(exp, adapter)


def test_container_restart_is_one_shot(tmp_path, adapters_dir):
    text = (FIXTURES / "valid-role-fault.yaml").read_text()
    path = tmp_path / "valid-role-fault.yaml"
    path.write_text(text.replace("kill", "restart-in-place"))
    exp, _ = load_experiment(path)
    validate_adapter(exp, load_adapter(adapters_dir(), "openshell-docker"))

    path.write_text(
        text.replace("kill", "restart-in-place").replace(
            "select: one}", "select: one, duration: 10s}"
        )
    )
    with pytest.raises(ExperimentError, match="restart-in-place is one-shot"):
        load_experiment(path)


@pytest.mark.parametrize(
    ("legacy", "current"),
    [
        ("process-restart", "restart"),
        ("process-outage", "outage"),
        ("container-kill", "kill"),
        ("container-restart", "restart-in-place"),
        ("container-pause", "freeze"),
    ],
)
def test_legacy_operation_names_load_as_their_current_name(legacy, current):
    op = Operation.model_validate(
        {
            "name": "f",
            "description": "d",
            "uses": legacy,
            "target": "gateway",
            "select": "all",
        }
        | ({"duration": "1s"} if current in {"outage", "freeze"} else {})
    )
    assert op.uses == current


def test_metrics_entries_and_metric_checks(tmp_path):
    raw = read_yaml(FIXTURES / "valid-minimal.yaml")
    raw["id"] = "metrics"
    raw["metrics"] = [
        "up_gauge",
        {"family": "lat_seconds", "description": "d"},
        {"title": "Up", "expr": "sum(up_gauge)"},
    ]
    raw["checks"] = [
        {
            "name": "slow",
            "description": "d",
            "kind": "metric",
            "metric": "lat_seconds_count",
            "value": "increase",
            "at-least": 1,
            "at-most": 9,
        }
    ]
    exp = parse_experiment(raw, "test")
    assert exp.metric_families == ["up_gauge", "lat_seconds"]
    assert [m.title for m in exp.metrics if m.expr] == ["Up"]
    (check,) = exp.checks
    assert check.wants() == "at-least 1 and at-most 9"
    assert check.holds(9) and not check.holds(9.5)
