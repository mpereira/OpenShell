# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""One run: the run directory, the flow under a timeout, and the abort path.

Abort path, in order: undo applied faults; restart process targets that are
down; collect and delete every instance still present; run the adapter
`collect`; run every check; write the verdict, forced to ERROR. A second
SIGINT/SIGTERM skips to deleting instances and exits 2.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.metadata
import json
import logging
import os
import platform as host
import secrets
import signal
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from chaos import envvars
from chaos.adapter import Adapter, run_hook
from chaos.envvars import RunInfo
from chaos.evaluate import (
    EXIT_CODES,
    Result,
    append_index,
    format_verdict,
    run_checks,
    run_preflight,
    write_verdict,
)
from chaos.flow import Engine
from chaos.inject.base import Injector
from chaos.inject.docker import DockerInjector
from chaos.inject.kubernetes import KubernetesInjector
from chaos.instances import InstanceManager, write_data
from chaos.model import Experiment, RunError, parse_duration
from chaos.observe import Observers
from chaos.operations import Operations
from chaos.report import write_report
from chaos.rundir import SCHEMA_VERSION
from chaos.signals import Signals
from chaos.timeline import Timeline, utc_now

log = logging.getLogger("chaos")

PROFILE_COLLECT_TIMEOUT = 300.0
FORCED_RESTORE_TIMEOUT = 120.0
INJECTORS: dict[str, type[Injector]] = {
    "docker": DockerInjector,
    "kubernetes": KubernetesInjector,
}
# Reverting operations, collecting and deleting after an error, all together.
CLEANUP_TIMEOUT = 600.0


def new_run_id() -> str:
    return f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(2)}"


def environment() -> dict[str, Any]:
    """The harness and its host. `system` is what the adapter's `describe`
    hook prints about the system under test, filled in after preflight."""
    return {
        "harness": importlib.metadata.version("openshell-chaos"),
        "os": host.system(),
        "arch": host.machine(),
        "python": host.python_version(),
        "system": None,
    }


def descriptions(exp: Experiment) -> dict[str, Any]:
    return {
        "experiment": exp.description,
        "workloads": {
            w.name: {
                "description": w.description,
                "checks": {c.name: c.description for c in w.checks},
            }
            for w in exp.workloads
        },
        "operations": {o.name: o.description for o in exp.operations},
        "checks": {c.name: c.description for c in exp.checks},
    }


def _setup_logging(run_dir: Path) -> logging.Handler:
    handler = logging.FileHandler(run_dir / "logs" / "harness.log")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    return handler


