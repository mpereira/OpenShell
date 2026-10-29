#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# Runs a command with what the openshell-kubernetes adapter needs on top of
# e2e/with-kube-gateway.sh, then stops the port forwards the run started:
#
#   e2e/with-kube-gateway.sh e2e/chaos/scripts/kubernetes-env.sh \
#     uv run --project e2e/chaos chaos run --adapter openshell-kubernetes ...
#
# Exports:
#   OPENSHELL_E2E_KUBE_GATEWAY_RESOURCE  statefulset/<release> or deployment/<release>
#   OPENSHELL_E2E_GATEWAY_PORT           local port of the gateway's gRPC forward
#   OPENSHELL_E2E_METRICS_PORT           local port for the gateway's metrics forward
#   OPENSHELL_E2E_KUBE_FORWARD_DIR       pid and log files of the run's forwards
#
# The adapter's setup hook starts the forwards, and the gateway's start hook
# starts them again after a fault replaces the gateway pod. Supports the
# wrapper's port-forward mode only, not the OpenShift Route mode.

set -euo pipefail

for name in OPENSHELL_GATEWAY OPENSHELL_E2E_KUBE_CONTEXT OPENSHELL_E2E_KUBE_NAMESPACE \
  OPENSHELL_E2E_KUBE_RELEASE OPENSHELL_E2E_HEALTH_PORT; do
  if [ -z "${!name:-}" ]; then
    echo "$name is not set; run this under e2e/with-kube-gateway.sh" >&2
    exit 2
  fi
done
if [ "$#" -eq 0 ]; then
  echo "usage: $0 <command> [args...]" >&2
  exit 2
fi

kctl() {
  kubectl --context "$OPENSHELL_E2E_KUBE_CONTEXT" -n "$OPENSHELL_E2E_KUBE_NAMESPACE" "$@"
}

resource=
for candidate in "statefulset/$OPENSHELL_E2E_KUBE_RELEASE" "deployment/$OPENSHELL_E2E_KUBE_RELEASE"; do
  if kctl get "$candidate" > /dev/null 2>&1; then
    resource=$candidate
    break
  fi
done
if [ -z "$resource" ]; then
  echo "no statefulset or deployment $OPENSHELL_E2E_KUBE_RELEASE in $OPENSHELL_E2E_KUBE_NAMESPACE" >&2
  exit 2
fi

# The wrapper names the gateway openshell-e2e-kube-<local port>.
gateway_port=${OPENSHELL_GATEWAY##*-}
if ! [[ "$gateway_port" =~ ^[0-9]+$ ]]; then
  echo "cannot read the gateway port from OPENSHELL_GATEWAY=$OPENSHELL_GATEWAY" >&2
  exit 2
fi

metrics_port=$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
forward_dir=$(mktemp -d "${TMPDIR:-/tmp}/chaos-kube-forwards.XXXXXX")

stop_forwards() {
  local pid_file pid
  for pid_file in "$forward_dir"/*.pid; do
    [ -f "$pid_file" ] || continue
    pid=$(cat "$pid_file")
    kill "$pid" 2> /dev/null || true
  done
  rm -rf "$forward_dir"
}
trap stop_forwards EXIT

export OPENSHELL_E2E_KUBE_GATEWAY_RESOURCE=$resource
export OPENSHELL_E2E_GATEWAY_PORT=$gateway_port
export OPENSHELL_E2E_METRICS_PORT=$metrics_port
export OPENSHELL_E2E_KUBE_FORWARD_DIR=$forward_dir

# Not exec: the trap must run after the command.
"$@"
