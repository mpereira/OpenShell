# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The run timeline: one JSON object per line in `timeline.jsonl`."""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("chaos")


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Timeline:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.start = time.monotonic()
        self._file = path.open("a")

    def emit(self, event: str, **fields: Any) -> None:
        record = {
            "t": round(time.monotonic() - self.start, 3),
            "ts": utc_now(),
            "event": event,
            **fields,
        }
        self._file.write(json.dumps(record, sort_keys=False) + "\n")
        self._file.flush()
        log.info("%s %s", event, json.dumps(fields) if fields else "")

    def close(self) -> None:
        self._file.close()


def read_timeline(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
