# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Checks and verdicts. Every check reads only the run directory, so a run can
be evaluated again after its environment is gone."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from chaos import envvars
from chaos.adapter import Adapter, expand, run_shell
from chaos.envvars import InstanceInfo, RunInfo
from chaos.instances import instance_dir, read_exit_code
from chaos.model import (
    Check,
    ExitCodeCheck,
    Expectation,
    Experiment,
    MetricCheck,
    ShellCheck,
    check_key,
    parse_duration,
)
from chaos.observe import fetch, parse_prometheus
from chaos.timeline import Timeline, read_timeline, utc_now

# XFAIL: every failed check is a known failure an `expectations` entry claims.
EXIT_CODES = {"PASS": 0, "XFAIL": 0, "FAIL": 1, "ERROR": 2}
# Bash exits with these when it cannot run a command: 126 not executable, 127
# not found. A check that exits with one could not decide, so it is an error.
CHECKER_ERROR_CODES = {126, 127}
# The built-in preflight check an experiment with `metrics:` gets.
METRICS_CHECK = "metrics-exported"
LABEL_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="((?:[^"\\]|\\.)*)"')


@dataclass
class Result:
    scope: str  # preflight | workload | experiment | adapter
    workload: str | None
    instance: str | None
    check: str
    description: str
    ok: bool
    output: str
    # The issue known to make this check fail, from the experiment's `expectations`.
    issue: str | None = None
    # The issue is flaky, so a pass is expected too.
    flaky: bool = False
    # The check could not decide: it timed out, was killed, or could not run a
    # command. Neither a pass nor a failure; the verdict is ERROR.
    error: bool = False

    @property
    def unexpected(self) -> bool:
        """A failure no issue explains, or a pass of a strict known failure."""
        if self.error or (self.flaky and self.ok):
            return False
        return self.ok == (self.issue is not None)


@dataclass
class _Outcome:
    ok: bool
    output: str
    error: bool = False


async def _shell(run: str, env: dict[str, str], timeout: str, cwd: Path) -> _Outcome:
    result = await run_shell(["bash", "-c", run], env, parse_duration(timeout), cwd=cwd)
    output = result.stdout
    if not result.ok and result.stderr.strip():
        output += ("\n" if output.strip() else "") + result.stderr
    error = result.timed_out or result.code < 0 or result.code in CHECKER_ERROR_CODES
    if error and not result.timed_out:
        output += f"\nchecker error: exit code {result.code}"
    # The whole output: verdict.json is the evidence, so nothing is cut.
    return _Outcome(result.ok, output.strip(), error)


def instance_infos(
    exp: Experiment, run: RunInfo, output_dir: str
) -> list[tuple[str, InstanceInfo]]:
    infos = []
    for w in exp.workloads:
        for index in range(w.instances):
            name = f"{w.name}-{index}"
            infos.append(
                (
                    w.name,
                    InstanceInfo(
                        workload=w.name,
                        count=w.instances,
                        index=index,
                        name=name,
                        dir=instance_dir(run.run_dir, w.name, name),
                        sandbox_output_dir=output_dir,
                    ),
                )
            )
    return infos


async def run_preflight(
    exp: Experiment, adapter: Adapter, run: RunInfo
) -> list[Result]:
    """The adapter's preflight checks, then the experiment's, then, when the
    experiment lists metric families, `metrics-exported`."""
    results: list[Result] = []
    for check in [*adapter.preflight, *exp.preflight]:
        env = {**check.env, **envvars.experiment_check_env(run, check.name)}
        outcome = await _shell(check.run, env, check.timeout, run.run_dir)
        results.append(_result("preflight", None, None, check, outcome))
    if exp.metric_families:
        results.append(await _metrics_exported(exp, adapter, run))
    return results


