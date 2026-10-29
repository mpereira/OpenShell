# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Experiment model, loading, `--set` overrides and validation.

The model is generic: nothing here knows about OpenShell. What a system
supports is checked against its adapter and the injector capability table.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
METRIC_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
ISSUE_RE = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*|https?://\S+)$")
DURATION_RE = re.compile(r"^(\d+)(ms|s|m|h)$")
DURATION_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}

# The version of the experiment and adapter languages. An experiment or an
# adapter that names another is rejected; there is no compatibility code.
API_VERSION = "chaos/v1alpha1"

# The operations the harness runs itself, named by their effect on the target;
# each backend chooses the mechanism. Any other `uses` names an operation of
# the adapter, which runs its hook.
#   restart           stop the target, SIGTERM then SIGKILL after the grace
#                     period, and start it again
#   outage            stop the target, hold for the duration, start it again
#   kill              SIGKILL the target and leave recovery to the system
#   restart-in-place  the runtime stops the container and starts the same one
#   freeze            suspend every process of the target for the duration
ONE_SHOT_OPERATIONS = {"restart", "kill", "restart-in-place"}
DURATION_OPERATIONS = {"outage", "freeze"}
BUILTIN_OPERATIONS = ONE_SHOT_OPERATIONS | DURATION_OPERATIONS
# The names the built-ins had before they were named by effect, so runs
# recorded under them still evaluate and report.
LEGACY_OPERATION_NAMES = {
    "process-restart": "restart",
    "process-outage": "outage",
    "container-kill": "kill",
    "container-restart": "restart-in-place",
    "container-pause": "freeze",
}


def operation_name(uses: str) -> str:
    """The current name of an operation, given a current or legacy one."""
    return LEGACY_OPERATION_NAMES.get(uses, uses)


class ExperimentError(Exception):
    """An experiment, adapter or override that cannot be used."""


class NotApplicable(ExperimentError):
    """A valid experiment that the adapter has no way to run: it lacks an
    operation, a signal, a role or a target the experiment needs. A run
    reports N/A instead of failing."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


class RunError(Exception):
    """An ERROR-class event during a run; starts the abort path."""


def parse_duration(value: str) -> float:
    """Return the duration `<int>(ms|s|m|h)` in seconds."""
    match = DURATION_RE.match(value)
    if not match:
        raise ValueError(f"invalid duration {value!r}; want <int>(ms|s|m|h)")
    return int(match.group(1)) * DURATION_UNITS[match.group(2)]


def _check_duration(value: str | None) -> str | None:
    if value is not None:
        parse_duration(value)
    return value


def _check_name(value: str) -> str:
    if not NAME_RE.match(value):
        raise ValueError(f"name {value!r} must match [a-z0-9][a-z0-9-]*")
    return value


def _check_metric_name(value: str) -> str:
    if not METRIC_RE.match(value):
        raise ValueError(f"{value!r} is not a metric name")
    return value


def _check_env(env: dict[str, str]) -> dict[str, str]:
    for key, value in env.items():
        if key.startswith("CHAOS_"):
            raise ValueError(f"env name {key!r} must not start with CHAOS_")
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            raise ValueError(f"env name {key!r} is not a valid variable name")
        if "\n" in value:
            raise ValueError(f"env {key} must not contain a newline")
    return env


def kebab_case(name: str) -> str:
    """The YAML key of a field: experiment and adapter keys are kebab-case."""
    return name.replace("_", "-")


class Strict(BaseModel):
    model_config = ConfigDict(
        extra="forbid", coerce_numbers_to_str=True, alias_generator=kebab_case
    )


class CheckBase(Strict):
    name: str
    description: str
    env: dict[str, str] = Field(default_factory=dict)

    _name = field_validator("name")(_check_name)
    _env = field_validator("env")(_check_env)


class ExitCodeCheck(CheckBase):
    kind: Literal["exit-code"]
    equals: int


class ShellCheck(CheckBase):
    kind: Literal["shell"]
    run: str
    timeout: str = "60s"

    _timeout = field_validator("timeout")(_check_duration)


class MetricCheck(CheckBase):
    """Tests one value of a metric family the experiment lists under
    `metrics:`, computed from the scrapes the run recorded. Needs no shell."""

    kind: Literal["metric"]
    # The family, or `<family>_count`, `_sum` or `_bucket` of a histogram or
    # summary. The family must be under `metrics:`.
    metric: str
    # The adapter metrics source that exports the family. Empty: the one
    # source that recorded it.
    source: str = ""
    # Only series with every one of these labels count. Those series are
    # summed in each scrape before `value` is taken.
    labels: dict[str, str] = Field(default_factory=dict)
    # `last`, `min` and `max` are over the summed series' values; `increase`
    # is the sum of its positive steps, so a counter reset does not undo it.
    value: Literal["last", "min", "max", "increase"] = "last"
    equals: float | None = None
    at_least: float | None = None
    at_most: float | None = None

    _metric = field_validator("metric")(_check_metric_name)

    @model_validator(mode="after")
    def _bounds(self) -> MetricCheck:
        bounds = [self.equals, self.at_least, self.at_most]
        if all(b is None for b in bounds):
            raise ValueError("set equals, at-least or at-most")
        if self.equals is not None and bounds[1:] != [None, None]:
            raise ValueError("equals excludes at-least and at-most")
        if (
            self.at_least is not None
            and self.at_most is not None
            and self.at_least > self.at_most
        ):
            raise ValueError("at-least is over at-most")
        return self

    def wants(self) -> str:
        """`at-least 8`, `at-most 0` or `equals 1`, as in a result's output."""
        if self.equals is not None:
            return f"equals {self.equals:g}"
        parts = []
        if self.at_least is not None:
            parts.append(f"at-least {self.at_least:g}")
        if self.at_most is not None:
            parts.append(f"at-most {self.at_most:g}")
        return " and ".join(parts)

    def holds(self, value: float) -> bool:
        if self.equals is not None:
            return value == self.equals
        return (self.at_least is None or value >= self.at_least) and (
            self.at_most is None or value <= self.at_most
        )


