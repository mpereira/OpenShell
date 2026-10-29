# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""`chaos share RUN_DIR`: one Markdown page of a run to paste into an issue
comment or a post. It holds the verdict and the failed checks, a Mermaid
gantt of the flow (workloads, waits, operations, faults) over the instance
states, and the metrics the experiment lists under `metrics:` as a table of
their check results and one Mermaid chart each. GitHub draws the Mermaid
blocks; `--png` renders them with mermaid-cli for places that do not.

Everything comes from `chaos.report.build`, so the page agrees with
`report.html` and works on a copied run dir.
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

from chaos.report import build

# Gantt rows before the instance sections are left out: Mermaid draws one
# row per task, and GitHub refuses large diagrams.
ROW_BUDGET = 40
# A node or fault with the same name more often than this is one row.
COLLAPSE_AFTER = 3
# Points per chart; longer series are averaged into this many buckets.
MAX_POINTS = 60
# Charts shown before the rest fold.
CHARTS_SHOWN = 2
# Mermaid gantt tags by instance state; the others take the default look.
TAG = {
    "Running": "active",
    "Succeeded": "done",
    "Unknown": "crit",
    "Failed": "crit",
    "Error": "crit",
}
# Instances kept first when the chart cannot hold them all: a check failed
# on them, or they ended badly, then those that went through Unknown.
ENDED_BADLY = {"Failed", "Error"}
# The default xychart line is nearly white on Mermaid's default theme.
CHART_INIT = (
    '%%{init: {"theme": "base", "themeVariables": '
    '{"xyChart": {"plotColorPalette": "#1f3fb5"}}}}%%'
)


class ShareError(Exception):
    pass


