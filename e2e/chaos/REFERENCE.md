<!-- SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Chaos experiment reference

The experiment format, the verdict, the environment variables, the run
directory and the commands. [README.md](README.md) explains how to use them
and defines the [terms](README.md#terms); [ADAPTER.md](ADAPTER.md) is the
adapter contract.

- [Experiment](#experiment): [Workloads](#workloads), [Checks](#checks),
  [Operations](#operations), [Built-in faults](#built-in-faults),
  [Adapter operations](#adapter-operations), [Data](#data), [Flow](#flow),
  [Expectations](#expectations), [Preflight](#preflight)
- [Verdict](#verdict)
- [Environment variables](#environment-variables),
  [Hook variables](#hook-variables)
- [Run directory](#run-directory), [Timeline events](#timeline-events),
  [verdict.json](#verdictjson)
- [Commands](#commands), [Signals](#signals)

## Experiment

Keys are kebab-case, in experiments and in adapters: `grace-period`,
`kind: exit-code`, `select: {count: 1}`. Keys inside `data` and `env` are not
the harness's and keep their own names. Names match `[a-z0-9][a-z0-9-]*`.
Durations are `<int>(ms|s|m|h)`. Unknown keys are errors.

| Field | Meaning |
|---|---|
| `apiVersion` | `chaos/v1alpha1`, the version of this language. Any other value is rejected. |
| `id` | Equals the file name without `.yaml`. |
| `version` | Increase it when the meaning of the experiment changes. |
| `title`, `hypothesis`, `description` | For people. Follow [Writing descriptions](README.md#writing-descriptions). |
| `default-adapter` | The adapter it runs on, `adapters/<default-adapter>.yaml`, such as `openshell-docker`, unless `--adapter` names another. |
| `timeout` | Limit on the flow, default `30m`. Expiry aborts the run with `ERROR`. Cleanup, collection and checks run after it, each bounded by its own hook or check timeout. |
| `workloads[]` | See [Workloads](#workloads). At least one. |
| `operations[]` | See [Operations](#operations). |
| `data` | Named values for the system. See [Data](#data). |
| `flow` | The order of workloads and operations. See [Flow](#flow). |
| `checks[]` | Experiment checks, `kind: shell` or `kind: metric`. See [Checks](#checks). |
| `preflight[]` | Shell checks that run before anything is created. See [Preflight](#preflight). |
| `expectations[]` | Known failures. See [Expectations](#expectations). |
| `metrics[]` | The metrics the experiment is about. See [Metrics](#metrics). |

### Workloads

| Field | Meaning |
|---|---|
| `name`, `description` | The workload's name and what it does. |
| `kind` | `shell`. |
| `instances` | How many instances, at least 1. They are named `<workload>-0`, `<workload>-1` and so on. |
| `timeout` | One deadline for all the instances, counted from when the workload node starts. |
| `env` | Variables for the script and its checks. Names must not start with `CHAOS_`. Each value is at most the adapter's `limits.env-value-bytes`. |
| `options` | Adapter options, checked against the adapter's `options`. |
| `run` | A POSIX `sh` script that runs in each instance, at most the adapter's `limits.run-script-bytes`. |
| `checks[]` | Workload checks. At least one. |

The harness wraps `run`. The wrapper appends a timestamp to
`$CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_OUTPUT_DIR/.chaos/attempts` before each
start and writes the exit code to `.chaos/exit_code`, so restarts and exit
codes do not depend on the system. `.chaos/` is reserved.

A workload node ends when every instance has ended. The harness polls each
instance's state every 2s and collects and deletes an instance as soon as it
ends: first the `collect` hook, then `delete`; a failed collect still deletes,
then ends the run with `ERROR`. When the workload `timeout` expires, every
instance that has not ended gets an `instance.timed_out` event and is
collected and deleted.

### Checks

| Kind | Fields | Meaning |
|---|---|---|
| `exit-code` | `name`, `description`, `equals`, `env` | The exit code of the last attempt equals `equals`. No recorded exit code fails the check. Workload checks only. |
| `shell` | `name`, `description`, `run`, `timeout` (default `60s`), `env` | `run` runs with bash on the host, from the run directory, with `env` added to its environment. Exit 0 passes. A timeout, a signal, or exit 126 or 127 is a checker error. |
| `metric` | `name`, `description`, `metric`, `labels`, `source`, `value` (default `last`), `equals`, `at-least`, `at-most` | A value of a recorded metric is within bounds. `metric` is a series name under `metrics:`, or its `_count`, `_sum` or `_bucket` series; `labels` picks the series with those label values, and the matching series are summed per scrape. `value` is `last`, `min`, `max` or `increase` (last minus first) over the scrapes that answered. Set `equals`, or `at-least` and/or `at-most`. `source` names the adapter metrics source; without it the series must be in exactly one source. No matching series in any scrape fails the check; no scrapes of the source is a checker error. Experiment checks only. |

A workload check runs once for each instance of its workload, as the result
`<instance>/<check>`, for example `counter-0/exited-ok`. An experiment check
runs once per run, as `experiment/<check>`. Checks only read the run
directory, so `chaos evaluate` can run them after the environment is gone.

### Operations

An operation does something to the system during the flow. `uses` names a
built-in fault, which the harness runs, or an operation of the adapter, which
the adapter's hook runs.

| Field | Meaning |
|---|---|
| `name`, `description` | The operation's name and what it does. |
| `uses` | A [built-in fault](#built-in-faults), or an operation in the adapter's `operations`. |
| `target` | What it acts on. A built-in takes a named adapter target, such as `gateway`, or `{workload, role}` for a role of a workload's instances. An adapter operation takes `{workload}`. |
| `scope` | `global`: an adapter operation that acts on the whole system. Set exactly one of `target` and `scope`. |
| `select` | Which candidates of the target it acts on. Required with `target`; not allowed with `scope`. |
| `duration` | How long a duration fault holds. Required for those; not allowed otherwise. |
| `grace-period` | `restart` only: how long the target has to stop after SIGTERM, or after `kubectl delete`, before it is killed. Default 0. |
| `options` | Adapter operation options, checked against the adapter. Built-ins take none. |

`select` is `one`, `all` or `{count: N}`. The candidates are sorted by
instance index, role and id, then picked by a generator seeded with `--seed`
and the operation name. A rerun with the same seed and the same instance
counts picks the same candidates. No candidate, or a count above the number
of candidates, ends the run with `ERROR`.

#### Built-in faults

A built-in fault is named by its effect, and each backend chooses the
mechanism for each [target type](ADAPTER.md#targets) it supports. The
`docker` backend has `process` and `container` targets; the `kubernetes`
backend has `workload` and `pod` targets.

| Fault | Kind | `process` (docker) | `container` (docker) | `workload` (kubernetes) | `pod` (kubernetes) |
|---|---|---|---|---|---|
| `restart` | one-shot | SIGKILL, then the `start` hook; with a `grace-period`, SIGTERM first, and SIGKILL only if the process is still alive when the period ends | not supported | `kubectl delete pod` of the running pods with `--grace-period` set to the `grace-period`, or `--grace-period=0 --force` without one, then `start` | not supported |
| `outage` | `duration` | SIGKILL, hold `duration`, then `start` | not supported | `kubectl scale` the resource to 0 and force-delete its pods, hold `duration`, scale back, then `start` | not supported |
| `kill` | one-shot | not supported | `docker kill -s KILL` | not supported | `kubectl delete pod --grace-period=0 --force` |
| `restart-in-place` | one-shot | not supported | `docker restart`: SIGTERM, SIGKILL after the container's stop timeout (10s unless the image sets one), then start the same container | not supported | not supported |
| `freeze` | `duration` | SIGSTOP, then SIGCONT | `docker pause`, then `docker unpause` | not supported | not supported |

Kubernetes has no portable way to signal one process in a container, so its
faults delete pods, and a deleted pod comes back only if its controller
replaces it. An adapter's `unavailable` block says why a built-in is missing
on it, and that reason is part of the message when an experiment that uses
the fault is reported as not applicable there; see
[Commands](#commands). Every `kubectl` call names the adapter's
`kubernetes.context`, never the current context.

A built-in records `operation.started`, `operation.applied` when the fault is
in place, `operation.reverted` when a duration fault is undone, and
`operation.completed` with `outcome: ok`. After `restart` and `outage`, the
target's `start` hook brings the target back and the harness records
`target.restarted`, then runs the target's `after-recovery` hook if it has
one. The outage lasts from `operation.applied` to `target.restarted`, so it
is longer than `duration`. `freeze` on a process target sends SIGCONT and
records `operation.reverted`, without either hook.

The faults were named after their mechanism before: `process-restart`,
`process-outage`, `container-kill`, `container-restart` and `container-pause`
are read as `restart`, `outage`, `kill`, `restart-in-place` and `freeze`, so
runs recorded under them still evaluate and report. New experiments use the
new names.

`restart` with `grace-period` above 0 on the OpenShell gateway is a graceful
restart. The gateway's graceful-stop sweep stops every sandbox, and the
restarted gateway starts each one again, so the script runs a second attempt
whatever its `restart-policy`.

#### Adapter operations

An adapter operation's hook runs once for each selected instance, in parallel,
or once for `scope: global`. It returns once the system reports the change in
effect. It records `operation.started`, then one `operation.completed` per
instance (one for `scope: global`) with `outcome` and `reason`, or
`operation.error`.

The hook writes its outcome to `$CHAOS_EXPERIMENT_OPERATION_OUTCOME_FILE`:
`ok`, `skipped` or `failed`, then an optional reason. A `failed` outcome is a
result for the checks. A hook that exits non-zero or writes no outcome is a
harness error and ends the run with `ERROR`.

While the hook runs, a terminal state of an instance it acts on does not
finish the instance, because the operation can cause it on purpose, as the
stop in `openshell-sandbox-restart` does. The state is still recorded, and the instance
is finished only if a poll after the hook returns reads it terminal again.

Inside a `repeat`, an adapter operation runs once per iteration, and its
events carry `run`, counting from 1.

Every run with outcome `ok` or `failed` is reverted with the adapter's
`revert` hook, newest first, at the end of the flow (`operation.reverted`
with `abort: false`) and on abort (`abort: true`). A run whose instance is
already deleted is not reverted, and its `operation.reverted` says so in
`skipped`. A `revert` that exits non-zero emits `operation.error` and the run
ends with `ERROR`. An adapter operation with `revert: none` changes nothing
that outlasts the run, and the adapter says why; see
[ADAPTER.md](ADAPTER.md#operations).

Both OpenShell adapters have `openshell-policy-set` and `openshell-sandbox-restart`; see
[ADAPTER.md](ADAPTER.md#openshell-docker).

### Data

`data` maps a name (matching `[a-z0-9][a-z0-9-]*`) to a value that the
harness passes to the system without reading it. The harness writes each one
to `data/<name>` in the run directory:
a string as it is, a number or boolean as JSON, and a mapping or list as YAML.
An adapter option declared `data: true` takes an entry's name and reaches the
hooks as that file's path. Data reaches hooks and checks on the host. A
workload script sees it only if an adapter hook passes it into the sandbox.

### Flow

A node is a workload or operation name, or a mapping with one key:

| Node | Meaning |
|---|---|
| `serial: [...]` | Runs the children in order. |
| `parallel: [...]` | Runs the children together. An error in one branch cancels the others. |
| `delay: 10s` | Waits. |
| `wait: {workload: W, condition: started}` | Blocks until every instance of `W` has been observed started. It must be in a sibling branch of a `parallel` that runs `W`. |
| `wait: {signal: S, delay: 55s, timeout: 3m}` | Blocks until the next event of the adapter signal `S` and returns `delay` after it (default 0s). |
| `repeat: {times: 3, serial: [...]}` | Runs the `serial` children 3 times. |
| `repeat: {for: 20m, serial: [...]}` | Runs the children again until 20m have passed since the first run started; a run that starts before then finishes. |

A signal wait returns for an event newer than the one it last returned for,
and only if the event's time plus `delay` is not in the past, so inside a
`repeat` each run waits for a new event. With no `delay` it returns on the
first event after the wait starts. An event's time is when the harness read
the line. No event within `timeout` (default 5m) ends the run with `ERROR`.
Each return records `signal.waited` with the event's `signal_t`. Each run of
a `repeat` records `repeat.iteration`.

Placement rules:

- Every workload and operation appears in the flow exactly once.
- A `repeat` holds adapter operations, `delay`, `wait`, and nested `serial`,
  `parallel` or `repeat`. It holds no workload and no built-in fault.
- An operation on a workload's instances must be in a `parallel` with that
  workload and come after a `wait: {workload: ..., condition: started}` on it
  in a `serial`.

### Expectations

An experiment that reproduces a known bug names it in `expectations`, so a run
tells a known failure from a new one.

| Field | Meaning |
|---|---|
| `issue` | An issue URL or `<owner>/<repo>#<number>`. |
| `checks` | The checks the bug makes fail: `<workload>/<check>` for a workload check, or `experiment/<check>`. A check can be in only one expectation. Empty marks the experiment as a regression test for a fixed bug: every check must pass. |
| `kind` | `strict` (default): the bug triggers on every run, so a listed check that passes on every covered instance is an unexpected pass, `XPASS`, and fails the run. `flaky`: the bug triggers on some runs, so a pass is expected too. |
| `instances` | `all` (default) covers every instance. `affected` covers the instances an operation acted on in this run, as `operation.started` records them; the others are controls and must pass. `affected` applies to workload checks of a workload that an operation targets. |

If some covered instances pass and others fail, as with a race, the passes
are not unexpected. A check with a checker error matches no expectation: it
is `ERROR`. `chaos list --issue N` finds the experiments for an issue, and
`chaos show` and the report link them. When the fix lands, the run is `FAIL`
with `XPASS`: empty `checks` and increase `version`.

### Preflight

Preflight checks are shell checks that run before setup and before any
instance is created: first the adapter's `preflight`, then the experiment's.
They get the experiment check variables. If one fails, the run records
`preflight.failed`, skips the flow and the other checks, and ends with
`ERROR`. Their results are in `verdict.json` with `scope: preflight`.

When the experiment lists `metrics:`, the built-in `metrics-exported`
preflight check scrapes every adapter metrics source once and fails when a
listed family is in none of them, or when the adapter has no metrics source.
The run then ends with `ERROR` before anything is created, with the missing
families in the check output.

### Metrics

`metrics[]` lists the metrics the experiment is about. The report draws them
first, each with its description, and folds the other families the run
scraped; the Grafana dashboard draws only them; and `metric` checks can only
name them. Scraping is not narrowed: the run records every family the
sources export. An entry is one of:

| Entry | Meaning |
|---|---|
| `openshell_server_supervisor_sessions` | A metric family name, drawn as in the report. |
| `{family, description}` | The same, with a description shown under the chart. |
| `{title, expr, description}` | A raw PromQL panel for the Grafana dashboard only. The report skips it, and checks cannot name it. |

Without `metrics:` the report draws every family the run scraped and the
dashboard repeats one panel over every family, as before.

## Verdict

| Verdict | Exit code | Meaning |
|---|---|---|
| `PASS` | 0 | No errors, and every check passed. |
| `XFAIL` | 0 | No errors, at least one check failed, and an expectation covers every failure. |
| `FAIL` | 1 | No errors, and a check failed that no expectation covers, or a check that a strict expectation lists passed on every covered instance (`XPASS`). |
| `ERROR` | 2 | The harness could not do what the experiment says, or a check could not decide. |

Examples of `ERROR`: a preflight check failed, a hook exited non-zero, a fault
could not be applied or undone, `select` found too few candidates, an
instance was never created, the experiment `timeout` expired, the run was
aborted, or a check timed out, was killed by a signal, or exited 126 or 127.

`chaos run` prints one line per result, failures and errors with the last
lines of their output, then the verdict and the run directory:

```text
  ok    counter-1/exited-ok
  xfail counter-0/exited-ok (known: NVIDIA/OpenShell#4828)
          no exit code recorded
  XPASS counter-0/exited-ok (expected to fail: NVIDIA/OpenShell#4828; fixed?)
  ok    counter-0/exited-ok (flaky: NVIDIA/OpenShell#4828 did not trigger)
  FAIL  counter-1/journal-complete
          < 59
          < 60
          want each tick from 1 to 60 at least once
  ERROR adapter/gateway-healthy (the check could not decide)
```

## Environment variables

Every `CHAOS_` variable starts with the path of its entity. A variable that
has no meaning in a scope is unset. Only `OUTPUT_DIR` has a different value in
the sandbox and on the host. `tests/test_envvars.py` checks this table against
the code.

| Variable | Workload `run` (sandbox) | Workload check (host) | Experiment / adapter check (host) |
|---|---|---|---|
| `CHAOS_EXPERIMENT_ID` | ✓ | ✓ | ✓ |
| `CHAOS_EXPERIMENT_VERSION` | ✓ | ✓ | ✓ |
| `CHAOS_EXPERIMENT_ADAPTER` | ✓ | ✓ | ✓ |
| `CHAOS_EXPERIMENT_RUN_ID` | ✓ | ✓ | ✓ |
| `CHAOS_EXPERIMENT_RUN_SEED` | ✓ | ✓ | ✓ |
| `CHAOS_EXPERIMENT_RUN_DIR` | | ✓ | ✓ |
| `CHAOS_EXPERIMENT_DATA_DIR` | | ✓ | ✓ |
| `CHAOS_EXPERIMENT_WORKLOAD_NAME` | ✓ | ✓ | |
| `CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_COUNT` | ✓ | ✓ | |
| `CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_NAME` | ✓ | ✓ | |
| `CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_INDEX` | ✓ | ✓ | |
| `CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_OUTPUT_DIR` | ✓ sandbox path | ✓ host copy | |
| `CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_DIR` | | ✓ | |
| `CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_EXIT_CODE` | | ✓ unset if none recorded | |
| `CHAOS_EXPERIMENT_WORKLOAD_CHECK_NAME` | | ✓ | |
| `CHAOS_EXPERIMENT_CHECK_NAME` | | | ✓ |

Preflight checks get the experiment check variables.

### Hook variables

Every hook gets the process environment, which under the e2e wrapper includes
the `OPENSHELL_*` exports, and `CHAOS_EXPERIMENT_HOOK_TIMEOUT`: the hook's
timeout in whole seconds.

Run hooks (`describe`, `setup`, `collect`, a target's `start` and
`after-recovery`) get the run variables, `CHAOS_EXPERIMENT_RUN_DIR` and `CHAOS_EXPERIMENT_DATA_DIR`.

Instance hooks (`create`, `status`, `collect`, `delete`) get the workload
`run` variables, `CHAOS_EXPERIMENT_RUN_DIR`, `CHAOS_EXPERIMENT_DATA_DIR`,
`CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_DIR`, and these:

- `CHAOS_EXPERIMENT_WORKLOAD_RUN_FILE`: the workload script.
- `CHAOS_EXPERIMENT_WORKLOAD_WRAPPER_FILE`: the wrapper script.
- `CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_ENV_FILE`: `NAME=value` lines to pass
  into the sandbox (workload `env` and the workload `run` variables).
- `CHAOS_EXPERIMENT_WORKLOAD_OPTION_<KEY>`: each resolved option, upper-cased
  with `-` as `_`. A data option is the path of its file.
- `CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_SYSTEM_ID`: the system's id for the
  instance, after `create`.

Adapter operation hooks get these:

- `CHAOS_EXPERIMENT_OPERATION_NAME`: the operation name.
- `CHAOS_EXPERIMENT_OPERATION_USES`: the adapter operation, such as
  `openshell-policy-set`.
- `CHAOS_EXPERIMENT_OPERATION_SCOPE`: `instances` for an operation with a
  `target`, `global` for `scope: global`.
- `CHAOS_EXPERIMENT_OPERATION_OUTCOME_FILE`: where the hook writes its
  outcome.
- `CHAOS_EXPERIMENT_OPERATION_STATE_DIR`: a directory for this run of the
  operation, where `run` saves what `revert` puts back.
- `CHAOS_EXPERIMENT_OPERATION_INSTANCES_FILE`: `global` only; the names of the
  instances that were `Running` when the operation started, one per line.

Each resolved option is in `CHAOS_EXPERIMENT_OPERATION_OPTION_<KEY>`, named
as for workload options. With scope `instances`, the hook also gets the
instance hook variables of the instance it acts on. With scope `global`, it
also gets the run hook variables.

## Run directory

```text
e2e/chaos/runs/
  <experiment-id>/
    <run-id>/                    # CHAOS_EXPERIMENT_RUN_DIR
      manifest.json              # schema_version, ids, adapter, sha256s, seed, overrides, environment, descriptions
      experiment.yaml            # the experiment after --set
      adapter.yaml               # the adapter as it ran
      data/                      # CHAOS_EXPERIMENT_DATA_DIR
        <name>                   # each entry of the experiment's data
      timeline.jsonl
      verdict.json
      evaluations/
        <stamp>.json             # one verdict per `chaos evaluate`
      report.html
      share.md                   # by `chaos share`
      share/                     # its charts as PNG, by `chaos share --png`
      logs/
        harness.log
        gateway.log
        collect.log
        watch-<name>.log         # stderr of an adapter watch
        port-forward.log         # Kubernetes only
        operations/
          <operation>[.<instance>].log      # adapter operation hook output
          <operation>[.<instance>].outcome  # its outcome file
          <operation>.instances             # scope: global, the instances file
      system/                    # adapter collect output
        healthz.json
        containers.json          # Docker only
        pods.json                # Kubernetes only
        metrics/
          <name>.jsonl           # one record per scrape of an adapter metrics source
          <name>.types.json
        watch/
          <name>.jsonl           # one record per stdout line of an adapter watch
      workloads/
        <workload>/
          run.sh                 # the script
          wrapper.sh             # its wrapper
          instances/
            <instance>/          # CHAOS_EXPERIMENT_WORKLOAD_INSTANCE_DIR
              env                # the NAME=value lines passed into the sandbox
              output/            # the instance's output dir, copied out
              logs/
                <role>.log       # sandbox.log and supervisor.log on Docker; workload.log and supervisor.log on Kubernetes
  index.jsonl                    # one line per run or evaluation; the last line per run id wins
                                 # `path` and `evaluation` are relative to this directory;
                                 # `flaky_missed` names the checks a flaky known failure did not trigger on
```

`run-id` is `<UTC yyyymmddThhmmssZ>-<4 hex>`. An operation log gets
`--- run <n>` before each run of the hook.

### Lifecycles

Each kind of thing in a run goes through a fixed sequence of states, and the
timeline records each step. A failed step never has its own state: the step
raises an error, the run emits `abort` with it, and the verdict is `ERROR`.

**Run.** `run.started`; then the preflight checks (`preflight.failed` ends
the run), the adapter's `describe` hook, `setup`, the observers, the flow, the checks, and `run.finished` with the
verdict. When the flow raises, the experiment `timeout` expires or the run
is interrupted, `abort` lists the errors; the harness then reverts every
fault it still holds, newest first, restarts process targets that are down,
collects and deletes every instance within the cleanup timeout, deletes
uncollected whatever is left at that deadline, runs the adapter `collect`,
and still runs the checks. A second interrupt emits `abort` with `forced:
true`, deletes the instances without collecting them, writes an `ERROR`
verdict from the preflight results alone and exits 2.

**Flow node.** `node.started`, then `node.finished`. A `serial` node runs
its children in order; `parallel` runs them together and waits for all;
`delay` sleeps; `wait` for a workload returns once every instance of it has
started (`Running` or a terminal state), and raises if the workload ended
before that; `wait` for a signal emits `signal.waited` when it returns, and
emits `signal.timeout` and raises if no signal arrives within its `timeout`;
`repeat` emits `repeat.iteration` before each pass over its children. A node
that raises never emits `node.finished`; a node cancelled because a sibling
in a `parallel` raised, or the run aborted, emits it with `status:
cancelled`, so a timeline never has open nodes.

**Instance.** `instance.created` with its `system_id`, then one
`instance.state` for each change the 2s poll sees. The states are the five
the `status` hook may print: `Pending` (the initial state), `Running`,
`Succeeded`, `Failed` and `Unknown`. `Unknown` is what a failing or unparsed
`status` reads as, so it also covers a gateway that is down; it is not
terminal. The hook may print a second word, which the event records as
`reason`; the OpenShell adapters print the gateway's Ready condition reason,
such as `ContainerExited` or `MainProcessFailed`, so an `Unknown` or `Failed`
says why. `Succeeded` and `Failed` are terminal; `Running` and the terminal
states count as started. While an operation holds the instance (an adapter
operation's hook is running on it), a terminal state is recorded but does
not end it, because the operation may cause it on purpose, as a stop does.
When the instance ends, or the workload `timeout` expires
(`instance.timed_out`, with the state it was in), it is collected
(`instance.collected`, with the exit code the wrapper wrote, or `null`) and
deleted (`instance.deleted`). An instance whose `create` failed is deleted
without collection; a failed `collect` still deletes, then ends the run with
`ERROR`.

The OpenShell adapters' `status` hook maps the gateway's sandbox phase to
these states, following the CLI's own rule for a finished sandbox:

| Gateway phase | State |
|---|---|
| `Ready` | `Running` |
| `Completed` or `Stopped` with a main-process exit code, or `Error` with Ready=False/`MainProcessFailed` and an exit code | `Succeeded` for exit code 0, `Failed` otherwise |
| `Completed` without an exit code | `Succeeded` |
| `Error` without an exit code and Ready=False/`ContainerExited` | `Unknown`: provisional, the supervisor may restart the sandbox |
| any other `Error` | `Failed` |
| `Provisioning`, `Starting` or `Unspecified` | `Pending`; an exit code while `Starting` is a restart in progress |
| anything else, or a hook that fails or prints no state | `Unknown` |

**Built-in fault.** `operation.started` with the target and the selected
instances, then `operation.applied` once the fault is in effect, then, for a
fault with a `duration` (`outage`, `freeze`), `operation.reverted` when it
has passed and the fault is undone; `restart` emits `target.restarted` once
the target is back; `kill` and `restart-in-place` are one-shot and are not
undone. `operation.completed` with outcome `ok` closes the operation. A
failure at any step emits `operation.error` and ends the flow. On abort, a
fault still in effect is undone with `operation.reverted` (`abort: true`) or
`operation.error`.

**Adapter operation.** `operation.started` with its `run` number and
options, then the hook runs once per selected instance (or once for `scope:
global`) while the instances are held, then one `operation.completed` per
instance with the hook's outcome: `ok`, `skipped` or `failed`, each with a
reason. `failed` is evidence for the checks, not an error; a hook that exits
non-zero or reports no outcome emits `operation.error` and ends the flow. At
the end of the flow, and on abort, each run with outcome `ok` or `failed` is
reverted, newest first, with `operation.reverted` (`skipped` when its
instance is already deleted) or `operation.error`.

**Signal.** `signal.received` each time a watched stream matches the
signal's pattern; `signal.waited` each time a `wait` node returns for one.

**Check.** Checks have no timeline events. Each result in `verdict.json` is
`ok` true or false, or `error` true when the check could not decide, with
`issue` and `flaky` from the expectation that covers it.

### Timeline events

Each `timeline.jsonl` line has `t` (seconds since the start), `ts` (UTC) and
`event`:

| Event | Fields |
|---|---|
| `run.started` | `experiment`, `version`, `seed`, `adapter` |
| `node.started`, `node.finished` | `node`, plus the node's label: `name` for a workload, `wait` and `condition`, `signal`, `delay`, or `repeat`; `node.finished` adds `status: cancelled` when the node did not finish on its own |
| `instance.created` | `workload`, `instance`, `system_id` |
| `instance.state` | `workload`, `instance`, `state`, and `reason` when the `status` hook printed one |
| `instance.timed_out` | `workload`, `instance`, `state` |
| `instance.deleted` | `workload`, `instance` |
| `instance.collected` | `workload`, `instance`, `exit_code` |
| `operation.started` | `operation`, `uses`, `run`, `scope`, `instances`; a built-in adds `target` and `ids` and its `scope` is `target` or `instances`, an adapter operation adds `options` and its `scope` is `global` or `instances` |
| `operation.applied`, `operation.reverted` | `operation`, `uses` (built-ins); `operation.applied` on a Kubernetes workload target adds `pods`. A fault reverted while the run aborts, and an adapter operation reverted at the end of the flow or on abort, emit `operation.reverted` with `operation`, `undo` and `abort` instead; `skipped` says why a revert did not run |
| `operation.completed` | `operation`, `uses`, `run`, `outcome`; an adapter operation adds `instance` and `reason` |
| `operation.error` | `operation`, `uses`, `error`; an adapter operation adds `run`. A revert that fails emits only `operation` and `error` |
| `target.restarted` | `target`, `operation`, and `pid` for a Docker process target |
| `repeat.iteration` | `node`, `iteration` |
| `signal.received` | `signal` |
| `signal.waited` | `node`, `signal`, `signal_t`, `delay` |
| `signal.timeout` | `node`, `signal`, `timeout`, and `last_t`, when the signal was seen at all, the time of its last event |
| `check.started`, `check.finished` | `scope`, `instance`, `check`; `check.finished` adds `ok`. Emitted by the run's own checks, not by `chaos evaluate` |
| `observer.failed` | `error` |
| `preflight.failed` | `checks`, the names that failed |
| `abort` | `errors`, or `forced: true` when the run was interrupted |
| `run.finished` | `verdict`, and `processes` when a built-in fault on a Docker process target recorded gateway pids: the pids seen per target across restarts |

`node` is the node's path in the flow, such as `flow` for a top-level
workload, `flow.parallel[1].serial[0].wait` or
`flow.parallel[1].serial[1].delay`.

### verdict.json

The file has `verdict`, `errors` (the harness errors that made the verdict
`ERROR`, if any) and `results`. A file `chaos evaluate` writes under
`evaluations/` adds `evaluated_at`. Each result has `scope` (`workload`, `experiment`, `adapter` or `preflight`),
`workload`, `instance`, `check`, `description`, `ok`, `output`, `issue` (the
expectation that covers the result, or null), `flaky` (that expectation is
flaky) and `error` (the check could not decide).

## Commands

Run them with `uv run --project e2e/chaos chaos ...` from the repository root,
or with `mise run e2e:chaos` and `mise run e2e:chaos:preflight`, which run
`chaos run` and `chaos preflight` under `e2e/with-docker-gateway.sh`;
`mise run e2e:chaos:check` runs `hello-world` and `smoke` there and fails
unless both verdicts are `PASS`. For
the `openshell-kubernetes` adapter, run them under `e2e/with-kube-gateway.sh`
and `e2e/chaos/scripts/kubernetes-env.sh`, which exports the variables the
adapter derives from the wrapper's:

```shell
OPENSHELL_E2E_KUBE_CONTEXT=kind-openshell-chaos e2e/with-kube-gateway.sh e2e/chaos/scripts/kubernetes-env.sh uv run --project e2e/chaos chaos run --adapter openshell-kubernetes e2e/chaos/experiments/gateway-graceful-restart.yaml
```

With `OPENSHELL_E2E_KUBE_CONTEXT` set, the wrapper installs the chart into
that cluster in a namespace of its own and port-forwards the gateway; the
cluster's lifecycle is yours. Unset, it creates a local k3d cluster and
deletes it on exit.

| Command | What it does |
|---|---|
| `chaos run FILE [--adapter ID] [--seed N] [--set PATH=VALUE]... [--runs-dir DIR]` | Runs one experiment. The default seed is 0. If the experiment is not applicable on the adapter, prints `n/a on <adapter>` with the reasons and exits 0 without a run. |
| `chaos preflight FILE [--adapter ID] [--set PATH=VALUE]... [--runs-dir DIR]` | Runs only the preflight checks, in a temporary run directory. Exits 0 if they all pass or the experiment is not applicable on the adapter, else 2. |
| `chaos validate [--adapter ID \| --all-adapters] [FILE...]` | Checks experiment manifests against the format and their adapter. With `--all-adapters`, checks every manifest given, or every experiment in `experiments/` when none is given, on every adapter and prints a table. Exits 0 if each manifest is ok or not applicable, 1 if one is invalid, 2 if neither files nor `--all-adapters` is given. |
| `chaos evaluate RUN_DIR [--experiment FILE] [--adapter ID] [--index FILE]` | Runs the workload, experiment and adapter checks again against a finished run and keeps the run's preflight results. |
| `chaos report RUN_DIR [-o FILE]` | Writes `report.html` again. |
| `chaos share RUN_DIR [-o FILE] [--png]` | Writes `share.md`, a Markdown page of the run with Mermaid charts for an issue or a post. `--png` also renders the charts with mermaid-cli (`mmdc`) into a directory next to the page. |
| `chaos export RUN_DIR [-o FILE] [--grafana URL]` | Writes the run as OpenMetrics text, default `RUN_DIR/export.om`. |
| `chaos dashboard PATH... [-o FILE \| --dir DIR]` | Writes the Grafana dashboard of each experiment named by an experiment manifest or a run directory. |
| `chaos list [--issue N]` | Lists the experiments, or those with an expectation for issue `N`. |
| `chaos show ID` | Shows one experiment. |

`--adapter ID` runs or checks the experiment on `adapters/<ID>.yaml` instead
of its `default-adapter`. Before anything is created, the harness checks the
experiment against the adapter, and `chaos validate --adapter ID` makes the
same check without Docker, a cluster or a gateway. Two results are possible.

- Invalid: an option the adapter does not have, or a preflight check whose
  name clashes with the adapter's. `validate` prints
  `invalid on <adapter>: <reason>` and exits 1; `run` and `preflight` print
  the same and exit 2.
- Not applicable: the experiment is valid but the adapter has no way to run
  it, because it lacks an operation, a signal, a target or a role the
  experiment uses, or its backend does not support a fault. Every gap is
  listed, and an unsupported fault carries the adapter's `unavailable`
  reason. `validate` prints `n/a on <adapter>: <path>: <reasons>` and exits
  0; `run` and `preflight` print `n/a on <adapter>: <reasons>` and exit 0
  without running. On
  the experiment's own `default-adapter`, not applicable is invalid.

For example, from `validate`:

```text
n/a on openshell-kubernetes: experiments/supervisor-pause.yaml: operations.pause-supervisors: freeze on a pod target (role supervisor) is not supported on kubernetes: freezing a pod's processes needs node-level cgroup access through a privileged pod, which this adapter does not have
```

`chaos validate --all-adapters` prints one row per experiment and one column
per adapter, `ok`, `n/a` or `invalid`, then one line per cell that is not
`ok` with its reasons. The run records the adapter it used.

`--set` takes dotted paths and addresses list items by `name`, for example
`--set workloads.counter.instances=10` or
`--set workloads.counter.env.TICKS=30`. `--set` values are written to the run
directory and passed to `openshell` on its command line, so do not pass
secrets with it.

`chaos evaluate` writes `RUN_DIR/evaluations/<stamp>.json` and never rewrites
`verdict.json`; the report reads the latest evaluation. The file records under
`read` the experiment file it evaluated with, the sha256 of that file's bytes,
and the adapter's id and file, and the printed verdict starts with them, so a
reader can compare them with the run's `manifest.json`. Errors
from the run are kept, and a run with no `run.finished` event is `ERROR`.
`--experiment` takes the checks from an edited file, with the run's `--set`
overrides applied again. Adapter checks come from the run's `adapter.yaml`, or
with `--adapter ID` from the current `adapters/<ID>.yaml`.

### Signals

The first `SIGINT` or `SIGTERM` aborts the run: the harness undoes faults,
restarts a process target that is down, such as the gateway, and collects and
deletes instances, within 10 minutes; what is left at that deadline is
deleted without collection. Then it runs the adapter's `collect` hook, with
its 5-minute timeout, and the checks, each with its own timeout, and writes
an `ERROR` verdict. A second signal undoes faults (for at most 2 minutes), deletes
instances and exits 2 without collection or checks. A harness error during
the flow takes the same path as the first signal; an uncaught exception in
the harness takes the path of the second. A third signal exits 2 at once and
leaves faults and instances behind. Send the signal to the harness pid,
not Ctrl-C to the e2e wrapper, so the wrapper does not tear the gateway down
first.