Check = Annotated[ExitCodeCheck | ShellCheck, Field(discriminator="kind")]
ExperimentCheck = Annotated[ShellCheck | MetricCheck, Field(discriminator="kind")]


class Workload(Strict):
    name: str
    description: str
    kind: Literal["shell"]
    instances: int = Field(ge=1)
    timeout: str
    env: dict[str, str] = Field(default_factory=dict)
    options: dict[str, str] = Field(default_factory=dict)
    run: str
    checks: list[Check] = Field(min_length=1)

    _name = field_validator("name")(_check_name)
    _env = field_validator("env")(_check_env)
    _timeout = field_validator("timeout")(_check_duration)

    @field_validator("checks")
    @classmethod
    def _unique_checks(cls, checks: list[Check]) -> list[Check]:
        _require_unique([c.name for c in checks], "check")
        return checks


class RoleTarget(Strict):
    workload: str
    role: str


class WorkloadTarget(Strict):
    workload: str


class SelectCount(Strict):
    count: int = Field(ge=1)


# Which of the candidate targets an operation acts on.
Select = Literal["one", "all"] | SelectCount


class Operation(Strict):
    """A flow step that acts on the system under test.

    `uses` names a built-in (`BUILTIN_OPERATIONS`), which the harness runs and
    undoes itself, or an operation of the adapter, which runs the adapter's
    hook and is not undone. "Fault" is the prose word for a disruptive one.

    A built-in targets a named target of the adapter (a string) or a role of a
    workload's instances (`{workload, role}`). An adapter operation targets a
    workload's instances (`{workload}`) or, with `scope: global`, the whole
    system. `select` picks among the instances.
    """

    name: str
    description: str
    uses: str
    target: str | RoleTarget | WorkloadTarget | None = None
    scope: Literal["global"] | None = None
    select: Select | None = None
    # How long an outage or a freeze holds the fault.
    duration: str | None = None
    # How long restart waits after SIGTERM before SIGKILL.
    grace_period: str | None = None
    # Checked against the options the adapter declares for the operation.
    options: dict[str, str] = Field(default_factory=dict)

    _name = field_validator("name")(_check_name)
    _uses = field_validator("uses")(_check_name)

    @field_validator("uses", mode="before")
    @classmethod
    def _current_name(cls, value: object) -> object:
        return operation_name(value) if isinstance(value, str) else value

    _duration = field_validator("duration", "grace_period")(_check_duration)

    @model_validator(mode="after")
    def _rules(self) -> Operation:
        if (self.target is None) == (self.scope is None):
            raise ValueError("set exactly one of target and scope")
        if self.scope is not None and self.select is not None:
            raise ValueError("scope global takes no select")
        if self.target is not None and self.select is None:
            raise ValueError("target requires select")
        if self.builtin:
            self._builtin_rules()
        else:
            if self.scope is None and not isinstance(self.target, WorkloadTarget):
                raise ValueError(
                    f"adapter operation {self.uses} takes target {{workload}} "
                    "or scope global"
                )
            if self.duration is not None or self.grace_period is not None:
                raise ValueError(
                    "duration and grace-period are only for built-in operations"
                )
        return self

    def _builtin_rules(self) -> None:
        if self.scope is not None or isinstance(self.target, WorkloadTarget):
            raise ValueError(f"{self.uses} takes a named target or {{workload, role}}")
        if self.options:
            raise ValueError(f"{self.uses} takes no options")
        if self.uses in ONE_SHOT_OPERATIONS and self.duration is not None:
            raise ValueError(f"{self.uses} is one-shot and takes no duration")
        if self.uses in DURATION_OPERATIONS and self.duration is None:
            raise ValueError(f"{self.uses} requires duration")
        if self.grace_period is not None and self.uses != "restart":
            raise ValueError("grace-period is only valid for restart")

    @property
    def builtin(self) -> bool:
        return self.uses in BUILTIN_OPERATIONS

    @property
    def workload(self) -> str | None:
        """The workload whose instances the operation targets, if any."""
        if isinstance(self.target, RoleTarget | WorkloadTarget):
            return self.target.workload
        return None

    @property
    def duration_seconds(self) -> float | None:
        return parse_duration(self.duration) if self.duration else None

    @property
    def grace_seconds(self) -> float | None:
        return parse_duration(self.grace_period) if self.grace_period else None


