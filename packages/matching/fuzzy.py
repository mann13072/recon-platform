"""Deterministic string similarity (spec section 23, stage V1).

Everything here is pure, dependency-free and reproducible. No embeddings: the
spec is explicit that embeddings must not be used for exact identifiers, and
that deterministic techniques come first.

All functions return a value in [0.0, 1.0].
"""

from __future__ import annotations

from functools import lru_cache

from packages.ingestion.normalization.text import tokenize

__all__ = [
    "jaro_similarity",
    "jaro_winkler_similarity",
    "levenshtein_distance",
    "levenshtein_similarity",
    "token_set_ratio",
    "trigram_similarity",
]


def levenshtein_distance(a: str, b: str) -> int:
    """Classic edit distance, iterative with a single row of state."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(
                min(
                    previous[j] + 1,  # deletion
                    current[j - 1] + 1,  # insertion
                    previous[j - 1] + (ca != cb),  # substitution
                )
            )
        previous = current
    return previous[-1]


def levenshtein_similarity(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    longest = max(len(a), len(b))
    return 1.0 - (levenshtein_distance(a, b) / longest)


def jaro_similarity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0

    match_window = max(len(a), len(b)) // 2 - 1
    if match_window < 0:
        match_window = 0

    a_flags = [False] * len(a)
    b_flags = [False] * len(b)
    matches = 0

    for i, ch in enumerate(a):
        start = max(0, i - match_window)
        end = min(i + match_window + 1, len(b))
        for j in range(start, end):
            if b_flags[j] or b[j] != ch:
                continue
            a_flags[i] = b_flags[j] = True
            matches += 1
            break

    if matches == 0:
        return 0.0

    transpositions = 0
    k = 0
    for i, flagged in enumerate(a_flags):
        if not flagged:
            continue
        while not b_flags[k]:
            k += 1
        if a[i] != b[k]:
            transpositions += 1
        k += 1

    transpositions //= 2
    m = float(matches)
    return (m / len(a) + m / len(b) + (m - transpositions) / m) / 3.0


def jaro_winkler_similarity(a: str | None, b: str | None, prefix_scale: float = 0.1) -> float:
    """Jaro-Winkler: rewards a shared prefix, which suits names and references."""
    if not a or not b:
        return 0.0
    jaro = jaro_similarity(a, b)
    if jaro < 0.7:
        return jaro
    prefix = 0
    for ca, cb in zip(a, b, strict=False):
        if ca != cb:
            break
        prefix += 1
        if prefix == 4:
            break
    return jaro + prefix * prefix_scale * (1.0 - jaro)


@lru_cache(maxsize=8192)
def _trigrams(value: str) -> frozenset[str]:
    padded = f"  {value} "
    return frozenset(padded[i : i + 3] for i in range(len(padded) - 2))


def trigram_similarity(a: str | None, b: str | None) -> float:
    """Jaccard similarity over character trigrams (the Postgres pg_trgm model)."""
    if not a or not b:
        return 0.0
    ta, tb = _trigrams(a), _trigrams(b)
    if not ta or not tb:
        return 0.0
    intersection = len(ta & tb)
    union = len(ta | tb)
    return intersection / union if union else 0.0


def token_set_ratio(a: str | None, b: str | None) -> float:
    """Order-insensitive token overlap, with tolerance for spelling differences.

    Bank narratives reorder the same words constantly, so a pure sequence
    measure understates real matches. The score is weighted toward exact token
    overlap; the trigram component only lifts pairs that differ by spelling
    rather than by content.

    Trigram similarity is used for that second component rather than
    ``difflib.SequenceMatcher``: the results are close, but SequenceMatcher is
    quadratic in the string length and this runs once per candidate pair, which
    made it the dominant cost of a large run.
    """
    if not a or not b:
        return 0.0
    set_a, set_b = set(tokenize(a)), set(tokenize(b))
    if not set_a or not set_b:
        return 0.0

    jaccard = len(set_a & set_b) / len(set_a | set_b)

    # With no shared tokens the blended score cannot reach any useful threshold,
    # so the spelling comparison is not worth running.
    if jaccard == 0.0:
        return 0.0

    spelling = trigram_similarity(" ".join(sorted(set_a)), " ".join(sorted(set_b)))
    return round(0.7 * jaccard + 0.3 * spelling, 6)
