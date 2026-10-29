# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The run dir's manifest and its schema version."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from chaos.model import ExperimentError

# The layout of the run dir and its files, written to manifest.json as
# `schema_version`. Every reader of a run dir refuses any other value; there
# is no compatibility code.
SCHEMA_VERSION = "v1alpha1"


def read_manifest(run_dir: Path) -> dict[str, Any]:
    """The manifest of a run dir, or ExperimentError if it is missing or was
    written under another schema version."""
    path = run_dir / "manifest.json"
    if not path.is_file():
        raise ExperimentError(f"{run_dir}: no manifest.json; not a run dir")
    manifest = json.loads(path.read_text())
    version = manifest.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ExperimentError(
            f"{path}: schema_version {version!r} is not {SCHEMA_VERSION!r}; "
            "this harness cannot read it"
        )
    return manifest
