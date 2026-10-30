# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The kubernetes backend: adapter rules and which experiments validate on
openshell-kubernetes. The adapter has not been run against a cluster, so
nothing here exercises the injector's kubectl calls."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from conftest import ADAPTERS, EXPERIMENTS, minimal_adapter

from chaos.adapter import load_adapter
from chaos.cli import main
from chaos.model import ExperimentError


def kubernetes_adapter(**extra) -> dict:
    """A Kubernetes adapter whose hooks only record that they ran."""
    adapter = minimal_adapter(
        backend="kubernetes",
        kubernetes={"context": "${CHAOS_TEST_CONTEXT}"},
        targets={
            "gateway": {
                "type": "workload",
                "namespace": "openshell",
                "resource": "statefulset/openshell",
                "start": 'echo started >> "$CHAOS_TEST_LOG"',
            },
            "roles": {
                "sandbox": {
                    "type": "pod",
                    "namespace": "sandboxes",
                    "labels": {
                        "pair": "${CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_SYSTEM_ID}",
                        "role": "workload",
                    },
                },
                "supervisor": {
                    "type": "pod",
                    "namespace": "sandboxes",
                    "labels": {
                        "pair": "${CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_SYSTEM_ID}",
                        "role": "supervisor",
                    },
                },
            },
        },
    )
    adapter.update(extra)
    return adapter


def write_adapter(tmp_path: Path, adapter: dict) -> Path:
    directory = tmp_path / "adapters"
    directory.mkdir(exist_ok=True)
    (directory / f"{adapter['id']}.yaml").write_text(yaml.safe_dump(adapter))
    return directory


def load(tmp_path: Path, **extra):
    return load_adapter(
        write_adapter(tmp_path, kubernetes_adapter(**extra)), "openshell-docker"
    )


# Adapter rules.


def test_openshell_kubernetes_adapter_loads():
    adapter = load_adapter(ADAPTERS, "openshell-kubernetes")
    assert adapter.backend == "kubernetes"
    assert adapter.kubernetes is not None
    assert adapter.kubernetes.context == "${OPENSHELL_E2E_KUBE_CONTEXT}"
    assert adapter.target("gateway").type == "workload"
    assert {r.type for r in adapter.targets.roles.values()} == {"pod"}
    assert {c.name for c in adapter.checks} == {"no-leaked-pods", "gateway-healthy"}
    assert adapter.setup


def test_kubernetes_needs_a_context(tmp_path):
    adapter = kubernetes_adapter()
    del adapter["kubernetes"]
    with pytest.raises(
        ExperimentError, match="the kubernetes backend needs kubernetes.context"
    ):
        load_adapter(write_adapter(tmp_path, adapter), "openshell-docker")


def test_docker_takes_no_kubernetes_block(tmp_path):
    adapter = minimal_adapter(kubernetes={"context": "kind-chaos"})
    with pytest.raises(ExperimentError, match="kubernetes"):
        load_adapter(write_adapter(tmp_path, adapter), "openshell-docker")


def test_workload_target_needs_namespace_resource_and_start(tmp_path):
    with pytest.raises(
        ExperimentError, match="a workload target needs namespace, resource and start"
    ):
        load(
            tmp_path,
            targets={"gateway": {"type": "workload", "namespace": "openshell"}},
        )


def test_pod_target_needs_a_namespace(tmp_path):
    targets = {"roles": {"sandbox": {"type": "pod", "labels": {"role": "workload"}}}}
    with pytest.raises(ExperimentError, match="a pod target needs a namespace"):
        load(tmp_path, targets=targets)


def test_process_target_is_not_available_on_kubernetes(tmp_path):
    targets = {"gateway": {"type": "process", "pid-file": "/pid", "start": "true"}}
    with pytest.raises(
        ExperimentError, match="a process target is not available on kubernetes"
    ):
        load(tmp_path, targets=targets)


def test_pod_target_is_not_available_on_docker(tmp_path):
    targets = {
        "roles": {
            "sandbox": {
                "type": "pod",
                "namespace": "ns",
                "labels": {"role": "workload"},
            }
        }
    }
    adapter = minimal_adapter(targets=targets)
    with pytest.raises(
        ExperimentError, match="a pod target is not available on docker"
    ):
        load_adapter(write_adapter(tmp_path, adapter), "openshell-docker")