class Metric(Strict):
    """An entry of `metrics:`. A `family` is a metric family the adapter's
    metrics sources export: the report and the dashboard chart it, and
    `metric` checks can test it. An `expr` is raw PromQL for a dashboard panel
    only (scraped series carry `chaos_run_id`); the report skips it."""

    family: str = ""
    # Only with expr.
    title: str = ""
    expr: str = ""
    # Shown under the chart: why the metric matters to the experiment.
    description: str = ""

    @model_validator(mode="after")
    def _one_of(self) -> Metric:
        if bool(self.family) == bool(self.expr):
            raise ValueError("set exactly one of family and expr")
        if self.family:
            if not METRIC_RE.match(self.family):
                raise ValueError(f"{self.family!r} is not a metric family name")
            if self.title:
                raise ValueError("title is only for expr; a family is its own title")
        elif not self.title:
            raise ValueError("expr requires title")
        return self


def _metric_entry(raw: Any) -> Any:
    """A bare string is a family."""
    return {"family": raw} if isinstance(raw, str) else raw


MetricEntry = Annotated[Metric, BeforeValidator(_metric_entry)]


def metric_family(series: str, families: set[str]) -> str | None:
    """The family in `families` that the series name `series` belongs to:
    itself, or the family of a `_count`, `_sum` or `_bucket` series."""
    if series in families:
        return series
    for suffix in ("_count", "_sum", "_bucket"):
        if series.endswith(suffix) and series[: -len(suffix)] in families:
            return series[: -len(suffix)]
    return None


# Flow nodes. Each node gets a stable id (its path in the tree) so the
# timeline can name it.


@dataclass
class Node:
    path: str = field(default="", kw_only=True)


@dataclass
class Serial(Node):
    children: list[Node]


@dataclass
class Parallel(Node):
    children: list[Node]


@dataclass
class Delay(Node):
    duration: str

    @property
    def seconds(self) -> float:
        return parse_duration(self.duration)


