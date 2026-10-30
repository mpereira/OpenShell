# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The HTML report, built from a synthetic run dir."""

from __future__ import annotations

import itertools
import json
import shutil
from pathlib import Path

from chaos.export import export
from chaos.report import build, write_report

TIMELINE = [
    {"t": 0.0, "event": "run.started"},
    {
        "t": 1.0,
        "event": "instance.created",
        "workload": "counter",
        "instance": "counter-0",
        "system_id": "sb-0",
    },
    {
        "t": 1.5,
        "event": "instance.state",
        "workload": "counter",
        "instance": "counter-0",
        "state": "Running",
    },
    {
        "t": 4.0,
        "event": "operation.started",
        "operation": "gateway-down",
        "uses": "outage",
        "run": 1,
        "target": "gateway",
        "scope": "target",
        "instances": [],
        "ids": [],
    },
    {
        "t": 4.0,
        "event": "operation.applied",
        "operation": "gateway-down",
        "uses": "outage",
    },
    {
        "t": 9.0,
        "event": "operation.reverted",
        "operation": "gateway-down",
        "uses": "outage",
    },
    {
        "t": 10.0,
        "event": "operation.applied",
        "operation": "kill-one",
        "uses": "kill",
    },
    {
        "t": 12.0,
        "event": "instance.state",
        "workload": "counter",
        "instance": "counter-0",
        "state": "Succeeded",
    },
    {
        "t": 12.5,
        "event": "instance.collected",
        "workload": "counter",
        "instance": "counter-0",
        "exit_code": 0,
    },
    {
        "t": 13.0,
        "event": "instance.deleted",
        "workload": "counter",
        "instance": "counter-0",
    },
    {"t": 14.0, "event": "run.finished", "verdict": "PASS"},
]


def jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def run_dir(tmp_path: Path) -> Path:
    run = tmp_path / "run"
    jsonl(
        run / "timeline.jsonl", [{"ts": "2026-10-27T00:00:00Z", **e} for e in TIMELINE]
    )
    (run / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "v1alpha1",
                "run_id": "r1",
                "seed": 7,
                "adapter": {"id": "openshell-docker"},
                "environment": {"system": {"openshell": "1.2.3"}},
            }
        )
    )
    (run / "verdict.json").write_text(
        json.dumps({"verdict": "PASS", "errors": [], "results": []})
    )
    (run / "experiment.yaml").write_text(
        "id: demo\ntitle: Demo </script>\nhypothesis: |\n  It\n  holds.\n"
    )
    metrics = run / "system" / "metrics"
    jsonl(
        metrics / "gateway.jsonl",
        [
            {
                "t": 0.0,
                "up": 1,
                "samples": {
                    'req_total{m="a"}': 10,
                    'req_total{m="b"}': 0,
                    "sessions": 1,
                    "lat_count": 0,
                    "lat_sum": 0,
                    'lat_bucket{le="1"}': 0,
                    "idle": 0,
                },
            },
            {
                "t": 2.0,
                "up": 1,
                "samples": {
                    'req_total{m="a"}': 14,
                    'req_total{m="b"}': 2,
                    "sessions": 2,
                    "lat_count": 4,
                    "lat_sum": 2,
                    'lat_bucket{le="1"}': 4,
                    "idle": 0,
                },
            },
            {"t": 4.0, "up": 0, "error": "refused"},
            {"t": 6.0, "up": 0, "error": "refused"},
            {
                "t": 8.0,
                "up": 1,
                "samples": {
                    'req_total{m="a"}': 1,
                    "sessions": 1,
                    "lat_count": 6,
                    "lat_sum": 3,
                    "idle": 0,
                },
            },
        ],
    )
    (metrics / "gateway.types.json").write_text(
        json.dumps(
            {
                "req_total": "counter",
                "sessions": "gauge",
                "lat": "histogram",
                "idle": "gauge",
            }
        )
    )
    line = {
        "Action": "die",
        "Actor": {
            "Attributes": {
                "openshell.ai/sandbox-id": "sb-0",
                "openshell.ai/isolation-role": "supervisor",
                "exitCode": "137",
            }
        },
    }
    other = {
        "Action": "die",
        "Actor": {"Attributes": {"openshell.ai/sandbox-id": "elsewhere"}},
    }
    jsonl(
        run / "system" / "watch" / "docker-events.jsonl",
        [
            {"t": 10.1, "line": json.dumps(line)},
            {"t": 10.2, "line": json.dumps(other)},
            {"t": 10.3, "line": "not json"},
        ],
    )
    return run


