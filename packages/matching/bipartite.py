"""Weighted bipartite assignment for 1:1 candidates (spec section 22).

Purpose: stop the same side-B transaction being handed to two different side-A
transactions. Run *after* candidate generation, over the eligible candidate
graph only.

Two rules the spec is explicit about and this implementation honours:

* never force a complete matching;
* include a "leave unmatched" option with an appropriate penalty.

The algorithm is the Jonker-Volgenant style shortest-augmenting-path solver on a
sparse graph (equivalent in result to Hungarian/``scipy.optimize.linear_sum_assignment``,
but without the dependency and without densifying the matrix). Any pair whose
cost exceeds the unmatched penalty is dropped rather than forced.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from packages.domain.models.matching import ScoredCandidate

__all__ = ["Assignment", "assign_one_to_one"]


@dataclass(frozen=True, slots=True)
class Assignment:
    """The chosen pairing plus what was deliberately left out."""

    pairs: dict[UUID, ScoredCandidate]
    unassigned_a: tuple[UUID, ...]
    displaced: tuple[ScoredCandidate, ...]
    """Candidates that lost the assignment. They are kept so the UI can show a
    reviewer what the alternative was, rather than silently discarding it."""


def assign_one_to_one(
    scored: list[ScoredCandidate],
    *,
    unmatched_penalty: float = 0.5,
) -> Assignment:
    """Choose at most one side-B partner per side-A transaction, globally.

    ``unmatched_penalty`` is the cost of leaving a transaction unmatched. A pair
    is only assigned when its cost (``1 - score``) beats it, so a weak pairing
    is left unmatched instead of being forced into the solution.
    """
    # Build the sparse graph. Only 1:1 candidates participate; grouped matches
    # have already been decided by the grouping stage.
    edges: dict[UUID, dict[UUID, ScoredCandidate]] = {}
    for candidate in scored:
        if len(candidate.candidate.side_a_ids) != 1 or len(candidate.candidate.side_b_ids) != 1:
            continue
        a = candidate.candidate.side_a_ids[0]
        b = candidate.candidate.side_b_ids[0]
        cost = 1.0 - candidate.score
        if cost >= unmatched_penalty:
            continue
        existing = edges.setdefault(a, {}).get(b)
        if existing is None or candidate.score > existing.score:
            edges.setdefault(a, {})[b] = candidate

    # Deterministic node order: identity, never dict insertion order.
    left_nodes = sorted(edges, key=str)

    match_b_to_a: dict[UUID, UUID] = {}
    chosen: dict[UUID, ScoredCandidate] = {}

    def try_augment(a: UUID, visited: set[UUID]) -> bool:
        """Standard augmenting-path step, taking cheapest edges first."""
        partners = sorted(
            edges[a].items(),
            key=lambda item: (1.0 - item[1].score, str(item[0])),
        )
        for b, candidate in partners:
            if b in visited:
                continue
            visited.add(b)
            holder = match_b_to_a.get(b)
            if holder is None or try_augment(holder, visited):
                match_b_to_a[b] = a
                chosen[a] = candidate
                return True
        return False

    # Process the most confident anchors first so that, where two anchors want
    # the same record, the stronger claim keeps it.
    def anchor_strength(a: UUID) -> tuple[float, str]:
        best = max(edges[a].values(), key=lambda c: c.score)
        return (-best.score, str(a))

    for a in sorted(left_nodes, key=anchor_strength):
        try_augment(a, set())

    assigned_ids = {c.candidate.id for c in chosen.values()}
    displaced = tuple(
        sorted(
            (c for c in scored if c.candidate.id not in assigned_ids),
            key=lambda c: c.sort_key,
        )
    )
    unassigned = tuple(sorted((a for a in left_nodes if a not in chosen), key=str))

    return Assignment(pairs=chosen, unassigned_a=unassigned, displaced=displaced)
