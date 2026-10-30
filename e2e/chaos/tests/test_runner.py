# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Whole runs against a fake docker adapter: bash hooks, no Docker, no gateway."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import signal
import time
from pathlib import Path

import pytest
import yaml
from conftest import minimal_adapter

from chaos import cli, flow, instances, runner
from chaos.adapter import run_shell
from chaos.evaluate import read_verdict
from chaos.inject.docker import DockerInjector
from chaos.runner import Run

HOOKS = {
    # Runs the workload on the host, writing to the instance's output dir.
    "create": """
name=$CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_NAME
echo "$name" >> "$FAKE_STATE/created"
[ "${FAKE_CREATE_FAIL:-}" = "$name" ] && exit 1
while IFS= read -r line; do export "${line?}"; done < "$CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_ENV_FILE"
export CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_OUTPUT_DIR="$CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_DIR/output"
echo "$name ${CHAOS_EXPERIMENT_WORKLOAD_OPTION_POLICY:-}" >> "$FAKE_STATE/options"
if [ -n "${FAKE_BACKGROUND:-}" ]; then
  sh -c "$(cat "$CHAOS_EXPERIMENT_WORKLOAD_WRAPPER_FILE")" chaos \\
    "$(cat "$CHAOS_EXPERIMENT_WORKLOAD_RUN_FILE")" > /dev/null 2>&1 &
else
  sh -c "$(cat "$CHAOS_EXPERIMENT_WORKLOAD_WRAPPER_FILE")" chaos \\
    "$(cat "$CHAOS_EXPERIMENT_WORKLOAD_RUN_FILE")" > /dev/null 2>&1
fi
echo "id-$name"
""",
    "status": """
out=$CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_DIR/output/.chaos
if [ -f "$out/exit_code" ]; then echo Succeeded
elif [ -f "$out/attempts" ]; then echo Running
else echo Pending; fi
""",
    # Records the call, then releases each instance it applies to.
    "openshell-policy-set": """
case "${FAKE_OPERATION_FAIL:-}" in
  1|"$CHAOS_EXPERIMENT_OPERATION_NAME") echo "policy rejected" >&2; exit 3;;
esac
cp "$CHAOS_EXPERIMENT_OPERATION_OPTION_POLICY" "$FAKE_STATE/applied-policy.yaml"
echo "$CHAOS_EXPERIMENT_OPERATION_SCOPE" > "$CHAOS_EXPERIMENT_OPERATION_STATE_DIR/scope"
echo ok > "$CHAOS_EXPERIMENT_OPERATION_OUTCOME_FILE"
release() {
  echo "$CHAOS_EXPERIMENT_OPERATION_NAME $CHAOS_EXPERIMENT_OPERATION_SCOPE $1" >> "$FAKE_STATE/operations"
  touch "$CHAOS_EXPERIMENT_RUN_DIR/workloads/w/instances/$1/output/go"
}
if [ "$CHAOS_EXPERIMENT_OPERATION_SCOPE" = instances ]; then
  release "$CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_NAME"
else
  while IFS= read -r name; do release "$name"; done < "$CHAOS_EXPERIMENT_OPERATION_INSTANCES_FILE"
fi
""",
    "collect": """
name=$CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_NAME
echo "$name $CHAOS_EXPERIMENT_HOOK_TIMEOUT" >> "$FAKE_STATE/collected"
[ "${FAKE_COLLECT_FAIL:-}" = "$name" ] && exit 1
[ -n "${FAKE_COLLECT_SLOW:-}" ] && sleep 30
true
""",
    "delete": """
[ "${FAKE_DELETE_FAIL:-}" = "$CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_NAME" ] && exit 5
echo "$CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_NAME" >> "$FAKE_STATE/deleted"
""",
    "start": """
nohup sleep 300 > /dev/null 2>&1 &
echo $! > "$FAKE_STATE/gateway.pid"
""",
}