def test_lanes_faults_and_marks(tmp_path):
    data = build(run_dir(tmp_path))
    (lane,) = data["lanes"]
    assert [(s["state"], s["from"], s["to"]) for s in lane["segments"]] == [
        ("Created", 1.0, 1.5),
        ("Running", 1.5, 12.0),
        ("Succeeded", 12.0, 13.0),
    ]
    labels = [m["label"] for m in lane["marks"]]
    assert "supervisor die (exit 137)" in labels
    assert len(labels) == 3  # collected, deleted, one docker event for this run
    assert [(f["name"], f["from"], f["to"], f["ended_by"]) for f in data["faults"]] == [
        ("gateway-down", 4.0, 9.0, "operation.reverted"),
        ("kill-one", 10.0, 10.0, "operation.applied"),
    ]
    assert data["experiment"]["hypothesis"] == "It holds."
    assert [e["t"] for e in data["events"]] == [0.0, 14.0]


def test_metric_charts(tmp_path):
    data = build(run_dir(tmp_path))
    charts = {c["title"]: c for c in data["charts"]}
    assert set(charts) == {
        "req_total",
        "sessions",
        "lat (observations)",
        "lat (mean per observation)",
    }
    # Counter rate: none at the first scrape, (16 - 10) / 2s, a gap while
    # down, then a reset (16 -> 1) read as 1 since the last answer at t=2.
    rates = charts["req_total"]["points"]
    assert rates == [[0.0, None], [2.0, 3.0], [4.0, None], [6.0, None], [8.0, 0.166667]]
    assert charts["req_total"]["subtitle"] == "per second, sum of 2 series"
    assert charts["sessions"]["points"][0] == [0.0, 1.0]
    assert charts["lat (mean per observation)"]["points"] == [
        [0.0, None],
        [2.0, 0.5],
        [4.0, None],
        [6.0, None],
        [8.0, None],
    ]
    assert data["down"] == [{"source": "gateway", "from": 4.0, "to": 8.0}]


def test_listed_metrics_come_first_and_keep_zero_charts(tmp_path):
    run = run_dir(tmp_path)
    (run / "experiment.yaml").write_text(
        "id: demo\ntitle: Demo\nhypothesis: h\n"
        "metrics:\n"
        "  - family: lat\n    description: |\n      Why it\n      matters.\n"
        "  - sessions\n"
        "  - family: absent_total\n"
        "  - {title: Up, expr: 'sum(up)'}\n"
    )
    metrics = run / "system" / "metrics"
    scrapes = [
        json.loads(line)
        for line in (metrics / "gateway.jsonl").read_text().splitlines()
    ]
    for s in scrapes:
        if s["up"]:
            s["samples"]["zero_total"] = 0
    jsonl(metrics / "gateway.jsonl", scrapes)
    (metrics / "gateway.types.json").write_text(
        json.dumps(
            {"req_total": "counter", "lat": "histogram", "zero_total": "counter"}
        )
    )
    data = build(run)
    assert data["metrics"] == [
        {"family": "lat", "description": "Why it matters."},
        {"family": "sessions", "description": ""},
        {"family": "absent_total", "description": ""},
    ]
    assert [(c["title"], c["declared"]) for c in data["charts"]] == [
        ("lat (observations)", True),
        ("lat (mean per observation)", True),
        ("sessions", True),
        ("req_total", False),
    ]
    # An all-zero family is charted only when it is listed.
    (run / "experiment.yaml").write_text(
        "id: demo\ntitle: Demo\nhypothesis: h\nmetrics: [zero_total]\n"
    )
    data = build(run)
    assert [(c["title"], c["declared"]) for c in data["charts"]][:1] == [
        ("zero_total", True)
    ]
    assert not any(v for _, v in data["charts"][0]["points"])


