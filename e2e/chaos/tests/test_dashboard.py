# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Per-experiment Grafana dashboards."""

from __future__ import annotations

import json

import pytest
from conftest import EXPERIMENTS

from chaos.cli import main
from chaos.dashboard import UID_MAX, build_dashboard, dashboard_uid
from chaos.model import load_experiment, parse_experiment, read_yaml


def experiment(**extra):
    raw = read_yaml(EXPERIMENTS / "supervisor-kill.yaml")
    raw.update(extra)
    return parse_experiment(raw, "test")


def by_title(dashboard):
    return {p["title"]: p for p in dashboard["panels"]}


def test_panels_follow_the_experiment():
    exp = experiment(metrics=[], checks=[])
    dash = build_dashboard(exp)
    assert dash["uid"] == "exp-supervisor-kill"
    panels = by_title(dash)
    assert exp.hypothesis.split()[0] in panels[exp.title]["options"]["content"]
    for w in exp.workloads:
        (target,) = panels[f"{w.name} instances"]["targets"]
        assert f'workload="{w.name}"' in target["expr"]
    assert "Faults" in panels
    assert "chaos_check_result" in panels["Check results"]["targets"][0]["expr"]
    run, chart = dash["templating"]["list"]
    assert f'experiment="{exp.id}"' in run["query"]["query"]
    # No `metrics:`: every chart of the run, repeated over `$chart`.
    assert panels["$chart"]["repeat"] == chart["name"] == "chart"


def test_listed_metrics_replace_the_chart_repeat():
    dash = build_dashboard(
        experiment(
            checks=[],
            metrics=[
                "openshell_gateway_sessions",
                {"title": "Up", "expr": 'up{chaos_run_id="$run"}'},
            ],
        )
    )
    panels = by_title(dash)
    assert "$chart" not in panels
    variable = dash["templating"]["list"][1]
    assert variable["name"] == "metric0" and variable["hide"] == 2
    assert 'family="openshell_gateway_sessions"' in variable["query"]["query"]
    assert panels["$metric0"]["repeat"] == "metric0"
    assert panels["Up"]["targets"][0]["expr"] == 'up{chaos_run_id="$run"}'


def test_layout_does_not_overlap():
    cells = set()
    for p in build_dashboard(experiment())["panels"]:
        g = p["gridPos"]
        assert g["x"] + g["w"] <= 24
        for x in range(g["x"], g["x"] + g["w"]):
            for y in range(g["y"], g["y"] + g["h"]):
                assert (x, y) not in cells, p["title"]
                cells.add((x, y))


def test_long_ids_get_a_short_unique_uid():
    a, b = dashboard_uid("a" * 50), dashboard_uid("a" * 49 + "b")
    assert len(a) <= UID_MAX and len(b) <= UID_MAX and a != b


@pytest.mark.parametrize(
    "path", sorted(EXPERIMENTS.glob("*.yaml")), ids=lambda p: p.stem
)
def test_every_shipped_experiment_has_a_dashboard(path, tmp_path):
    exp, _ = load_experiment(path)
    assert main(["dashboard", str(path), "--dir", str(tmp_path)]) == 0
    dash = json.loads((tmp_path / f"{exp.id}.json").read_text())
    assert dash["uid"] == dashboard_uid(exp.id)