@dataclass
class Wait(Node):
    """Returns once every instance of the workload has started. An ERROR if the
    workload ends before that."""

    workload: str
    condition: Literal["started"] = "started"


@dataclass
class SignalWait(Node):
    """Returns at e + delay for the next event e of an adapter signal: the first
    event newer than the one this node last returned for whose e + delay is not
    in the past. An ERROR if none arrives within `timeout` of the wait start."""

    signal: str
    delay: str | None = None
    timeout: str = "5m"

    @property
    def delay_seconds(self) -> float:
        return parse_duration(self.delay) if self.delay else 0.0

    @property
    def timeout_seconds(self) -> float:
        return parse_duration(self.timeout)


@dataclass
class Repeat(Serial):
    """Runs its children in order, `times` times or, with `duration`, until
    that long has passed since the first run started."""

    times: int | None = None
    duration: str | None = None

    @property
    def seconds(self) -> float | None:
        return parse_duration(self.duration) if self.duration else None


@dataclass
class Ref(Node):
    """A workload or operation, named in the flow."""

    name: str


def parse_flow(raw: Any, path: str = "flow") -> Node:
    if isinstance(raw, str):
        return Ref(raw, path=path)
    if not isinstance(raw, dict) or len(raw) != 1:
        raise ValueError(
            f"{path}: a node is a name or a mapping with exactly one key "
            "(serial, parallel, delay, wait, repeat)"
        )
    ((key, value),) = raw.items()
    if key in ("serial", "parallel"):
        if not isinstance(value, list) or not value:
            raise ValueError(f"{path}.{key}: want a non-empty list")
        children = [parse_flow(c, f"{path}.{key}[{i}]") for i, c in enumerate(value)]
        cls = Serial if key == "serial" else Parallel
        return cls(children, path=f"{path}.{key}")
    if key == "delay":
        if not isinstance(value, str):
            raise ValueError(f"{path}.delay: want a duration string")
        try:
            parse_duration(value)
        except ValueError as err:
            raise ValueError(f"{path}.delay: {err}") from None
        return Delay(value, path=f"{path}.delay")
    if key == "wait":
        where = f"{path}.wait"
        if isinstance(value, dict) and "signal" in value:
            return _parse_signal_wait(value, where)
        if isinstance(value, dict) and "workload" in value:
            return _parse_workload_wait(value, where)
        raise ValueError(
            f"{where}: want {{workload: <name>, condition: started}} or "
            "{signal: <name>, delay: <duration>, timeout: <duration>}"
        )
    if key == "repeat":
        where = f"{path}.repeat"
        if not isinstance(value, dict) or set(value) not in (
            {"times", "serial"},
            {"for", "serial"},
        ):
            raise ValueError(
                f"{where}: want {{times: <n>, serial: [...]}} or "
                "{for: <duration>, serial: [...]}"
            )
        times = value.get("times")
        if "times" in value and (
            not isinstance(times, int) or isinstance(times, bool) or times < 1
        ):
            raise ValueError(f"{where}.times: want a positive integer")
        duration = value.get("for")
        if "for" in value:
            try:
                parse_duration(str(duration))
            except ValueError as err:
                raise ValueError(f"{where}.for: {err}") from None
        body = parse_flow({"serial": value["serial"]}, where)
        return Repeat(body.children, times=times, duration=duration, path=where)
    raise ValueError(f"{path}: unknown flow node {key!r}")


def _parse_workload_wait(value: dict[str, Any], where: str) -> Wait:
    unknown = set(value) - {"workload", "condition"}
    if unknown:
        raise ValueError(f"{where}: unknown keys {sorted(unknown)}")
    workload = value["workload"]
    if not isinstance(workload, str) or not NAME_RE.match(workload):
        raise ValueError(f"{where}.workload: want a workload name")
    if value.get("condition") != "started":
        raise ValueError(f"{where}.condition: want started")
    return Wait(workload, path=where)


