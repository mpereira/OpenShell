# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Run ShellCheck on the scripts embedded in adapters and experiments.

Each script is written to its own file, named after where it lives in the YAML,
with the shell the harness runs it in: adapter hooks are bash with the
adapter's prelude in front, checks and watches are bash on their own, and
workload scripts are POSIX sh inside the sandbox.

Usage: shellcheck-embedded-scripts.py [YAML ...]
With no arguments, checks every file in adapters/ and experiments/.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from chaos.adapter import Adapter, hook_script
from chaos.model import ExperimentError, parse_experiment, read_yaml

CHAOS_DIR = Path(__file__).resolve().parents[1]


def adapter_scripts(adapter: Adapter) -> dict[str, tuple[str, str]]:
    """Every script in an adapter, by its YAML path: (shell, script)."""
    hooks: dict[str, str | None] = {
        "describe": adapter.describe,
        "setup": adapter.setup,
        "collect": adapter.collect,
        "instance.create": adapter.instance.create,
        "instance.status": adapter.instance.status,
        "instance.collect": adapter.instance.collect,
        "instance.delete": adapter.instance.delete,
    }
    targets = {**adapter.targets.named, **adapter.targets.roles}
    for name, target in targets.items():
        hooks[f"targets.{name}.start"] = target.start
        hooks[f"targets.{name}.after-recovery"] = target.after_recovery
    for name, operation in adapter.operations.items():
        hooks[f"operations.{name}.run"] = operation.run
        if operation.reverts:
            hooks[f"operations.{name}.revert"] = operation.revert
    scripts = {
        path: ("bash", hook_script(adapter, script))
        for path, script in hooks.items()
        if script
    }
    for section, checks in (
        ("preflight", adapter.preflight),
        ("checks", adapter.checks),
    ):
        for check in checks:
            if hasattr(check, "run"):
                scripts[f"{section}.{check.name}.run"] = ("bash", check.run)
    for watch in adapter.watch:
        scripts[f"watch.{watch.name}.run"] = ("bash", watch.run)
    return scripts


def experiment_scripts(raw: dict, source: str) -> dict[str, tuple[str, str]]:
    """Every script in an experiment, by its YAML path: (shell, script)."""
    exp = parse_experiment(raw, source)
    scripts: dict[str, tuple[str, str]] = {}
    for workload in exp.workloads:
        scripts[f"workloads.{workload.name}.run"] = ("sh", workload.run)
        for check in workload.checks:
            if hasattr(check, "run"):
                scripts[f"workloads.{workload.name}.checks.{check.name}.run"] = (
                    "bash",
                    check.run,
                )
    for section, checks in (("preflight", exp.preflight), ("checks", exp.checks)):
        for check in checks:
            if hasattr(check, "run"):
                scripts[f"{section}.{check.name}.run"] = ("bash", check.run)
    return scripts


def scripts_of(path: Path) -> dict[str, tuple[str, str]]:
    raw = read_yaml(path)
    if path.parent.name == "adapters":
        return adapter_scripts(Adapter.model_validate(raw))
    return experiment_scripts(raw, str(path))


def main(argv: list[str]) -> int:
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        print(
            "shellcheck is not installed; see https://www.shellcheck.net",
            file=sys.stderr,
        )
        return 2
    paths = [Path(a) for a in argv] or sorted(
        [*CHAOS_DIR.glob("adapters/*.yaml"), *CHAOS_DIR.glob("experiments/*.yaml")]
    )
    failed = False
    with tempfile.TemporaryDirectory(prefix="chaos-shellcheck-") as tmp:
        for path in paths:
            try:
                scripts = scripts_of(path)
            except ExperimentError as err:
                print(err, file=sys.stderr)
                failed = True
                continue
            # One directory per YAML file, one file per script, so ShellCheck's
            # messages name both.
            out_dir = Path(tmp) / path.name
            out_dir.mkdir()
            files = []
            for yaml_path, (shell, script) in scripts.items():
                file = out_dir / f"{yaml_path}.{shell}"
                file.write_text(f"# shellcheck shell={shell}\n{script}")
                files.append(file)
            if not files:
                continue
            result = subprocess.run(
                # SC2317: a hook that does not call every prelude helper sees
                # the others as unreachable.
                [shellcheck, "--format=gcc", "--exclude=SC2317", *map(str, files)],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                failed = True
                sys.stdout.write(result.stdout.replace(f"{tmp}/", ""))
                sys.stderr.write(result.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