async def _metrics_exported(exp: Experiment, adapter: Adapter, run: RunInfo) -> Result:
    """Scrapes each adapter metrics source once and looks for every family
    under `metrics:`, so a family that is misspelled, renamed or not exported
    by this system ends the run before anything is created."""
    wanted = exp.metric_families
    found: dict[str, str] = {}
    lines: list[str] = []
    failed = False
    if not adapter.metrics:
        lines.append(f"adapter {adapter.id} has no metrics sources to scrape")
        failed = True
    env = {**os.environ, **envvars.run_hook_env(run)}
    for source in adapter.metrics:
        try:
            text = await asyncio.to_thread(fetch, expand(source.url, env))
        except Exception as err:  # noqa: BLE001 (any failure to scrape is the finding)
            lines.append(f"{source.name}: {err}")
            failed = True
            continue
        samples, types = parse_prometheus(text)
        exported = set(types) | {_series_name(s) for s in samples}
        hits = [f for f in wanted if f not in found and _exported(f, exported)]
        found.update(dict.fromkeys(hits, source.name))
        lines.append(f"{source.name}: {len(exported)} families, {len(hits)} listed")
    missing = [f for f in wanted if f not in found]
    if missing:
        lines.append("missing: " + ", ".join(missing))
        failed = True
    return Result(
        scope="preflight",
        workload=None,
        instance=None,
        check=METRICS_CHECK,
        description=(
            "Every metric family under metrics: is in a scrape of an adapter "
            "metrics source, so the run can record and check it."
        ),
        ok=not failed,
        output="\n".join(lines),
    )


def _exported(family: str, names: set[str]) -> bool:
    return family in names or any(
        f"{family}{suffix}" in names for suffix in ("_count", "_sum", "_bucket")
    )


def _series_name(series: str) -> str:
    return series.partition("{")[0]


def _series_labels(series: str) -> dict[str, str]:
    return {
        k: v.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
        for k, v in LABEL_RE.findall(series.partition("{")[2])
    }


def metric_outcome(run_dir: Path, check: MetricCheck) -> _Outcome:
    """A `metric` check over the scrapes in `system/metrics`: in each scrape
    that answered, the series named `metric` carrying `labels` are summed;
    `value` is taken over those sums."""
    directory = run_dir / "system" / "metrics"
    if check.source:
        sources = [check.source]
    else:
        sources = (
            sorted(p.stem for p in directory.glob("*.jsonl"))
            if directory.is_dir()
            else []
        )
    series = check.metric + (
        "{" + ",".join(f'{k}="{v}"' for k, v in check.labels.items()) + "}"
        if check.labels
        else ""
    )
    found: dict[str, tuple[list[float], set[str]]] = {}
    for source in sources:
        path = directory / f"{source}.jsonl"
        if not path.is_file():
            return _Outcome(
                False,
                f"no scrapes of metrics source {source!r} in system/metrics",
                True,
            )
        sums: list[float] = []
        matched: set[str] = set()
        for record in read_timeline(path):
            if not record.get("up"):
                continue
            total = None
            for name, value in record.get("samples", {}).items():
                if _series_name(name) != check.metric:
                    continue
                labels = _series_labels(name)
                if any(labels.get(k) != v for k, v in check.labels.items()):
                    continue
                total = (total or 0.0) + value
                matched.add(name)
            if total is not None:
                sums.append(total)
        if sums:
            found[source] = (sums, matched)
    if not found:
        where = ", ".join(sources) or "none recorded"
        return _Outcome(False, f"no series {series} in any scrape ({where})", False)
    if len(found) > 1:
        return _Outcome(
            False,
            f"{series} is in more than one source ({', '.join(found)}); set source",
            True,
        )
    ((source, (sums, matched)),) = found.items()
    match check.value:
        case "last":
            value = sums[-1]
        case "min":
            value = min(sums)
        case "max":
            value = max(sums)
        case "increase":
            # A drop is a reset: the new value is the increase since it.
            value = sum(
                b - a if b >= a else b for a, b in zip(sums, sums[1:], strict=False)
            )
    output = (
        f"{check.value} = {value:g} (want {check.wants()}); {len(matched)} series "
        f"of {series} summed over {len(sums)} scrapes of {source}"
    )
    return _Outcome(check.holds(value), output, False)


def _result(
    scope: str,
    workload: str | None,
    instance: str | None,
    check: Check,
    outcome: _Outcome,
) -> Result:
    return Result(
        scope,
        workload,
        instance,
        check.name,
        check.description,
        outcome.ok,
        outcome.output,
        error=outcome.error,
    )


