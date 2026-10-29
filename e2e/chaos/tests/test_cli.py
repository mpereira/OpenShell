# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""`--adapter` picks the adapter instead of the experiment's `default-adapter`."""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml
from conftest import ADAPTERS, PACKAGE_DIR, minimal_adapter

from chaos.cli import main

EXPERIMENTS = PACKAGE_DIR / "experiments"


def adapters(tmp_path: Path) -> Path:
    """The real adapters, and `bare-docker`: a Docker adapter with no
    operations, options or signals."""
    directory = tmp_path / "adapters"
    shutil.copytree(ADAPTERS, directory)
    bare = minimal_adapter(id="bare-docker")
    (directory / "bare-docker.yaml").write_text(yaml.safe_dump(bare))
    return directory


def test_validate_uses_default_adapter(tmp_path, capsys):
    directory = adapters(tmp_path)
    path = EXPERIMENTS / "hello-world.yaml"
    assert main(["--adapters-dir", str(directory), "validate", str(path)]) == 0
    assert "ok on openshell-docker:" in capsys.readouterr().out


def test_validate_with_another_adapter(tmp_path, capsys):
    directory = adapters(tmp_path)
    path = EXPERIMENTS / "hello-world.yaml"
    argv = ["--adapters-dir", str(directory), "validate", "--adapter", "bare-docker"]
    assert main([*argv, str(path)]) == 0
    assert "ok on bare-docker:" in capsys.readouterr().out


def test_validate_names_what_the_adapter_lacks(tmp_path, capsys):
    directory = adapters(tmp_path)
    path = EXPERIMENTS / "policy-tighten-survives-gateway-restart.yaml"
    argv = ["--adapters-dir", str(directory), "validate", "--adapter", "bare-docker"]
    assert main([*argv, str(path)]) == 1
    err = capsys.readouterr().err
    assert err.startswith("invalid on bare-docker: ")
    assert "unknown option 'policy' for bare-docker" in err


def test_unknown_adapter(tmp_path, capsys):
    directory = adapters(tmp_path)
    path = EXPERIMENTS / "hello-world.yaml"
    argv = ["--adapters-dir", str(directory), "preflight", "--adapter", "nope"]
    assert main([*argv, str(path)]) == 2
    assert "no adapter nope" in capsys.readouterr().err
