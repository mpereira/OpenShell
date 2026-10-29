# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Kubernetes injector: the built-in operations, on workloads and pods.

A `workload` target is a controller such as `statefulset/openshell`, whose
pods the controller replaces; its `start` hook waits until the replacement is
ready, and re-establishes whatever the run reaches it through, such as port
forwards. A `pod` target is a set of pods found by label.

Every kubectl call names the adapter's `kubernetes.context`, never the
current context. Kubernetes has no portable way to signal a process in a
container, so the faults delete pods: `--grace-period` is the SIGTERM grace,
and a grace of 0 force-deletes, which is the closest to SIGKILL the API
offers. A pod that `restartPolicy: Never` keeps from restarting in place is
replaced only if its controller replaces it.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from typing import Any

from chaos.adapter import Target, expand, run_shell
from chaos.inject import Candidate
from chaos.inject.base import Injector, Undo
from chaos.model import Operation, RoleTarget, RunError

KUBECTL_TIMEOUT = 60.0
# Bounds the wait for deleted pods to be gone, on top of any grace period.
DELETE_TIMEOUT = 60.0


def _running(pod: dict[str, Any]) -> bool:
    """A pod that is running and not already being deleted."""
    return pod.get("status", {}).get("phase") == "Running" and not pod.get(
        "metadata", {}
    ).get("deletionTimestamp")


def _ready(pod: dict[str, Any]) -> bool:
    return _running(pod) and any(
        c.get("type") == "Ready" and c.get("status") == "True"
        for c in pod.get("status", {}).get("conditions", [])
    )


def _selector(labels: dict[str, str], env: dict[str, str]) -> str:
    pairs = []
    for key, value in labels.items():
        key, value = expand(key, env), expand(value, env)
        if not key or not value:
            # An empty selector would match every pod in the namespace.
            raise RunError(f"label selector {key}={value} expands to empty")
        pairs.append(f"{key}={value}")
    if not pairs:
        raise RunError("a pod selector needs at least one label")
    return ",".join(pairs)