def _parse_signal_wait(value: dict[str, Any], where: str) -> SignalWait:
    unknown = set(value) - {"signal", "delay", "timeout"}
    if unknown:
        raise ValueError(f"{where}: unknown keys {sorted(unknown)}")
    signal = value.get("signal")
    if not isinstance(signal, str) or not NAME_RE.match(signal):
        raise ValueError(f"{where}.signal: want a signal name")
    for key in ("delay", "timeout"):
        if key in value:
            try:
                parse_duration(str(value[key]))
            except ValueError as err:
                raise ValueError(f"{where}.{key}: {err}") from None
    return SignalWait(
        signal,
        delay=value.get("delay"),
        timeout=value.get("timeout", "5m"),
        path=where,
    )


def signal_waits(node: Node) -> list[SignalWait]:
    if isinstance(node, SignalWait):
        return [node]
    if isinstance(node, Serial | Parallel):
        return [w for child in node.children for w in signal_waits(child)]
    return []


class Expectation(Strict):
    """A known failure: an issue that makes some checks fail. `checks` names
    them: `<workload>/<check>` or `experiment/<check>`. Empty means the
    experiment is a regression test for the issue and every check must pass.

    `instances: affected` limits workload checks to the instances an operation
    acted on during the run, as the timeline records them; `all` covers every
    instance. A strict expectation fails the run when none of the results it
    covers fails, because the bug may be fixed. A flaky one is racy, so its
    checks may pass or fail."""

    # An issue URL or <owner>/<repo>#<number>.
    issue: str
    checks: list[str] = Field(default_factory=list)
    kind: Literal["strict", "flaky"] = "strict"
    instances: Literal["affected", "all"] = "all"

    @field_validator("issue")
    @classmethod
    def _issue(cls, value: str) -> str:
        if not ISSUE_RE.match(value):
            raise ValueError(
                f"issue {value!r} must be a URL or look like <owner>/<repo>#<number>"
            )
        return value


def check_key(scope: str, workload: str | None, check: str) -> str:
    """The name an `expectations[].checks` entry uses for a check result."""
    return f"{workload or scope}/{check}"


class Experiment(Strict):
    model_config = ConfigDict(
        extra="forbid", coerce_numbers_to_str=True, arbitrary_types_allowed=True
    )

    api_version: Literal["chaos/v1alpha1"] = Field(alias="apiVersion")
    id: str
    version: int
    title: str
    hypothesis: str
    description: str
    # The adapter that runs the experiment unless `--adapter` names another:
    # adapters/<default-adapter>.yaml.
    default_adapter: str
    timeout: str = "30m"
    workloads: list[Workload] = Field(min_length=1)
    operations: list[Operation] = Field(default_factory=list)
    # Named values the harness passes to the system without reading them: text,
    # a number, or a document in the system's own format. Each is written to
    # `data/<name>` in the run dir; adapter options declared `data: true` name
    # an entry and reach hooks as its file's path.
    data: dict[str, Any] = Field(default_factory=dict)
    flow: Node
    checks: list[ExperimentCheck] = Field(default_factory=list)
    # Run before anything is created, after the adapter's preflight checks.
    preflight: list[ShellCheck] = Field(default_factory=list)
    # The metrics that matter to the experiment: family names the adapter's
    # metrics sources export (the report and the dashboard chart them; the
    # preflight checks they are exported; `metric` checks test them) and raw
    # PromQL panels for the dashboard. The run still records every family.
    # Empty: the report charts every family the run scraped and the dashboard
    # shows every chart of the run.
    metrics: list[MetricEntry] = Field(default_factory=list)
    expectations: list[Expectation] = Field(default_factory=list)

    _id = field_validator("id")(_check_name)
    _default_adapter = field_validator("default_adapter")(_check_name)
    _timeout = field_validator("timeout")(_check_duration)

    @field_validator("flow", mode="before")
    @classmethod
    def _parse_flow(cls, raw: Any) -> Node:
        if isinstance(raw, Node):
            return raw
        return parse_flow(raw)

    @model_validator(mode="after")
    def _references(self) -> Experiment:
        validate_references(self)
        return self

    def workload(self, name: str) -> Workload:
        return next(w for w in self.workloads if w.name == name)

    def operation(self, name: str) -> Operation:
        return next(o for o in self.operations if o.name == name)

    def known_failure(self, key: str) -> Expectation | None:
        """The expectation that the check `key` fails."""
        return next((e for e in self.expectations if key in e.checks), None)

    @property
    def metric_families(self) -> list[str]:
        """The families under `metrics:`, in order."""
        return [m.family for m in self.metrics if m.family]