EXPERIMENT = {
    "apiVersion": "chaos/v1alpha1",
    "id": "fake",
    "version": 1,
    "title": "t",
    "hypothesis": "h",
    "description": "d",
    "default-adapter": "openshell-docker",
    "workloads": [
        {
            "name": "w",
            "description": "d",
            "kind": "shell",
            "instances": 2,
            "timeout": "1m",
            "run": 'echo ok > "$CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_OUTPUT_DIR/result"',
            "checks": [
                {"name": "ok", "description": "d", "kind": "exit-code", "equals": 0}
            ],
        }
    ],
    "flow": "w",
}


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """A fake docker adapter. Returns the state dir that hooks record into."""
    state = tmp_path / "state"
    state.mkdir()
    adapters = tmp_path / "adapters"
    adapters.mkdir()
    adapter = minimal_adapter(prelude="set -u\n")
    adapter["instance"] = {
        **adapter["instance"],
        **{k: HOOKS[k] for k in ("create", "status", "collect", "delete")},
    }
    adapter["targets"]["gateway"] = {
        "type": "process",
        "pid-file": "${FAKE_STATE}/gateway.pid",
        "start": HOOKS["start"],
    }
    (adapters / "openshell-docker.yaml").write_text(yaml.safe_dump(adapter))
    (tmp_path / "fake.yaml").write_text(yaml.safe_dump(EXPERIMENT))
    monkeypatch.setenv("FAKE_STATE", str(state))
    monkeypatch.setattr(instances, "POLL_INTERVAL", 0.05)
    monkeypatch.setattr(flow, "POLL_INTERVAL", 0.05)
    monkeypatch.setattr(instances, "RETRY_WINDOW", 0.5)
    yield state
    pid_file = state / "gateway.pid"
    if pid_file.exists():
        with contextlib.suppress(ProcessLookupError):
            os.kill(int(pid_file.read_text()), signal.SIGKILL)


def run(fake: Path) -> tuple[int, dict]:
    root = fake.parent
    code = cli.main(
        [
            "--adapters-dir",
            str(root / "adapters"),
            "run",
            str(root / "fake.yaml"),
            "--runs-dir",
            str(root / "runs"),
        ]
    )
    (run_dir,) = (root / "runs" / "fake").iterdir()
    return code, json.loads((run_dir / "verdict.json").read_text())


def lines(path: Path) -> list[str]:
    return sorted(path.read_text().split()) if path.exists() else []


def index(fake: Path) -> list[str]:
    path = fake.parent / "runs" / "index.jsonl"
    return [json.loads(line)["verdict"] for line in path.read_text().splitlines()]


def test_run_passes_and_deletes_every_instance(fake):
    code, verdict = run(fake)
    assert (code, verdict["verdict"]) == (0, "PASS"), verdict
    assert len(verdict["results"]) == 2
    assert lines(fake / "deleted") == lines(fake / "created") == ["w-0", "w-1"]
    assert index(fake) == ["PASS"]
    # The run's checks are on the timeline; no gateway fault ran, so the
    # `run.finished` event names no processes.
    checks = [(e["event"], e["instance"], e.get("ok")) for e in _events(fake, "check.")]
    assert checks == [
        ("check.started", "w-0", None),
        ("check.finished", "w-0", True),
        ("check.started", "w-1", None),
        ("check.finished", "w-1", True),
    ]
    (finished,) = _events(fake, "run.finished")
    assert finished == {
        "t": finished["t"],
        "ts": finished["ts"],
        "event": "run.finished",
        "verdict": "PASS",
    }
    (path,) = (fake.parent / "runs" / "fake").iterdir()
    (line,) = _index_lines(fake)
    assert (line["path"], line["flaky_missed"]) == (f"fake/{path.name}", [])


def _index_lines(fake: Path) -> list[dict]:
    path = fake.parent / "runs" / "index.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_state_reason_comes_from_the_status_hook(fake, monkeypatch):
    """A second word on the status hook's line is the state's reason."""
    adapters = fake.parent / "adapters"
    path = adapters / "openshell-docker.yaml"
    adapter = yaml.safe_load(path.read_text())
    adapter["instance"]["status"] = HOOKS["status"].replace(
        "echo Succeeded", 'echo "Succeeded MainProcessExited"'
    )
    path.write_text(yaml.safe_dump(adapter))
    code, verdict = run(fake)
    assert (code, verdict["verdict"]) == (0, "PASS"), verdict
    states = [
        (e["instance"], e["state"], e.get("reason"))
        for e in _events(fake, "instance.state")
    ]
    assert ("w-0", "Succeeded", "MainProcessExited") in states
    assert all(reason is None for _, state, reason in states if state != "Succeeded")


def test_unparsed_status_is_logged_once_per_instance(fake):
    """A status hook output that is no state reads as Unknown and is logged
    the first time, with the output, so a hook bug is visible without a
    debugger."""
    adapters = fake.parent / "adapters"
    path = adapters / "openshell-docker.yaml"
    adapter = yaml.safe_load(path.read_text())
    adapter["instance"]["status"] = (
        "polls=$FAKE_STATE/polls-$CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_NAME\n"
        'n=$(cat "$polls" 2>/dev/null || echo 0); echo $((n + 1)) > "$polls"\n'
        'if [ "$n" -lt 2 ]; then echo "Bogus state"; exit 0; fi\n'
    ) + HOOKS["status"]
    path.write_text(yaml.safe_dump(adapter))
    code, verdict = run(fake)
    assert (code, verdict["verdict"]) == (0, "PASS"), verdict
    log = next((fake.parent / "runs" / "fake").glob("*/logs/harness.log"))
    lines = [line for line in log.read_text().splitlines() if "read as Unknown" in line]
    assert len(lines) == 2, lines
    assert sorted(
        line.split("status hook for ")[1].split(" ")[0] for line in lines
    ) == [
        "w-0",
        "w-1",
    ]
    assert all("stdout 'Bogus state'" in line for line in lines), lines


