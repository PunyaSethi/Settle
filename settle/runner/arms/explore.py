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

from typing import Final

from settle.policy.gates import evaluate_gates
from settle.policy.legal import legal_actions
from settle.policy.params import hour_offsets, max_horizon_h
from settle.schema.action import Action, DoNothing, Retry
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


def is_explore_seed(seed: int) -> bool:
    return seed in EXPLORE_SEED_RANGE


def is_evaluation_seed(seed: int) -> bool:
    return seed in EVALUATION_SEED_RANGE


def expand_grid(action: Action) -> list[Action]:
    """One action becomes its row of the action grid. A71.

    Only `retry` carries a schedulable offset in §5.3's frozen verb set, so the
    grid widens retries and leaves every other verb at the runner's current
    hour. That is a real limitation of the offset dimension, not a modelling
    choice — see the CP5 report.
    """
    if not isinstance(action, Retry):
        return [action]
    return [
        Retry(at_hour_offset=offset, rail=action.rail)
        for offset in hour_offsets()
        if offset < max_horizon_h()
    ]


def candidate_pairs(case: ObservedCase, state: CaseState) -> list[Action]:
    """Every (action, hour) pair the grid admits for this case, before gates."""
    pairs: list[Action] = []
    for action in legal_actions(case, state):
        pairs.extend(expand_grid(action))
    return pairs


def gate_passing_pairs(case: ObservedCase, state: CaseState) -> list[Action]:
    """The set EXPLORE samples from, and the set OURS must search at CP8.

    Binding constraint (A71): both arms enumerate candidates through this
    function. An estimator trained on one grid and queried on another has zero
    coverage exactly where it is asked to predict.
    """
    return [
        action
        for action in candidate_pairs(case, state)
        if evaluate_gates(case, state, action, ArmMode.ENFORCE).allowed
    ]


class ExploreArm:
    """Uniform over gate-passing pairs, with the propensity logged at draw time."""

    name = "EXPLORE"
    mode = ArmMode.ENFORCE

    def __init__(self, seed: int) -> None:
        if not is_explore_seed(seed):
            raise ValueError(
                f"seed {seed} is outside EXPLORE_SEED_RANGE {EXPLORE_SEED_RANGE.start}.."
                f"{EXPLORE_SEED_RANGE.stop - 1}. Training on evaluation seeds is not a "
                "held-out set, it is a memorisation test."
            )
        self.seed = seed
        self.decisions: list[Decision] = []

    def choose(self, case: ObservedCase, state: CaseState, legal: list[Action]) -> Action:
        passing = gate_passing_pairs(case, state)
        if not passing:
            # Nothing survives the gates. `do_nothing` is always available and
            # always passes, so this is unreachable in practice; recorded with
            # propensity 1.0 rather than silently omitted.
            return self._record(case, state, DoNothing(), 1.0)

        draw = derive_unit_float(self.seed, EXPLORE_ADDRESS, case.case_id, state.tick)
        index = min(int(draw * len(passing)), len(passing) - 1)
        return self._record(case, state, passing[index], 1.0 / len(passing))

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
