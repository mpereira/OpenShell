#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# Runs the harness's unit tests, Ruff lint and format checks, and ShellCheck on
# the shell script files and the scripts embedded in adapters/ and experiments/. Needs uv and shellcheck.
# Runs every step and exits 1 if any failed. Extra arguments go to pytest.

set -uo pipefail

cd "$(dirname "$0")/.." || exit 2

failed=()
step() {
  local name=$1
  shift
  echo "== $name"
  "$@" || failed+=("$name")
}

step pytest uv run pytest -q "$@"
step "ruff check" uv run ruff check .
step "ruff format" uv run ruff format --check .
step "shellcheck embedded scripts" uv run python scripts/shellcheck-embedded-scripts.py
step "shellcheck script files" shellcheck scripts/*.sh grafana/*.sh

if [ "${#failed[@]}" -gt 0 ]; then
  echo "failed: ${failed[*]}"
  exit 1
fi
echo "all passed"
