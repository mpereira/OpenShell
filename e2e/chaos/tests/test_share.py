# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""`chaos share`: the Markdown page, built from a synthetic run dir."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from test_report import TIMELINE, jsonl, run_dir

from chaos.cli import main
from chaos.share import MAX_POINTS, ROW_BUDGET, flow_spans, gantt, xychart

NODES = [
    {"t": 0.5, "event": "node.started", "node": "flow.serial"},
    {"t": 0.5, "event": "node.started", "node": "flow.serial[0]", "name": "counter"},
    {
        "t": 1.0,
        "event": "node.started",
        "node": "flow.serial[1].wait",
        "wait": "counter",
        "condition": "started",
    },
    {
        "t": 1.5,
        "event": "node.finished",
        "node": "flow.serial[1].wait",
        "wait": "counter",
        "condition": "started",
    },
    {"t": 1.5, "event": "node.started", "node": "flow.serial[2].delay", "delay": "2s"},
    {"t": 3.5, "event": "node.finished", "node": "flow.serial[2].delay", "delay": "2s"},
    {
        "t": 4.0,
        "event": "node.started",
        "node": "flow.serial[3]",
        "name": "gateway-down",
    },
    {
        "t": 9.0,
        "event": "node.finished",
        "node": "flow.serial[3]",
        "name": "gateway-down",
    },
    {"t": 13.0, "event": "node.finished", "node": "flow.serial[0]", "name": "counter"},
]

RESULTS = [
    {
        "scope": "preflight",
        "workload": None,
        "instance": None,
        "check": "gateway-up",
        "description": "",
        "ok": True,
        "output": "",
        "issue": None,
        "flaky": False,
        "error": None,
    },
    {
        "scope": "workload",
        "workload": "counter",
        "instance": "counter-0",
        "check": "exited-ok",
        "description": "",
        "ok": True,
        "output": "exit code 0",
        "issue": None,
        "flaky": False,
        "error": None,
    },
    {
        "scope": "workload",
        "workload": "counter",
        "instance": "counter-0",
        "check": "journal-complete",
        "description": "",
        "ok": False,
        "output": "12,60d11\nmore",
        "issue": "NVIDIA/OpenShell#1",
        "flaky": False,
        "error": None,
    },
    {
        "scope": "experiment",
        "workload": None,
        "instance": None,
        "check": "sessions-max",
        "description": "",
        "ok": True,
        "output": "max = 2 (want at-least 2); 1 series of sessions summed over "
        "3 scrapes of gateway",
        "issue": None,
        "flaky": False,
        "error": None,
    },
]


def share_run(tmp_path: Path) -> Path:
    run = run_dir(tmp_path)
    events = sorted(TIMELINE + NODES, key=lambda e: e["t"])
    jsonl(run / "timeline.jsonl", [{"ts": "2026-10-27T00:00:00Z", **e} for e in events])
    (run / "verdict.json").write_text(
        json.dumps(
            {"verdict": "XFAIL", "errors": ["adapter said no"], "results": RESULTS}
        )
    )
    (run / "experiment.yaml").write_text(
        "id: demo\ntitle: Demo\nhypothesis: |\n  It\n  holds.\n"
        "metrics:\n"
        "  - family: sessions\n    description: Sessions on the gateway.\n"
        "  - family: lat\n    description: Request latency.\n"
        "  - req_total\n"
        "checks:\n"
        "  - name: sessions-max\n    kind: metric\n    metric: sessions\n"
        "    value: max\n    at-least: 2\n"
        "  - name: exited-ok\n    kind: exit-code\n    equals: 0\n"
    )
    return run