def _require_unique(names: list[str], what: str) -> None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise ValueError(f"duplicate {what} name {name!r}")
        seen.add(name)


@dataclass
class _Placed:
    node: Node
    # (container node, child index) from the root down to the node.
    ancestry: list[tuple[Node, int]]


def _walk(node: Node, ancestry: list[tuple[Node, int]], out: list[_Placed]) -> None:
    out.append(_Placed(node, ancestry))
    if isinstance(node, Serial | Parallel):
        for i, child in enumerate(node.children):
            _walk(child, [*ancestry, (node, i)], out)


def _split(a: _Placed, b: _Placed) -> tuple[Node, int, int] | None:
    """Return the deepest common container of a and b and each one's branch."""
    for (na, ia), (nb, ib) in zip(a.ancestry, b.ancestry, strict=False):
        if na is not nb:
            break
        if ia != ib:
            return na, ia, ib
    return None


def _validate_affected(exp: Experiment, i: int, e: Expectation) -> None:
    """`instances: affected` covers workload checks of workloads that an
    operation targets."""
    where = f"expectations.{i}"
    listed = {key.split("/", 1)[0] for key in e.checks}
    if not listed <= {w.name for w in exp.workloads}:
        raise ValueError(
            f"{where}: only workload checks run per instance; list experiment "
            "checks in an expectation with instances: all"
        )
    untargeted = sorted(listed - {o.workload for o in exp.operations})
    if untargeted:
        raise ValueError(
            f"{where}.instances: no operation targets {untargeted[0]} instances, "
            "so none are affected"
        )


def validate_references(exp: Experiment) -> None:
    workloads = [w.name for w in exp.workloads]
    named = workloads + [o.name for o in exp.operations]
    _require_unique(named, "workload/operation")
    _require_unique([c.name for c in exp.checks], "experiment check")
    _require_unique([c.name for c in exp.preflight], "preflight check")
    _require_unique(exp.metric_families, "metric family")
    _require_unique([m.title for m in exp.metrics if m.expr], "metric panel title")
    families = set(exp.metric_families)
    for i, c in enumerate(exp.checks):
        if isinstance(c, MetricCheck) and metric_family(c.metric, families) is None:
            raise ValueError(
                f"checks.{i}.metric: {c.metric!r} is not a family under metrics: "
                "(or its _count, _sum or _bucket series)"
            )
    for name in exp.data:
        if not NAME_RE.match(name):
            raise ValueError(f"data.{name}: want lowercase letters, digits, '-'")
    _require_unique([e.issue for e in exp.expectations], "expectation issue")
    keys = {
        check_key("workload", w.name, c.name) for w in exp.workloads for c in w.checks
    }
    keys |= {check_key("experiment", None, c.name) for c in exp.checks}
    claimed: set[str] = set()
    for i, e in enumerate(exp.expectations):
        for key in e.checks:
            if key not in keys:
                raise ValueError(
                    f"expectations.{i}.checks: no check {key!r}; want "
                    "<workload>/<check> or experiment/<check>"
                )
            if key in claimed:
                raise ValueError(
                    f"expectations.{i}.checks: {key!r} is in more than one expectation"
                )
            claimed.add(key)
        if e.instances == "affected":
            _validate_affected(exp, i, e)

    placed: list[_Placed] = []
    _walk(exp.flow, [], placed)

    refs: dict[str, _Placed] = {}
    for p in placed:
        if isinstance(p.node, Ref):
            if p.node.name not in named:
                raise ValueError(
                    f"{p.node.path}: unknown workload or operation {p.node.name!r}"
                )
            if p.node.name in refs:
                raise ValueError(
                    f"{p.node.path}: {p.node.name!r} appears more than once"
                )
            refs[p.node.name] = p
            # A repeat runs adapter operations again; a workload or a built-in
            # operation runs once.
            repeats = {o.name for o in exp.operations if not o.builtin}
            if p.node.name not in repeats and any(
                isinstance(n, Repeat) for n, _ in p.ancestry
            ):
                raise ValueError(
                    f"{p.node.path}: only adapter operations can be in a repeat, "
                    f"not {p.node.name!r}"
                )
    for name in named:
        if name not in refs:
            raise ValueError(f"flow: {name!r} is never used")

    waits = [p for p in placed if isinstance(p.node, Wait)]
    for w in waits:
        target = w.node.workload  # type: ignore[attr-defined]
        if target not in workloads:
            raise ValueError(f"{w.node.path}: unknown workload {target!r}")
        split = _split(w, refs[target])
        if split is None or not isinstance(split[0], Parallel):
            raise ValueError(
                f"{w.node.path}: wait on {target!r} must be in a sibling branch of "
                f"a parallel that runs {target!r}"
            )

    # Operations on a workload's instances run while they exist.
    for o in exp.operations:
        wl = o.workload
        if wl is None:
            continue
        if wl not in workloads:
            raise ValueError(f"operations.{o.name}.target: unknown workload {wl!r}")
        step = refs[o.name]
        split = _split(step, refs[wl])
        if split is None or not isinstance(split[0], Parallel):
            raise ValueError(
                f"operations.{o.name}: an operation on {wl!r} instances must be "
                f"in a parallel with {wl!r}"
            )
        if not any(_waits_before(w, step) for w in waits if w.node.workload == wl):  # type: ignore[attr-defined]
            raise ValueError(
                f"operations.{o.name}: must come after "
                f"wait: {{workload: {wl}, condition: started}}"
            )