def test_observers_run_beside_the_flow(fake):
    path = fake.parent / "adapters" / "openshell-docker.yaml"
    adapter = yaml.safe_load(path.read_text())
    (fake / "metrics.txt").write_text("up_gauge 1\n")
    adapter["metrics"] = [
        {"name": "m", "url": "file://${FAKE_STATE}/metrics.txt", "interval": "50ms"}
    ]
    adapter["watch"] = [{"name": "w", "run": "echo started; sleep 60"}]
    path.write_text(yaml.safe_dump(adapter))
    code, _ = run(fake)
    assert code == 0
    (run_dir,) = (fake.parent / "runs" / "fake").iterdir()
    scrapes = (run_dir / "system" / "metrics" / "m.jsonl").read_text().splitlines()
    assert json.loads(scrapes[0])["samples"] == {"up_gauge": 1.0}
    (line,) = (run_dir / "system" / "watch" / "w.jsonl").read_text().splitlines()
    assert json.loads(line)["line"] == "started"
    assert '"title": "up_gauge"' in (run_dir / "report.html").read_text()


def _with_metrics(fake: Path, families: list[str], text: str | None = "up_gauge 1\n"):
    """The experiment lists `families`; the adapter scrapes a file with `text`
    (no metrics source when None)."""
    path = fake.parent / "adapters" / "openshell-docker.yaml"
    adapter = yaml.safe_load(path.read_text())
    if text is not None:
        (fake / "metrics.txt").write_text(text)
        adapter["metrics"] = [
            {"name": "m", "url": "file://${FAKE_STATE}/metrics.txt", "interval": "50ms"}
        ]
    path.write_text(yaml.safe_dump(adapter))
    (fake.parent / "fake.yaml").write_text(
        yaml.safe_dump({**EXPERIMENT, "metrics": families})
    )


def test_listed_metrics_must_be_exported(fake):
    _with_metrics(
        fake,
        ["up_gauge", "lat_seconds", "gone_total"],
        "up_gauge 1\nlat_seconds_count 2\n",
    )
    code, verdict = run(fake)
    assert (code, verdict["verdict"]) == (2, "ERROR")
    assert verdict["errors"] == ["preflight failed: metrics-exported"]
    (result,) = verdict["results"]
    assert (result["scope"], result["check"], result["ok"]) == (
        "preflight",
        "metrics-exported",
        False,
    )
    assert result["output"] == "m: 2 families, 2 listed\nmissing: gone_total"
    assert not (fake / "created").exists()


def test_listed_metrics_without_a_source_fail_preflight(fake):
    _with_metrics(fake, ["up_gauge"], None)
    code, verdict = run(fake)
    assert (code, verdict["verdict"]) == (2, "ERROR")
    assert verdict["results"][0]["output"] == (
        "adapter openshell-docker has no metrics sources to scrape\nmissing: up_gauge"
    )


def test_listed_metrics_are_checked_and_reported(fake):
    _with_metrics(fake, ["up_gauge", "zero_total"], "up_gauge 1\nzero_total 0\n")
    code, verdict = run(fake)
    assert (code, verdict["verdict"]) == (0, "PASS"), verdict
    assert [(r["scope"], r["check"]) for r in verdict["results"]][:1] == [
        ("preflight", "metrics-exported")
    ]
    (run_dir,) = (fake.parent / "runs" / "fake").iterdir()
    report = (run_dir / "report.html").read_text()
    # A listed family is charted even when it stays at zero.
    assert '"title": "zero_total"' in report
    assert '"declared": true' in report


def test_failed_create_is_still_deleted(fake, monkeypatch):
    monkeypatch.setenv("FAKE_CREATE_FAIL", "w-1")
    code, verdict = run(fake)
    assert (code, verdict["verdict"]) == (2, "ERROR")
    assert any("create w-1 failed" in e for e in verdict["errors"])
    assert "w-1" in lines(fake / "deleted")
    assert lines(fake / "deleted") == lines(fake / "created")


POLICY = {"version": 1, "network_policies": {}}
STARTED = {"wait": {"workload": "w", "condition": "started"}}