def test_share_page(tmp_path):
    run = share_run(tmp_path)
    out = tmp_path / "page.md"
    assert main(["share", str(run), "-o", str(out)]) == 0
    text = out.read_text()
    assert text.startswith("## demo · r1 · XFAIL\n")
    assert "Hypothesis: It holds.\n" in text
    assert (
        "| | |\n|---|---|\n| Experiment | `demo` |\n| Run | `r1` |\n"
        "| Run directory | `run` |\n| Length | 14s |\n"
        "| Seed | `7`, no operation selects instances; scripts get it as "
        "CHAOS_EXPERIMENT_RUN_SEED |\n"
        "| Adapter | `openshell-docker` |\n| OpenShell | `1.2.3` |\n"
    ) in text
    # Preflight checks are not counted; the failed one is listed with its issue.
    assert "**Checks:** 2 passed, 1 failed (1 known)." in text
    assert (
        "- `journal-complete` on counter-0 failed, known NVIDIA/OpenShell#1: 12,60d11\n"
        in text
    )
    assert "Harness error: adapter said no" in text
    chart = text.split("```mermaid\n")[1].split("```")[0]
    assert chart.startswith("gantt\n  title demo · r1 · XFAIL\n  dateFormat mm:ss\n")
    assert "  section flow\n    counter :00:00, 00:13\n" in chart
    assert "    wait counter started :00:01, 00:02\n" in chart
    assert "    delay 2s :00:02, 00:04\n" in chart
    # A built-in operation is its fault window, not a second row for its node.
    assert "    fault gateway-down (outage) :crit, 00:04, 00:09\n" in chart
    assert "\n    gateway-down :" not in chart
    assert "    fault kill-one (kill) :milestone, crit, 00:10, 00:10\n" in chart
    assert "  section counter-0\n" in chart
    assert "    Running :active, 00:02, 00:12\n" in chart
    assert "    Succeeded :done, 00:12, 00:13\n" in chart
    assert "would not fit" not in text
    # The metrics table joins the experiment's metric checks to their family.
    assert (
        "| `sessions` | Sessions on the gateway. | "
        "`sessions-max` ok: max = 2 (want at-least 2) |\n" in text
    )
    assert "| `lat` | Request latency. | none |\n" in text
    assert "| `req_total` |  | none |\n" in text
    # Charts in the declared order; after the first two they fold.
    heads = [line for line in text.splitlines() if line.startswith("#### ")]
    assert heads == [
        "#### sessions",
        "#### lat (observations)",
        "#### lat (mean per observation)",
        "#### req_total",
    ]
    assert "<summary>2 more charts</summary>" in text
    assert text.index("#### lat (observations)") < text.index("<details>")
    assert text.index("<details>") < text.index("#### lat (mean per observation)")
    sessions = text.split("#### sessions\n")[1].split("####")[0]
    assert '  x-axis "seconds since the run started" 0 --> 8\n' in sessions
    assert "  line [1, 2, 0, 0, 1]\n" in sessions
    assert (
        "Sessions on the gateway. Scrapes the endpoint did not answer are drawn at 0."
        in sessions
    )


def test_default_output_is_next_to_the_report(tmp_path, capsys):
    run = share_run(tmp_path)
    assert main(["share", str(run)]) == 0
    assert capsys.readouterr().out.strip() == str(run / "share.md")
    assert (run / "share.md").is_file()


def test_run_dir_without_manifest_is_refused(tmp_path, capsys):
    assert main(["share", str(tmp_path)]) == 2
    assert "manifest" in capsys.readouterr().err


