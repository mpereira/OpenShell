# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Checks, verdicts and `chaos evaluate` against a hand-made run directory."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
import yaml
from conftest import FIXTURES

from chaos import cli
from chaos.adapter import load_adapter
from chaos.envvars import RunInfo
from chaos.evaluate import (
    Result,
    format_verdict,
    read_verdict,
    run_checks,
    selected_instances,
    verdict_of,
    write_verdict,
)
from chaos.model import load_experiment

RUN_ID = "20260101T000000Z-abcd"
EXPERIMENT = {
    "apiVersion": "chaos/v1alpha1",
    "id": "evaluated",
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
            "env": {"WANT": "done"},
            "run": "true",
            "checks": [
                {
                    "name": "exited-ok",
                    "description": "d",
                    "kind": "exit-code",
                    "equals": 0,
                },
                {
                    "name": "wrote-done",
                    "description": "d",
                    "kind": "shell",
                    "run": 'grep -qx "$WANT" "$CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_OUTPUT_DIR/result"'
                    ' || { echo "$CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_NAME missing"; exit 1; }',
                },
            ],
        }
    ],
    "flow": "w",
    "checks": [
        {
            "name": "timeline-written",
            "description": "d",
            "kind": "shell",
            "run": 'test -f "$CHAOS_EXPERIMENT_RUN_DIR/timeline.jsonl"'
            ' && test "$CHAOS_EXPERIMENT_CHECK_NAME" = timeline-written',
        }
    ],
}


def instance_output(run_dir: Path, name: str) -> Path:
    path = run_dir / "workloads" / "w" / "instances" / name / "output"
    (path / ".chaos").mkdir(parents=True)
    return path


@pytest.fixture
def run_dir(tmp_path, adapters_dir):
    """A finished run: w-0 succeeded, w-1 exited 1 without writing its result."""
    directory = tmp_path / "runs" / "evaluated" / RUN_ID
    directory.mkdir(parents=True)
    (directory / "timeline.jsonl").write_text(
        json.dumps({"event": "run.finished", "verdict": "FAIL"}) + "\n"
    )
    (directory / "experiment.yaml").write_text(yaml.safe_dump(EXPERIMENT))
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "v1alpha1",
                "run_id": RUN_ID,
                "experiment": {"id": "evaluated", "version": 1},
                "seed": 0,
                "adapter": {"id": "openshell-docker"},
                "overrides": [],
                "environment": {"system": {"git_sha": "abc"}},
            }
        )
    )
    ok = instance_output(directory, "w-0")
    (ok / ".chaos" / "exit_code").write_text("0\n")
    (ok / "result").write_text("done\n")
    bad = instance_output(directory, "w-1")
    (bad / ".chaos" / "exit_code").write_text("1\n")
    adapters_dir()
    return directory


def evaluate(run_dir: Path, *extra: str) -> int:
    adapters = run_dir.parents[2] / "adapters"
    return cli.main(["--adapters-dir", str(adapters), "evaluate", str(run_dir), *extra])


def test_run_dir_commands_refuse_another_schema_version(run_dir, capsys):
    manifest = json.loads((run_dir / "manifest.json").read_text())
    manifest["schema_version"] = "v0"
    (run_dir / "manifest.json").write_text(json.dumps(manifest))
    assert evaluate(run_dir) == 2
    assert cli.main(["report", str(run_dir)]) == 2
    assert cli.main(["dashboard", str(run_dir)]) == 2
    err = capsys.readouterr().err
    assert (
        err.count("schema_version 'v0' is not 'v1alpha1'; this harness cannot read it")
        == 3
    )
    assert not (run_dir / "evaluations").exists()


def test_evaluate_reports_each_check(run_dir, capsys):
    assert evaluate(run_dir) == 1
    verdict = read_verdict(run_dir)
    assert verdict["verdict"] == "FAIL"
    results = {(r["scope"], r["instance"], r["check"]): r for r in verdict["results"]}
    assert set(results) == {
        ("workload", "w-0", "exited-ok"),
        ("workload", "w-0", "wrote-done"),
        ("workload", "w-1", "exited-ok"),
        ("workload", "w-1", "wrote-done"),
        ("experiment", None, "timeline-written"),
    }
    assert results["workload", "w-0", "exited-ok"]["ok"]
    assert results["workload", "w-0", "wrote-done"]["ok"]
    assert results["workload", "w-1", "exited-ok"]["output"] == "exit code 1, want 0"
    assert "w-1 missing" in results["workload", "w-1", "wrote-done"]["output"]
    assert results["experiment", None, "timeline-written"]["ok"]
    *_, last_verdict, path = capsys.readouterr().out.strip().splitlines()
    assert last_verdict == "FAIL"
    assert Path(path).parent == run_dir / "evaluations"