def _with_operation(fake: Path, monkeypatch, operation: dict) -> None:
    """Instances that wait for the operation to release them, then exit 0."""
    root = fake.parent
    path = root / "adapters" / "openshell-docker.yaml"
    adapter = yaml.safe_load(path.read_text())
    adapter["options"] = {"policy": {"default": "", "data": True}}
    adapter["operations"] = {
        "openshell-policy-set": {
            "run": HOOKS["openshell-policy-set"],
            # Records what it was asked to put back and what `run` left behind.
            "revert": 'echo "$CHAOS_EXPERIMENT_OPERATION_NAME '
            "$CHAOS_EXPERIMENT_OPERATION_SCOPE "
            "${CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_NAME:-} "
            '$(cat "$CHAOS_EXPERIMENT_OPERATION_STATE_DIR/scope")" '
            '>> "$FAKE_STATE/reverted"\n'
            '[ -z "${FAKE_REVERT_FAIL:-}" ] || { echo "cannot" >&2; exit 4; }\n',
            "options": {"policy": {"data": True, "required": True}},
        }
    }
    path.write_text(yaml.safe_dump(adapter))
    exp = json.loads(json.dumps(EXPERIMENT))
    exp["data"] = {"p": POLICY, "note": "free text\n", "limit": 3}
    exp["operations"] = [
        {
            "name": "a",
            "description": "d",
            "uses": "openshell-policy-set",
            "options": {"policy": "p"},
            **operation,
        }
    ]
    exp["workloads"][0]["options"] = {"policy": "p"}
    exp["workloads"][0]["run"] = (
        'i=0; until [ -f "$CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_OUTPUT_DIR/go" ]; do '
        "i=$((i + 1)); [ $i -gt 100 ] && exit 1; sleep 0.05; done"
    )
    exp["flow"] = {"parallel": ["w", {"serial": [STARTED, "a"]}]}
    (root / "fake.yaml").write_text(yaml.safe_dump(exp))
    monkeypatch.setenv("FAKE_BACKGROUND", "1")


def _events(fake: Path, prefix: str) -> list[dict]:
    (run_dir,) = (fake.parent / "runs" / "fake").iterdir()
    timeline = (run_dir / "timeline.jsonl").read_text().splitlines()
    return [e for e in map(json.loads, timeline) if e["event"].startswith(prefix)]


def test_targeted_operation_runs_once_per_instance(fake, monkeypatch):
    _with_operation(fake, monkeypatch, {"target": {"workload": "w"}, "select": "all"})
    code, verdict = run(fake)
    assert (code, verdict["verdict"]) == (0, "PASS"), verdict
    assert sorted((fake / "operations").read_text().splitlines()) == [
        "a instances w-0",
        "a instances w-1",
    ]
    assert yaml.safe_load((fake / "applied-policy.yaml").read_text()) == POLICY
    # The workload option resolves to the data file the runner wrote.
    for line in (fake / "options").read_text().splitlines():
        assert line.split()[1].endswith("/data/p")
    (run_dir,) = (fake.parent / "runs" / "fake").iterdir()
    assert (run_dir / "data" / "note").read_text() == "free text\n"
    assert (run_dir / "data" / "limit").read_text() == "3\n"
    started, *rest = _events(fake, "operation.")
    assert started["event"] == "operation.started"
    assert started["options"] == {"policy": "p"}
    assert (started["scope"], started["instances"]) == ("instances", ["w-0", "w-1"])
    completed = [e for e in rest if e["event"] == "operation.completed"]
    assert sorted((e["instance"], e["outcome"]) for e in completed) == [
        ("w-0", "ok"),
        ("w-1", "ok"),
    ]
    # The instances exited and were deleted before the end of the flow, so
    # there is nothing to put the policy back on: each revert is skipped.
    reverted = [e for e in rest if e["event"] == "operation.reverted"]
    assert sorted((e["abort"], e["skipped"]) for e in reverted) == [
        (False, "w-0 is deleted"),
        (False, "w-1 is deleted"),
    ]
    assert not (fake / "reverted").exists()


def test_global_operation_runs_once_for_running_instances(fake, monkeypatch):
    _with_operation(fake, monkeypatch, {"scope": "global"})
    code, verdict = run(fake)
    assert (code, verdict["verdict"]) == (0, "PASS"), verdict
    assert sorted((fake / "operations").read_text().splitlines()) == [
        "a global w-0",
        "a global w-1",
    ]
    started, completed, reverted = _events(fake, "operation.")
    assert (started["scope"], started["instances"]) == ("global", ["w-0", "w-1"])
    assert (completed["event"], completed["outcome"]) == ("operation.completed", "ok")
    assert (reverted["event"], reverted["abort"]) == ("operation.reverted", False)
    assert reverted["undo"] == "revert a (run 1)"
    assert (fake / "reverted").read_text() == "a global  global\n"


def test_operation_hook_must_report_an_outcome(fake, monkeypatch):
    _with_operation(fake, monkeypatch, {"scope": "global"})
    path = fake.parent / "adapters" / "openshell-docker.yaml"
    adapter = yaml.safe_load(path.read_text())
    adapter["operations"]["openshell-policy-set"]["run"] = "true\n"
    path.write_text(yaml.safe_dump(adapter))
    code, verdict = run(fake)
    assert (code, verdict["verdict"]) == (2, "ERROR")
    assert any("a: hook wrote no outcome" in e for e in verdict["errors"]), verdict
    assert not (fake / "reverted").exists()