def test_png_needs_mermaid_cli(tmp_path, capsys, monkeypatch):
    run = share_run(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert main(["share", str(run), "--png"]) == 2
    err = capsys.readouterr().err
    assert "mermaid-cli" in err and "mmdc -i" in err
    assert (run / "share.md").is_file()


def test_repeated_nodes_collapse_into_one_row():
    timeline = []
    for i in range(4):
        node = f"flow.repeat[{i}]"
        timeline.append(
            {"t": i * 10.0, "event": "node.started", "node": node, "name": "restart"}
        )
        timeline.append(
            {
                "t": i * 10.0 + 2,
                "event": "node.finished",
                "node": node,
                "name": "restart",
            }
        )
    timeline.append(
        {"t": 40.0, "event": "node.started", "node": "flow.open", "name": "tail"}
    )
    spans = flow_spans(timeline)
    assert [s["name"] for s in spans] == ["restart"] * 4 + ["tail"]
    # A node still open when the timeline ends runs to its last event.
    assert spans[-1]["to"] == 40.0
    data = {
        "experiment": {"id": "x"},
        "run_id": "r",
        "verdict": "PASS",
        "end": 40.0,
        "timeline": timeline,
        "faults": [],
        "lanes": [],
        "results": [],
    }
    chart, note = gantt(data)
    assert "    restart ×4 :00:00, 00:32\n" in chart
    assert "\n    restart :" not in chart
    assert note == ""


def lane(name: str, states: list[str]) -> dict:
    return {
        "name": name,
        "segments": [
            {"from": float(i), "to": float(i + 1), "state": s}
            for i, s in enumerate(states)
        ],
    }


def test_instances_beyond_the_row_budget_are_left_out():
    per_lane = ROW_BUDGET // 4
    data = {
        "experiment": {"id": "x"},
        "run_id": "r",
        "verdict": "FAIL",
        "end": 100.0,
        "timeline": [],
        "faults": [{"name": "f", "uses": "kill", "from": 1.0, "to": 2.0}],
        "lanes": [lane(f"c-{i}", ["Running"] * per_lane) for i in range(5)]
        + [lane("c-5", ["Running"] * (per_lane - 1) + ["Unknown"])],
        "results": [
            {"scope": "workload", "instance": "c-3", "check": "k", "ok": False},
        ],
    }
    chart, note = gantt(data)
    sections = [line.split()[1] for line in chart.splitlines() if "section" in line]
    # The instance with a failed check, then the one that went Unknown, then
    # the rest in run order, up to the budget.
    assert sections == ["flow", "c-3", "c-5", "c-0"]
    assert chart.count("\n    ") == 1 + 3 * per_lane
    assert note.startswith(
        f"3 of 6 instances ({3 * per_lane} state changes) would not fit"
    )
    data["lanes"] = data["lanes"][:1]
    chart, note = gantt(data)
    assert note == ""
    data["lanes"][0]["segments"] *= 5
    chart, note = gantt(data)
    assert "section c-0" not in chart
    assert note.startswith(f"The instance states ({5 * per_lane} state changes)")


def test_xychart_buckets_long_series_and_drops_leading_gaps():
    points = [
        [float(t), None if t < 2 else float(t % 7)] for t in range(3 * MAX_POINTS)
    ]
    text, note = xychart(
        {"title": 'a "b"', "subtitle": "per second", "points": points, "family": "a"}
    )
    assert '  title "a  b "\n' in text
    values = text.split("line [")[1].split("]")[0].split(", ")
    assert len(values) <= MAX_POINTS
    assert note == f"{3 * MAX_POINTS - 2} scrapes averaged into {len(values)} points"
    assert '  x-axis "seconds since the run started" 2 --> ' in text
    assert xychart(
        {"title": "t", "subtitle": "s", "points": [[0, None]], "family": "a"}
    ) == (
        "",
        "no scrapes with a value",
    )


def test_mermaid_numbers_are_plain_decimals(tmp_path):
    """Mermaid's xychart parser rejects exponent form, which Python prints
    for small per-tick latencies (`9e-05`) and for large values."""
    run = share_run(tmp_path)
    jsonl(
        run / "system" / "metrics" / "gateway.jsonl",
        [
            {"t": float(t), "up": 1, "samples": {"sessions": v}}
            for t, v in enumerate([0.000235, 0, 0.00009, 0.0001855, 0, 123456.7])
        ],
    )
    out = tmp_path / "page.md"
    assert main(["share", str(run), "-o", str(out)]) == 0
    text = out.read_text()
    fences = [block.split("```")[0] for block in text.split("```mermaid\n")[1:]]
    assert any("xychart-beta" in fence for fence in fences)
    for fence in fences:
        assert not re.search(r"\d[eE][+-]?\d", fence), fence
    sessions = text.split("#### sessions\n")[1].split("####")[0]
    assert "  line [0.000235, 0, 0.00009, 0.0001855, 0, 123500]\n" in sessions
    assert '" 0 --> 135800\n  line [' in sessions
