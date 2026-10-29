# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Export a run as OpenMetrics text for `promtool tsdb create-blocks-from
openmetrics`, so Grafana can show it (see `grafana/` and the README).

Every series carries `run_id`. Timestamps are wall-clock: the run's start time
plus each record's `t`.

- Each scraped series, as scraped, with `chaos_run_id` and `chaos_source`
  labels added (the gateway's own labels keep their names).
- `chaos_chart{family,title,detail,source}`: the report's metric charts (rates and
  means already computed). A point with no value (the endpoint was down, or
  a rate has no previous scrape) is NaN, and every chart ends with NaN at the
  run's end, so Prometheus does not carry a value across a gap.
- `chaos_instance_state{workload,instance}`: the instance state as a code
  (STATE_CODES), repeated every STEP seconds so the value holds between
  changes.
- `chaos_fault_active{operation,uses}`: 1 while a built-in operation (a fault)
  is applied, then 0.
- `chaos_event{instance,label}`: one sample per harness or docker event. The
  value is the event time in epoch milliseconds, for a Grafana annotation that
  uses the series value as the time.
- `chaos_run_info{experiment,verdict,adapter}`: 1 for the run's duration.
- `chaos_check_result{scope,workload,instance,check}`: 1 if the check passed,
  else 0, for the run's duration.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from chaos.report import build

STEP = 5.0
STATE_CODES = {
    "Created": 0,
    "Pending": 1,
    "Running": 2,
    "Unknown": 3,
    "Succeeded": 4,
    "Failed": 5,
    "Error": 6,
}
OTHER_STATE = 7


def _escape(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _value(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    return repr(value)


def _labels(labels: dict[str, object]) -> str:
    return ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items() if v is not None)


def _with_labels(series: str, extra: str) -> tuple[str, str]:
    """Split a scraped series key into (name, labels) and append `extra`."""
    name, brace, rest = series.partition("{")
    inner = rest[:-1] if brace else ""
    return name, f"{inner},{extra}" if inner else extra


class _Families:
    def __init__(self) -> None:
        # name -> labels -> {timestamp: value}
        self.data: dict[str, dict[str, dict[float, float]]] = {}

    def add(self, name: str, labels: str, ts: float, value: float) -> None:
        series = self.data.setdefault(name, {}).setdefault(labels, {})
        series[round(ts, 3)] = value

    def hold(
        self, name: str, labels: str, start: float, end: float, value: float
    ) -> None:
        """`value` from `start` to `end`, repeated so it never goes stale."""
        ts = start
        while ts < end:
            self.add(name, labels, ts, value)
            ts += STEP
        self.add(name, labels, end, value)

    def render(self) -> str:
        out: list[str] = []
        for name in sorted(self.data):
            if name.startswith("chaos_"):
                out.append(f"# TYPE {name} gauge")
            for labels in sorted(self.data[name]):
                points = self.data[name][labels]
                for ts in sorted(points):
                    out.append(f"{name}{{{labels}}} {_value(points[ts])} {ts:.3f}")
        out.append("# EOF")
        return "\n".join(out) + "\n"


class Exported(NamedTuple):
    run_id: str
    experiment: str
    start: float  # epoch seconds
    end: float


def export(run_dir: Path, out: Path) -> Exported:
    """Write the OpenMetrics file."""
    data = build(run_dir)
    timeline = data["timeline"]
    if not timeline:
        raise ValueError(f"{run_dir}: empty timeline")
    first = timeline[0]
    start = (
        datetime.fromisoformat(first["ts"].replace("Z", "+00:00")).timestamp()
        - first["t"]
    )
    run_id = data["run_id"] or run_dir.name
    run = _labels({"run_id": run_id})
    families = _Families()

    families.hold(
        "chaos_run_info",
        _labels(
            {
                "run_id": run_id,
                "experiment": data["experiment"]["id"],
                "verdict": data["verdict"],
                "adapter": data["adapter"],
            }
        ),
        start,
        start + data["end"],
        1,
    )
    for lane in data["lanes"]:
        labels = (
            f"{run},{_labels({'workload': lane['workload'], 'instance': lane['name']})}"
        )
        for seg in lane["segments"]:
            code = STATE_CODES.get(seg["state"], OTHER_STATE)
            families.hold(
                "chaos_instance_state",
                labels,
                start + seg["from"],
                start + seg["to"],
                code,
            )
        for mark in lane["marks"]:
            ts = start + mark["t"]
            families.add(
                "chaos_event",
                f"{run},{_labels({'instance': lane['name'], 'label': mark['label']})}",
                ts,
                round(ts * 1000),
            )
    for ev in data["events"]:
        ts = start + ev["t"]
        families.add(
            "chaos_event",
            f"{run},{_labels({'instance': 'run', 'label': ev['label']})}",
            ts,
            round(ts * 1000),
        )
    for fault in data["faults"]:
        labels = f"{run},{_labels({'operation': fault['name'], 'uses': fault['uses']})}"
        until = max(fault["to"], fault["from"] + 1)
        families.hold(
            "chaos_fault_active", labels, start + fault["from"], start + until, 1
        )
        families.add("chaos_fault_active", labels, start + until + 0.001, 0)
    for chart in data["charts"]:
        labels = f"{run},{_labels({'source': chart['source'], 'family': chart['family'], 'title': chart['title'], 'detail': chart['subtitle']})}"
        for t, value in chart["points"]:
            families.add(
                "chaos_chart", labels, start + t, math.nan if value is None else value
            )
        if chart["points"] and chart["points"][-1][0] < data["end"]:
            families.add("chaos_chart", labels, start + data["end"], math.nan)

    for result in data["results"]:
        labels = {k: result.get(k) for k in ("scope", "workload", "instance", "check")}
        families.hold(
            "chaos_check_result",
            f"{run},{_labels(labels)}",
            start,
            start + data["end"],
            1 if result.get("ok") else 0,
        )

    directory = run_dir / "system" / "metrics"
    extra = _labels({"chaos_run_id": run_id})
    for path in sorted(directory.glob("*.jsonl")) if directory.is_dir() else []:
        source = _labels({"chaos_source": path.stem})
        for line in path.read_text().splitlines():
            record = json.loads(line)
            for series, value in (record.get("samples") or {}).items():
                name, labels = _with_labels(series, f"{extra},{source}")
                families.add(name, labels, start + record["t"], value)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(families.render())
    return Exported(run_id, data["experiment"]["id"], start, start + data["end"])
