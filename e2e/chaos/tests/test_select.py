# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import random

import pytest

from chaos.inject import Candidate, select
from chaos.model import Operation, RunError

CANDIDATES = [
    Candidate(f"counter-{i}", i, "supervisor", f"cid{9 - i}") for i in range(6)
]


def fault(select_: str | dict, name: str = "f") -> Operation:
    return Operation(
        name=name,
        description="d",
        uses="kill",
        target={"workload": "counter", "role": "supervisor"},
        select=select_,
    )


def names(picks: list[Candidate]) -> list[str]:
    return [c.instance for c in picks]


@pytest.mark.parametrize("select_", ["one", {"count": 3}])
def test_same_seed_same_picks_in_any_order(select_):
    expected = names(select(fault(select_), 42, CANDIDATES))
    for shuffle_seed in range(10):
        shuffled = CANDIDATES[:]
        random.Random(shuffle_seed).shuffle(shuffled)
        # New container ids, as in a second run: picks follow instance names.
        renamed = [
            Candidate(c.instance, c.index, c.role, f"new-{c.id}") for c in shuffled
        ]
        assert names(select(fault(select_), 42, renamed)) == expected


def test_seed_and_fault_name_change_picks():
    picks = {
        tuple(names(select(fault({"count": 2}, name), seed, CANDIDATES)))
        for seed in range(20)
        for name in ("a", "b")
    }
    assert len(picks) > 1


def test_select_all_returns_every_candidate_sorted():
    assert names(select(fault("all"), 0, list(reversed(CANDIDATES)))) == names(
        CANDIDATES
    )


def test_select_count_picks_distinct_sorted():
    picks = names(select(fault({"count": 4}), 7, CANDIDATES))
    assert len(picks) == len(set(picks)) == 4
    assert picks == sorted(picks, key=lambda n: int(n.rsplit("-", 1)[1]))


def test_select_count_needs_enough_candidates():
    with pytest.raises(RunError, match="wants 7 targets, only 6 found"):
        select(fault({"count": 7}), 0, CANDIDATES)


def test_no_candidates():
    with pytest.raises(RunError, match="no candidates"):
        select(fault("one"), 0, [])


def test_adapter_operation_picks_like_a_fault_and_names_itself_in_errors():
    operation = Operation(
        name="f",
        description="d",
        uses="openshell-policy-set",
        options={"policy": "p"},
        target={"workload": "counter"},
        select={"count": 2},
    )
    assert names(select(operation, 42, CANDIDATES)) == names(
        select(fault({"count": 2}), 42, CANDIDATES)
    )
    with pytest.raises(RunError, match="^operation f: no candidates"):
        select(operation, 0, [])
