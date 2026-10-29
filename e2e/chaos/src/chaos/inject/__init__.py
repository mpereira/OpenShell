# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Fault injection: seeded target selection shared by every operation."""

from __future__ import annotations

import random
from dataclasses import dataclass

from chaos.model import Operation, RunError


@dataclass(frozen=True)
class Candidate:
    """One thing an operation can act on.

    `instance` is the stable name (`counter-1`, or the target name for an
    adapter-level target); `id` is the backend's id (container id, pid),
    which changes between runs.
    """

    instance: str
    index: int
    role: str
    id: str


def select(op: Operation, seed: int, candidates: list[Candidate]) -> list[Candidate]:
    """Apply the operation's `select` with an RNG seeded by its name, so picks
    depend only on the seed, the name and the sorted candidates, never on
    scheduling order."""
    what = f"operation {op.name}"
    ordered = sorted(candidates, key=lambda c: (c.index, c.role, c.id))
    if not ordered:
        raise RunError(f"{what}: no candidates to select from")
    rng = random.Random(f"{seed}:{op.name}")
    if op.select == "all":
        return ordered
    if op.select == "one" or op.select is None:
        return [rng.choice(ordered)]
    count = op.select.count
    if count > len(ordered):
        raise RunError(
            f"{what}: select wants {count} targets, only {len(ordered)} found"
        )
    picked = rng.sample(range(len(ordered)), count)
    return [ordered[i] for i in sorted(picked)]
