# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Signal events: the arrival times of watch lines that match an adapter signal.

Times are `time.monotonic()` at the moment the harness read the line, not the
time the system under test produced it.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict


class Signals:
    def __init__(self) -> None:
        self.events: dict[str, list[float]] = defaultdict(list)
        self._changed = asyncio.Event()

    def add(self, name: str, at: float | None = None) -> None:
        self.events[name].append(time.monotonic() if at is None else at)
        self._changed.set()
        self._changed = asyncio.Event()

    async def next(
        self, name: str, after: float | None, not_before: float, deadline: float
    ) -> float:
        """Return the first event of `name` newer than `after` and not before
        `not_before`, waiting for one until `deadline`. Raises TimeoutError."""
        while True:
            for at in self.events[name]:
                if (after is None or at > after) and at >= not_before:
                    return at
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            changed = self._changed
            try:
                await asyncio.wait_for(changed.wait(), remaining)
            except TimeoutError:
                continue
