# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Adapter signals and the flow's `wait: {signal: ...}` node."""

from __future__ import annotations

import asyncio
import time

import pytest
from conftest import minimal_adapter
from pydantic import ValidationError

from chaos.adapter import Adapter
from chaos.model import SignalWait, parse_flow
from chaos.signals import Signals


def test_signal_wait_parses():
    node = parse_flow({"wait": {"signal": "sweep", "delay": "55s", "timeout": "3m"}})
    assert node == SignalWait("sweep", delay="55s", timeout="3m", path="flow.wait")
    assert (node.delay_seconds, node.timeout_seconds) == (55, 180)
    bare = parse_flow({"wait": {"signal": "sweep"}})
    assert (bare.delay_seconds, bare.timeout) == (0, "5m")


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ({"signal": "sweep", "every": "1s"}, r"unknown keys \['every'\]"),
        ({"delay": "1s"}, r"want \{workload: <name>, condition: started\} or"),
        ({"signal": 1}, r"flow\.wait\.signal: want a signal name"),
        (
            {"workload": "w", "condition": "ready"},
            r"flow\.wait\.condition: want started",
        ),
        (
            {"workload": "w", "condition": "started", "for": "1s"},
            r"unknown keys \['for'\]",
        ),
        ({"signal": "sweep", "delay": "soon"}, r"flow\.wait\.delay: invalid duration"),
        ({"signal": "sweep", "timeout": "3"}, r"flow\.wait\.timeout: invalid duration"),
        ("w.running", r"want \{workload: <name>, condition: started\} or"),
    ],
)
def test_signal_wait_rejects(value, error):
    with pytest.raises(ValueError, match=error):
        parse_flow({"wait": value})


def adapter(**extra) -> Adapter:
    return Adapter.model_validate(
        minimal_adapter(watch=[{"name": "otel", "run": "true"}], **extra)
    )


def test_signal_names_a_watch_and_a_regex():
    (signal,) = adapter(signals=[{"name": "s", "watch": "otel", "match": "a+"}]).signals
    assert signal.watch == "otel"
    with pytest.raises(ValidationError, match="no watch 'logs'"):
        adapter(signals=[{"name": "s", "watch": "logs", "match": "a"}])
    with pytest.raises(ValidationError, match="invalid regex"):
        adapter(signals=[{"name": "s", "watch": "otel", "match": "("}])
    with pytest.raises(ValidationError, match="signals names must be unique"):
        adapter(signals=[{"name": "s", "watch": "otel", "match": "a"}] * 2)


def next_event(signals: Signals, after, not_before, timeout=0.5) -> float:
    async def go() -> float:
        return await signals.next(
            "s", after, not_before, deadline=time.monotonic() + timeout
        )

    return asyncio.run(go())


def test_next_skips_old_events():
    now = time.monotonic()
    signals = Signals()
    for at in (now - 30, now - 10, now - 5):
        signals.add("s", at)
    # A delay of 20s still reaches the event 10s ago, not the one 30s ago.
    assert next_event(signals, None, now - 20) == now - 10
    # The node already returned for it: the next one.
    assert next_event(signals, now - 10, now - 20) == now - 5
    with pytest.raises(TimeoutError):
        next_event(signals, now - 5, now - 20, timeout=0.1)


def test_next_waits_for_a_new_event():
    signals = Signals()
    signals.add("s", time.monotonic() - 1)

    async def go() -> tuple[float, float]:
        began = time.monotonic()
        later = asyncio.get_running_loop().call_later(0.1, signals.add, "s")
        try:
            return began, await signals.next("s", None, began, began + 2)
        finally:
            later.cancel()

    began, at = asyncio.run(go())
    assert at >= began + 0.1
