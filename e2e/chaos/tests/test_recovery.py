# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""State that must survive a retry: the wrapper's exit code and the undo stack."""

from __future__ import annotations

import asyncio
import os
import subprocess
from typing import cast

from chaos.inject.base import Undo
from chaos.inject.docker import DockerInjector
from chaos.instances import WRAPPER
from chaos.model import RunError
from chaos.timeline import Timeline


def test_wrapper_clears_previous_attempts_exit_code(tmp_path):
    chaos = tmp_path / ".chaos"
    chaos.mkdir()
    (chaos / "exit_code").write_text("0\n")
    env = {**os.environ, "CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_OUTPUT_DIR": str(tmp_path)}
    # The script sees whether the previous attempt's exit code is still there.
    script = f'test ! -e "{chaos}/exit_code"'
    result = subprocess.run(["sh", "-c", WRAPPER, "chaos", script], env=env)
    assert result.returncode == 0
    assert (chaos / "exit_code").read_text() == "0\n"
    assert len((chaos / "attempts").read_text().splitlines()) == 1


class _Timeline:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event: str, **fields) -> None:
        self.events.append((event, fields))


def _injector() -> tuple[DockerInjector, _Timeline]:
    injector = object.__new__(DockerInjector)
    injector.undo_stack = []
    timeline = _Timeline()
    injector.timeline = cast(Timeline, timeline)
    return injector, timeline


def test_cancelled_undo_stays_on_the_stack():
    injector, _ = _injector()
    started = asyncio.Event()

    async def hang() -> None:
        started.set()
        await asyncio.sleep(60)

    entry = Undo("pause", "unpause", hang)
    injector.undo_stack.append(entry)

    async def go() -> None:
        task = asyncio.create_task(injector.undo_all())
        await started.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())
    assert injector.undo_stack == [entry]


def test_failed_undo_is_recorded_and_dropped():
    injector, timeline = _injector()
    ran = []

    async def ok() -> None:
        ran.append("older")

    async def fail() -> None:
        raise RunError("docker unpause: no such container")

    injector.undo_stack = [Undo("a", "older", ok), Undo("b", "newer", fail)]
    errors = asyncio.run(injector.undo_all())
    assert errors == ["undo b (newer): docker unpause: no such container"]
    assert ran == ["older"]
    assert injector.undo_stack == []
    assert [e for e, _ in timeline.events] == [
        "operation.error",
        "operation.reverted",
    ]