# Which shipped experiments validate on openshell-kubernetes.


def _validate(tmp_path, capsys, experiment: str) -> tuple[int, str]:
    directory = tmp_path / "adapters"
    shutil.copytree(ADAPTERS, directory)
    argv = [
        "--adapters-dir",
        str(directory),
        "validate",
        "--adapter",
        "openshell-kubernetes",
    ]
    code = main([*argv, str(EXPERIMENTS / f"{experiment}.yaml")])
    out = capsys.readouterr()
    return code, out.out + out.err


@pytest.mark.parametrize(
    "experiment",
    [
        "hello-world",
        "smoke",
        "gateway-graceful-restart",
        "gateway-kill-keeps-state",
        "policy-tighten-survives-gateway-restart",
        "supervisor-kill",
    ],
)
def test_experiment_validates_on_kubernetes(tmp_path, capsys, experiment):
    code, out = _validate(tmp_path, capsys, experiment)
    assert code == 0, out
    assert "ok on openshell-kubernetes:" in out


@pytest.mark.parametrize(
    ("experiment", "reason"),
    [
        (
            "supervisor-pause",
            "freeze on a pod target (role supervisor) is not supported on "
            "kubernetes: freezing a pod's processes needs node-level cgroup access",
        ),
        (
            "sandbox-container-restart",
            "restart-in-place on a pod target (role sandbox) is not supported on "
            "kubernetes: the Kubernetes driver runs sandbox pods with "
            "restartPolicy: Never",
        ),
        ("sandbox-stop-start", "openshell-kubernetes has no signal 'reconcile-sweep'"),
    ],
)
def test_experiment_is_not_applicable_on_kubernetes(
    tmp_path, capsys, experiment, reason
):
    code, out = _validate(tmp_path, capsys, experiment)
    assert code == 0, out
    assert "n/a on openshell-kubernetes:" in out
    assert reason in out


def test_run_of_a_not_applicable_experiment_exits_0_without_a_run(tmp_path, capsys):
    runs = tmp_path / "runs"
    code = main(
        [
            "run",
            "--adapter",
            "openshell-kubernetes",
            "--runs-dir",
            str(runs),
            str(EXPERIMENTS / "supervisor-pause.yaml"),
        ]
    )
    assert code == 0
    assert "n/a on openshell-kubernetes:" in capsys.readouterr().out
    assert not runs.exists()


def test_not_applicable_on_the_default_adapter_is_invalid(tmp_path, capsys):
    raw = yaml.safe_load((EXPERIMENTS / "supervisor-pause.yaml").read_text())
    raw["default-adapter"] = "openshell-kubernetes"
    path = tmp_path / "supervisor-pause.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    code = main(["validate", str(path)])
    out = capsys.readouterr()
    assert code == 1
    assert "invalid on openshell-kubernetes:" in out.err
    assert "(on its default-adapter)" in out.err


def test_validate_all_adapters_prints_the_matrix(capsys):
    code = main(["validate", "--all-adapters"])
    out = capsys.readouterr().out
    assert code == 0, out
    table, _, reasons = out.partition("\n\n")
    lines = table.splitlines()
    assert lines[0].split() == [
        "experiment",
        "openshell-docker",
        "openshell-kubernetes",
    ]
    rows = {line.split()[0]: line.split()[1:] for line in lines[1:]}
    assert rows["supervisor-kill"] == ["ok", "ok"]
    assert rows["supervisor-pause"] == ["ok", "n/a"]
    assert rows["sandbox-container-restart"] == ["ok", "n/a"]
    assert "sandbox-container-restart on openshell-kubernetes: n/a:" in reasons


def test_validate_takes_adapter_or_all_adapters_not_both(capsys):
    with pytest.raises(SystemExit):
        main(["validate", "--adapter", "openshell-docker", "--all-adapters"])
    capsys.readouterr()


def test_validate_needs_files_without_all_adapters(capsys):
    assert main(["validate"]) == 2
    assert "give experiment files" in capsys.readouterr().err


def test_unavailable_names_a_builtin(tmp_path):
    with pytest.raises(ExperimentError, match="unavailable.pause: not a built-in"):
        load(tmp_path, unavailable={"pause": "no"})
