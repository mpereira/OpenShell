# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Observers that run beside the flow: metrics scrapes and watch commands.

Each record carries `t` on the timeline's clock, so a report can draw metrics,
watch lines and timeline events on one axis.

- `system/metrics/<name>.jsonl`: one record per scrape. `up` is 1 with
  `samples` ({series: value}) when the endpoint answered with 200, else 0 with
  `error`. `system/metrics/<name>.types.json` maps each metric family seen to
  its `# TYPE`.
- `system/watch/<name>.jsonl`: one record per stdout line, as `line`. The
  command's stderr goes to `logs/watch-<name>.log`. A line that matches a
  adapter signal on that watch adds a signal event and emits
  `signal.received` on the timeline.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import time
import urllib.request
from pathlib import Path
from typing import IO, Any

from chaos.adapter import Adapter, MetricsSource, Watch, expand
from chaos.model import parse_duration
from chaos.signals import Signals
from chaos.timeline import Timeline, utc_now

log = logging.getLogger("chaos")

SCRAPE_TIMEOUT = 5.0
STOP_GRACE = 5.0


def parse_prometheus(text: str) -> tuple[dict[str, float], dict[str, str]]:
    """Parse the Prometheus text format into ({series: value}, {family: type}).

    A series key is the sample's name and label set exactly as exposed, for
    example `openshell_server_grpc_requests_total{method="Health"}`.
    Timestamps after the value are ignored.
    """
    samples: dict[str, float] = {}
    types: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            parts = line.split(None, 3)
            if len(parts) == 4 and parts[1] == "TYPE":
                types[parts[2]] = parts[3]
            continue
        series, rest = _split_series(line)
        fields = rest.split()
        if not fields:
            continue
        try:
            samples[series] = float(fields[0])
        except ValueError:
            continue
    return samples, types


def _split_series(line: str) -> tuple[str, str]:
    brace = line.find("{")
    space = line.find(" ")
    if brace == -1 or (space != -1 and space < brace):
        return line[:space], line[space + 1 :] if space != -1 else ""
    quoted = escaped = False
    for i in range(brace, len(line)):
        c = line[i]
        if escaped:
            escaped = False
        elif c == "\\":
            escaped = True
        elif c == '"':
            quoted = not quoted
        elif c == "}" and not quoted:
            return line[: i + 1], line[i + 1 :]
    return line, ""


def fetch(url: str) -> str:
    """One scrape of a metrics source; `file://` works too."""
    with urllib.request.urlopen(url, timeout=SCRAPE_TIMEOUT) as resp:  # noqa: S310 (adapter-configured URL)
        return resp.read().decode()


class Observers:
    """Start with `start()`; `stop()` is idempotent and never raises."""

    def __init__(
        self,
        adapter: Adapter,
        timeline: Timeline,
        run_dir: Path,
        env: dict[str, str],
        signals: Signals | None = None,
    ) -> None:
        self.adapter = adapter
        self.timeline = timeline
        self.run_dir = run_dir
        self.env = env
        self.signals = signals or Signals()
        self.tasks: list[asyncio.Task[None]] = []
        self.readers: list[asyncio.Task[None]] = []
        self.procs: list[asyncio.subprocess.Process] = []
        self.watch_names: dict[int, str] = {}
        self.errors: list[str] = []

    def _t(self) -> float:
        return round(time.monotonic() - self.timeline.start, 3)

    async def start(self) -> None:
        if self.adapter.metrics:
            (self.run_dir / "system" / "metrics").mkdir(exist_ok=True)
        if self.adapter.watch:
            (self.run_dir / "system" / "watch").mkdir(exist_ok=True)
        lookup = {**os.environ, **self.env}
        for source in self.adapter.metrics:
            try:
                url = expand(source.url, lookup)
            except Exception as err:  # noqa: BLE001 (an observer never fails the run)
                self.errors.append(f"metrics {source.name}: {err}")
                continue
            self.tasks.append(asyncio.create_task(self._scrape(source, url)))
        for watch in self.adapter.watch:
            try:
                await self._spawn(watch)
            except OSError as err:
                self.errors.append(f"watch {watch.name}: {err}")

    async def _scrape(self, source: MetricsSource, url: str) -> None:
        interval = parse_duration(source.interval)
        directory = self.run_dir / "system" / "metrics"
        types: dict[str, str] = {}
        with (directory / f"{source.name}.jsonl").open("a") as out:
            try:
                while True:
                    began = time.monotonic()
                    record: dict[str, Any] = {"t": self._t(), "ts": utc_now()}
                    try:
                        text = await asyncio.to_thread(fetch, url)
                    except Exception as err:  # noqa: BLE001 (down is a data point)
                        record.update(up=0, error=str(err))
                    else:
                        samples, seen = parse_prometheus(text)
                        types.update(seen)
                        record.update(up=1, samples=samples)
                    out.write(json.dumps(record) + "\n")
                    out.flush()
                    await asyncio.sleep(max(0.0, interval - (time.monotonic() - began)))
            finally:
                (directory / f"{source.name}.types.json").write_text(
                    json.dumps(types, indent=2, sort_keys=True) + "\n"
                )

    async def _spawn(self, watch: Watch) -> None:
        stderr = (self.run_dir / "logs" / f"watch-{watch.name}.log").open("w")
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash",
                "-c",
                watch.run,
                env={**os.environ, **self.env},
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr,
                start_new_session=True,
            )
        finally:
            stderr.close()
        self.procs.append(proc)
        self.watch_names[proc.pid] = watch.name
        out = (self.run_dir / "system" / "watch" / f"{watch.name}.jsonl").open("a")
        matchers = [
            (s.name, re.compile(s.match))
            for s in self.adapter.signals
            if s.watch == watch.name
        ]
        self.readers.append(asyncio.create_task(self._record(proc, out, matchers)))

    async def _record(
        self,
        proc: asyncio.subprocess.Process,
        out: IO[str],
        matchers: list[tuple[str, re.Pattern[str]]],
    ) -> None:
        assert proc.stdout is not None
        with out:
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").rstrip("\n")
                record = {"t": self._t(), "ts": utc_now(), "line": line}
                out.write(json.dumps(record) + "\n")
                out.flush()
                for name, pattern in matchers:
                    if pattern.search(line):
                        self.signals.add(name)
                        self.timeline.emit("signal.received", signal=name)

    async def stop(self) -> None:
        for proc in self.procs:
            if proc.returncode not in (None, 0):
                name = self.watch_names.get(proc.pid, proc.pid)
                self.errors.append(f"watch {name} exited {proc.returncode}")
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGTERM)
        for proc in self.procs:
            try:
                await asyncio.wait_for(proc.wait(), STOP_GRACE)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()
        self.procs = []
        # Readers end at EOF, after the last line; scrapers loop until cancelled.
        for task in self.tasks:
            task.cancel()
        if self.readers:
            _, pending = await asyncio.wait(self.readers, timeout=STOP_GRACE)
            for task in pending:
                task.cancel()
        results = await asyncio.gather(
            *self.tasks, *self.readers, return_exceptions=True
        )
        self.tasks = []
        self.readers = []
        for result in results:
            if isinstance(result, Exception):
                log.warning("observer failed: %r", result)
                self.errors.append(f"observer: {result!r}")
