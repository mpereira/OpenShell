#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# The one end-to-end check: runs hello-world and then smoke against the gateway
# the e2e wrapper started, and fails unless both verdicts are PASS. Run it with
# `mise run e2e:chaos:check` from the repository root, which wraps it in
# e2e/with-docker-gateway.sh. Extra arguments go to every `chaos run`.

set -uo pipefail

root="$(cd "$(dirname "$0")/../../.." && pwd)"
index="$root/e2e/chaos/runs/index.jsonl"
failed=()

for name in hello-world smoke; do
  echo "== $name"
  before=$(wc -l < "$index" 2>/dev/null || echo 0)
  uv run --project "$root/e2e/chaos" chaos run "$root/e2e/chaos/experiments/$name.yaml" "$@"
  code=$?
  after=$(wc -l < "$index" 2>/dev/null || echo 0)
  if [ "$after" -le "$before" ]; then
    echo "$name: no run recorded in $index (chaos run exited $code)" >&2
    failed+=("$name")
    continue
  fi
  verdict=$(tail -n 1 "$index" | jq -r .verdict)
  if [ "$verdict" != PASS ]; then
    echo "$name: verdict $verdict, see $(tail -n 1 "$index" | jq -r .path)" >&2
    failed+=("$name")
  fi
done

if [ "${#failed[@]}" -gt 0 ]; then
  echo "failed: ${failed[*]}"
  exit 1
fi
echo "hello-world and smoke: PASS"