def test_evaluate_is_reproducible_and_appends_index(run_dir):
    (run_dir / "verdict.json").write_text(json.dumps({"verdict": "PASS"}))
    index = run_dir.parents[1] / "index.jsonl"
    evaluate(run_dir, "--index", str(index))
    first = read_verdict(run_dir)
    evaluate(run_dir, "--index", str(index))
    assert read_verdict(run_dir)["results"] == first["results"]
    assert len(list((run_dir / "evaluations").glob("*.json"))) == 2
    # An evaluation never replaces the verdict the run wrote.
    assert json.loads((run_dir / "verdict.json").read_text()) == {"verdict": "PASS"}
    lines = [json.loads(line) for line in index.read_text().splitlines()]
    assert [line["verdict"] for line in lines] == ["FAIL", "FAIL"]
    assert lines[0]["system"] == {"git_sha": "abc"}
    assert lines[0]["adapter"] == "openshell-docker"
    assert lines[0]["flaky_missed"] == []
    # Paths are relative to the index's directory.
    assert lines[0]["path"] == f"evaluated/{RUN_ID}"
    assert lines[0]["evaluation"].startswith(f"evaluated/{RUN_ID}/evaluations/")
    # Each evaluation says what it read, and the printed verdict starts with it.
    archived = run_dir / "experiment.yaml"
    assert first["read"] == {
        "experiment": {
            "file": str(archived),
            "sha256": hashlib.sha256(archived.read_bytes()).hexdigest(),
        },
        "adapter": {
            "id": "openshell-docker",
            "file": str(run_dir.parents[2] / "adapters" / "openshell-docker.yaml"),
        },
    }
    assert format_verdict(first).startswith(f"  evaluated with {archived} (sha256 ")


def test_evaluate_keeps_run_errors(run_dir):
    (run_dir / "verdict.json").write_text(json.dumps({"errors": ["aborted by SIGINT"]}))
    assert evaluate(run_dir) == 2
    verdict = read_verdict(run_dir)
    assert verdict["verdict"] == "ERROR"
    assert verdict["errors"] == ["aborted by SIGINT"]


def test_evaluate_of_unfinished_run_is_error(run_dir):
    (run_dir / "timeline.jsonl").write_text(json.dumps({"event": "run.started"}) + "\n")
    assert evaluate(run_dir) == 2
    assert read_verdict(run_dir)["errors"] == ["run did not finish"]
    # The run wrote the same error: it is not repeated.
    (run_dir / "verdict.json").write_text(
        json.dumps({"errors": ["run did not finish"]})
    )
    evaluate(run_dir)
    assert read_verdict(run_dir)["errors"] == ["run did not finish"]


def test_evaluate_with_edited_experiment(run_dir, tmp_path):
    edited = json.loads(json.dumps(EXPERIMENT))
    edited["workloads"][0]["checks"] = edited["workloads"][0]["checks"][1:]
    path = tmp_path / "evaluated.yaml"
    path.write_text(yaml.safe_dump(edited))
    (
        run_dir / "workloads" / "w" / "instances" / "w-1" / "output" / "result"
    ).write_text("done\n")
    assert evaluate(run_dir, "--experiment", str(path)) == 0


CHECKS = ["w/exited-ok", "w/wrote-done"]


def test_evaluate_known_failures_are_xfail(run_dir, tmp_path):
    path = _edited(tmp_path, expectations=[{"issue": "o/r#1", "checks": CHECKS}])
    assert evaluate(run_dir, "--experiment", path) == 0
    verdict = read_verdict(run_dir)
    assert verdict["verdict"] == "XFAIL"
    issues = {(r["instance"], r["check"]): r["issue"] for r in verdict["results"]}
    assert issues["w-1", "exited-ok"] == issues["w-1", "wrote-done"] == "o/r#1"
    # w-0 passed checks that w-1 failed: a race, not a fix.
    assert issues["w-0", "exited-ok"] is None and issues["w-0", "wrote-done"] is None


def test_evaluate_unexpected_pass_fails(run_dir, tmp_path):
    expectations = [
        {"issue": "o/r#1", "checks": CHECKS},
        {"issue": "o/r#2", "checks": ["experiment/timeline-written"]},
    ]
    path = _edited(tmp_path, expectations=expectations)
    assert evaluate(run_dir, "--experiment", path) == 1
    verdict = read_verdict(run_dir)
    assert verdict["verdict"] == "FAIL"
    xpass = [r for r in verdict["results"] if r["ok"] and r["issue"]]
    assert [(r["check"], r["issue"]) for r in xpass] == [("timeline-written", "o/r#2")]


