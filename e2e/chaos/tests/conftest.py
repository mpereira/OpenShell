# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

PACKAGE_DIR = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXPERIMENTS = PACKAGE_DIR / "experiments"
ADAPTERS = PACKAGE_DIR / "adapters"


def minimal_adapter(**extra) -> dict:
    """A Docker adapter with no hooks that are ever run."""
    adapter = {
        "apiVersion": "chaos/v1alpha1",
        "id": "openshell-docker",
        "backend": "docker",
        "limits": {"env-value-bytes": 8192, "run-script-bytes": 28672},
        "describe": 'echo \'{"openshell": "test"}\'',
        "instance": {
            "output-dir": "/out",
            "create": "true",
            "status": "true",
            "collect": "true",
            "delete": "true",
        },
        "targets": {
            "gateway": {"type": "process", "pid-file": "/pid", "start": "true"},
            "roles": {
                "sandbox": {"type": "container", "labels": {"role": "sandbox"}},
                "supervisor": {"type": "container", "labels": {"role": "supervisor"}},
            },
        },
    }
    adapter.update(extra)
    return adapter


@pytest.fixture
def adapters_dir(tmp_path: Path):
    """Write an adapter into a temporary adapters dir: `adapters_dir(**extra)`."""
    directory = tmp_path / "adapters"
    directory.mkdir()

    def write(**extra) -> Path:
        path = directory / "openshell-docker.yaml"
        path.write_text(yaml.safe_dump(minimal_adapter(**extra)))
        return directory

    return write