class Run:
    def __init__(
        self,
        exp: Experiment,
        raw: dict[str, Any],
        experiment_path: Path,
        adapter: Adapter,
        seed: int,
        overrides: list[str],
        runs_dir: Path,
        environ: dict[str, str],
    ) -> None:
        self.exp = exp
        self.adapter = adapter
        self.runs_dir = runs_dir
        run_id = new_run_id()
        run_dir = runs_dir / exp.id / run_id
        (run_dir / "logs").mkdir(parents=True)
        (run_dir / "system").mkdir()
        (run_dir / "workloads").mkdir()
        self.run_dir = run_dir
        self.info = RunInfo(
            experiment_id=exp.id,
            experiment_version=exp.version,
            adapter=adapter.id,
            run_id=run_id,
            seed=seed,
            run_dir=run_dir,
        )
        (run_dir / "experiment.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))
        # The adapter as it ran, so `evaluate` needs nothing outside the run dir.
        (run_dir / "adapter.yaml").write_bytes(adapter.path.read_bytes())
        for name, value in exp.data.items():
            write_data(run_dir, name, value)
        # The sha256 is of the archived copy, the experiment after --set, so
        # an evaluation can check that the copy it reads is what ran.
        archived = (run_dir / "experiment.yaml").read_bytes()
        self.manifest: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "experiment": {
                "id": exp.id,
                "version": exp.version,
                "file": str(experiment_path),
                "sha256": hashlib.sha256(archived).hexdigest(),
            },
            "adapter": {
                "id": adapter.id,
                "file": str(adapter.path),
                "sha256": adapter.sha256,
                "backend": adapter.backend,
            },
            "seed": seed,
            "overrides": overrides,
            "environment": environment(),
            "started_at": utc_now(),
            "finished_at": None,
            "descriptions": descriptions(exp),
        }
        self._write_manifest()
        self.timeline = Timeline(run_dir / "timeline.jsonl")
        self.instances = InstanceManager(self.info, adapter, self.timeline)
        self.injector = INJECTORS[adapter.backend](
            adapter, self.info, self.timeline, self.instances
        )
        self.operations = Operations(
            adapter, self.info, self.timeline, self.instances, self.injector
        )
        self.signal_events = Signals()
        self.observers = Observers(
            adapter,
            self.timeline,
            run_dir,
            envvars.run_hook_env(self.info),
            self.signal_events,
        )
        self.errors: list[str] = []
        self.preflight: list[Result] = []
        self.signals = 0
        self.flow_task: asyncio.Task[None] | None = None
        self.main_task: asyncio.Task[int] | None = None

    def _write_manifest(self) -> None:
        (self.run_dir / "manifest.json").write_text(
            json.dumps(self.manifest, indent=2) + "\n"
        )

    def _on_signal(self, sig: signal.Signals) -> None:
        self.signals += 1
        if self.signals == 1:
            log.warning(
                "%s: aborting; send again to skip collection and exit", sig.name
            )
            self.errors.append(f"aborted by {sig.name}")
            if self.flow_task is not None and not self.flow_task.done():
                self.flow_task.cancel()
        elif self.signals == 2 and self.main_task is not None:
            log.warning("%s: deleting instances; send again to exit at once", sig.name)
            self.main_task.cancel()
        else:
            log.warning("%s: exiting now; faults and instances are left", sig.name)
            for proc in self.observers.procs:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
            os._exit(2)

    def execute(self) -> int:
        """Run to completion and return the exit code."""
        handler = _setup_logging(self.run_dir)
        try:
            return asyncio.run(self._outer())
        finally:
            log.removeHandler(handler)
            self.timeline.close()

    async def _outer(self) -> int:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._on_signal, sig)
        self.main_task = asyncio.create_task(self._main())
        try:
            return await self.main_task
        except asyncio.CancelledError:
            return await self._forced_exit(
                "forced exit: instances deleted without collection; checks not run"
            )
        except Exception as err:  # noqa: BLE001 (a harness bug is still an ERROR verdict)
            log.exception("harness error")
            self.errors.append(f"harness error: {err!r}")
            return await self._forced_exit(
                "instances deleted without collection after a harness error"
            )

    async def _main(self) -> int:
        log.info("run %s -> %s", self.info.run_id, self.run_dir)
        self.timeline.emit(
            "run.started",
            experiment=self.exp.id,
            version=self.exp.version,
            seed=self.info.seed,
            adapter=self.info.adapter,
        )
        self.preflight = await run_preflight(self.exp, self.adapter, self.info)
        failed = [r.check for r in self.preflight if not r.ok]
        if failed:
            self.timeline.emit("preflight.failed", checks=failed)
            self.errors.append(f"preflight failed: {', '.join(failed)}")
            return await self._evaluate(checks=False)
        await self._describe()
        await self._setup()
        # A signal before the flow exists only records an error; honor it here.
        if not self.errors and not self.signals:
            await self.observers.start()
            await self._flow()
        if not self.errors:
            # Adapter operations still applied are put back, newest first,
            # before the instances they may act on are gone.
            self.errors.extend(await self.injector.undo_all(abort=False))
        if self.errors:
            await self._abort()
        await self._collect_run()
        await self._stop_observers()
        return await self._evaluate()

    async def _stop_observers(self) -> None:
        """Observers run until adapter collection ends. Their failures are
        recorded but never change the verdict."""
        await self.observers.stop()
        for error in self.observers.errors:
            self.timeline.emit("observer.failed", error=error)
        self.observers.errors = []

    async def _describe(self) -> None:
        """Record what the adapter's `describe` hook says about the system
        under test, verbatim, as manifest.json `environment.system`."""
        result = await run_hook(
            self.adapter,
            "describe",
            self.adapter.describe,
            envvars.run_hook_env(self.info),
        )
        if not result.ok:
            self.errors.append(
                f"describe failed (exit {result.code}): {result.stderr.strip()[-2000:]}"
            )
            return
        try:
            system = json.loads(result.stdout)
        except ValueError as err:
            self.errors.append(f"describe printed no JSON object: {err}")
            return
        if not isinstance(system, dict):
            self.errors.append(
                f"describe printed a JSON {type(system).__name__}, not an object"
            )
            return
        self.manifest["environment"]["system"] = system
        self._write_manifest()

    async def _setup(self) -> None:
        if self.adapter.setup is None:
            return
        result = await run_hook(
            self.adapter, "setup", self.adapter.setup, envvars.run_hook_env(self.info)
        )
        if not result.ok:
            self.errors.append(
                f"setup failed (exit {result.code}): {result.stderr.strip()[-2000:]}"
            )

    async def _flow(self) -> None:
        engine = Engine(
            self.exp,
            self.timeline,
            self.instances,
            self.operations,
            self.signal_events,
        )
        self.flow_task = asyncio.create_task(
            asyncio.wait_for(engine.run(), parse_duration(self.exp.timeout))
        )
        try:
            await self.flow_task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            # Cancelled by the first signal; the error is already recorded.
        except TimeoutError:
            self.errors.append(f"experiment timeout {self.exp.timeout} expired")
        except RunError as err:
            self.errors.append(str(err))
        except Exception as err:  # noqa: BLE001 (any harness bug is an ERROR verdict)
            log.exception("harness error")
            self.errors.append(f"harness error: {err!r}")

    async def _abort(self) -> None:
        """Revert operations, then collect and delete every instance, within
        CLEANUP_TIMEOUT. What is left at the deadline is deleted uncollected."""
        self.timeline.emit("abort", errors=self.errors)
        loop = asyncio.get_running_loop()
        self.instances.deadline = loop.time() + CLEANUP_TIMEOUT
        try:
            async with asyncio.timeout_at(self.instances.deadline):
                await self._cleanup()
        except TimeoutError:
            self.errors.append(
                f"cleanup did not finish in {CLEANUP_TIMEOUT:g}s; "
                "instances left were deleted without collection"
            )
            self.instances.deadline = loop.time() + FORCED_RESTORE_TIMEOUT
            await self._delete_all()
        finally:
            self.instances.deadline = None

    async def _delete_all(self) -> None:
        """One delete attempt per instance left. A failure is an error with
        the instance's id, and the verdict says the cleanup is incomplete."""
        results = await asyncio.gather(
            *(self.instances.delete_only(i) for i in self.instances.all()),
            return_exceptions=True,
        )
        failed = [str(r) for r in results if isinstance(r, BaseException)]
        if failed:
            self.errors.extend(failed)
            self.errors.append(
                f"cleanup incomplete: {len(failed)} instance(s) not deleted"
            )

    async def _cleanup(self) -> None:
        self.errors.extend(await self.injector.undo_all())
        try:
            await self.injector.ensure_up()
        except Exception as err:  # noqa: BLE001 (keep going to delete instances)
            self.errors.append(f"restart targets: {err}")
        results = await asyncio.gather(
            *(self.instances.finish(i) for i in self.instances.all() if i.present),
            return_exceptions=True,
        )
        self.errors.extend(str(r) for r in results if isinstance(r, BaseException))

    async def _collect_run(self) -> None:
        if self.adapter.collect is None:
            return
        result = await run_hook(
            self.adapter,
            "collect",
            self.adapter.collect,
            envvars.run_hook_env(self.info),
            timeout=PROFILE_COLLECT_TIMEOUT,
        )
        (self.run_dir / "logs" / "collect.log").write_text(
            result.stdout + result.stderr
        )
        if not result.ok:
            self.errors.append(f"adapter collect failed (exit {result.code})")

    async def _evaluate(self, checks: bool = True) -> int:
        results = list(self.preflight)
        if checks:
            results += await run_checks(
                self.exp, self.adapter, self.info, self.timeline
            )
        verdict = write_verdict(self.run_dir, results, self.errors)
        # The gateway processes seen across restarts, when a fault recorded any.
        extra = {"processes": self.injector.pids} if self.injector.pids else {}
        self._finish(verdict, **extra)
        print(format_verdict(verdict))
        print(self.run_dir)
        return EXIT_CODES[verdict["verdict"]]

    def _finish(self, verdict: dict[str, Any], **extra: Any) -> None:
        self.timeline.emit("run.finished", verdict=verdict["verdict"], **extra)
        self.manifest["finished_at"] = utc_now()
        self._write_manifest()
        append_index(
            self.runs_dir / "index.jsonl", self.manifest, verdict, self.run_dir
        )
        try:
            write_report(self.run_dir)
        except Exception as err:  # noqa: BLE001 (`chaos report` can retry it)
            log.warning("report.html not written: %r", err)

    async def _forced_exit(self, reason: str) -> int:
        """Second signal or harness error: best-effort revert operations and delete
        what is left, write an ERROR verdict, exit 2."""
        self.timeline.emit("abort", forced=True)
        await self._stop_observers()
        try:
            async with asyncio.timeout(FORCED_RESTORE_TIMEOUT):
                self.errors.extend(await self.injector.undo_all())
                await self.injector.ensure_up()
        except Exception as err:  # noqa: BLE001 (deleting instances matters more)
            self.errors.append(f"restore operations: {err!r}")
        await self._delete_all()
        self.errors.append(reason)
        self._finish(write_verdict(self.run_dir, list(self.preflight), self.errors))
        return 2
