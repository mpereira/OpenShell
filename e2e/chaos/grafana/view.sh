#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# Load chaos runs into a local Prometheus, provision a dashboard for each
# experiment among them, and print a Grafana link for each run.
# Usage: e2e/chaos/grafana/view.sh RUN_DIR... (replaces runs loaded before)
#        e2e/chaos/grafana/view.sh --down     (stops it and deletes the data)
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
compose=(docker compose -f "${here}/compose.yaml")
import="${here}/../runs/.grafana"

if [[ "${1:-}" == "--down" ]]; then
  CHAOS_GRAFANA_IMPORT="${import}" "${compose[@]}" down --volumes
  exit 0
fi
if [[ $# -eq 0 ]]; then
  echo "usage: $0 RUN_DIR... | --down" >&2
  exit 2
fi

chaos=(env -u VIRTUAL_ENV uv run --project "${here}/.." chaos)
# Grafana mounts this directory; remove what earlier calls wrote into it (the
# exported series and the provisioned dashboards) rather than replace it, so
# the running container keeps seeing it. Nothing else in it is touched.
mkdir -p "${import}"
rm -f "${import}"/*.om
rm -rf "${import}/dashboards"
links=()
for run in "$@"; do
  links+=("$("${chaos[@]}" export "${run}" \
    -o "${import}/$(basename "${run}").om" \
    --grafana "http://127.0.0.1:${CHAOS_GRAFANA_PORT:-3030}")")
done
# One dashboard per experiment, from the last run given for it. A hand-written
# dashboards/<id>.json replaces the generated one.
"${chaos[@]}" dashboard --dir "${import}/dashboards" "$@" >/dev/null
for custom in "${here}"/dashboards/*.json; do
  rm -f "${import}/dashboards/$(basename "${custom}")"
done

export CHAOS_GRAFANA_IMPORT="${import}"
# Prometheus loads blocks at startup, so stop it while the data is replaced.
"${compose[@]}" stop prometheus
"${compose[@]}" run --rm import
"${compose[@]}" up -d prometheus grafana
printf '%s\n' "${links[@]}"