def test_summary_quantiles_not_charted(tmp_path):
    run = run_dir(tmp_path)
    metrics = run / "system" / "metrics"
    jsonl(
        metrics / "gateway.jsonl",
        [
            {
                "t": float(t),
                "up": 1,
                "samples": {
                    'dur{quantile="0.5"}': 0.1,
                    'dur{quantile="0.99"}': 0.9,
                    "dur_count": 2 * t,
                    "dur_sum": t,
                },
            }
            for t in range(3)
        ],
    )
    (metrics / "gateway.types.json").write_text(json.dumps({"dur": "summary"}))
    titles = [c["title"] for c in build(run)["charts"]]
    assert titles == ["dur (observations)", "dur (mean per observation)"]


def test_run_details_table(tmp_path):
    run = run_dir(tmp_path)
    manifest = json.loads((run / "manifest.json").read_text())
    manifest["experiment"] = {"id": "demo", "version": 2}
    manifest["started_at"] = "2026-10-27T00:00:00.123456Z"
    manifest["adapter"]["backend"] = "docker"
    manifest["environment"]["system"] = {
        "openshell": "openshell 1.2.3",
        "docker": "29.8.2",
        "gateway_sha256": "ab" * 32,
        "git_sha": "c0ffee",
        "git_dirty": True,
        "images": {"supervisor_image": "openshell/supervisor:dev@sha256:00"},
    }
    (run / "manifest.json").write_text(json.dumps(manifest))
    (run / "experiment.yaml").write_text(
        "id: demo\noperations:\n"
        "- {name: gateway-down, uses: outage, target: gateway, select: one}\n"
        "- {name: crash, uses: kill-all, scope: global}\n"
    )
    # Under `<runs dir>/<experiment>/<run id>` the directory names both.
    copy = tmp_path / "runs" / "demo" / "r1"
    shutil.copytree(run, copy)
    rows = build(copy)["details"]
    table = [
        (r["key"], r["value"], r["note"], r["code"]) for r in rows if not r["more"]
    ]
    assert table == [
        ("Experiment", "demo", "version 2", True),
        ("Run", "r1", "", True),
        ("Run directory", "demo/r1", "", True),
        ("Started", "2026-10-27 00:00:00 UTC", "", False),
        ("Length", "14s", "", False),
        (
            "Seed",
            "7",
            "picks the instances that gateway-down acts on; "
            "the same seed and instance counts pick the same ones",
            True,
        ),
        ("Adapter", "openshell-docker", "docker backend", True),
        ("OpenShell", "1.2.3", "", True),
        ("Docker", "29.8.2", "", True),
        ("Git", "c0ffee", "dirty", True),
    ]
    # Digests and image refs are folded under the table.
    assert [(r["key"], r["value"]) for r in rows if r["more"]] == [
        ("Gateway sha256", "ab" * 32),
        ("Images: supervisor image", "openshell/supervisor:dev@sha256:00"),
    ]
    # A run dir copied elsewhere shows its own name only.
    assert build(run)["details"][2]["value"] == "run"
    # The page draws the table from the embedded rows.
    html = write_report(copy, tmp_path / "page.html").read_text()
    data = html.split('<script id="data" type="application/json">')[1]
    assert json.loads(data.split("</script>")[0])["details"] == rows
    assert 'html("table", "", "details")' in html


def test_seed_note_without_selecting_operations(tmp_path):
    rows = {r["key"]: r for r in build(run_dir(tmp_path))["details"]}
    assert rows["Seed"]["note"] == (
        "no operation selects instances; scripts get it as CHAOS_EXPERIMENT_RUN_SEED"
    )
    assert "Started" not in rows and "Git" not in rows