def test_missing_exit_code(run_dir, adapters_dir):
    (
        run_dir
        / "workloads"
        / "w"
        / "instances"
        / "w-1"
        / "output"
        / ".chaos"
        / "exit_code"
    ).unlink()
    exp, _ = load_experiment(_write(run_dir.parent, EXPERIMENT))
    adapter = load_adapter(adapters_dir(), "openshell-docker")
    info = RunInfo("evaluated", 1, "openshell-docker", RUN_ID, 0, run_dir)
    results = asyncio.run(run_checks(exp, adapter, info))
    missing = next(r for r in results if r.instance == "w-1" and r.check == "exited-ok")
    assert not missing.ok and missing.output == "no exit code recorded"


def _write(directory: Path, raw: dict) -> Path:
    path = directory / f"{raw['id']}.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


def test_verdict_of():
    ok = Result("workload", "w", "w-0", "c", "d", True, "")
    bad = Result("workload", "w", "w-1", "c", "d", False, "boom")
    assert verdict_of([ok], []) == "PASS"
    assert verdict_of([ok, bad], []) == "FAIL"
    assert verdict_of([ok], ["timeout"]) == "ERROR"
    assert verdict_of([], []) == "PASS"
    known = Result("workload", "w", "w-1", "c", "d", False, "boom", issue="o/r#1")
    fixed = Result("workload", "w", "w-0", "c", "d", True, "", issue="o/r#1")
    assert verdict_of([ok, known], []) == "XFAIL"
    assert verdict_of([known, bad], []) == "FAIL"
    assert verdict_of([fixed], []) == "FAIL"
    assert verdict_of([known], ["timeout"]) == "ERROR"


def test_format_verdict(tmp_path):
    bad = Result(
        "workload", "w", "w-1", "c", "d", False, "\n".join(f"l{i}" for i in range(9))
    )
    verdict = write_verdict(tmp_path, [bad], ["timeout"])
    text = format_verdict(verdict)
    assert "  FAIL  w-1/c\n" in text and "l8" in text and "l3" not in text
    assert text.splitlines()[-2:] == ["  error: timeout", "ERROR"]


def test_format_verdict_marks_known_failures(tmp_path):
    known = Result("workload", "w", "w-1", "c", "d", False, "boom", issue="o/r#1")
    fixed = Result("experiment", None, None, "e", "d", True, "", issue="o/r#2")
    text = format_verdict(write_verdict(tmp_path, [known, fixed], []))
    assert "  xfail w-1/c (known: o/r#1)" in text
    assert "  XPASS experiment/e (expected to fail: o/r#2; fixed?)" in text


def test_validate_command(capsys):
    assert cli.main(["validate", str(FIXTURES / "valid-minimal.yaml")]) == 0
    assert cli.main(["validate", str(FIXTURES / "invalid-no-checks.yaml")]) == 1
    assert "invalid-no-checks.yaml" in capsys.readouterr().err


def test_list_and_show(capsys):
    assert cli.main(["list"]) == 0
    assert "gateway-kill-keeps-state (v" in capsys.readouterr().out
    assert cli.main(["show", "smoke"]) == 0
    out = capsys.readouterr().out
    assert "check journal-complete:" in out and "hypothesis:" in out
    assert cli.main(["list", "--issue", "#4910"]) == 0
    out = capsys.readouterr().out
    assert "gateway-kill-keeps-state (v" in out and "smoke (v" not in out
    assert cli.main(["show", "gateway-kill-keeps-state"]) == 0
    assert (
        "NVIDIA/OpenShell#4910, known to fail:\n    counter/exited-ok\n"
        in capsys.readouterr().out
    )


def test_list_and_show_fit_80_columns(capsys):
    cli.main(["list"])
    ids = [line for line in capsys.readouterr().out.splitlines() if " (v" in line]
    assert ids
    for line in ids:
        cli.main(["show", line.split(" (v")[0]])
    cli.main(["list"])
    lines = capsys.readouterr().out.splitlines()
    assert [line for line in lines if len(line) > 80] == []


def test_run_refuses_without_wrapper_env(monkeypatch, tmp_path, capsys):
    adapter = load_adapter(cli.DEFAULT_ADAPTERS_DIR, "openshell-docker")
    for name in adapter.requires_env:
        monkeypatch.delenv(name, raising=False)
    smoke = cli.DEFAULT_EXPERIMENTS_DIR / "smoke.yaml"
    code = cli.main(["run", str(smoke), "--runs-dir", str(tmp_path)])
    assert code == 2
    assert "needs these environment variables:\n  OPENSHELL_BIN\n" in (
        capsys.readouterr().err
    )
    assert not any(tmp_path.iterdir())


