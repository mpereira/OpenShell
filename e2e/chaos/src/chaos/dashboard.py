# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""A Grafana dashboard for one experiment, derived from its file.

It shows the runs of that experiment loaded by `grafana/view.sh` (see
`export.py` for the series). From the top: the hypothesis and what each check
tests, the run and its check results, one instance timeline per workload, the
faults, and the metrics listed in `metrics:` (every chart of the run when
there are none).

A hand-written `grafana/dashboards/<id>.json` takes its place; give it the uid
from `dashboard_uid` so the links `chaos export --grafana` prints still work.
"""

from __future__ import annotations

import hashlib
from typing import Any

from chaos.model import Experiment

DATASOURCE = {"type": "prometheus", "uid": "chaos-prometheus"}
# Grafana dashboard uids are at most 40 characters.
UID_MAX = 40
STATE_COLORS = [
    ("Created", "#c7c6c0"),
    ("Pending", "#9e9d97"),
    ("Running", "#2a78d6"),
    ("Unknown", "#fab219"),
    ("Succeeded", "#0ca30c"),
    ("Failed", "#d03b3b"),
    ("Error", "#d03b3b"),
    ("Other", "#6f6e69"),
]


def dashboard_uid(experiment_id: str) -> str:
    uid = f"exp-{experiment_id}"
    if len(uid) <= UID_MAX:
        return uid
    digest = hashlib.sha256(experiment_id.encode()).hexdigest()[:8]
    return f"{uid[: UID_MAX - 9]}-{digest}"


def _para(text: str) -> str:
    return " ".join(text.split())


def _target(expr: str, legend: str | None = None, **extra: Any) -> dict[str, Any]:
    target: dict[str, Any] = {
        "refId": "A",
        "datasource": DATASOURCE,
        "expr": expr,
        "range": True,
        "interval": "1s",
    }
    if legend is not None:
        target["legendFormat"] = legend
    target.update(extra)
    return target


def _state_timeline(
    title: str, description: str, expr: str, legend: str, mappings: dict[str, Any]
) -> dict[str, Any]:
    return {
        "type": "state-timeline",
        "title": title,
        "description": description,
        "datasource": DATASOURCE,
        "targets": [_target(expr, legend)],
        "fieldConfig": {
            "defaults": {
                "mappings": [{"type": "value", "options": mappings}],
                "color": {"mode": "fixed", "fixedColor": "transparent"},
                "custom": {"fillOpacity": 85, "lineWidth": 0},
            },
            "overrides": [],
        },
        "options": {
            "mergeValues": True,
            "showValue": "never",
            "rowHeight": 0.8,
            "alignValue": "left",
            "legend": {
                "showLegend": True,
                "displayMode": "list",
                "placement": "bottom",
            },
            "tooltip": {"mode": "single", "sort": "none"},
        },
    }


def _timeseries(
    title: str, description: str, expr: str, legend: str, **extra: Any
) -> dict[str, Any]:
    return {
        "type": "timeseries",
        "title": title,
        "description": description,
        "datasource": DATASOURCE,
        "targets": [_target(expr, legend)],
        "fieldConfig": {
            "defaults": {
                "custom": {
                    "lineWidth": 2,
                    "fillOpacity": 0,
                    "showPoints": "auto",
                    "spanNulls": False,
                    "drawStyle": "line",
                },
                "noValue": "No data for this run",
            },
            "overrides": [],
        },
        "options": {
            "legend": {
                "showLegend": True,
                "displayMode": "list",
                "placement": "bottom",
            },
            "tooltip": {"mode": "multi", "sort": "none"},
        },
        **extra,
    }


def _summary(exp: Experiment) -> str:
    lines = [f"**Hypothesis.** {_para(exp.hypothesis)}", ""]
    checks = [
        (f"{w.name} (each instance)", c) for w in exp.workloads for c in w.checks
    ] + [("experiment", c) for c in exp.checks]
    if checks:
        lines.append("**Checks.**")
        lines += [
            f"- `{c.name}`, {scope}: {_para(c.description)}" for scope, c in checks
        ]
    return "\n".join(lines)


def _run_table() -> dict[str, Any]:
    return {
        "type": "table",
        "title": "Run",
        "datasource": DATASOURCE,
        "targets": [
            _target(
                "max by (run_id, verdict, adapter) "
                '(max_over_time(chaos_run_info{run_id="$run"}[$__range]))',
                format="table",
                instant=True,
                range=False,
            )
        ],
        "transformations": [
            {
                "id": "organize",
                "options": {
                    "excludeByName": {"Time": True, "Value": True},
                    "indexByName": {"verdict": 0, "run_id": 1, "adapter": 2},
                },
            }
        ],
        "fieldConfig": {"defaults": {}, "overrides": []},
        "options": {"showHeader": True},
    }


def _checks_table() -> dict[str, Any]:
    return {
        "type": "table",
        "title": "Check results",
        "description": "One row per check run: workload checks once per instance.",
        "datasource": DATASOURCE,
        "targets": [
            _target(
                'last_over_time(chaos_check_result{run_id="$run"}[$__range])',
                format="table",
                instant=True,
                range=False,
            )
        ],
        "transformations": [
            {
                "id": "organize",
                "options": {
                    "excludeByName": {
                        "Time": True,
                        "run_id": True,
                        "__name__": True,
                    },
                    "indexByName": {
                        "Value": 0,
                        "check": 1,
                        "scope": 2,
                        "workload": 3,
                        "instance": 4,
                    },
                    "renameByName": {"Value": "result"},
                },
            },
            {"id": "sortBy", "options": {"sort": [{"field": "result"}]}},
        ],
        "fieldConfig": {
            "defaults": {"custom": {"align": "left"}},
            "overrides": [
                {
                    "matcher": {"id": "byName", "options": "result"},
                    "properties": [
                        {
                            "id": "mappings",
                            "value": [
                                {
                                    "type": "value",
                                    "options": {
                                        "0": {"text": "failed", "color": "#d03b3b"},
                                        "1": {"text": "passed", "color": "#0ca30c"},
                                    },
                                }
                            ],
                        },
                        {
                            "id": "custom.cellOptions",
                            "value": {"type": "color-text"},
                        },
                        {"id": "custom.width", "value": 90},
                    ],
                }
            ],
        },
        "options": {"showHeader": True},
    }


def _query_variable(name: str, query: str, **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "type": "query",
        "datasource": DATASOURCE,
        "query": {"query": query, "refId": name},
        "definition": query,
        "refresh": 2,
        **extra,
    }


def _all() -> dict[str, Any]:
    return {
        "multi": True,
        "includeAll": True,
        "current": {"text": ["All"], "value": ["$__all"]},
    }


def build_dashboard(exp: Experiment) -> dict[str, Any]:
    variables = [
        _query_variable(
            "run",
            f'label_values(chaos_run_info{{experiment="{exp.id}"}}, run_id)',
            label="Run",
            sort=2,
            multi=False,
            includeAll=False,
        )
    ]
    # (panel, width, height), laid out left to right in rows of 24 columns.
    layout: list[tuple[dict[str, Any], int, int]] = []
    summary = _summary(exp)
    summary_height = min(10, 2 + summary.count("\n") + len(summary) // 300)
    layout.append(
        (
            {
                "type": "text",
                "title": exp.title,
                "options": {"mode": "markdown", "content": summary},
            },
            16,
            summary_height,
        )
    )
    layout.append((_run_table(), 8, summary_height))
    # One row per result; the adapter adds system checks the file does not list.
    rows = sum(len(w.checks) * w.instances for w in exp.workloads) + len(exp.checks)
    layout.append((_checks_table(), 24, min(14, 5 + rows)))

    states = {
        str(i): {"text": text, "color": color, "index": i}
        for i, (text, color) in enumerate(STATE_COLORS)
    }
    for w in exp.workloads:
        layout.append(
            (
                _state_timeline(
                    f"{w.name} instances",
                    _para(w.description),
                    f'chaos_instance_state{{run_id="$run", workload="{w.name}"}}',
                    "{{instance}}",
                    states,
                ),
                24,
                min(12, 3 + w.instances),
            )
        )
    faults = [op for op in exp.operations if op.builtin]
    if faults:
        layout.append(
            (
                _state_timeline(
                    "Faults",
                    " ".join(f"{f.name}: {_para(f.description)}" for f in faults),
                    'chaos_fault_active{run_id="$run"}',
                    "{{operation}} ({{uses}})",
                    {
                        "0": {
                            "text": "not applied",
                            "color": "transparent",
                            "index": 0,
                        },
                        "1": {"text": "applied", "color": "#ec835a", "index": 1},
                    },
                ),
                24,
                min(12, 3 + len(faults)),
            )
        )

    layout.append(
        ({"type": "row", "title": "Metrics", "collapsed": False, "panels": []}, 24, 1)
    )
    rate_note = (
        "Counters as per-second rates, histogram sums as the mean per observation."
    )
    if not exp.metrics:
        variables.append(
            _query_variable(
                "chart",
                'label_values(chaos_chart{run_id="$run"}, title)',
                label="Metric",
                sort=1,
                **_all(),
            )
        )
        layout.append(
            (
                _timeseries(
                    "$chart",
                    rate_note,
                    'chaos_chart{run_id="$run", title="$chart"}',
                    "{{detail}} ({{source}})",
                    repeat="chart",
                    repeatDirection="h",
                    maxPerRow=2,
                ),
                12,
                8,
            )
        )
    for i, metric in enumerate(exp.metrics):
        if metric.expr:
            layout.append(
                (
                    _timeseries(
                        metric.title, _para(metric.description), metric.expr, ""
                    ),
                    12,
                    8,
                )
            )
            continue
        # A histogram has two charts (observations, mean); repeat over them.
        name = f"metric{i}"
        variables.append(
            _query_variable(
                name,
                "label_values("
                f'chaos_chart{{run_id="$run", family="{metric.family}"}}, title)',
                hide=2,
                sort=1,
                **_all(),
            )
        )
        layout.append(
            (
                _timeseries(
                    f"${name}",
                    " ".join(filter(None, [_para(metric.description), rate_note])),
                    f'chaos_chart{{run_id="$run", title="${name}"}}',
                    "{{detail}} ({{source}})",
                    repeat=name,
                    repeatDirection="h",
                    maxPerRow=2,
                ),
                12,
                8,
            )
        )

    panels: list[dict[str, Any]] = []
    x = y = row_height = 0
    for panel_id, (panel, w, h) in enumerate(layout, start=1):
        if x + w > 24:
            x, y, row_height = 0, y + row_height, 0
        panel["id"] = panel_id
        panel["gridPos"] = {"x": x, "y": y, "w": w, "h": h}
        panels.append(panel)
        x += w
        row_height = max(row_height, h)

    return {
        "uid": dashboard_uid(exp.id),
        "title": exp.title,
        "tags": ["chaos", "experiment"],
        "editable": True,
        "schemaVersion": 39,
        "version": 1,
        "time": {"from": "now-1h", "to": "now"},
        "refresh": "",
        "graphTooltip": 1,
        "description": f"Runs of the chaos experiment {exp.id} (v{exp.version}). "
        "Generated by `chaos dashboard`.",
        "annotations": {
            "list": [
                {
                    "name": "Events",
                    "datasource": DATASOURCE,
                    "enable": True,
                    "iconColor": "#52514e",
                    "expr": 'chaos_event{run_id="$run"}',
                    "step": "1s",
                    "useValueForTime": True,
                    "titleFormat": "{{instance}}",
                    "textFormat": "{{label}}",
                    "tagKeys": "instance",
                }
            ]
        },
        "templating": {"list": variables},
        "panels": panels,
    }