def _clock(t: float, hours: bool) -> str:
    t = max(0, round(t))
    h, rest = divmod(t, 3600)
    m, s = divmod(rest, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if hours else f"{m:02d}:{s:02d}"


def _label(text: str) -> str:
    """Text safe inside a gantt task or section line."""
    return " ".join(re.sub(r"[:;,#%]+", " ", str(text)).split()) or "-"


def _task(label: str, tags: list[str], start: float, end: float, hours: bool) -> str:
    a, b = _clock(start, hours), _clock(end, hours)
    # A task shorter than a second has no width; a milestone has a diamond.
    tags = ["milestone", *tags] if a == b else tags
    return f"    {_label(label)} :{', '.join([*tags, a, b])}"


def flow_spans(timeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every flow node that started, by name: a workload or operation, a
    `wait` or a `delay`. A node still open at the end of the timeline ends
    there. A node id that starts again (under `repeat`) is a new span."""
    end = max((e["t"] for e in timeline), default=0.0)
    open_: dict[str, dict[str, Any]] = {}
    pairs: list[tuple[dict[str, Any], float]] = []
    for e in timeline:
        if e["event"] == "node.started":
            open_[e["node"]] = e
        elif e["event"] == "node.finished" and e["node"] in open_:
            pairs.append((open_.pop(e["node"]), e["t"]))
    pairs.extend((s, end) for s in open_.values())
    spans = []
    for s, to in pairs:
        if "name" in s:
            kind, name = "node", str(s["name"])
        elif "wait" in s:
            kind, name = "wait", f"wait {s['wait']} {s.get('condition', 'started')}"
        elif "delay" in s:
            kind, name = "delay", f"delay {s['delay']}"
        else:
            continue
        spans.append({"kind": kind, "name": name, "from": s["t"], "to": to})
    return sorted(spans, key=lambda s: (s["from"], s["to"]))


def _collapse(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows with the same name, more than COLLAPSE_AFTER of them, become one
    row from the first start to the last end, named `name ×N`."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row["name"], []).append(row)
    out = []
    for name, group in groups.items():
        if len(group) > COLLAPSE_AFTER:
            out.append(
                {
                    **group[0],
                    "name": f"{name} ×{len(group)}",
                    "from": min(r["from"] for r in group),
                    "to": max(r["to"] for r in group),
                }
            )
        else:
            out.extend(group)
    return sorted(out, key=lambda r: (r["from"], r["to"]))


def gantt(data: dict[str, Any]) -> tuple[str, str]:
    """The gantt text and a note on what it leaves out ('' when nothing)."""
    hours = data["end"] >= 3600
    title = " · ".join(
        str(x) for x in (data["experiment"]["id"], data["run_id"], data["verdict"]) if x
    )
    lines = [
        "gantt",
        f"  title {_label(title)}",
        f"  dateFormat {'HH:mm:ss' if hours else 'mm:ss'}",
        f"  axisFormat {'%H:%M:%S' if hours else '%M:%S'}",
        "  todayMarker off",
        "  section flow",
    ]
    # A built-in operation shows as its fault window, not as its flow node.
    fault_names = {f["name"] for f in data["faults"]}
    nodes = [s for s in flow_spans(data["timeline"]) if s["name"] not in fault_names]
    faults = [
        {
            "name": f"fault {f['name']} ({f['uses']})",
            "from": f["from"],
            "to": f["to"],
            "tags": ["crit"],
        }
        for f in data["faults"]
    ]
    flow = _collapse([{**n, "tags": []} for n in nodes] + faults)
    for row in flow:
        lines.append(_task(row["name"], row["tags"], row["from"], row["to"], hours))
    # Instance sections, failed ones first, until the row budget is spent.
    budget = ROW_BUDGET - len(flow)
    failed = {
        r.get("instance")
        for r in data["results"]
        if r.get("instance") and (not r["ok"] or r.get("error"))
    }
    lanes = sorted(data["lanes"], key=lambda ln: _lane_rank(ln, failed))
    left_out: list[dict[str, Any]] = []
    for ln in lanes:
        if left_out or len(ln["segments"]) > budget:
            left_out.append(ln)
            continue
        budget -= len(ln["segments"])
        lines.append(f"  section {_label(ln['name'])}")
        for seg in ln["segments"]:
            tag = TAG.get(seg["state"])
            lines.append(
                _task(seg["state"], [tag] if tag else [], seg["from"], seg["to"], hours)
            )
    note = ""
    if left_out:
        changes = sum(len(ln["segments"]) for ln in left_out)
        which = (
            "The instance states"
            if len(left_out) == len(lanes)
            else (f"{len(left_out)} of {len(lanes)} instances")
        )
        note = (
            f"{which} ({changes} state changes) would not fit the chart; "
            "`report.html` in the run directory has them."
        )
    return "\n".join(lines) + "\n", note


def _lane_rank(lane: dict[str, Any], failed: set[str]) -> int:
    states = {seg["state"] for seg in lane["segments"]}
    if lane["name"] in failed or states & ENDED_BADLY:
        return 0
    return 1 if "Unknown" in states else 2


def _bucket(points: list[list[float | None]]) -> list[list[float | None]]:
    if len(points) <= MAX_POINTS:
        return points
    size = math.ceil(len(points) / MAX_POINTS)
    out = []
    for i in range(0, len(points), size):
        chunk = points[i : i + size]
        values = [v for _, v in chunk if v is not None]
        out.append([chunk[0][0], sum(values) / len(values) if values else None])
    return out


def _num(v: float) -> str:
    return f"{float(f'{v:.4g}'):g}"


def xychart(chart: dict[str, Any]) -> tuple[str, str]:
    """A Mermaid xychart of one report chart and a note under it ('' when
    nothing to say). Scrapes without an answer are drawn at 0, since the
    chart has no gaps; leading ones (a counter has no rate at its first
    scrape) are dropped instead."""
    points = list(chart["points"])
    while points and points[0][1] is None:
        points = points[1:]
    if not points:
        return "", "no scrapes with a value"
    scrapes = len(points)
    points = _bucket(points)
    values = [0.0 if v is None else v for _, v in points]
    low, high = min(0.0, min(values)), max(values)
    high = high + (high - low) * 0.1 if high > low else low + 1
    x0, x1 = int(points[0][0]), int(points[-1][0])
    text = "\n".join(
        [
            CHART_INIT,
            "xychart-beta",
            f'  title "{chart["title"].replace(chr(34), " ")}"',
            f'  x-axis "seconds since the run started" {x0} --> {max(x1, x0 + 1)}',
            f'  y-axis "{chart["subtitle"].replace(chr(34), " ")}" '
            f"{_num(low)} --> {_num(high)}",
            f"  line [{', '.join(_num(v) for v in values)}]",
        ]
    )
    notes = []
    if any(v is None for _, v in points):
        notes.append("scrapes the endpoint did not answer are drawn at 0")
    if len(points) < scrapes:
        notes.append(f"{scrapes} scrapes averaged into {len(points)} points")
    return text + "\n", "; ".join(notes)


def _metric_checks(run_dir: Path, families: list[str]) -> dict[str, list[str]]:
    """The names of the experiment's `metric` checks by declared family."""
    path = run_dir / "experiment.yaml"
    raw = yaml.safe_load(path.read_text()) if path.is_file() else None
    checks = (raw or {}).get("checks") or []
    out: dict[str, list[str]] = {f: [] for f in families}
    for check in checks:
        if not isinstance(check, dict) or check.get("kind") != "metric":
            continue
        metric = str(check.get("metric", ""))
        for family in families:
            if metric == family or (
                metric.startswith(family + "_")
                and metric[len(family) :] in ("_count", "_sum", "_bucket")
            ):
                out[family].append(str(check.get("name")))
    return out


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def _status(result: dict[str, Any]) -> str:
    if result.get("error"):
        return "ERROR"
    if result["ok"]:
        return "ok"
    return "FAILED"


def metrics_table(run_dir: Path, data: dict[str, Any]) -> str:
    families = [m["family"] for m in data["metrics"]]
    checks = _metric_checks(run_dir, families)
    results = {r["check"]: r for r in data["results"] if r["scope"] == "experiment"}
    lines = ["| Metric | Why it is here | Checks |", "|---|---|---|"]
    for m in data["metrics"]:
        cells = []
        for name in checks[m["family"]]:
            r = results.get(name)
            if r is None:
                cells.append(f"`{name}`: not run")
                continue
            # The output starts with the value and the bound; the series
            # count after the semicolon is for the report.
            summary = r["output"].split(";")[0]
            cells.append(f"`{name}` {_status(r)}: {_cell(summary)}")
        lines.append(
            f"| `{m['family']}` | {_cell(m['description'])} | "
            f"{'<br>'.join(cells) or 'none'} |"
        )
    return "\n".join(lines) + "\n"


def _failures(results: list[dict[str, Any]]) -> list[str]:
    out = []
    for r in results:
        if r["ok"] and not r.get("error"):
            continue
        where = f" on {r['instance']}" if r.get("instance") else ""
        verb = "could not decide" if r.get("error") else "failed"
        known = f", known {r['issue']}" if r.get("issue") else ""
        output = str(r.get("output", "")).strip().splitlines()
        detail = f": {_cell(output[0])}" if output else ""
        out.append(f"- `{r['check']}`{where} {verb}{known}{detail}")
    return out


def details_table(data: dict[str, Any]) -> str:
    """The report's run details as a two-column table. Digests and image
    refs are left out; the report has them."""
    lines = ["| | |", "|---|---|"]
    for row in data["details"]:
        if row["more"]:
            continue
        value = f"`{row['value']}`" if row["code"] else row["value"]
        note = f", {row['note']}" if row["note"] else ""
        lines.append(f"| {_cell(row['key'])} | {_cell(value + note)} |")
    return "\n".join(lines) + "\n"


def share_markdown(run_dir: Path) -> str:
    data = build(run_dir)
    exp = data["experiment"]
    heading = " · ".join(
        str(x) for x in (exp["id"], data["run_id"], data["verdict"]) if x
    )
    results = [r for r in data["results"] if r["scope"] != "preflight"]
    passed = sum(1 for r in results if r["ok"] and not r.get("error"))
    failed = sum(1 for r in results if not r["ok"] and not r.get("error"))
    errors = sum(1 for r in results if r.get("error"))
    known = sum(1 for r in results if not r["ok"] and r.get("issue"))
    counts = [f"{passed} passed"]
    if failed:
        counts.append(f"{failed} failed" + (f" ({known} known)" if known else ""))
    if errors:
        counts.append(f"{errors} could not decide")
    parts = [f"## {heading}\n"]
    if exp["title"]:
        parts.append(f"**{_cell(exp['title'])}**\n")
    if exp["hypothesis"]:
        parts.append(f"Hypothesis: {_cell(exp['hypothesis'])}\n")
    parts.append(details_table(data))
    parts.append(f"**Checks:** {', '.join(counts)}.\n")
    failures = _failures(results)
    if failures:
        parts.append("\n".join(failures) + "\n")
    for error in data["errors"]:
        parts.append(f"Harness error: {_cell(error)}\n")
    chart, note = gantt(data)
    parts.append("### Flow\n")
    parts.append(f"```mermaid\n{chart}```\n")
    if note:
        parts.append(note + "\n")
    if data["metrics"]:
        parts.append("### Metrics\n")
        parts.append(metrics_table(run_dir, data))
        descriptions = {m["family"]: m["description"] for m in data["metrics"]}
        blocks = []
        for c in data["charts"]:
            if not c["declared"]:
                continue
            text, chart_note = xychart(c)
            block = [f"#### {_cell(c['title'])}\n"]
            if text:
                block.append(f"```mermaid\n{text}```\n")
            caption = [descriptions.get(c["family"], "")]
            if chart_note:
                caption.append(chart_note[0].upper() + chart_note[1:] + ".")
            if any(caption):
                block.append(" ".join(_cell(c) for c in caption if c) + "\n")
            blocks.append("\n".join(block))
        parts.extend(blocks[:CHARTS_SHOWN])
        if len(blocks) > CHARTS_SHOWN:
            rest = len(blocks) - CHARTS_SHOWN
            parts.append(
                f"<details>\n<summary>{rest} more chart{'s' if rest > 1 else ''}"
                "</summary>\n\n" + "\n".join(blocks[CHARTS_SHOWN:]) + "\n</details>\n"
            )
    return "\n".join(parts)


def write_share(run_dir: Path, out: Path | None = None) -> Path:
    path = out or run_dir / "share.md"
    path.write_text(share_markdown(run_dir))
    return path


def render_png(markdown: Path) -> Path:
    """`markdown` with every Mermaid block replaced by a PNG, written next
    to the images under `<markdown stem>/`, through mermaid-cli (`mmdc`)."""
    out_dir = markdown.with_suffix("")
    out = out_dir / markdown.name
    command = [
        "mmdc",
        "-i",
        str(markdown),
        "-o",
        str(out),
        "-e",
        "png",
        "-s",
        "2",
        "-b",
        "white",
    ]
    if shutil.which("mmdc") is None:
        raise ShareError(
            "mermaid-cli is not on PATH (npm install -g @mermaid-js/mermaid-cli); "
            "with it, run: " + " ".join(command)
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise ShareError(f"mmdc failed ({done.returncode}): {done.stderr.strip()}")
    return out