def _waits_before(wait: _Placed, step: _Placed) -> bool:
    split = _split(wait, step)
    return split is not None and isinstance(split[0], Serial) and split[1] < split[2]


# Loading and overrides.


def read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as err:
        raise ExperimentError(f"{path}: {err}") from None
    if not isinstance(data, dict):
        raise ExperimentError(f"{path}: want a mapping at the top level")
    return data


def apply_overrides(raw: dict[str, Any], overrides: list[str]) -> dict[str, Any]:
    """Apply `path=value` overrides. List items are addressed by `name`."""
    data = copy.deepcopy(raw)
    for override in overrides:
        dotted, sep, value = override.partition("=")
        if not sep or not dotted:
            raise ExperimentError(f"--set {override!r}: want path=value")
        keys = dotted.split(".")
        node: Any = data
        for i, key in enumerate(keys):
            last = i == len(keys) - 1
            where = ".".join(keys[: i + 1])
            if isinstance(node, list):
                matches = [
                    x for x in node if isinstance(x, dict) and x.get("name") == key
                ]
                if not matches:
                    raise ExperimentError(
                        f"--set {dotted}: no item named {key!r} at {where}"
                    )
                if last:
                    raise ExperimentError(
                        f"--set {dotted}: cannot replace a whole list item"
                    )
                node = matches[0]
            elif isinstance(node, dict):
                if last:
                    node[key] = yaml.safe_load(value) if value else ""
                else:
                    if key not in node:
                        node[key] = {}
                    node = node[key]
            else:
                parent = ".".join(keys[:i])
                raise ExperimentError(
                    f"--set {dotted}: {parent} is not a mapping or list"
                )
    return data


def format_validation_error(err: ValidationError) -> str:
    lines = []
    for e in err.errors():
        loc = ".".join(str(p) for p in e["loc"]) or "<experiment>"
        lines.append(f"  {loc}: {e['msg']}")
    return "\n".join(lines)


def parse_experiment(raw: dict[str, Any], source: str) -> Experiment:
    try:
        return Experiment.model_validate(raw)
    except ValidationError as err:
        raise ExperimentError(f"{source}:\n{format_validation_error(err)}") from None


def load_experiment(
    path: Path, overrides: list[str] | None = None
) -> tuple[Experiment, dict[str, Any]]:
    """Load an experiment file; return the model and the resolved raw mapping."""
    raw = apply_overrides(read_yaml(path), overrides or [])
    exp = parse_experiment(raw, str(path))
    if exp.id != path.stem:
        raise ExperimentError(
            f"{path}: id {exp.id!r} must equal the file name {path.stem!r}"
        )
    return exp, raw