def test_operations_are_reverted_on_abort_while_instances_are_up(fake, monkeypatch):
    """`a` is applied to each instance, then `b` fails: the abort reverts
    `a` before the instances are collected and deleted."""
    _with_operation(fake, monkeypatch, {"target": {"workload": "w"}, "select": "all"})
    exp = yaml.safe_load((fake.parent / "fake.yaml").read_text())
    exp["operations"].append({**exp["operations"][0], "name": "b", "scope": "global"})
    del exp["operations"][1]["target"], exp["operations"][1]["select"]
    exp["flow"] = {"parallel": ["w", {"serial": [STARTED, "a", "b"]}]}
    # Released by `a`, the instances then wait for a second release that
    # never comes, so they are still up when `b` fails.
    exp["workloads"][0]["run"] += (
        '; until [ -f "$CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_OUTPUT_DIR/go2" ]; do '
        "i=$((i + 1)); [ $i -gt 100 ] && exit 1; sleep 0.05; done"
    )
    (fake.parent / "fake.yaml").write_text(yaml.safe_dump(exp))
    monkeypatch.setenv("FAKE_OPERATION_FAIL", "b")
    code, verdict = run(fake)
    assert (code, verdict["verdict"]) == (2, "ERROR")
    assert any("operation b: " in e for e in verdict["errors"]), verdict
    reverted = _events(fake, "operation.reverted")
    assert [(e["operation"], e["abort"], e.get("skipped")) for e in reverted] == [
        ("a", True, None),
        ("a", True, None),
    ]
    assert sorted((fake / "reverted").read_text().splitlines()) == [
        "a instances w-0 instances",
        "a instances w-1 instances",
    ]
    assert lines(fake / "deleted") == lines(fake / "created") == ["w-0", "w-1"]


def test_failed_revert_is_an_error(fake, monkeypatch):
    _with_operation(fake, monkeypatch, {"scope": "global"})
    monkeypatch.setenv("FAKE_REVERT_FAIL", "1")
    code, verdict = run(fake)
    assert (code, verdict["verdict"]) == (2, "ERROR")
    assert any(
        e == "undo a (revert a (run 1)): revert a (run 1): "
        "operations.openshell-policy-set.revert exited 4: cannot"
        for e in verdict["errors"]
    ), verdict
    assert [e["event"] for e in _events(fake, "operation.")] == [
        "operation.started",
        "operation.completed",
        "operation.error",
    ]
    assert lines(fake / "deleted") == lines(fake / "created") == ["w-0", "w-1"]


def test_failed_operation_is_an_error_verdict(fake, monkeypatch):
    _with_operation(fake, monkeypatch, {"target": {"workload": "w"}, "select": "one"})
    monkeypatch.setenv("FAKE_OPERATION_FAIL", "1")
    code, verdict = run(fake)
    assert (code, verdict["verdict"]) == (2, "ERROR")
    assert any("exited 3: policy rejected" in e for e in verdict["errors"]), verdict
    assert [e["event"] for e in _events(fake, "operation.")] == [
        "operation.started",
        "operation.error",
    ]
    assert lines(fake / "deleted") == lines(fake / "created") == ["w-0", "w-1"]


def test_failed_collect_is_still_deleted(fake, monkeypatch):
    monkeypatch.setenv("FAKE_COLLECT_FAIL", "w-0")
    code, verdict = run(fake)
    assert (code, verdict["verdict"]) == (2, "ERROR")
    assert any("collect w-0 failed" in e for e in verdict["errors"]), verdict
    assert lines(fake / "deleted") == lines(fake / "created") == ["w-0", "w-1"]


def test_cleanup_is_bounded_and_hooks_get_the_budget(fake, monkeypatch):
    _with_operation(fake, monkeypatch, {"target": {"workload": "w"}, "select": "one"})
    monkeypatch.setenv("FAKE_OPERATION_FAIL", "1")
    monkeypatch.setenv("FAKE_COLLECT_SLOW", "1")
    monkeypatch.setattr(runner, "CLEANUP_TIMEOUT", 1.0)
    start = time.monotonic()
    code, verdict = run(fake)
    assert time.monotonic() - start < 10
    assert (code, verdict["verdict"]) == (2, "ERROR")
    assert any(e.startswith("cleanup did not finish in 1s") for e in verdict["errors"])
    assert lines(fake / "deleted") == lines(fake / "created") == ["w-0", "w-1"]
    # Each collect was told it had at most the 1s left of the cleanup.
    collected = (fake / "collected").read_text().splitlines()
    assert {line.split()[1] for line in collected} == {"1"}