async def run_checks(
    exp: Experiment, adapter: Adapter, run: RunInfo, timeline: Timeline | None = None
) -> list[Result]:
    """Every check of the run. With a `timeline` (the run's own checks, not a
    later `chaos evaluate`) each check gets `check.started` and `check.finished`."""
    results: list[Result] = []

    async def record(coro, scope: str, instance: str | None, check: Check) -> None:
        if timeline:
            timeline.emit(
                "check.started", scope=scope, instance=instance, check=check.name
            )
        r = await coro
        if timeline:
            timeline.emit(
                "check.finished",
                scope=scope,
                instance=instance,
                check=check.name,
                ok=r.ok,
            )
        results.append(r)

    for wname, inst in instance_infos(exp, run, adapter.instance.output_dir):
        w = exp.workload(wname)
        exit_code = read_exit_code(inst.dir)
        for check in w.checks:
            await record(
                _workload_check(run, w.env, inst, check, exit_code),
                "workload",
                inst.name,
                check,
            )
    for scope, checks in (("experiment", exp.checks), ("adapter", adapter.checks)):
        for check in checks:
            await record(_experiment_check(run, scope, check), scope, None, check)
    selected = selected_instances(read_timeline(run.run_dir / "timeline.jsonl"))
    _match_known_failures(exp, results, selected)
    return results


def selected_instances(events: list[dict[str, Any]]) -> set[str]:
    """The workload instances any operation selected, over all its runs."""
    selected: set[str] = set()
    for e in events:
        if e.get("event") == "operation.started" and e.get("scope") == "instances":
            selected.update(e.get("instances", []))
    return selected


def _covers(expectation: Expectation, r: Result, selected: set[str]) -> bool:
    if r.instance is None or expectation.instances == "all":
        return True
    return r.instance in selected


def _match_known_failures(
    exp: Experiment, results: list[Result], selected: set[str]
) -> None:
    """Set the issue of each result an `expectations` entry covers.

    A strict issue's check is fixed only if every result it covers passed. With
    a racy bug some instances pass while others fail, and those passes are
    expected. A result with a checker error matches no issue.
    """
    covered = [
        (r, issue)
        for r in results
        if not r.error
        and (issue := exp.known_failure(check_key(r.scope, r.workload, r.check)))
        and _covers(issue, r, selected)
    ]
    failed = {
        (issue.issue, check_key(r.scope, r.workload, r.check))
        for r, issue in covered
        if not r.ok
    }
    for r, issue in covered:
        key = check_key(r.scope, r.workload, r.check)
        if issue.kind == "flaky":
            r.issue, r.flaky = issue.issue, True
        elif not r.ok or (issue.issue, key) not in failed:
            r.issue = issue.issue


async def _experiment_check(
    run: RunInfo, scope: str, check: ShellCheck | MetricCheck
) -> Result:
    if isinstance(check, MetricCheck):
        # The scrape files of a long run are large; keep the loop free.
        outcome = await asyncio.to_thread(metric_outcome, run.run_dir, check)
    else:
        env = {**check.env, **envvars.experiment_check_env(run, check.name)}
        outcome = await _shell(check.run, env, check.timeout, run.run_dir)
    return _result(scope, None, None, check, outcome)


async def _workload_check(
    run: RunInfo,
    wenv: dict[str, str],
    inst: InstanceInfo,
    check: Check,
    exit_code: int | None,
) -> Result:
    if isinstance(check, ExitCodeCheck):
        if exit_code is None:
            outcome = _Outcome(False, "no exit code recorded")
        else:
            ok = exit_code == check.equals
            output = f"exit code {exit_code}" + ("" if ok else f", want {check.equals}")
            outcome = _Outcome(ok, output)
    else:
        env = {
            **wenv,
            **check.env,
            **envvars.workload_check_env(run, inst, check.name, exit_code),
        }
        outcome = await _shell(check.run, env, check.timeout, run.run_dir)
    return _result("workload", inst.workload, inst.name, check, outcome)


def verdict_of(results: list[Result], errors: list[str]) -> str:
    if errors or any(r.error for r in results):
        return "ERROR"
    if any(r.unexpected for r in results):
        return "FAIL"
    return "PASS" if all(r.ok for r in results) else "XFAIL"


def _verdict(results: list[Result], errors: list[str]) -> dict[str, Any]:
    return {
        "verdict": verdict_of(results, errors),
        "errors": errors,
        "results": [asdict(r) for r in results],
    }


def write_verdict(
    run_dir: Path, results: list[Result], errors: list[str]
) -> dict[str, Any]:
    """The verdict of the run itself, written once when the run ends."""
    verdict = _verdict(results, errors)
    (run_dir / "verdict.json").write_text(json.dumps(verdict, indent=2) + "\n")
    return verdict