class KubernetesInjector(Injector):
    async def _kubectl(self, *args: str, timeout: float = KUBECTL_TIMEOUT) -> str:
        assert self.adapter.kubernetes is not None
        context = expand(self.adapter.kubernetes.context, self._run_env())
        if not context:
            raise RunError("kubernetes.context expands to empty")
        result = await run_shell(["kubectl", "--context", context, *args], {}, timeout)
        if not result.ok:
            raise RunError(f"kubectl {' '.join(args)}: {result.stderr.strip()}")
        return result.stdout

    def _namespace(self, target: Target, env: dict[str, str]) -> str:
        assert target.namespace is not None
        namespace = expand(target.namespace, env)
        if not namespace:
            raise RunError(f"namespace {target.namespace} expands to empty")
        return namespace

    async def _pods(self, namespace: str, selector: str) -> list[dict[str, Any]]:
        out = await self._kubectl(
            "-n", namespace, "get", "pods", "-l", selector, "-o", "json"
        )
        try:
            return json.loads(out).get("items", [])
        except json.JSONDecodeError as err:
            raise RunError(f"kubectl get pods -l {selector}: {err}") from None

    # Workload targets.

    def _resource(self, target: Target) -> str:
        assert target.resource is not None
        resource = expand(target.resource, self._run_env())
        if not resource:
            raise RunError(f"resource {target.resource} expands to empty")
        return resource

    async def _workload_selector(self, namespace: str, resource: str) -> str:
        """The label selector of the workload's pods, from its spec."""
        out = await self._kubectl(
            "-n",
            namespace,
            "get",
            resource,
            "-o",
            "jsonpath={.spec.selector.matchLabels}",
        )
        try:
            labels = json.loads(out) if out.strip() else {}
        except json.JSONDecodeError as err:
            raise RunError(f"{resource}: selector is not JSON: {err}") from None
        if not labels:
            raise RunError(f"{resource} has no spec.selector.matchLabels")
        return ",".join(f"{k}={v}" for k, v in sorted(labels.items()))

    async def _workload_pods(
        self, target: Target
    ) -> tuple[str, str, list[dict[str, Any]]]:
        namespace = self._namespace(target, self._run_env())
        selector = await self._workload_selector(namespace, self._resource(target))
        return namespace, selector, await self._pods(namespace, selector)

    async def ensure_up(self) -> None:
        """Abort path: start any workload target that has no ready pod."""
        for name, target in self.adapter.targets.named.items():
            if target.type != "workload":
                continue
            _, _, pods = await self._workload_pods(target)
            if not any(_ready(p) for p in pods):
                await self.start(name, target, operation=None)

    # Selection.

    async def _candidates(self, op: Operation) -> tuple[str, Target, list[Candidate]]:
        if isinstance(op.target, RoleTarget):
            role = op.target.role
            target = self.adapter.role(role)
            candidates: list[Candidate] = []
            for inst in self.instances.by_workload.get(op.target.workload, []):
                if inst.system_id is None or inst.deleted:
                    continue
                env = {**os.environ, **self.instances.hook_env(inst)}
                namespace = self._namespace(target, env)
                for pod in await self._pods(namespace, _selector(target.labels, env)):
                    if _running(pod):
                        pod_id = f"{namespace}/{pod['metadata']['name']}"
                        candidates.append(
                            Candidate(inst.name, inst.info.index, role, pod_id)
                        )
            return role, target, candidates

        assert isinstance(op.target, str)
        name = op.target
        target = self.adapter.target(name)
        if target.type == "workload":
            namespace, _, pods = await self._workload_pods(target)
            ref = f"{namespace}/{self._resource(target)}"
            alive = any(_running(p) for p in pods)
            return name, target, [Candidate(name, 0, name, ref)] if alive else []
        env = self._run_env()
        namespace = self._namespace(target, env)
        pods = await self._pods(namespace, _selector(target.labels, env))
        ids = [f"{namespace}/{p['metadata']['name']}" for p in pods if _running(p)]
        return name, target, [Candidate(name, 0, name, i) for i in ids]

    # Operations.

    async def _apply(
        self, op: Operation, name: str, target: Target, picks: list[Candidate]
    ) -> None:
        if target.type == "workload":
            await self._workload_fault(op, name, target)
        elif target.type == "pod":
            await self._pod_fault(op, picks)
        else:
            raise RunError(f"kubernetes can't run {op.uses} on a {target.type} target")

    async def _delete_pods(self, namespace: str, pods: list[str], grace: float) -> None:
        """Delete the pods and wait until they are gone. Grace 0 force-deletes."""
        if not pods:
            return
        seconds = int(grace)
        flags = (
            [f"--grace-period={seconds}"]
            if seconds > 0
            else ["--grace-period=0", "--force"]
        )
        timeout = seconds + DELETE_TIMEOUT
        await self._kubectl(
            "-n", namespace, "delete", "pod", *pods, *flags,
            "--ignore-not-found", "--wait=true", f"--timeout={timeout:.0f}s",
            timeout=timeout + 10,
        )  # fmt: skip

    async def _workload_fault(self, op: Operation, name: str, target: Target) -> None:
        namespace, selector, pods = await self._workload_pods(target)
        names = [p["metadata"]["name"] for p in pods if _running(p)]
        restart = Undo(
            op.name, f"start {name}", lambda: self.start(name, target, op.name)
        )
        if op.uses == "restart":
            await self._delete_pods(namespace, names, op.grace_seconds or 0)
            self._emit("operation.applied", op, pods=names)
            await self._hold(op, [restart], hold=0)
        elif op.uses == "outage":
            resource = self._resource(target)
            replicas = (
                await self._kubectl(
                    "-n", namespace, "get", resource, "-o", "jsonpath={.spec.replicas}"
                )
            ).strip() or "1"
            if replicas == "0":
                raise RunError(f"{resource} is already scaled to 0")
            scale_back = Undo(
                op.name,
                f"scale {resource} to {replicas}",
                self._scale(namespace, resource, replicas),
            )
            # On the stack before scaling down, so an abort mid-outage still
            # scales back and starts; scaling to the same count is a no-op.
            undo = [restart, scale_back]
            self.undo_stack.extend(undo)
            await self._scale(namespace, resource, "0")()
            await self._delete_pods(namespace, names, 0)
            await self._wait_no_pods(namespace, selector)
            self._emit("operation.applied", op, pods=names)
            await self._hold(op, undo)
            self._emit("operation.reverted", op)
        else:
            raise RunError(f"{op.uses} is not supported on a workload target")

    async def _wait_no_pods(self, namespace: str, selector: str) -> None:
        """Wait until the selector matches no pod, terminating ones included."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + DELETE_TIMEOUT
        while pods := await self._pods(namespace, selector):
            if loop.time() >= deadline:
                left = " ".join(p["metadata"]["name"] for p in pods)
                raise RunError(f"pods still there after {DELETE_TIMEOUT:g}s: {left}")
            await asyncio.sleep(1)

    def _scale(
        self, namespace: str, resource: str, replicas: str
    ) -> Callable[[], Awaitable[None]]:
        async def run() -> None:
            await self._kubectl(
                "-n", namespace, "scale", resource, f"--replicas={replicas}"
            )

        return run

    async def _pod_fault(self, op: Operation, picks: list[Candidate]) -> None:
        if op.uses != "kill":
            raise RunError(f"{op.uses} is not supported on a pod target")
        by_namespace: dict[str, list[str]] = {}
        for p in picks:
            namespace, _, pod = p.id.partition("/")
            by_namespace.setdefault(namespace, []).append(pod)
        for namespace, pods in by_namespace.items():
            await self._delete_pods(namespace, pods, 0)
        self._emit("operation.applied", op)