def _with_hook(fake: Path, hook: str) -> None:
    """Replaces the operation hook of the adapter `_with_operation` wrote. The
    hook reports `ok`, as every hook must report something."""
    path = fake.parent / "adapters" / "openshell-docker.yaml"
    adapter = yaml.safe_load(path.read_text())
    adapter["operations"]["openshell-policy-set"]["run"] = (
        hook + 'echo ok > "$CHAOS_EXPERIMENT_OPERATION_OUTCOME_FILE"\n'
    )
    path.write_text(yaml.safe_dump(adapter))


def test_repeat_runs_an_operation_each_iteration(fake, monkeypatch):
    _with_operation(fake, monkeypatch, {"target": {"workload": "w"}, "select": "all"})
    path = fake.parent / "fake.yaml"
    exp = yaml.safe_load(path.read_text())
    exp["workloads"][0]["run"] = (
        "out=$CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_OUTPUT_DIR; i=0; "
        'until [ "$(wc -l < "$out/go" 2>/dev/null || echo 0)" -ge 3 ]; do '
        "i=$((i + 1)); [ $i -gt 200 ] && exit 1; sleep 0.05; done"
    )
    exp["flow"] = {
        "parallel": [
            "w",
            {
                "serial": [
                    STARTED,
                    {"repeat": {"times": 3, "serial": ["a"]}},
                ]
            },
        ]
    }
    path.write_text(yaml.safe_dump(exp))
    _with_hook(
        fake,
        'echo "run" >> "$CHAOS_EXPERIMENT_RUN_DIR/workloads/w/instances/'
        '$CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_NAME/output/go"\n'
        'echo "applied $CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_NAME"\n',
    )
    code, verdict = run(fake)
    assert (code, verdict["verdict"]) == (0, "PASS"), verdict
    assert [e["iteration"] for e in _events(fake, "repeat.")] == [1, 2, 3]
    completed = _events(fake, "operation.completed")
    assert [e["run"] for e in completed if e["instance"] == "w-0"] == [1, 2, 3]
    (run_dir,) = (fake.parent / "runs" / "fake").iterdir()
    log = (run_dir / "logs" / "operations" / "a.w-0.log").read_text()
    assert log == "".join(f"--- run {n}\napplied w-0\n" for n in (1, 2, 3))


def test_held_instance_is_not_finished_on_a_terminal_state(fake, monkeypatch):
    """An operation that makes the instance read as Succeeded for a while, as a
    stop does, does not get it collected and deleted mid-operation."""
    _with_operation(fake, monkeypatch, {"target": {"workload": "w"}, "select": "all"})
    _with_hook(
        fake,
        "out=$CHAOS_EXPERIMENT_RUN_DIR/workloads/w/instances/"
        "$CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_NAME/output\n"
        'echo 0 > "$out/.chaos/exit_code"; sleep 0.5; rm "$out/.chaos/exit_code"\n'
        'touch "$out/go"\n',
    )
    code, verdict = run(fake)
    assert (code, verdict["verdict"]) == (0, "PASS"), verdict
    events = _events(fake, "")
    names = [(e["event"], e.get("instance")) for e in events]
    completed = names.index(("operation.completed", "w-0"))
    # Seen Succeeded while held, deleted only after the operation finished.
    assert names.index(("instance.state", "w-0")) < completed
    assert any(
        e["event"] == "instance.state" and e["state"] == "Succeeded"
        for e in events[:completed]
    )
    assert names.index(("instance.deleted", "w-0")) > completed


def _add_preflight(fake: Path, adapter_checks: list, exp_checks: list) -> None:
    root = fake.parent
    path = root / "adapters" / "openshell-docker.yaml"
    adapter = yaml.safe_load(path.read_text())
    adapter["preflight"] = adapter_checks
    path.write_text(yaml.safe_dump(adapter))
    (root / "fake.yaml").write_text(
        yaml.safe_dump({**EXPERIMENT, "preflight": exp_checks})
    )


def _check(name: str, run: str) -> dict:
    return {"name": name, "description": "d", "kind": "shell", "run": run}


def test_failed_preflight_creates_nothing(fake):
    _add_preflight(
        fake,
        [_check("up", "true"), _check("disk", "echo full; exit 1")],
        [_check("mine", 'echo "$CHAOS_EXPERIMENT_CHECK_NAME"')],
    )
    code, verdict = run(fake)
    assert (code, verdict["verdict"]) == (2, "ERROR")
    assert verdict["errors"] == ["preflight failed: disk"]
    assert [(r["scope"], r["check"], r["ok"]) for r in verdict["results"]] == [
        ("preflight", "up", True),
        ("preflight", "disk", False),
        ("preflight", "mine", True),
    ]
    assert not (fake / "created").exists()
    (run_dir,) = (fake.parent / "runs" / "fake").iterdir()
    events = [
        json.loads(line)["event"]
        for line in (run_dir / "timeline.jsonl").read_text().splitlines()
    ]
    assert events == ["run.started", "preflight.failed", "run.finished"]
    # Evaluating again keeps the preflight results and runs no other check.
    adapters = str(fake.parent / "adapters")
    assert cli.main(["--adapters-dir", adapters, "evaluate", str(run_dir)]) == 2
    assert read_verdict(run_dir)["results"] == verdict["results"]


