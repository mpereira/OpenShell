# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""`chaos report RUN_DIR`: one self-contained HTML page for a run.

Reads only files in the run dir (timeline, verdict, manifest, observer
output), so it works on a copied run dir after the system is gone. The page
draws, on one time axis: a lane per instance coloured by its state, fault
windows, harness and docker events, and one chart per metric family.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from chaos.evaluate import read_verdict
from chaos.model import operation_name
from chaos.rundir import read_manifest
from chaos.timeline import read_timeline

TEMPLATE = Path(__file__).with_name("report_template.html")
SERIES_RE = re.compile(r"^([A-Za-z_:][A-Za-z0-9_:]*)")
DOCKER_ACTIONS = {
    "create",
    "start",
    "die",
    "kill",
    "oom",
    "pause",
    "unpause",
    "restart",
    "destroy",
}
# What ends the window of a built-in operation (a fault), by operation: a
# one-shot has no window, a restart ends when the target is back, and a
# duration operation ends when it is reverted.
ZERO_WIDTH = {"kill", "restart-in-place"}
FAULT_END = {
    "restart": "target.restarted",
    "outage": "operation.reverted",
    "freeze": "operation.reverted",
}


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text()) if path.is_file() else None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return read_timeline(path)


def lanes(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One lane per instance: state segments plus point marks."""
    by_name: dict[str, dict[str, Any]] = {}
    end = max((e["t"] for e in events), default=0.0)

    def lane(e: dict[str, Any]) -> dict[str, Any]:
        name = e["instance"]
        if name not in by_name:
            by_name[name] = {
                "name": name,
                "workload": e.get("workload"),
                "system_id": None,
                "segments": [],
                "marks": [],
            }
        return by_name[name]

    def enter(ln: dict[str, Any], t: float, state: str) -> None:
        if ln["segments"] and ln["segments"][-1]["to"] is None:
            ln["segments"][-1]["to"] = t
        ln["segments"].append({"from": t, "to": None, "state": state})

    for e in events:
        if "instance" not in e or not isinstance(e["instance"], str):
            continue
        ln = lane(e)
        t = e["t"]
        match e["event"]:
            case "instance.created":
                ln["system_id"] = e.get("system_id")
                enter(ln, t, "Created")
            case "instance.state":
                enter(ln, t, e["state"])
                if e.get("reason"):
                    ln["marks"].append(
                        {"t": t, "kind": "harness", "label": e["reason"]}
                    )
            case "instance.timed_out":
                ln["marks"].append({"t": t, "kind": "harness", "label": "timed out"})
            case "instance.collected":
                code = e.get("exit_code")
                label = "no exit code" if code is None else f"exit code {code}"
                ln["marks"].append({"t": t, "kind": "harness", "label": label})
            case "instance.deleted":
                if ln["segments"] and ln["segments"][-1]["to"] is None:
                    ln["segments"][-1]["to"] = t
                ln["marks"].append({"t": t, "kind": "harness", "label": "deleted"})
    for ln in by_name.values():
        for seg in ln["segments"]:
            if seg["to"] is None:
                seg["to"] = end
    return sorted(by_name.values(), key=lambda ln: (ln["workload"] or "", ln["name"]))


def faults(events: list[dict[str, Any]], end: float) -> list[dict[str, Any]]:
    """Fault windows: from the `operation.applied` of a built-in operation to
    the event that ends it (`FAULT_END`); `kill` and `restart-in-place` have
    nothing to end, so their window is the instant they were applied.
    Adapter operations have no window."""
    selected: dict[str, list[str]] = {}
    out: list[dict[str, Any]] = []
    open_: dict[str, dict[str, Any]] = {}
    for e in events:
        name = str(e.get("operation", ""))
        match e["event"]:
            case "operation.started":
                selected[name] = list(e.get("instances", []))
            case "operation.applied":
                window = {
                    "name": name,
                    "uses": e.get("uses"),
                    "from": e["t"],
                    "to": None,
                    "ended_by": None,
                    "instances": selected.get(name, []),
                }
                out.append(window)
                if operation_name(window["uses"]) in ZERO_WIDTH:
                    window["to"] = e["t"]
                    window["ended_by"] = "operation.applied"
                else:
                    open_[name] = window
            case ev if name in open_ and ev == FAULT_END.get(
                operation_name(open_[name]["uses"])
            ):
                window = open_.pop(name)
                window["to"] = e["t"]
                window["ended_by"] = ev
            case "operation.error" if name in open_:
                window = open_.pop(name)
                window["to"] = e["t"]
                window["ended_by"] = "operation.error"
                window["error"] = e.get("error")
    # Anything left open ran to the end of the run.
    for window in open_.values():
        window["to"] = end
    return out


def docker_marks(records: list[dict[str, Any]], by_id: dict[str, str]) -> list:
    """Docker events for this run's containers, as (instance, mark) pairs."""
    out = []
    for record in records:
        try:
            event = json.loads(record["line"])
        except (json.JSONDecodeError, KeyError):
            continue
        action = str(event.get("Action") or event.get("status") or "")
        verb = action.split(":", 1)[0]
        if verb not in DOCKER_ACTIONS:
            continue
        attrs = (event.get("Actor") or {}).get("Attributes") or {}
        instance = by_id.get(attrs.get("openshell.ai/sandbox-id", ""))
        if instance is None:
            continue
        role = attrs.get("openshell.ai/isolation-role", "container")
        label = f"{role} {verb}"
        if verb == "die" and "exitCode" in attrs:
            label += f" (exit {attrs['exitCode']})"
        out.append((instance, {"t": record["t"], "kind": "docker", "label": label}))
    return out


def _family(series: str, types: dict[str, str]) -> tuple[str, str]:
    """(family, suffix) for a series key; suffix is '', '_bucket', '_sum' or '_count'."""
    match = SERIES_RE.match(series)
    name = match.group(1) if match else series
    for suffix in ("_bucket", "_sum", "_count"):
        base = name.removesuffix(suffix)
        if base != name and types.get(base) in ("histogram", "summary"):
            return base, suffix
    return name, ""


def _rate(points: list[tuple[float, float | None]]) -> list[list[float | None]]:
    """Per-second rate of a counter; a drop is a reset, as in Prometheus."""
    out: list[list[float | None]] = []
    prev: tuple[float, float] | None = None
    for t, v in points:
        if v is None:
            # Keep `prev`: the next answer gives the rate across the gap.
            out.append([t, None])
            continue
        rate = None
        if prev is not None and t > prev[0]:
            delta = v - prev[1] if v >= prev[1] else v
            rate = round(delta / (t - prev[0]), 6)
        out.append([t, rate])
        prev = (t, v)
    return out


def metric_charts(
    run_dir: Path, declared: set[str] | frozenset[str] = frozenset()
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Charts and down windows. A chart per metric family with a nonzero
    value, plus one per family in `declared` (the experiment's `metrics:`)
    even when it stays at zero; those charts have `declared: true`."""
    charts: list[dict[str, Any]] = []
    down: list[dict[str, Any]] = []
    directory = run_dir / "system" / "metrics"
    for path in sorted(directory.glob("*.jsonl")) if directory.is_dir() else []:
        source = path.stem
        types = _read_json(directory / f"{source}.types.json") or {}
        records = _read_jsonl(path)
        # Sum every series of a family (per histogram suffix) at each scrape.
        sums: dict[tuple[str, str], dict[int, float]] = {}
        seen: dict[tuple[str, str], set[str]] = {}
        window: dict[str, Any] | None = None
        for i, r in enumerate(records):
            if not r.get("up"):
                if window is None:
                    window = {"source": source, "from": r["t"], "to": r["t"]}
                    down.append(window)
                window["to"] = r["t"]
                continue
            if window is not None:
                window["to"] = r["t"]
                window = None
            for series, value in r.get("samples", {}).items():
                family, suffix = _family(series, types)
                # Buckets and summary quantiles do not add up across series.
                if suffix == "_bucket" or (
                    suffix == "" and types.get(family) == "summary"
                ):
                    continue
                key = (family, suffix)
                sums.setdefault(key, {})
                sums[key][i] = sums[key].get(i, 0.0) + value
                seen.setdefault(key, set()).add(series)
        for (family, suffix), by_index in sorted(sums.items()):
            points = [(r["t"], by_index.get(i)) for i, r in enumerate(records)]
            kind = types.get(family, "untyped")
            if kind == "counter" or suffix == "_count":
                data = _rate(points)
                unit = "per second"
            elif suffix == "_sum":
                continue  # drawn as the mean below
            else:
                data = [[t, v] for t, v in points]
                unit = "value"
            wanted = family in declared
            if not wanted and not any(v for _, v in data):
                continue
            n = len(seen[(family, suffix)])
            title = family + ("" if suffix != "_count" else " (observations)")
            charts.append(
                {
                    "source": source,
                    "family": family,
                    "title": title,
                    "subtitle": f"{unit}, sum of {n} series",
                    "points": data,
                    "declared": wanted,
                }
            )
            if suffix == "_count" and (family, "_sum") in sums:
                mean = _mean(points, sums[(family, "_sum")])
                if wanted or any(v for _, v in mean):
                    charts.append(
                        {
                            "source": source,
                            "family": family,
                            "title": f"{family} (mean per observation)",
                            "subtitle": "Δsum / Δcount between scrapes",
                            "points": mean,
                            "declared": wanted,
                        }
                    )
    return charts, down


def declared_metrics(experiment: dict[str, Any]) -> list[dict[str, str]]:
    """The families under the experiment's `metrics:` with their descriptions,
    in order. PromQL entries are for the dashboard only, so they are left out."""
    metrics = []
    for raw in experiment.get("metrics") or []:
        entry = {"family": raw} if isinstance(raw, str) else raw
        if entry.get("family"):
            metrics.append(
                {
                    "family": str(entry["family"]),
                    "description": " ".join(str(entry.get("description", "")).split()),
                }
            )
    return metrics


def _mean(
    counts: list[tuple[float, float | None]],
    sums: dict[int, float],
) -> list[list[float | None]]:
    out: list[list[float | None]] = []
    prev: tuple[float, float] | None = None
    for i, (t, c) in enumerate(counts):
        s = sums.get(i)
        if c is None or s is None:
            out.append([t, None])
            prev = None
            continue
        mean = None
        if prev is not None and c > prev[0] and s >= prev[1]:
            mean = round((s - prev[1]) / (c - prev[0]), 6)
        out.append([t, mean])
        prev = (c, s)
    return out


def build(run_dir: Path) -> dict[str, Any]:
    events = read_timeline(run_dir / "timeline.jsonl")
    manifest = read_manifest(run_dir)
    verdict = read_verdict(run_dir)
    experiment = {}
    if (run_dir / "experiment.yaml").is_file():
        experiment = yaml.safe_load((run_dir / "experiment.yaml").read_text()) or {}
    end = max((e["t"] for e in events), default=0.0)
    instance_lanes = lanes(events)
    by_id = {ln["system_id"]: ln["name"] for ln in instance_lanes if ln["system_id"]}
    by_name = {ln["name"]: ln for ln in instance_lanes}
    watch_dir = run_dir / "system" / "watch"
    for path in sorted(watch_dir.glob("*.jsonl")) if watch_dir.is_dir() else []:
        for instance, mark in docker_marks(_read_jsonl(path), by_id):
            by_name[instance]["marks"].append(mark)
    for ln in instance_lanes:
        ln["marks"].sort(key=lambda m: m["t"])
    metrics = declared_metrics(experiment)
    charts, down = metric_charts(run_dir, {m["family"] for m in metrics})
    # Declared families first, in the order the experiment lists them.
    order = {m["family"]: i for i, m in enumerate(metrics)}
    charts.sort(key=lambda c: order.get(c["family"], len(order)))
    run_events = [
        {"t": e["t"], "label": _describe(e)}
        for e in events
        if e["event"]
        in {
            "run.started",
            "abort",
            "run.finished",
            "target.restarted",
            "observer.failed",
            "preflight.failed",
            "operation.completed",
            "operation.error",
            "repeat.iteration",
            "signal.waited",
            "signal.timeout",
        }
        # Built-in operations show as fault windows instead.
        or (e["event"] == "operation.started" and "options" in e)
        or (e["event"] == "node.started" and ("delay" in e or "wait" in e))
    ]
    return {
        "run_id": manifest.get("run_id"),
        "experiment": {
            "id": experiment.get("id"),
            "title": experiment.get("title"),
            "hypothesis": " ".join(str(experiment.get("hypothesis", "")).split()),
            "expectations": experiment.get("expectations", []),
        },
        "seed": manifest.get("seed"),
        "adapter": (manifest.get("adapter") or {}).get("id"),
        # What the adapter's describe hook said about the system under test.
        "system": (manifest.get("environment") or {}).get("system") or {},
        "started_at": manifest.get("started_at"),
        "details": run_details(run_dir, manifest, experiment, end),
        "verdict": verdict.get("verdict"),
        "errors": verdict.get("errors", []),
        "results": verdict.get("results", []),
        "end": end,
        "lanes": instance_lanes,
        "faults": faults(events, end),
        "events": run_events,
        "metrics": metrics,
        "charts": charts,
        "down": down,
        "timeline": events,
    }


# Labels for the keys an adapter's describe hook commonly returns; any
# other key is shown with its underscores as spaces.
SYSTEM_LABELS = {
    "openshell": "OpenShell",
    "docker": "Docker",
    "kubernetes": "Kubernetes",
    "gateway_sha256": "Gateway sha256",
    "gateway_image": "Gateway image",
}


def _duration(t: float) -> str:
    t = max(0, round(t))
    h, rest = divmod(t, 3600)
    m, s = divmod(rest, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s" if m else f"{s}s"


def _started(value: Any) -> str:
    """`2026-10-29T12:08:42.316123Z` as `2026-10-29 12:08:42 UTC`."""
    text = str(value)
    match = re.match(r"^(\d{4}-\d\d-\d\d)T(\d\d:\d\d:\d\d)(?:\.\d+)?Z$", text)
    return f"{match.group(1)} {match.group(2)} UTC" if match else text


def _seed_use(experiment: dict[str, Any]) -> str:
    """What the seed decided in this experiment: the instances that each
    operation with `select` acts on."""
    names = [
        str(op.get("name"))
        for op in experiment.get("operations") or []
        if isinstance(op, dict) and op.get("select") is not None
    ]
    if not names:
        return (
            "no operation selects instances; scripts get it as "
            "CHAOS_EXPERIMENT_RUN_SEED"
        )
    verb = "acts" if len(names) == 1 else "act"
    return (
        f"picks the instances that {', '.join(names)} {verb} on; "
        "the same seed and instance counts pick the same ones"
    )


def run_details(
    run_dir: Path,
    manifest: dict[str, Any],
    experiment: dict[str, Any],
    end: float,
) -> list[dict[str, Any]]:
    """The rows of the run details table, in order. Each row has `key`,
    `value`, `note` ('' when none), `code` (draw the value as code) and
    `more` (a digest or image ref: folded in the report, left out of the
    share page)."""
    rows: list[dict[str, Any]] = []

    def add(
        key: str, value: Any, note: str = "", code: bool = True, more: bool = False
    ) -> None:
        if value is None or value == "":
            return
        rows.append(
            {"key": key, "value": str(value), "note": note, "code": code, "more": more}
        )

    meta = manifest.get("experiment") or {}
    exp_id = meta.get("id") or experiment.get("id")
    version = meta.get("version") or experiment.get("version")
    add("Experiment", exp_id, f"version {version}" if version is not None else "")
    add("Run", manifest.get("run_id"))
    # Runs live at `<runs dir>/<experiment>/<run id>`. A copied run dir can
    # sit anywhere, so the parent is shown only when it is the experiment.
    where = run_dir.resolve()
    add(
        "Run directory",
        f"{where.parent.name}/{where.name}"
        if exp_id and where.parent.name == exp_id
        else where.name,
    )
    if manifest.get("started_at"):
        add("Started", _started(manifest["started_at"]), code=False)
    add("Length", _duration(end), code=False)
    if manifest.get("seed") is not None:
        add("Seed", manifest["seed"], _seed_use(experiment))
    adapter = manifest.get("adapter") or {}
    backend = adapter.get("backend")
    add("Adapter", adapter.get("id"), f"{backend} backend" if backend else "")
    # What the adapter's describe hook said about the system under test.
    system = dict((manifest.get("environment") or {}).get("system") or {})
    git_sha = system.pop("git_sha", None)
    git_dirty = system.pop("git_dirty", None)
    for key, value in system.items():
        label = SYSTEM_LABELS.get(key) or key.replace("_", " ").capitalize()
        if isinstance(value, dict):
            for sub, text in value.items():
                add(f"{label}: {sub.replace('_', ' ')}", text, more=True)
            continue
        text = str(value)
        # `openshell --version` prints `openshell 0.1.7`; the label says it.
        if text.lower().startswith(f"{key.lower()} "):
            text = text[len(key) + 1 :]
        add(label, text, more="sha" in key or "image" in key)
    if git_sha == "unknown":
        add("Git", "unknown", "the gateway binary is not in a git checkout")
    elif git_sha:
        add("Git", git_sha, "dirty" if git_dirty else "clean")
    return rows


def _describe(e: dict[str, Any]) -> str:
    match e["event"]:
        case "target.restarted":
            return f"{e.get('target')} restarted (pid {e.get('pid')})"
        case "node.started" if "delay" in e:
            return f"delay {e['delay']}"
        case "node.started":
            return f"wait {e['wait']} {e.get('condition', 'started')}"
        case "observer.failed":
            return f"observer failed: {e.get('error')}"
        case "preflight.failed":
            return f"preflight failed: {', '.join(e.get('checks') or [])}"
        case "run.finished":
            return f"run finished: {e.get('verdict')}"
        case "repeat.iteration":
            return f"repeat iteration {e.get('iteration')}"
        case "signal.waited":
            return f"{e.get('signal')} at {e.get('signal_t')}s + {e.get('delay')}"
        case "signal.timeout":
            return f"no {e.get('signal')} signal within {e.get('timeout')}"
        case "operation.started":
            on = ", ".join(e.get("instances") or []) or "no instances"
            options = "".join(f" {k}={v}" for k, v in (e.get("options") or {}).items())
            return f"{_run(e)}: {e.get('uses')}{options} on {on}"
        case "operation.completed":
            on = f" on {e['instance']}" if e.get("instance") else ""
            reason = f" ({e['reason']})" if e.get("reason") else ""
            return f"{_run(e)}{on}: {e.get('outcome')}{reason}"
        case "operation.error":
            return f"{_run(e)} failed: {e.get('error')}"
        case other:
            return other


def _run(e: dict[str, Any]) -> str:
    """The operation's name, with the run number after the first run."""
    run = e.get("run") or 1
    name = str(e.get("operation"))
    return f"{name} run {run}" if run > 1 else name


def write_report(run_dir: Path, out: Path | None = None) -> Path:
    data = build(run_dir)
    title = f"{data['experiment']['id'] or 'chaos'} {data['run_id'] or ''}".strip()
    template = TEMPLATE.read_text()
    if template.count("__DATA__") != 1 or template.count("__TITLE__") != 1:
        raise RuntimeError(f"{TEMPLATE} must hold __DATA__ and __TITLE__ once each")
    html = template.replace("__TITLE__", _escape(title)).replace(
        "__DATA__", json_for_script(data)
    )
    path = out or run_dir / "report.html"
    path.write_text(html)
    return path


def json_for_script(data: Any) -> str:
    """JSON that cannot end or alter the <script> it is inlined in: `<`, `>`
    and `&` become `\\uXXXX` escapes, which JSON parsers decode and HTML
    parsers never see. `json.dumps` already escapes everything outside ASCII,
    including the JS line terminators U+2028 and U+2029."""
    text = json.dumps(data)
    return re.sub("[<>&]", lambda m: f"\\u{ord(m.group()):04x}", text)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