def _edited(tmp_path: Path, **changes) -> str:
    """The experiment with `changes`."""
    edited = {**json.loads(json.dumps(EXPERIMENT)), **changes}
    path = tmp_path / "evaluated.yaml"
    path.write_text(yaml.safe_dump(edited))
    return str(path)


def _results(run_dir: Path) -> dict[tuple[str | None, str], dict]:
    return {(r["instance"], r["check"]): r for r in read_verdict(run_dir)["results"]}


OPERATION = {
    "name": "f",
    "description": "d",
    "uses": "kill",
    "target": {"workload": "w", "role": "supervisor"},
    "select": "one",
}


@pytest.mark.parametrize(
    ("selected", "code", "verdict"),
    [(["w-1"], 0, "XFAIL"), (["w-0"], 1, "FAIL")],
)
def test_evaluate_expectation_on_affected_instances(
    run_dir, tmp_path, selected, code, verdict
):
    with (run_dir / "timeline.jsonl").open("w") as f:
        for event in (
            {
                "event": "operation.started",
                "operation": "f",
                "scope": "instances",
                "instances": selected,
            },
            {"event": "run.finished", "verdict": "FAIL"},
        ):
            f.write(json.dumps(event) + "\n")
    started = {"wait": {"workload": "w", "condition": "started"}}
    path = _edited(
        tmp_path,
        operations=[OPERATION],
        flow={"parallel": ["w", {"serial": [started, "f"]}]},
        expectations=[{"issue": "o/r#1", "checks": CHECKS, "instances": "affected"}],
    )
    assert evaluate(run_dir, "--experiment", path) == code
    assert read_verdict(run_dir)["verdict"] == verdict
    # Only the selected instance is covered; the other one is the control.
    results = _results(run_dir)
    assert {i for (i, _), r in results.items() if r["issue"]} == set(selected)


def test_evaluate_flaky_expectation(run_dir, tmp_path):
    expectation = {"issue": "o/r#1", "checks": CHECKS, "kind": "flaky"}
    path = _edited(tmp_path, expectations=[expectation])
    assert evaluate(run_dir, "--experiment", path) == 0
    assert read_verdict(run_dir)["verdict"] == "XFAIL"
    # The bug did not trigger anywhere: still a pass, not an XPASS.
    w1 = run_dir / "workloads" / "w" / "instances" / "w-1" / "output"
    (w1 / ".chaos" / "exit_code").write_text("0\n")
    (w1 / "result").write_text("done\n")
    index = run_dir.parents[1] / "index.jsonl"
    assert evaluate(run_dir, "--experiment", path, "--index", str(index)) == 0
    assert read_verdict(run_dir)["verdict"] == "PASS"
    assert all(r["flaky"] for r in _results(run_dir).values() if r["issue"])
    # The index says which flaky known failures did not trigger.
    assert read_verdict(run_dir)["read"]["experiment"]["file"] == path
    (line,) = [json.loads(line) for line in index.read_text().splitlines()]
    assert line["flaky_missed"] == CHECKS
    strict = _edited(tmp_path, expectations=[{"issue": "o/r#1", "checks": CHECKS}])
    assert evaluate(run_dir, "--experiment", strict) == 1


@pytest.mark.parametrize(
    ("run", "timeout"), [("no-such-command-xyz", "1m"), ("sleep 5", "1s")]
)
def test_evaluate_checker_error_is_error(run_dir, tmp_path, run, timeout):
    workloads = json.loads(json.dumps(EXPERIMENT["workloads"]))
    workloads[0]["checks"][1].update(run=run, timeout=timeout)
    # The expectation does not excuse a check that could not decide.
    path = _edited(
        tmp_path,
        workloads=workloads,
        expectations=[{"issue": "o/r#1", "checks": CHECKS}],
    )
    assert evaluate(run_dir, "--experiment", path) == 2
    assert read_verdict(run_dir)["verdict"] == "ERROR"
    errored = _results(run_dir)["w-0", "wrote-done"]
    assert errored["error"] and errored["issue"] is None


def test_selected_instances():
    events = [
        {
            "event": "operation.started",
            "operation": "f",
            "scope": "instances",
            "instances": ["w-0"],
        },
        {
            "event": "operation.started",
            "operation": "a",
            "scope": "instances",
            "instances": ["w-1", "w-2"],
        },
        {"event": "operation.started", "operation": "g", "scope": "global"},
        {"event": "operation.completed", "operation": "f", "outcome": "ok"},
    ]
    assert selected_instances(events) == {"w-0", "w-1", "w-2"}