def test_passed_preflight_is_part_of_the_verdict(fake):
    _add_preflight(fake, [_check("up", "true")], [])
    code, verdict = run(fake)
    assert (code, verdict["verdict"]) == (0, "PASS"), verdict
    assert [r["scope"] for r in verdict["results"]] == [
        "preflight",
        "workload",
        "workload",
    ]


def test_preflight_command(fake, capsys):
    root = fake.parent
    args = [
        "--adapters-dir",
        str(root / "adapters"),
        "preflight",
        str(root / "fake.yaml"),
        "--runs-dir",
        str(root / "runs"),
    ]
    _add_preflight(
        fake, [_check("up", 'test -f "$CHAOS_EXPERIMENT_RUN_DIR/experiment.yaml"')], []
    )
    assert cli.main(args) == 0
    assert "ok    preflight/up" in capsys.readouterr().out
    _add_preflight(fake, [_check("up", "exit 1")], [])
    assert cli.main(args) == 2
    assert capsys.readouterr().out.splitlines()[-1] == "ERROR"
    # Nothing is created and no run dir is left.
    assert not (fake / "created").exists()
    assert list((root / "runs").iterdir()) == []


def test_describe_output_is_the_manifests_system(fake):
    code, verdict = run(fake)
    assert (code, verdict["verdict"]) == (0, "PASS"), verdict
    (run_dir,) = (fake.parent / "runs" / "fake").iterdir()
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["schema_version"] == "v1alpha1"
    assert manifest["environment"]["system"] == {"openshell": "test"}
    assert set(manifest["environment"]) == {"harness", "os", "arch", "python", "system"}
    archived = (run_dir / "experiment.yaml").read_bytes()
    assert manifest["experiment"]["sha256"] == hashlib.sha256(archived).hexdigest()


@pytest.mark.parametrize(
    ("describe", "error"),
    [
        ("echo nope >&2; exit 3", "describe failed (exit 3): nope"),
        ("echo not json", "describe printed no JSON object"),
        ("echo '[1]'", "describe printed a JSON list, not an object"),
    ],
)
def test_failed_describe_is_an_error_verdict(fake, describe, error):
    path = fake.parent / "adapters" / "openshell-docker.yaml"
    raw = yaml.safe_load(path.read_text())
    raw["describe"] = describe
    path.write_text(yaml.safe_dump(raw))
    code, verdict = run(fake)
    assert (code, verdict["verdict"]) == (2, "ERROR")
    assert any(error in e for e in verdict["errors"]), verdict["errors"]
    assert lines(fake / "created") == []


def test_harness_error_is_an_error_verdict(fake, monkeypatch):
    async def broken(self):
        raise OSError("disk full")

    monkeypatch.setattr(Run, "_collect_run", broken)
    code, verdict = run(fake)
    assert (code, verdict["verdict"]) == (2, "ERROR")
    assert any("harness error: OSError('disk full')" in e for e in verdict["errors"])
    assert index(fake) == ["ERROR"]


def test_forced_exit_reports_an_instance_it_could_not_delete(fake, monkeypatch):
    async def broken(self):
        raise OSError("disk full")

    monkeypatch.setattr(Run, "_collect_run", broken)
    monkeypatch.setenv("FAKE_DELETE_FAIL", "w-1")
    code, verdict = run(fake)
    assert (code, verdict["verdict"]) == (2, "ERROR")
    assert any(
        e.startswith("delete w-1 (id-w-1) failed (exit 5)") for e in verdict["errors"]
    ), verdict
    assert "cleanup incomplete: 1 instance(s) not deleted" in verdict["errors"]
    assert lines(fake / "deleted") == ["w-0"]
    deleted = _events(fake, "instance.deleted")
    assert [e["instance"] for e in deleted] == ["w-0"]


