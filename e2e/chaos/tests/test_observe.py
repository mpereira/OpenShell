# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Metrics scrapes and watch commands, against files and shell commands."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from conftest import minimal_adapter

from chaos.adapter import Adapter
from chaos.observe import Observers, parse_prometheus
from chaos.timeline import Timeline

EXPOSITION = """\
# HELP openshell_server_grpc_requests_total gRPC requests.
# TYPE openshell_server_grpc_requests_total counter
openshell_server_grpc_requests_total{method="Health",code="0"} 3
openshell_server_grpc_requests_total{method="a b",note="x}y \\"q\\""} 1 1700000000000
# TYPE openshell_server_supervisor_sessions gauge
openshell_server_supervisor_sessions 2
broken_sample not-a-number
"""


def test_parse_prometheus():
    samples, types = parse_prometheus(EXPOSITION)
    assert samples == {
        'openshell_server_grpc_requests_total{method="Health",code="0"}': 3.0,
        'openshell_server_grpc_requests_total{method="a b",note="x}y \\"q\\""}': 1.0,
        "openshell_server_supervisor_sessions": 2.0,
    }
    assert types == {
        "openshell_server_grpc_requests_total": "counter",
        "openshell_server_supervisor_sessions": "gauge",
    }


def records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def observe(tmp_path: Path, seconds: float, **extra) -> Path:
    run_dir = tmp_path / "run"
    (run_dir / "logs").mkdir(parents=True)
    (run_dir / "system").mkdir()
    adapter = Adapter.model_validate(minimal_adapter(**extra))
    timeline = Timeline(run_dir / "timeline.jsonl")

    async def go() -> list[str]:
        observers = Observers(adapter, timeline, run_dir, {"RUN": str(run_dir)})
        await observers.start()
        await asyncio.sleep(seconds)
        await observers.stop()
        return observers.errors

    try:
        assert asyncio.run(go()) == []
    finally:
        timeline.close()
    return run_dir


def test_scrapes_up_and_down(tmp_path):
    exposition = tmp_path / "metrics.txt"
    exposition.write_text(EXPOSITION)
    run_dir = observe(
        tmp_path,
        0.35,
        metrics=[
            {"name": "up", "url": f"file://{exposition}", "interval": "100ms"},
            {"name": "down", "url": f"file://{tmp_path}/missing", "interval": "1s"},
        ],
    )
    up = records(run_dir / "system" / "metrics" / "up.jsonl")
    assert len(up) >= 2
    assert all(r["up"] == 1 for r in up)
    assert up[0]["samples"]["openshell_server_supervisor_sessions"] == 2.0
    assert [r["t"] for r in up] == sorted(r["t"] for r in up)
    types = json.loads((run_dir / "system" / "metrics" / "up.types.json").read_text())
    assert types["openshell_server_supervisor_sessions"] == "gauge"
    (down,) = records(run_dir / "system" / "metrics" / "down.jsonl")
    assert down["up"] == 0 and down["error"]


def test_watch_records_lines_and_stops(tmp_path):
    run_dir = observe(
        tmp_path,
        0.5,
        watch=[
            {"name": "events", "run": 'echo "{\\"a\\": 1}"; echo "$RUN" >&2; sleep 60'},
        ],
    )
    (record,) = records(run_dir / "system" / "watch" / "events.jsonl")
    assert json.loads(record["line"]) == {"a": 1}
    assert (run_dir / "logs" / "watch-events.log").read_text().strip() == str(run_dir)
