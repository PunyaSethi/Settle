"""EXPLORE. SPEC §10.1, §14.1, A71, A72.

Uniform over the **gate-passing** set, not the legal set. That distinction is
the whole point of the arm.

Why gate-passing and not legal
------------------------------
If EXPLORE sampled from the legal set and let gates block afterwards, the
executed distribution would be non-uniform in a way the logged propensity does
not describe: a case whose contact window is shut would silently execute retries
far more often than the 1/len(legal) it recorded. Every IPS estimate built on
that log would be wrong, and wrong in a direction that flatters whichever action
the gates happen to permit most.

So the propensity is the probability the executed action was **chosen**, not the
probability it was proposed. The arm evaluates gates itself, in ENFORCE, and
samples uniformly from what survives.

Randomness
----------
Drawn from an addressed hash, not a sequential PRNG, so the same seed yields the
same training data in any process. Deliberately NOT one of §14.2's seven named
streams: those carry *world* randomness and are shared across arms so every arm
faces identical luck. An arm's own choice must not be shared — B2 and EXPLORE
reaching the same tick should not make the same draw.

Seed ranges
-----------
EXPLORE runs on a seed range disjoint from the evaluation batch, asserted in
code rather than trusted to a comment. Training on the same seeds the model is
evaluated on is not a held-out set, it is a memorisation test.
"""

from collections import Counter
from datetime import timedelta, timezone
from typing import Final

from settle.policy.grid import (
    action_offset,
    candidate_pairs,
    expand_grid,
    gate_passing_pairs,
)
from settle.schema.action import Action, DoNothing
from settle.schema.decision import Decision
from settle.schema.enums import ArmMode, ChosenBy
from settle.schema.observed import ObservedCase
from settle.schema.state import CaseState, as_of
from settle.sim.streams import derive_unit_float

# Disjoint by construction, and asserted at import. EXP-5.
EVALUATION_SEED_RANGE: Final[range] = range(0, 10_000)
EXPLORE_SEED_RANGE: Final[range] = range(90_000, 100_000)

assert not (set(EVALUATION_SEED_RANGE) & set(EXPLORE_SEED_RANGE)), (
    "EXPLORE and evaluation seed ranges overlap: training data would leak into "
    "the held-out set and the calibration numbers would be memorisation"
)

EXPLORE_ADDRESS: Final[str] = "explore_draw"

IST: Final = timezone(timedelta(hours=5, minutes=30))

# Rows per (verb, 4h bucket, decline class) cell before it stops being boosted.
# Matches the calibration threshold: a cell under it is EXTRAPOLATED and
# excluded from the headline figures, so it is exactly the cell worth buying.
TARGET_CELL_ROWS: Final[int] = 200
OVERSAMPLE_WEIGHT: Final[float] = 6.0


def is_explore_seed(seed: int) -> bool:
    return seed in EXPLORE_SEED_RANGE


def is_evaluation_seed(seed: int) -> bool:
    return seed in EVALUATION_SEED_RANGE


# The grid itself lives in `settle/policy/grid.py` (OQ-50). It is re-exported
# here because it was defined here for three checkpoints and callers reasonably
# import it from the arm that made it famous — but there is one definition, and
# `settle/agent/` now reaches it without importing `settle/runner/`.
__all__ = [
    "ExploreArm",
    "candidate_pairs",
    "coverage_cell",
    "expand_grid",
    "gate_passing_pairs",
    "is_evaluation_seed",
    "is_explore_seed",
]


def coverage_cell(case: ObservedCase, action: Action, tick: int) -> tuple:
    """The cell coverage is measured in: (verb, 4h IST bucket, decline class).

    Computed at the *dispatch* hour, so a retry scheduled 48 hours out counts
    toward the cell it will actually land in rather than the one it was chosen
    in. Anything else would report coverage for a hour the action never touches.
    """
    from settle.diagnose.taxonomy import classify

    at = (case.created_at + timedelta(hours=tick + action_offset(action))).astimezone(IST)
    return (action.type.value, at.hour // 4, classify(case.decline_code).value)


class ExploreArm:
    """Uniform over gate-passing pairs, with the propensity logged at draw time.

    With `oversample`, the draw is weighted toward cells that are still thin.
    The propensity logged is then the **actual** sampling probability, not
    `1/len(passing)`. That distinction is the whole contract: if oversampling
    shifted the distribution while the log still claimed uniform, every IPS
    estimate built on it would be wrong, and wrong in the direction of whichever
    cell we boosted.
    """

    name = "EXPLORE"
    mode = ArmMode.ENFORCE

    def __init__(self, seed: int, oversample: bool = False, target: int = TARGET_CELL_ROWS) -> None:
        if not is_explore_seed(seed):
            raise ValueError(
                f"seed {seed} is outside EXPLORE_SEED_RANGE {EXPLORE_SEED_RANGE.start}.."
                f"{EXPLORE_SEED_RANGE.stop - 1}. Training on evaluation seeds is not a "
                "held-out set, it is a memorisation test."
            )
        self.seed = seed
        self.oversample = oversample
        self.target = target
        self.cell_counts: Counter = Counter()
        self.decisions: list[Decision] = []

    def _weights(self, case: ObservedCase, state: CaseState, passing: list[Action]) -> list[float]:
        """One weight per candidate. Uniform unless a cell is under target."""
        if not self.oversample:
            return [1.0] * len(passing)
        return [
            OVERSAMPLE_WEIGHT
            if self.cell_counts[coverage_cell(case, action, state.tick)] < self.target
            else 1.0
            for action in passing
        ]

    def choose(self, case: ObservedCase, state: CaseState, legal: list[Action]) -> Action:
        passing = gate_passing_pairs(case, state)
        if not passing:
            # Nothing survives the gates. `do_nothing` is always available and
            # always passes, so this is unreachable in practice; recorded with
            # propensity 1.0 rather than silently omitted.
            return self._record(case, state, DoNothing(), 1.0)

        weights = self._weights(case, state, passing)
        total = sum(weights)
        draw = derive_unit_float(self.seed, EXPLORE_ADDRESS, case.case_id, state.tick) * total

        cumulative = 0.0
        index = len(passing) - 1
        for position, weight in enumerate(weights):
            cumulative += weight
            if draw < cumulative:
                index = position
                break

        chosen = passing[index]
        self.cell_counts[coverage_cell(case, chosen, state.tick)] += 1
        # The probability this action was *chosen*, which under weighting is no
        # longer 1/len(passing).
        return self._record(case, state, chosen, weights[index] / total)

    def _record(
        self, case: ObservedCase, state: CaseState, action: Action, propensity: float
    ) -> Action:
        self.decisions.append(
            Decision(
                decision_id=f"{case.case_id}:{state.tick}",
                case_id=case.case_id,
                at=as_of(case.created_at, state),
                action=action,
                p_success=0.0,
                expected_value=0,
                alternatives=[],
                chosen_by=ChosenBy.HEURISTIC,
                reason_code="EXPLORE_UNIFORM",
                arm=self.name,
                propensity=propensity,
                arm_mode=self.mode,
            )
        )
        return action
