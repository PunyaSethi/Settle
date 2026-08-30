"""Calibration reporting. SPEC §10.1, §14.4.

A model that is accurate but miscalibrated is worse than useless for this
project: §10.2 multiplies `p_settle` by an amount, so a probability that reads
0.6 and means 0.3 halves every expected value the policy computes.

Cells below the observation threshold are marked EXTRAPOLATED and excluded from
the headline figures. Reporting a calibration number over a cell with four
observations is reporting noise with a decimal point on it.
"""

from __future__ import annotations

from typing import Final, Sequence

MIN_CELL_OBSERVATIONS: Final[int] = 50
DEFAULT_BINS: Final[int] = 10


def brier_score(probabilities: Sequence[float], outcomes: Sequence[int]) -> float:
    """Mean squared error of the probability. Lower is better."""
    if not probabilities:
        return 0.0
    return sum((p - y) ** 2 for p, y in zip(probabilities, outcomes)) / len(probabilities)


def reliability_table(
    probabilities: Sequence[float], outcomes: Sequence[int], bins: int = DEFAULT_BINS
) -> list[dict]:
    """Predicted vs actual per bucket. The reliability diagram, as numbers."""
    buckets: list[dict] = []
    for index in range(bins):
        low, high = index / bins, (index + 1) / bins
        members = [
            (p, y)
            for p, y in zip(probabilities, outcomes)
            if (low <= p < high) or (index == bins - 1 and p == 1.0)
        ]
        if not members:
            buckets.append({"bucket": f"{low:.1f}-{high:.1f}", "n": 0, "predicted": None, "actual": None})
            continue
        buckets.append(
            {
                "bucket": f"{low:.1f}-{high:.1f}",
                "n": len(members),
                "predicted": sum(p for p, _ in members) / len(members),
                "actual": sum(y for _, y in members) / len(members),
            }
        )
    return buckets


def expected_calibration_error(
    probabilities: Sequence[float], outcomes: Sequence[int], bins: int = DEFAULT_BINS
) -> float:
    """Weighted mean gap between predicted and actual, across buckets."""
    total = len(probabilities)
    if not total:
        return 0.0
    error = 0.0
    for bucket in reliability_table(probabilities, outcomes, bins):
        if bucket["n"]:
            error += (bucket["n"] / total) * abs(bucket["predicted"] - bucket["actual"])
    return error


def coverage_table(
    cells: Sequence[tuple], probabilities: Sequence[float], outcomes: Sequence[int],
    threshold: int = MIN_CELL_OBSERVATIONS,
) -> list[dict]:
    """Per-cell counts, with thin cells named rather than quietly averaged in."""
    grouped: dict[tuple, list[tuple[float, int]]] = {}
    for cell, p, y in zip(cells, probabilities, outcomes):
        grouped.setdefault(cell, []).append((p, y))
    rows = []
    for cell, members in sorted(grouped.items(), key=lambda kv: -len(kv[1])):
        n = len(members)
        rows.append(
            {
                "cell": cell,
                "n": n,
                "predicted": sum(p for p, _ in members) / n,
                "actual": sum(y for _, y in members) / n,
                "extrapolated": n < threshold,
            }
        )
    return rows


def headline(
    probabilities: Sequence[float], outcomes: Sequence[int], cells: Sequence[tuple],
    threshold: int = MIN_CELL_OBSERVATIONS,
) -> dict:
    """Calibration over covered cells only, plus what was excluded and why."""
    coverage = coverage_table(cells, probabilities, outcomes, threshold)
    thin = {row["cell"] for row in coverage if row["extrapolated"]}
    kept = [(p, y) for cell, p, y in zip(cells, probabilities, outcomes) if cell not in thin]
    kept_p = [p for p, _ in kept]
    kept_y = [y for _, y in kept]
    return {
        "n_all": len(probabilities),
        "n_covered": len(kept),
        "ece": expected_calibration_error(kept_p, kept_y),
        "brier": brier_score(kept_p, kept_y),
        "ece_all": expected_calibration_error(list(probabilities), list(outcomes)),
        "brier_all": brier_score(list(probabilities), list(outcomes)),
        "extrapolated_cells": sorted(thin),
        "coverage": coverage,
    }
