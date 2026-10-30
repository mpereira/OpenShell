# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess

import pytest
from conftest import ADAPTERS

from chaos.adapter import expand, hook_script, load_adapter, missing_env, run_hook
from chaos.model import ExperimentError


@pytest.fixture
def docker_adapter():
    return load_adapter(ADAPTERS, "openshell-docker")


def test_openshell_docker_adapter_loads(docker_adapter):
    assert docker_adapter.id == "openshell-docker"
    assert docker_adapter.backend == "docker"
    assert docker_adapter.target("gateway").type == "process"
    assert set(docker_adapter.targets.roles) == {"sandbox", "supervisor"}
    assert {c.name for c in docker_adapter.checks} == {
        "no-leaked-containers",
        "nothing-paused",
        "gateway-healthy",
    }
    assert [c.name for c in docker_adapter.preflight] == [
        "docker-reachable",
        "pull-otel-collector-image",
        "disk-space",
        "images-present",
        "cli-runs",
        "jq-installed",
        "gateway-up",
    ]
    assert [(s.name, s.watch) for s in docker_adapter.signals] == [
        ("reconcile-sweep", "otel")
    ]
    (sweep,) = docker_adapter.signals
    assert re.search(sweep.match, "    Name           : reconcile.sandboxes")
    assert not re.search(sweep.match, "    Name           : reconcile.sandboxes.one")
    assert len(docker_adapter.sha256) == 64


def _hooks(adapter) -> dict[str, str]:
    hooks = {
        f"instance.{k}": getattr(adapter.instance, k)
        for k in ("create", "status", "collect", "delete")
    }
    hooks["setup"] = adapter.setup
    hooks["collect"] = adapter.collect
    for name, target in adapter.targets.named.items():
        hooks[f"targets.{name}.start"] = target.start
    for name, spec in adapter.operations.items():
        hooks[f"operations.{name}"] = spec.run
        if spec.reverts:
            hooks[f"operations.{name}.revert"] = spec.revert
    return {k: v for k, v in hooks.items() if v}


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="needs shellcheck")
@pytest.mark.parametrize(
    ("adapter_id", "count"), [("openshell-docker", 9), ("openshell-kubernetes", 10)]
)
def test_adapter_hooks_pass_shellcheck(adapter_id, count):
    adapter = load_adapter(ADAPTERS, adapter_id)
    hooks = _hooks(adapter)
    assert len(hooks) == count
    for name, script in hooks.items():
        result = subprocess.run(
            # A hook uses only some of the prelude's helpers.
            ["shellcheck", "--shell=bash", "--exclude=SC2317,SC2329", "-"],
            input=hook_script(adapter, script),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"{name}:\n{result.stdout}"


def test_hook_runs_after_the_prelude_with_its_name(adapters_dir):
    adapter = load_adapter(
        adapters_dir(prelude='greet() { echo "hi from $0"; }\n'),
        "openshell-docker",
    )
    result = asyncio.run(run_hook(adapter, "instance.create", "greet", {}))
    assert result.ok, result.stderr
    assert result.stdout == "hi from instance.create\n"


def test_options_defaults_and_overrides(docker_adapter):
    assert docker_adapter.resolve_options({}, "w") == {
        "image": "",
        "policy": "",
        "restart-policy": "never",
    }
    resolved = docker_adapter.resolve_options({"restart-policy": "on-failure"}, "w")
    assert resolved["restart-policy"] == "on-failure"


def test_option_not_in_enum(docker_adapter):
    with pytest.raises(
        ExperimentError, match=r"w: option restart-policy='sometimes' not in"
    ):
        docker_adapter.resolve_options({"restart-policy": "sometimes"}, "w")


def test_unknown_option(docker_adapter):
    with pytest.raises(ExperimentError, match="w: unknown option 'gpu'"):
        docker_adapter.resolve_options({"gpu": "1"}, "w")


def test_unknown_target_and_role(docker_adapter):
    with pytest.raises(ExperimentError, match="no target 'db'"):
        docker_adapter.target("db")
    with pytest.raises(ExperimentError, match="no role 'proxy'"):
        docker_adapter.role("proxy")


def test_missing_env(docker_adapter):
    env = dict.fromkeys(docker_adapter.requires_env, "x")
    assert missing_env(docker_adapter, env) == []
    env["OPENSHELL_BIN"] = ""
    del env["OPENSHELL_GATEWAY"]
    assert missing_env(docker_adapter, env) == ["OPENSHELL_BIN", "OPENSHELL_GATEWAY"]


def test_missing_adapter(tmp_path):
    with pytest.raises(ExperimentError, match="no adapter openshell-kubernetes"):
        load_adapter(tmp_path, "openshell-kubernetes")


def test_adapter_id_must_match_file_name(adapters_dir):
    directory = adapters_dir()
    (directory / "openshell-docker.yaml").rename(directory / "other.yaml")
    with pytest.raises(ExperimentError, match="does not match the file name"):
        load_adapter(directory, "other")


def test_backend_must_be_supported(adapters_dir):
    with pytest.raises(
        ExperimentError, match=r"backend: Input should be 'docker' or 'kubernetes'"
    ):
        load_adapter(adapters_dir(backend="podman"), "openshell-docker")


def test_process_target_needs_pid_file_and_start(adapters_dir):
    directory = adapters_dir(targets={"gateway": {"type": "process"}})
    with pytest.raises(
        ExperimentError, match="a process target needs pid-file and start"
    ):
        load_adapter(directory, "openshell-docker")


def test_unknown_adapter_key(adapters_dir):
    with pytest.raises(ExperimentError, match="colour: Extra inputs are not permitted"):
        load_adapter(adapters_dir(colour="red"), "openshell-docker")


@pytest.mark.parametrize(
    ("operation", "error"),
    [
        ({"run": "true"}, r"operations\.op\.revert: Field required"),
        ({"run": "true", "revert": "none"}, "revert: none needs a reason"),
        (
            {"run": "true", "revert": "true", "reason": "r"},
            "reason goes with revert: none",
        ),
    ],
)
def test_operation_reverts_or_says_why_not(adapters_dir, operation, error):
    directory = adapters_dir(operations={"op": operation})
    with pytest.raises(ExperimentError, match=error):
        load_adapter(directory, "openshell-docker")


def test_adapter_check_is_shell_only(adapters_dir):
    check = {"name": "e", "description": "d", "kind": "exit-code", "equals": 0}
    with pytest.raises(
        ExperimentError, match=r"checks\.0\.kind: Input should be 'shell'"
    ):
        load_adapter(adapters_dir(checks=[check]), "openshell-docker")


def test_expand():
    assert expand("a-${X}-${Y}", {"X": "1", "Y": "2"}) == "a-1-2"
    assert expand("$X", {}) == "$X"
    with pytest.raises(ExperimentError, match=r"\$\{MISSING\} is not set"):
        expand("${MISSING}", {})