def test_write_report_embeds_data_safely(tmp_path):
    run = run_dir(tmp_path)
    hostile = "--> </script><img src=x onerror=alert(1)> & \u2028\u2029 \u00e9"
    (run / "experiment.yaml").write_text(
        f"id: demo\ntitle: {json.dumps(hostile)}\nhypothesis: {json.dumps(hostile)}\n"
    )
    path = write_report(run)
    html = path.read_text()
    assert path.name == "report.html"
    assert "__DATA__" not in html and "__TITLE__" not in html
    assert "<title>demo r1</title>" in html
    # The data block is the only script that closes, no HTML tag or comment
    # delimiter from the data reaches the parser, and the data round-trips.
    assert html.count("</script>") == 2
    data = html.split('<script id="data" type="application/json">')[1].split(
        "</script>"
    )[0]
    assert not any(c in data for c in "<>&\u2028\u2029")
    assert json.loads(data)["experiment"]["title"] == hostile
    assert json.loads(data)["experiment"]["hypothesis"] == " ".join(hostile.split())


def test_export_openmetrics(tmp_path):
    out = tmp_path / "run.om"
    directory = run_dir(tmp_path)
    result = {"scope": "workload", "workload": "counter", "instance": "counter-0"}
    (directory / "verdict.json").write_text(
        json.dumps(
            {
                "verdict": "FAIL",
                "errors": [],
                "results": [
                    {**result, "check": "a", "ok": True},
                    {**result, "check": "b", "ok": False},
                ],
            }
        )
    )
    run = export(directory, out)
    assert (run.run_id, run.experiment) == ("r1", "demo")
    assert run.end - run.start == 14.0
    lines = out.read_text().splitlines()
    assert lines[-1] == "# EOF"
    names = [ln.split("{")[0] for ln in lines if not ln.startswith("#")]
    # Each family is contiguous, as OpenMetrics requires.
    runs = [name for name, _ in itertools.groupby(names)]
    assert len(runs) == len(set(runs))
    raw = [ln for ln in lines if ln.startswith("req_total")]
    assert raw[0].startswith(
        'req_total{m="a",chaos_run_id="r1",chaos_source="gateway"} 10 '
    )
    # Gaps are NaN, so Prometheus does not hold the last value across them.
    rate = [ln.split()[-2] for ln in lines if 'title="req_total"' in ln]
    assert rate == ["NaN", "3.0", "NaN", "NaN", "0.166667", "NaN"]
    state = [ln for ln in lines if ln.startswith("chaos_instance_state")]
    assert {ln.split()[1] for ln in state} == {"0", "2", "4"}
    fault = [ln for ln in lines if 'operation="gateway-down"' in ln]
    assert fault[-1].split()[1] == "0"
    checks = {
        ln.split('check="')[1].split('"')[0]: ln.split()[1]
        for ln in lines
        if ln.startswith("chaos_check_result")
    }
    assert checks == {"a": "1", "b": "0"}
    assert 'family="lat",title="lat (observations)"' in out.read_text()
    (die,) = [ln for ln in lines if "exit 137" in ln]
    value, ts = die.split()[-2:]
    assert int(value) == round(float(ts) * 1000)


def test_fault_windows_end_per_operation():
    from chaos.report import faults

    def ev(t, event, op, uses):
        return {"t": t, "event": event, "operation": op, "uses": uses}

    events = [
        ev(1.0, "operation.applied", "bounce", "restart"),
        # A revert of another operation does not end the restart window.
        ev(2.0, "operation.reverted", "bounce", "restart"),
        ev(3.0, "target.restarted", "bounce", "restart"),
        ev(4.0, "operation.applied", "in-place", "restart-in-place"),
        ev(5.0, "operation.applied", "ice", "freeze"),
        ev(6.0, "operation.applied", "down", "outage"),
        ev(7.0, "operation.error", "down", "outage"),
    ]
    assert [
        (f["name"], f["from"], f["to"], f["ended_by"]) for f in faults(events, 9.0)
    ] == [
        ("bounce", 1.0, 3.0, "target.restarted"),
        ("in-place", 4.0, 4.0, "operation.applied"),
        ("ice", 5.0, 9.0, None),
        ("down", 6.0, 7.0, "operation.error"),
    ]