def test_forced_restore_is_bounded(fake, monkeypatch):
    async def broken(self):
        raise OSError("disk full")

    async def stuck(self):
        await asyncio.sleep(60)
        return []

    monkeypatch.setattr(Run, "_collect_run", broken)
    monkeypatch.setattr(DockerInjector, "undo_all", stuck)
    monkeypatch.setattr(runner, "FORCED_RESTORE_TIMEOUT", 0.2)
    start = time.monotonic()
    code, verdict = run(fake)
    assert time.monotonic() - start < 10
    assert (code, verdict["verdict"]) == (2, "ERROR")
    assert any(
        e.startswith("restore operations: TimeoutError") for e in verdict["errors"]
    )
    assert lines(fake / "deleted") == lines(fake / "created")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_hook_timeout_kills_its_children(tmp_path):
    pid_file = tmp_path / "pid"
    start = time.monotonic()
    result = asyncio.run(
        run_shell(["bash", "-c", f"sleep 30 & echo $! > {pid_file}; wait"], {}, 0.5)
    )
    assert result.code == 124
    assert time.monotonic() - start < 5
    assert not _alive(int(pid_file.read_text()))


def test_cancelled_hook_kills_its_children(tmp_path):
    pid_file = tmp_path / "pid"

    async def cancel_hook() -> None:
        task = asyncio.create_task(
            run_shell(["bash", "-c", f"sleep 30 & echo $! > {pid_file}; wait"], {}, 60)
        )
        while not pid_file.exists() or not pid_file.read_text().strip():
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_hook())
    assert not _alive(int(pid_file.read_text()))


def _with_signal_wait(fake: Path, monkeypatch, watch: str, wait: dict) -> None:
    """An operation after two signal waits in a repeat, on a `sweep` signal."""
    _with_operation(fake, monkeypatch, {"target": {"workload": "w"}, "select": "all"})
    path = fake.parent / "adapters" / "openshell-docker.yaml"
    adapter = yaml.safe_load(path.read_text())
    adapter["watch"] = [{"name": "otel", "run": watch}]
    adapter["signals"] = [
        {"name": "sweep", "watch": "otel", "match": "^Name +: sweep$"}
    ]
    path.write_text(yaml.safe_dump(adapter))
    exp_path = fake.parent / "fake.yaml"
    exp = yaml.safe_load(exp_path.read_text())
    exp["workloads"][0]["run"] = (
        "out=$CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_OUTPUT_DIR; i=0; "
        'until [ "$(wc -l < "$out/go" 2>/dev/null || echo 0)" -ge 2 ]; do '
        "i=$((i + 1)); [ $i -gt 200 ] && exit 1; sleep 0.05; done"
    )
    exp["flow"] = {
        "parallel": [
            "w",
            {
                "serial": [
                    STARTED,
                    {"repeat": {"times": 2, "serial": [{"wait": wait}, "a"]}},
                ]
            },
        ]
    }
    exp_path.write_text(yaml.safe_dump(exp))
    _with_hook(
        fake,
        'echo "run" >> "$CHAOS_EXPERIMENT_RUN_DIR/workloads/w/instances/'
        '$CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_NAME/output/go"\n',
    )


def test_signal_wait_returns_after_each_new_event(fake, monkeypatch):
    _with_signal_wait(
        fake,
        monkeypatch,
        "while true; do echo 'Name           : sweep'; echo other; sleep 0.3; done",
        {"signal": "sweep", "delay": "100ms", "timeout": "5s"},
    )
    code, verdict = run(fake)
    assert (code, verdict["verdict"]) == (0, "PASS"), verdict
    waited = _events(fake, "signal.waited")
    assert len(waited) == 2
    assert waited[0]["signal_t"] < waited[1]["signal_t"]
    # Timeline times are rounded to the millisecond.
    for e in waited:
        assert e["t"] >= e["signal_t"] + 0.1 - 0.001
    received = _events(fake, "signal.received")
    assert received and all(e["signal"] == "sweep" for e in received)


def test_signal_wait_timeout_is_an_error(fake, monkeypatch):
    _with_signal_wait(
        fake, monkeypatch, "sleep 60", {"signal": "sweep", "timeout": "300ms"}
    )
    code, verdict = run(fake)
    assert (code, verdict["verdict"]) == (2, "ERROR"), verdict
    assert any("no sweep signal within 300ms" in e for e in verdict["errors"])
    (timeout,) = _events(fake, "signal.timeout")
    assert (timeout["signal"], timeout["timeout"], timeout["last_t"]) == (
        "sweep",
        "300ms",
        None,
    )
    # The workload the wait ran beside was cancelled, and its node says so.
    finished = {e["node"]: e.get("status") for e in _events(fake, "node.finished")}
    assert finished["flow.parallel[0]"] == "cancelled"
    assert "flow.parallel[1].serial[1].repeat[0].wait" not in finished


def test_signal_must_exist_in_the_adapter(fake, monkeypatch, capsys):
    _with_signal_wait(fake, monkeypatch, "sleep 60", {"signal": "other"})
    argv = ["--adapters-dir", str(fake.parent / "adapters"), "validate"]
    code = cli.main([*argv, str(fake.parent / "fake.yaml")])
    assert code != 0
    assert "has no signal 'other'" in capsys.readouterr().err