def test_verdict_and_format_of_errors_and_flaky(tmp_path):
    error = Result("workload", "w", "w-0", "c", "d", False, "x", error=True)
    flaky_ok = Result(
        "workload", "w", "w-0", "f", "d", True, "", issue="o/r#1", flaky=True
    )
    flaky_bad = Result(
        "workload", "w", "w-1", "f", "d", False, "x", issue="o/r#1", flaky=True
    )
    assert verdict_of([error], []) == "ERROR"
    assert verdict_of([flaky_ok], []) == "PASS"
    assert verdict_of([flaky_ok, flaky_bad], []) == "XFAIL"
    text = format_verdict(write_verdict(tmp_path, [error, flaky_ok, flaky_bad], []))
    assert "  ERROR w-0/c (the check could not decide)" in text
    assert "  ok    w-0/f (flaky: o/r#1 did not trigger)" in text
    assert "  xfail w-1/f (known, flaky: o/r#1)" in text


def _scrapes(
    run_dir: Path, samples: list[dict[str, float]], source: str = "gw"
) -> None:
    directory = run_dir / "system" / "metrics"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{source}.jsonl").write_text(
        "".join(
            json.dumps({"t": float(t), "up": 1, "samples": s}) + "\n"
            for t, s in enumerate(samples)
        )
        + json.dumps({"t": float(len(samples)), "up": 0, "samples": {}})
        + "\n"
    )


def _metric_check(name: str, **fields) -> dict:
    fields = {k.replace("_", "-"): v for k, v in fields.items()}
    return {"name": name, "description": "d", "kind": "metric", **fields}


def test_metric_checks_read_the_recorded_scrapes(run_dir, tmp_path):
    _scrapes(
        run_dir,
        [
            {'req_total{m="a"}': 10, 'req_total{m="b"}': 1, "sessions": 1},
            {'req_total{m="a"}': 16, 'req_total{m="b"}': 1, "sessions": 3},
            # A counter reset: the increase counts 2, not -14.
            {'req_total{m="a"}': 2, 'req_total{m="b"}': 0, "sessions": 0},
        ],
    )
    (
        run_dir / "workloads" / "w" / "instances" / "w-1" / "output" / "result"
    ).write_text("done\n")
    path = _edited(
        tmp_path,
        metrics=["req_total", "sessions", "lat_seconds"],
        checks=[
            _metric_check("peak", metric="sessions", value="max", at_least=3),
            _metric_check("last", metric="sessions", equals=0),
            _metric_check("grew", metric="req_total", value="increase", equals=8),
            _metric_check(
                "a-only", metric="req_total", labels={"m": "a"}, value="min", at_least=2
            ),
            _metric_check("never", metric="lat_seconds_count", at_least=1),
            _metric_check("elsewhere", metric="sessions", source="other", equals=0),
        ],
    )
    # One exit code is 1, so the verdict is FAIL whatever the metric checks say.
    assert evaluate(run_dir, "--experiment", path) == 2
    results = _results(run_dir)
    assert [
        (results[None, n]["ok"], results[None, n]["error"])
        for n in ("peak", "last", "grew", "a-only", "never", "elsewhere")
    ] == [(True, False)] * 4 + [(False, False), (False, True)]
    assert results[None, "grew"]["output"] == (
        "increase = 8 (want equals 8); 2 series of req_total summed over 3 scrapes of gw"
    )
    assert results[None, "a-only"]["output"].startswith(
        'min = 2 (want at-least 2); 1 series of req_total{m="a"}'
    )
    assert results[None, "never"]["output"] == (
        "no series lat_seconds_count in any scrape (gw)"
    )
    assert results[None, "elsewhere"]["output"] == (
        "no scrapes of metrics source 'other' in system/metrics"
    )


def test_metric_check_needs_one_source(run_dir, tmp_path):
    _scrapes(run_dir, [{"sessions": 1}], source="gw")
    _scrapes(run_dir, [{"sessions": 2}], source="gw2")
    path = _edited(
        tmp_path,
        metrics=["sessions"],
        checks=[
            _metric_check("either", metric="sessions", at_least=1),
            _metric_check("second", metric="sessions", source="gw2", equals=2),
        ],
    )
    evaluate(run_dir, "--experiment", path)
    results = _results(run_dir)
    assert results[None, "either"]["error"]
    assert (
        "more than one source (gw, gw2); set source"
        in results[None, "either"]["output"]
    )
    assert results[None, "second"]["ok"]
