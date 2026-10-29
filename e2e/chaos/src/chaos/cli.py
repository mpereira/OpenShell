# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""`chaos validate | preflight | run | evaluate | report | share | export |
dashboard | list | show`."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
import textwrap
import urllib.parse
from dataclasses import asdict
from pathlib import Path

import yaml

from chaos.adapter import (
    DEFAULT_ADAPTERS_DIR,
    Adapter,
    load_adapter,
    load_adapter_file,
    missing_env,
    validate_adapter,
)
from chaos.envvars import RunInfo
from chaos.evaluate import (
    EXIT_CODES,
    Result,
    append_index,
    format_verdict,
    provenance,
    run_checks,
    run_preflight,
    verdict_of,
    write_evaluation,
)
from chaos.model import (
    Experiment,
    ExperimentError,
    NotApplicable,
    Operation,
    RoleTarget,
    load_experiment,
    parse_experiment,
    read_yaml,
)
from chaos.rundir import read_manifest

PACKAGE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_EXPERIMENTS_DIR = PACKAGE_DIR / "experiments"
DEFAULT_RUNS_DIR = PACKAGE_DIR / "runs"
ADAPTER_HELP = "run with adapters/ID.yaml instead of the experiment's default-adapter"


def _adapter_id(args: argparse.Namespace, exp: Experiment) -> str:
    """`--adapter` if given, else the experiment's `default-adapter`."""
    return args.adapter or exp.default_adapter


def _check(exp: Experiment, adapter: Adapter) -> None:
    """validate_adapter, except that an experiment must run on its own
    default-adapter: not applicable there is invalid."""
    try:
        validate_adapter(exp, adapter)
    except NotApplicable as err:
        if adapter.id == exp.default_adapter:
            raise ExperimentError(f"{err} (on its default-adapter)") from None
        raise


def _validate(args: argparse.Namespace) -> int:
    files = args.files
    if not files:
        if not args.all_adapters:
            print("give experiment files, or --all-adapters", file=sys.stderr)
            return 2
        files = sorted(args.experiments_dir.glob("*.yaml"))
    if args.all_adapters:
        return _validate_matrix(args, files)
    failed = False
    for path in files:
        adapter_id = None
        try:
            exp, _ = load_experiment(path)
            adapter_id = _adapter_id(args, exp)
            _check(exp, load_adapter(args.adapters_dir, adapter_id))
        except NotApplicable as err:
            print(f"n/a on {adapter_id}: {path}: {err}")
        except ExperimentError as err:
            on = f" on {adapter_id}" if adapter_id else ""
            print(f"invalid{on}: {err}", file=sys.stderr)
            failed = True
        else:
            print(f"ok on {adapter_id}: {path}")
    return 1 if failed else 0


def _validate_matrix(args: argparse.Namespace, files: list[Path]) -> int:
    """Every experiment against every adapter in the adapters dir: a table of
    ok, n/a and invalid, then the reason for each cell that is not ok."""
    adapters: dict[str, Adapter | ExperimentError] = {}
    for path in sorted(args.adapters_dir.glob("*.yaml")):
        try:
            adapters[path.stem] = load_adapter(args.adapters_dir, path.stem)
        except ExperimentError as err:
            adapters[path.stem] = err
    rows: list[tuple[str, list[str]]] = []
    reasons: list[str] = []
    for path in files:
        try:
            exp, _ = load_experiment(path)
        except ExperimentError as err:
            rows.append((str(path), ["invalid"] * len(adapters)))
            reasons.append(f"{path}: invalid: {err}")
            continue
        cells = []
        for adapter_id, adapter in adapters.items():
            try:
                if isinstance(adapter, ExperimentError):
                    raise adapter
                _check(exp, adapter)
            except NotApplicable as err:
                cells.append("n/a")
                reasons.extend(
                    f"{exp.id} on {adapter_id}: n/a: {r}" for r in err.reasons
                )
            except ExperimentError as err:
                cells.append("invalid")
                reasons.append(f"{exp.id} on {adapter_id}: invalid: {err}")
            else:
                cells.append("ok")
        rows.append((exp.id, cells))
    header = ["experiment", *adapters]
    widths = [
        max(len(row[i]) for row in [header, *([r[0], *r[1]] for r in rows)])
        for i in range(len(header))
    ]
    for row in [header, *([r[0], *r[1]] for r in rows)]:
        print(
            "  ".join(
                cell.ljust(w) for cell, w in zip(row, widths, strict=True)
            ).rstrip()
        )
    if reasons:
        print()
        print("\n".join(reasons))
    return 1 if any("invalid" in cells for _, cells in rows) else 0


def _load_for_run(args: argparse.Namespace):
    """The experiment, its raw YAML and the adapter, or the exit code after
    printing why the experiment does not run here: 0 if it is not applicable
    on the adapter, else 2."""
    adapter_id = None
    try:
        exp, raw = load_experiment(args.file, args.set)
        adapter_id = _adapter_id(args, exp)
        adapter = load_adapter(args.adapters_dir, adapter_id)
        _check(exp, adapter)
    except NotApplicable as err:
        print(f"n/a on {adapter_id}: {err}")
        return 0
    except ExperimentError as err:
        on = f" on {adapter_id}" if adapter_id else ""
        print(f"invalid{on}: {err}", file=sys.stderr)
        return 2
    missing = missing_env(adapter, dict(os.environ))
    if missing:
        print(
            f"{adapter.id} needs these environment variables:\n"
            + "".join(f"  {name}\n" for name in missing)
            + "Run under the adapter's e2e wrapper; see e2e/chaos/README.md.",
            file=sys.stderr,
        )
        return 2
    return exp, raw, adapter


def _preflight(args: argparse.Namespace) -> int:
    from chaos.runner import new_run_id

    loaded = _load_for_run(args)
    if isinstance(loaded, int):
        return loaded
    exp, raw, adapter = loaded
    args.runs_dir.mkdir(parents=True, exist_ok=True)
    # Checks run in a run dir, as in a real run; this one is thrown away.
    with tempfile.TemporaryDirectory(dir=args.runs_dir, prefix=".preflight-") as tmp:
        run_dir = Path(tmp)
        (run_dir / "experiment.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))
        info = RunInfo(
            experiment_id=exp.id,
            experiment_version=exp.version,
            adapter=adapter.id,
            run_id=new_run_id(),
            seed=0,
            run_dir=run_dir,
        )
        results = asyncio.run(run_preflight(exp, adapter, info))
    failed = [r.check for r in results if not r.ok]
    errors = [f"preflight failed: {', '.join(failed)}"] if failed else []
    verdict = verdict_of(results, errors)
    print(
        format_verdict(
            {
                "verdict": verdict,
                "errors": errors,
                "results": [asdict(r) for r in results],
            }
        )
    )
    return 2 if failed else 0


def _run(args: argparse.Namespace) -> int:
    from chaos.runner import Run  # Imports the injectors; only needed here.

    loaded = _load_for_run(args)
    if isinstance(loaded, int):
        return loaded
    exp, raw, adapter = loaded
    run = Run(
        exp,
        raw,
        args.file,
        adapter,
        args.seed,
        args.set,
        args.runs_dir.resolve(),
        dict(os.environ),
    )
    return run.execute()


def _evaluate(args: argparse.Namespace) -> int:
    run_dir: Path = args.run_dir.resolve()
    try:
        manifest = read_manifest(run_dir)
    except ExperimentError as err:
        print(err, file=sys.stderr)
        return 2
    # The verdict the run wrote; an evaluation never replaces it.
    old = (
        json.loads((run_dir / "verdict.json").read_text())
        if (run_dir / "verdict.json").is_file()
        else {}
    )
    try:
        if args.experiment:
            experiment_path = Path(args.experiment)
            exp, _ = load_experiment(experiment_path, manifest.get("overrides", []))
        else:
            experiment_path = run_dir / "experiment.yaml"
            exp = parse_experiment(read_yaml(experiment_path), str(experiment_path))
        adapter = _run_adapter(args, run_dir, manifest)
    except ExperimentError as err:
        print(f"invalid: {err}", file=sys.stderr)
        return 2
    info = RunInfo(
        experiment_id=exp.id,
        experiment_version=exp.version,
        adapter=adapter.id,
        run_id=manifest["run_id"],
        seed=manifest["seed"],
        run_dir=run_dir,
    )
    # Preflight checks ran against the environment, which is gone; keep them.
    results = [Result(**r) for r in old.get("results", []) if r["scope"] == "preflight"]
    if all(r.ok for r in results):
        results += asyncio.run(run_checks(exp, adapter, info))
    errors = list(old.get("errors", []))
    # Checks against a run that never finished could pass on partial output.
    timeline = run_dir / "timeline.jsonl"
    finished = timeline.is_file() and any(
        json.loads(line).get("event") == "run.finished"
        for line in timeline.read_text().splitlines()
        if line.strip()
    )
    if not finished and "run did not finish" not in errors:
        errors.append("run did not finish")
    verdict, path = write_evaluation(
        run_dir, results, errors, provenance(experiment_path, adapter)
    )
    if args.index:
        append_index(args.index, manifest, verdict, run_dir, path)
    try:
        from chaos.report import write_report

        write_report(run_dir)
    except Exception as err:  # noqa: BLE001 (`chaos report` can retry it)
        print(f"report.html not refreshed: {err!r}", file=sys.stderr)
    print(format_verdict(verdict))
    print(path)
    return EXIT_CODES[verdict["verdict"]]


def _run_adapter(args: argparse.Namespace, run_dir: Path, manifest: dict) -> Adapter:
    """The adapter the run used: the copy in the run dir, or with `--adapter`
    the current one of that id."""
    if args.adapter:
        return load_adapter(args.adapters_dir, args.adapter)
    copy = run_dir / "adapter.yaml"
    if copy.is_file():
        return load_adapter_file(copy)
    # A run from before the run dir kept a copy.
    return load_adapter(args.adapters_dir, manifest["adapter"]["id"])


def _report(args: argparse.Namespace) -> int:
    from chaos.report import write_report

    run_dir: Path = args.run_dir.resolve()
    try:
        read_manifest(run_dir)
    except ExperimentError as err:
        print(err, file=sys.stderr)
        return 2
    print(write_report(run_dir, args.output))
    return 0


def _share(args: argparse.Namespace) -> int:
    from chaos.share import ShareError, render_png, write_share

    run_dir: Path = args.run_dir.resolve()
    try:
        read_manifest(run_dir)
    except ExperimentError as err:
        print(err, file=sys.stderr)
        return 2
    path = write_share(run_dir, args.output)
    print(path)
    if args.png:
        try:
            print(render_png(path))
        except ShareError as err:
            print(err, file=sys.stderr)
            return 2
    return 0


def _export(args: argparse.Namespace) -> int:
    from chaos.export import export

    run_dir: Path = args.run_dir.resolve()
    if not (run_dir / "timeline.jsonl").is_file():
        print(f"{run_dir}: no timeline.jsonl; not a run dir", file=sys.stderr)
        return 2
    from chaos.dashboard import dashboard_uid

    out: Path = args.output or run_dir / "export.om"
    run = export(run_dir, out)
    if args.grafana:
        # A minute of margin on each side of the run.
        query = urllib.parse.urlencode(
            {
                "var-run": run.run_id,
                "from": int((run.start - 60) * 1000),
                "to": int((run.end + 60) * 1000),
            }
        )
        print(f"{args.grafana.rstrip('/')}/d/{dashboard_uid(run.experiment)}?{query}")
    else:
        print(out)
    return 0


def _dashboard(args: argparse.Namespace) -> int:
    from chaos.dashboard import build_dashboard

    if args.output and len(args.paths) > 1:
        print("-o takes one path; use --dir for several", file=sys.stderr)
        return 2
    for path in args.paths:
        try:
            if path.is_dir():
                # A run dir: the experiment as it ran, overrides included.
                read_manifest(path)
                exp = parse_experiment(
                    read_yaml(path / "experiment.yaml"), str(path / "experiment.yaml")
                )
            else:
                exp, _ = load_experiment(path)
        except ExperimentError as err:
            print(f"invalid: {err}", file=sys.stderr)
            return 2
        text = json.dumps(build_dashboard(exp), indent=2) + "\n"
        out = args.dir / f"{exp.id}.json" if args.dir else args.output
        if out is None:
            sys.stdout.write(text)
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        print(out)
    return 0


def _experiments(directory: Path):
    for path in sorted(directory.glob("*.yaml")):
        try:
            exp, _ = load_experiment(path)
        except ExperimentError as err:
            print(f"invalid: {err}", file=sys.stderr)
            continue
        yield exp


WIDTH = 80


def _wrap(text: str, indent: str) -> str:
    """`text` as one paragraph, filled to WIDTH columns."""
    return textwrap.fill(
        " ".join(text.split()),
        WIDTH,
        initial_indent=indent,
        subsequent_indent=indent,
        break_on_hyphens=False,
    )


def _list(args: argparse.Namespace) -> int:
    entries = []
    for exp in _experiments(args.experiments_dir):
        refs = [e.issue for e in exp.expectations]
        if args.issue and not any(_same_issue(r, args.issue) for r in refs):
            continue
        lines = [f"{exp.id} (v{exp.version})", _wrap(exp.title, "  ")]
        if refs:
            lines.append(_wrap(f"issues: {', '.join(refs)}", "  "))
        lines.append(_wrap(exp.hypothesis, "    "))
        entries.append("\n".join(lines))
    print("\n\n".join(entries))
    return 0


def _same_issue(ref: str, query: str) -> bool:
    """`query` is a full ref or a bare issue number."""
    return ref == query or ref.endswith(f"#{query.lstrip('#')}")


def _show(args: argparse.Namespace) -> int:
    path = args.experiments_dir / f"{args.id}.yaml"
    try:
        exp, _ = load_experiment(path)
    except ExperimentError as err:
        print(f"invalid: {err}", file=sys.stderr)
        return 1

    sections = [
        f"{exp.id} (v{exp.version})\n{_wrap(exp.title, '')}\n"
        f"default-adapter: {exp.default_adapter}   timeout: {exp.timeout}",
        "hypothesis:\n" + _wrap(exp.hypothesis, "  "),
        "description:\n" + _wrap(exp.description, "  "),
    ]
    workloads = []
    for w in exp.workloads:
        lines = [f"  {w.name} x{w.instances} (timeout {w.timeout})"]
        lines.append(_wrap(w.description, "    "))
        for c in w.checks:
            lines.append(f"    check {c.name}:\n" + _wrap(c.description, "      "))
        workloads.append("\n".join(lines))
    sections.append("workloads:\n" + "\n\n".join(workloads))
    if exp.data:
        sections.append("data: " + ", ".join(exp.data))
    if exp.operations:
        operations = []
        for op in exp.operations:
            options = "".join(f" {k}={v}" for k, v in op.options.items())
            operations.append(
                f"  {op.name}: {op.uses}{options}\n    on {_on(op)}\n"
                + _wrap(op.description, "    ")
            )
        sections.append("operations:\n" + "\n\n".join(operations))
    if exp.checks:
        sections.append(
            "checks:\n"
            + "\n\n".join(
                f"  {c.name}:\n" + _wrap(c.description, "    ") for c in exp.checks
            )
        )
    if exp.expectations:
        expectations = [
            f"  {e.issue}, known to fail{' (flaky)' if e.kind == 'flaky' else ''}"
            + (" on affected instances" if e.instances == "affected" else "")
            + ":"
            + "".join(f"\n    {key}" for key in e.checks)
            if e.checks
            else f"  {e.issue}, regression test"
            for e in exp.expectations
        ]
        sections.append("expectations:\n" + "\n".join(expectations))
    print("\n\n".join(sections))
    return 0


def _on(op: Operation) -> str:
    if op.target is None:
        return "global"
    select = op.select if isinstance(op.select, str) else f"count={op.select.count}"
    if isinstance(op.target, str):
        where = f"target {op.target}"
    elif isinstance(op.target, RoleTarget):
        where = f"role {op.target.role} of {op.target.workload}"
    else:
        where = f"workload {op.target.workload}"
    timing = f" for {op.duration}" if op.duration else ""
    return f"{where}, select {select}{timing}"


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="chaos", description="Declarative chaos experiments."
    )
    p.add_argument(
        "--adapters-dir",
        type=Path,
        default=DEFAULT_ADAPTERS_DIR,
        help=argparse.SUPPRESS,
    )
    p.add_argument(
        "--experiments-dir",
        type=Path,
        default=DEFAULT_EXPERIMENTS_DIR,
        help=argparse.SUPPRESS,
    )
    sub = p.add_subparsers(dest="command", required=True)

    v = sub.add_parser(
        "validate",
        help="check experiment files against their adapter (default-adapter or "
        "--adapter); exit 0 if each is ok or not applicable there",
    )
    v.add_argument("files", type=Path, nargs="*")
    on = v.add_mutually_exclusive_group()
    on.add_argument("--adapter", help=ADAPTER_HELP)
    on.add_argument(
        "--all-adapters",
        action="store_true",
        help="check against every adapter and print a table; with no files, "
        "every experiment",
    )
    v.set_defaults(func=_validate)

    pf = sub.add_parser(
        "preflight",
        help="run only the preflight checks; exit 0 if all pass or the experiment "
        "is not applicable on the adapter, else 2",
    )
    pf.add_argument("file", type=Path)
    pf.add_argument("--adapter", help=ADAPTER_HELP)
    pf.add_argument("--set", action="append", default=[], metavar="PATH=VALUE")
    pf.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    pf.set_defaults(func=_preflight)

    r = sub.add_parser(
        "run",
        help="run one experiment; exit 0 without running if it is not applicable "
        "on the adapter",
    )
    r.add_argument("file", type=Path)
    r.add_argument("--adapter", help=ADAPTER_HELP)
    r.add_argument("--seed", type=int, default=0)
    r.add_argument("--set", action="append", default=[], metavar="PATH=VALUE")
    r.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    r.set_defaults(func=_run)

    ev = sub.add_parser(
        "evaluate",
        help="re-run the checks of an existing run into RUN_DIR/evaluations/",
    )
    ev.add_argument("run_dir", type=Path)
    ev.add_argument(
        "--experiment", type=Path, help="take checks from this file instead"
    )
    ev.add_argument(
        "--adapter",
        help="take adapter checks from the current adapter of this id instead of "
        "the run's copy",
    )
    ev.add_argument("--index", type=Path, help="also append a line to this index.jsonl")
    ev.set_defaults(func=_evaluate)

    rp = sub.add_parser("report", help="write an HTML report of an existing run")
    rp.add_argument("run_dir", type=Path)
    rp.add_argument("-o", "--output", type=Path, help="default: RUN_DIR/report.html")
    rp.set_defaults(func=_report)

    sp = sub.add_parser(
        "share",
        help="write a Markdown page of an existing run with Mermaid charts, "
        "to paste into an issue or a post",
    )
    sp.add_argument("run_dir", type=Path)
    sp.add_argument("-o", "--output", type=Path, help="default: RUN_DIR/share.md")
    sp.add_argument(
        "--png",
        action="store_true",
        help="also render the charts to PNG with mermaid-cli (mmdc), "
        "for places that do not draw Mermaid",
    )
    sp.set_defaults(func=_share)

    ep = sub.add_parser(
        "export", help="write a run as OpenMetrics text, for Prometheus and Grafana"
    )
    ep.add_argument("run_dir", type=Path)
    ep.add_argument("-o", "--output", type=Path, help="default: RUN_DIR/export.om")
    ep.add_argument(
        "--grafana",
        metavar="URL",
        help="print a link to the experiment's Grafana dashboard instead of the path",
    )
    ep.set_defaults(func=_export)

    d = sub.add_parser(
        "dashboard", help="write the Grafana dashboard JSON for an experiment"
    )
    d.add_argument("paths", type=Path, nargs="+", help="experiment files or run dirs")
    out = d.add_mutually_exclusive_group()
    out.add_argument("-o", "--output", type=Path, help="default: standard output")
    out.add_argument(
        "--dir",
        type=Path,
        help="write DIR/<experiment id>.json for each; a later path for the "
        "same experiment replaces an earlier one",
    )
    d.set_defaults(func=_dashboard)

    ls = sub.add_parser("list", help="list experiments")
    ls.add_argument(
        "--issue", help="only experiments for this issue: owner/repo#N, #N or N"
    )
    ls.set_defaults(func=_list)

    s = sub.add_parser("show", help="describe one experiment")
    s.add_argument("id")
    s.set_defaults(func=_show)
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stderr
    )
    args = parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