def provenance(experiment: Path, adapter: Adapter) -> dict[str, Any]:
    """What an evaluation read: the experiment file and the hash of its bytes,
    the adapter's id and file. Compared with the run manifest, it says whether
    the checks are the ones that ran."""
    return {
        "experiment": {
            "file": str(experiment),
            "sha256": hashlib.sha256(experiment.read_bytes()).hexdigest(),
        },
        "adapter": {"id": adapter.id, "file": str(adapter.path)},
    }


def write_evaluation(
    run_dir: Path,
    results: list[Result],
    errors: list[str],
    read: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Path]:
    """A later evaluation, next to the run's verdict instead of over it. `read`
    is the `provenance` of the files it evaluated with."""
    evaluated_at = utc_now()
    verdict = {**_verdict(results, errors), "evaluated_at": evaluated_at}
    if read:
        verdict["read"] = read
    directory = run_dir / "evaluations"
    directory.mkdir(exist_ok=True)
    stamp = evaluated_at.replace("-", "").replace(":", "").split(".")[0]
    path = directory / f"{stamp}.json"
    n = 1
    while path.exists():
        n += 1
        path = directory / f"{stamp}-{n}.json"
    path.write_text(json.dumps(verdict, indent=2) + "\n")
    return verdict, path


def read_verdict(run_dir: Path) -> dict[str, Any]:
    """The latest evaluation of a run, else the verdict the run wrote."""
    evaluations = sorted(
        (run_dir / "evaluations").glob("*.json"), key=lambda p: p.stat().st_mtime
    )
    path = evaluations[-1] if evaluations else run_dir / "verdict.json"
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def append_index(
    index: Path,
    manifest: dict[str, Any],
    verdict: dict[str, Any],
    run_dir: Path,
    evaluation: Path | None = None,
) -> None:
    """Append one line for the run or evaluation. Paths are relative to the
    index's directory, so a copied `runs/` tree stays readable; `flaky_missed`
    names the checks a flaky known failure did not trigger on."""
    base = index.resolve().parent

    def relative(path: Path) -> str:
        try:
            return str(path.resolve().relative_to(base))
        except ValueError:
            return str(path)

    line = {
        "run_id": manifest["run_id"],
        "experiment": manifest["experiment"]["id"],
        "version": manifest["experiment"]["version"],
        "adapter": manifest["adapter"]["id"],
        "system": manifest.get("environment", {}).get("system"),
        "verdict": verdict["verdict"],
        "flaky_missed": sorted(
            {
                _check_name(r)
                for r in verdict.get("results", [])
                if r.get("flaky") and r.get("ok")
            }
        ),
        "path": relative(run_dir),
        "evaluation": relative(evaluation) if evaluation else None,
        "evaluated_at": verdict.get("evaluated_at") or utc_now(),
    }
    index.parent.mkdir(parents=True, exist_ok=True)
    with index.open("a") as f:
        f.write(json.dumps(line) + "\n")


def _check_name(r: dict[str, Any]) -> str:
    return check_key(r["scope"], r.get("workload"), r["check"])


def format_verdict(verdict: dict[str, Any]) -> str:
    lines = []
    if read := verdict.get("read"):
        lines.append(
            f"  evaluated with {read['experiment']['file']}"
            f" (sha256 {read['experiment']['sha256'][:12]})"
            f" and adapter {read['adapter']['id']} ({read['adapter']['file']})"
        )
    for r in verdict["results"]:
        where = r["instance"] or r["scope"]
        issue = r.get("issue")
        if r.get("error"):
            mark, note = "ERROR", " (the check could not decide)"
        elif issue is None:
            mark, note = ("ok" if r["ok"] else "FAIL"), ""
        elif r["ok"] and r.get("flaky"):
            mark, note = "ok", f" (flaky: {issue} did not trigger)"
        elif r["ok"]:
            mark, note = "XPASS", f" (expected to fail: {issue}; fixed?)"
        else:
            mark, note = (
                "xfail",
                f" (known{', flaky' if r.get('flaky') else ''}: {issue})",
            )
        lines.append(f"  {mark:<5} {where}/{r['check']}{note}")
        if (not r["ok"] or r.get("error")) and r["output"]:
            lines.extend(f"          {line}" for line in r["output"].splitlines()[-5:])
    lines.extend(f"  error: {e}" for e in verdict["errors"])
    lines.append(verdict["verdict"])
    return "\n".join(lines)
