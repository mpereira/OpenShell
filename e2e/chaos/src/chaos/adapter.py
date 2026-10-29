# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Adapters: how one system under test, on one backend, maps targets,
instances and operations to scripts."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
    model_validator,
)

from chaos.model import (
    BUILTIN_OPERATIONS,
    Experiment,
    ExperimentError,
    NotApplicable,
    RoleTarget,
    ShellCheck,
    format_validation_error,
    kebab_case,
    parse_duration,
    read_yaml,
    signal_waits,
)

DEFAULT_ADAPTERS_DIR = Path(__file__).resolve().parents[2] / "adapters"
VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# The built-in operations each backend's injector implements, per target
# type. See the operations matrix in REFERENCE.md.
SUPPORTED_OPERATIONS: dict[str, dict[str, set[str]]] = {
    "docker": {
        "process": {"restart", "outage", "freeze"},
        "container": {"kill", "restart-in-place", "freeze"},
    },
    # Kubernetes has no portable way to signal one container, so the pod
    # faults delete pods; see inject/kubernetes.py. An adapter says why a
    # built-in is missing in its `unavailable` block.
    "kubernetes": {
        "workload": {"restart", "outage"},
        "pod": {"kill"},
    },
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", alias_generator=kebab_case)


class Limits(_Strict):
    """What the system accepts from an experiment, in bytes: each `env` value
    of a workload or check, and a workload's `run` script."""

    env_value_bytes: int = Field(gt=0)
    run_script_bytes: int = Field(gt=0)


class OptionSpec(_Strict):
    default: str | None = None
    enum: list[str] | None = None
    # The value names an entry of the experiment's `data`; hooks get the path
    # of its file instead. Empty means none.
    data: bool = False
    required: bool = False


class OperationSpec(_Strict):
    # The bash script that applies the operation. It writes its outcome to
    # $CHAOS_EXPERIMENT_OPERATION_OUTCOME_FILE: `ok`, `skipped` or `failed`,
    # then an optional reason. A run that writes nothing is an error.
    run: str
    # The bash script that puts back what `run` changed, with the same
    # variables plus what `run` left in $CHAOS_EXPERIMENT_OPERATION_STATE_DIR,
    # or `none` with a `reason` when there is nothing to put back. The runner
    # reverts operations in reverse order at the end of the flow and on abort.
    revert: str
    reason: str | None = None
    options: dict[str, OptionSpec] = Field(default_factory=dict)

    @property
    def reverts(self) -> bool:
        return self.revert != "none"

    @model_validator(mode="after")
    def _revert_or_reason(self) -> OperationSpec:
        if not self.reverts and not self.reason:
            raise ValueError("revert: none needs a reason")
        if self.reverts and self.reason is not None:
            raise ValueError("reason goes with revert: none")
        return self


class InstanceHooks(_Strict):
    """The bash scripts that create, read, collect and delete one instance."""

    output_dir: str
    create: str
    status: str
    collect: str
    delete: str


class Target(_Strict):
    type: Literal["process", "container", "workload", "pod"]
    pid_file: str | None = None
    start: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    # Kubernetes targets: the namespace, and for a workload target the
    # controller that owns its pods, such as `statefulset/openshell`.
    namespace: str | None = None
    resource: str | None = None
    after_recovery: str | None = None

    @model_validator(mode="after")
    def _type_fields(self) -> Target:
        if self.type == "process" and (self.pid_file is None or self.start is None):
            raise ValueError("a process target needs pid-file and start")
        if self.type == "workload" and (
            self.namespace is None or self.resource is None or self.start is None
        ):
            raise ValueError("a workload target needs namespace, resource and start")
        if self.type == "pod" and self.namespace is None:
            raise ValueError("a pod target needs a namespace")
        if self.type in ("container", "pod") and not self.labels:
            raise ValueError(f"a {self.type} target needs labels")
        if not all(k and v for k, v in self.labels.items()):
            raise ValueError(f"{self.type} target labels need a key and a value")
        if self.type in ("process", "container") and (
            self.namespace is not None or self.resource is not None
        ):
            raise ValueError(f"a {self.type} target takes no namespace or resource")
        if self.type in ("workload", "pod") and self.pid_file is not None:
            raise ValueError(f"a {self.type} target takes no pid-file")
        return self


class Targets(_Strict):
    model_config = ConfigDict(extra="allow")

    roles: dict[str, Target] = Field(default_factory=dict)

    @property
    def named(self) -> dict[str, Target]:
        return {
            k: Target.model_validate(v) for k, v in (self.model_extra or {}).items()
        }


class MetricsSource(_Strict):
    """A Prometheus text endpoint the harness scrapes while a run is live."""

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    url: str
    interval: str = "5s"


class Watch(_Strict):
    """A long-running command whose stdout lines the harness records."""

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    run: str


class Signal(_Strict):
    """An event a flow can wait for: a line of a watch that matches a regex."""

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    watch: str
    match: str

    @field_validator("match")
    @classmethod
    def _regex(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as err:
            raise ValueError(f"invalid regex {value!r}: {err}") from None
        return value


class KubernetesSettings(_Strict):
    # The kubectl context of every command the injector runs, with `${VAR}`
    # expanded at run time. Always explicit: the injector never falls back to
    # the current context, which may be a cluster nobody meant to touch.
    context: str


class Adapter(_Strict):
    api_version: Literal["chaos/v1alpha1"] = Field(alias="apiVersion")
    id: str
    backend: Literal["docker", "kubernetes"]
    # Sizes the system accepts; validate_adapter rejects experiments over them.
    limits: Limits
    # Required for the kubernetes backend.
    kubernetes: KubernetesSettings | None = None
    requires_env: list[str] = Field(default_factory=list)
    # Run after requires-env and before setup; any failure ends the run with
    # ERROR before anything is created.
    preflight: list[ShellCheck] = Field(default_factory=list)
    # Bash that runs at the start of every hook: shared settings and helpers.
    # Hooks are bash scripts written in the adapter, not paths.
    prelude: str = ""
    # Prints one JSON object describing the system under test, such as its
    # versions and image ids. It runs after preflight; the run records its
    # output verbatim as manifest.json `environment.system`.
    describe: str
    setup: str | None = None
    options: dict[str, OptionSpec] = Field(default_factory=dict)
    instance: InstanceHooks
    targets: Targets
    # The script and options of each adapter operation, by the name an
    # experiment `uses`. The script runs once per selected instance with the
    # instance's variables, or once for `scope: global` with the run's.
    operations: dict[str, OperationSpec] = Field(default_factory=dict)
    collect: str | None = None
    metrics: list[MetricsSource] = Field(default_factory=list)
    watch: list[Watch] = Field(default_factory=list)
    # Events from watch lines that a flow `wait: {signal: ...}` can wait for.
    signals: list[Signal] = Field(default_factory=list)
    checks: list[ShellCheck] = Field(default_factory=list)
    # Why a built-in operation is not available on this adapter, by name. An
    # experiment that uses it is reported as not applicable, with the reason.
    unavailable: dict[str, str] = Field(default_factory=dict)

    # Set by load_adapter.
    _path: Path = PrivateAttr(default=Path())
    _sha256: str = PrivateAttr(default="")

    @model_validator(mode="after")
    def _named_targets(self) -> Adapter:
        types = SUPPORTED_OPERATIONS[self.backend]
        targets = {
            **{f"targets.{k}": v for k, v in self.targets.named.items()},
            **{f"targets.roles.{k}": v for k, v in self.targets.roles.items()},
        }
        for where, target in targets.items():
            if target.type not in types:
                raise ValueError(
                    f"{where}: a {target.type} target is not available on "
                    f"{self.backend}; use {' or '.join(sorted(types))}"
                )
        return self

    @model_validator(mode="after")
    def _unavailable_builtins(self) -> Adapter:
        for name in self.unavailable:
            if name not in BUILTIN_OPERATIONS:
                raise ValueError(
                    f"unavailable.{name}: not a built-in operation; use one of "
                    f"{', '.join(sorted(BUILTIN_OPERATIONS))}"
                )
        return self

    @model_validator(mode="after")
    def _backend_settings(self) -> Adapter:
        if self.backend == "kubernetes" and self.kubernetes is None:
            raise ValueError("the kubernetes backend needs kubernetes.context")
        if self.backend != "kubernetes" and self.kubernetes is not None:
            raise ValueError(f"the {self.backend} backend takes no kubernetes settings")
        return self

    @model_validator(mode="after")
    def _unique_observers(self) -> Adapter:
        for kind, names in (
            ("metrics", [m.name for m in self.metrics]),
            ("watch", [w.name for w in self.watch]),
            ("signals", [s.name for s in self.signals]),
            ("preflight", [c.name for c in self.preflight]),
        ):
            if len(set(names)) != len(names):
                raise ValueError(f"{kind} names must be unique")
        watches = {w.name for w in self.watch}
        for s in self.signals:
            if s.watch not in watches:
                raise ValueError(f"signals.{s.name}.watch: no watch {s.watch!r}")
        for m in self.metrics:
            parse_duration(m.interval)
        return self

    @property
    def path(self) -> Path:
        return self._path

    @property
    def sha256(self) -> str:
        return self._sha256

    def target(self, name: str) -> Target:
        named = self.targets.named
        if name not in named:
            raise ExperimentError(f"adapter {self.id} has no target {name!r}")
        return named[name]

    def role(self, name: str) -> Target:
        if name not in self.targets.roles:
            raise ExperimentError(f"adapter {self.id} has no role {name!r}")
        return self.targets.roles[name]

    def resolve_options(self, options: dict[str, str], where: str) -> dict[str, str]:
        """Merge workload options with defaults and check them against the schema."""
        return resolve_options(self.options, options, where, self.id)


def resolve_options(
    specs: dict[str, OptionSpec], options: dict[str, str], where: str, owner: str
) -> dict[str, str]:
    """Merge options with their defaults and check them against `specs`."""
    for key in options:
        if key not in specs:
            raise ExperimentError(f"{where}: unknown option {key!r} for {owner}")
    resolved: dict[str, str] = {}
    for key, spec in specs.items():
        value = options.get(key, spec.default)
        if value is None:
            if spec.required:
                raise ExperimentError(f"{where}: option {key} is required")
            continue
        if spec.enum is not None and value not in spec.enum:
            raise ExperimentError(f"{where}: option {key}={value!r} not in {spec.enum}")
        resolved[key] = value
    return resolved


def _check_data(
    exp: Experiment, specs: dict[str, OptionSpec], options: dict[str, str], where: str
) -> None:
    for key, value in options.items():
        if specs[key].data and value and value not in exp.data:
            raise ExperimentError(f"{where}.{key}: no data entry named {value!r}")


def adapter_path(adapters_dir: Path, adapter_id: str) -> Path:
    return adapters_dir / f"{adapter_id}.yaml"


def load_adapter(adapters_dir: Path, adapter_id: str) -> Adapter:
    path = adapter_path(adapters_dir, adapter_id)
    if not path.is_file():
        raise ExperimentError(f"no adapter {adapter_id}: {path}")
    adapter = load_adapter_file(path)
    if adapter.id != adapter_id:
        raise ExperimentError(f"{path}: id {adapter.id!r} does not match the file name")
    return adapter


def load_adapter_file(path: Path) -> Adapter:
    """An adapter from any file, such as the copy in a run dir."""
    raw = read_yaml(path)
    try:
        adapter = Adapter.model_validate(raw)
    except ValidationError as err:
        raise ExperimentError(f"{path}:\n{format_validation_error(err)}") from None
    adapter._path = path
    adapter._sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    return adapter


def validate_adapter(exp: Experiment, adapter: Adapter) -> None:
    """Reject options and names the adapter can't accept, and raise
    NotApplicable, after every such check, if the adapter lacks an operation,
    a signal, a role or a target the experiment needs."""
    supported = SUPPORTED_OPERATIONS[adapter.backend]
    gaps: list[str] = []
    _check_limits(exp, adapter)
    for w in exp.workloads:
        where = f"workloads.{w.name}.options"
        options = adapter.resolve_options(w.options, where)
        _check_data(exp, adapter.options, options, where)
    for op in exp.operations:
        if op.builtin:
            continue
        spec = adapter.operations.get(op.uses)
        if spec is None:
            gaps.append(
                f"operations.{op.name}: {adapter.id} has no operation {op.uses!r}"
            )
            continue
        where = f"operations.{op.name}.options"
        owner = f"{op.uses} in {adapter.id}"
        options = resolve_options(spec.options, op.options, where, owner)
        _check_data(exp, spec.options, options, where)
    signals = {s.name for s in adapter.signals}
    for wait in signal_waits(exp.flow):
        if wait.signal not in signals:
            gaps.append(f"{wait.path}: {adapter.id} has no signal {wait.signal!r}")
    for check in exp.preflight:
        if check.name in {c.name for c in adapter.preflight}:
            raise ExperimentError(
                f"preflight.{check.name}: the adapter {adapter.id} already has a "
                "preflight check with this name"
            )
    for op in exp.operations:
        if not op.builtin:
            continue
        where = f"operations.{op.name}"
        try:
            if isinstance(op.target, RoleTarget):
                target = adapter.role(op.target.role)
                label = f"role {op.target.role}"
            else:
                assert isinstance(op.target, str)
                target = adapter.target(op.target)
                label = f"target {op.target}"
        except ExperimentError as err:
            gaps.append(f"{where}: {err}")
            continue
        if op.uses not in supported.get(target.type, set()):
            reason = adapter.unavailable.get(op.uses)
            gaps.append(
                f"{where}: {op.uses} on a {target.type} target ({label}) is not "
                f"supported on {adapter.backend}" + (f": {reason}" if reason else "")
            )
    if gaps:
        raise NotApplicable(gaps)


def _check_limits(exp: Experiment, adapter: Adapter) -> None:
    """Reject a `run` script or an `env` value the system would not accept."""
    limits = adapter.limits
    envs: list[tuple[str, dict[str, str]]] = []
    for w in exp.workloads:
        size = len(w.run.encode())
        if size > limits.run_script_bytes:
            raise ExperimentError(
                f"workloads.{w.name}.run: {size} bytes is over "
                f"limits.run-script-bytes ({limits.run_script_bytes}) of {adapter.id}"
            )
        envs.append((f"workloads.{w.name}.env", w.env))
        envs += [(f"workloads.{w.name}.checks.{c.name}.env", c.env) for c in w.checks]
    envs += [(f"checks.{c.name}.env", c.env) for c in exp.checks]
    envs += [(f"preflight.{c.name}.env", c.env) for c in exp.preflight]
    for where, env in envs:
        for key, value in env.items():
            size = len(value.encode())
            if size > limits.env_value_bytes:
                raise ExperimentError(
                    f"{where}.{key}: {size} bytes is over limits.env-value-bytes "
                    f"({limits.env_value_bytes}) of {adapter.id}"
                )


def missing_env(adapter: Adapter, environ: dict[str, str] | None = None) -> list[str]:
    environ = os.environ if environ is None else environ  # type: ignore[assignment]
    return [name for name in adapter.requires_env if not environ.get(name)]


def expand(text: str, env: dict[str, str]) -> str:
    """Expand `${VAR}` from env; a missing variable is an error."""

    def sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in env:
            raise ExperimentError(f"${{{name}}} is not set")
        return env[name]

    return VAR_RE.sub(sub, text)


@dataclass
class HookResult:
    code: int
    stdout: str
    stderr: str
    # The harness killed the command at its timeout; code is then 124.
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.code == 0


async def run_shell(
    argv: list[str],
    env: dict[str, str],
    timeout: float,
    cwd: Path | None = None,
) -> HookResult:
    """Run a command with the process environment plus env; never raises on exit code.

    The command gets its own session, so a terminal Ctrl-C reaches only the
    harness, and a timeout or cancel kills everything the command started.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        env={**os.environ, **env},
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    communicate = asyncio.ensure_future(proc.communicate())
    try:
        out, err = await asyncio.wait_for(asyncio.shield(communicate), timeout)
    except TimeoutError:
        out, err = await _kill_group(proc, communicate)
        return HookResult(
            124,
            out.decode(errors="replace"),
            err.decode(errors="replace") + f"\ntimed out after {timeout:g}s",
            timed_out=True,
        )
    except asyncio.CancelledError:
        await _kill_group(proc, communicate)
        raise
    return HookResult(
        proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")
    )


async def _kill_group(
    proc: asyncio.subprocess.Process, communicate: asyncio.Future[tuple[bytes, bytes]]
) -> tuple[bytes, bytes]:
    """SIGKILL the command's process group and return what it wrote."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    await proc.wait()
    try:
        return await asyncio.wait_for(asyncio.shield(communicate), 5)
    except TimeoutError:
        # A grandchild that left the group still holds the pipes.
        communicate.cancel()
        return b"", b""


def hook_script(adapter: Adapter, script: str) -> str:
    """The script a hook runs: the adapter's prelude, then the hook's own."""
    return f"{adapter.prelude}\n{script}" if adapter.prelude else script


async def run_hook(
    adapter: Adapter, name: str, script: str, env: dict[str, str], timeout: float = 300
) -> HookResult:
    """Run one of the adapter's hooks; `name` is its `$0` in bash messages.

    The hook gets `CHAOS_EXPERIMENT_HOOK_TIMEOUT`, the whole seconds it has
    before the harness kills it, so it can bound its own waits."""
    env = {**env, "CHAOS_EXPERIMENT_HOOK_TIMEOUT": f"{max(timeout, 1):.0f}"}
    return await run_shell(
        ["bash", "-c", hook_script(adapter, script), name], env, timeout
    )
