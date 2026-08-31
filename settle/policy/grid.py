"""The action grid. SPEC §5.3 (A71), §14.1.

One declaration of the candidate space, in the package both consumers may
import. `POLICY_PARAMS` declares the eight hour offsets; this module turns them
into the actual set of `(action, hour)` pairs a case admits, and into the subset
that survives the gates.

Why it lives in `settle/policy/` and not in the EXPLORE arm
-----------------------------------------------------------
It used to live in `settle/runner/arms/explore.py`, and `settle/agent/policy.py`
imported it from there. That is backwards: the agent is the thing being
evaluated and the runner is the harness that evaluates it, so a dependency from
agent to runner makes the policy unusable without the experiment that measured
it. It is the same error class as the escalation rule at CP3.1, where the policy
could not recompute eligibility because the rule lived inside `settle/sim/`
(§2.1, A62). Both are fixed the same way: the shared rule moves to
`settle/policy/`, and the dependency runs one direction only.

`tests/test_policy.py` walks the AST of every module under `settle/agent/` and
asserts none of them imports `settle.runner` (OQ-50).

The binding constraint
----------------------
A71: EXPLORE and OURS enumerate candidates through **this** function and neither
may widen or narrow it locally. An estimator trained on one grid and queried on
another has zero coverage exactly where it is asked to predict, and its held-out
calibration would look fine because the held-out set shares the same blind spot.
Having one importable definition is what makes that constraint checkable rather
than aspirational.
"""

from settle.policy.gates import evaluate_gates
from settle.policy.legal import legal_actions
from settle.policy.params import hour_offsets, max_horizon_h
from settle.schema.action import Action, Retry
from settle.schema.enums import ArmMode
from settle.schema.observed import ObservedCase
from settle.schema.state import CaseState


def action_offset(action: Action) -> int:
    """Hours between choosing an action and its firing.

    Only `retry` carries a schedulable offset in §5.3's frozen verb set, so
    every other verb fires at the runner's current hour.
    """
    return action.at_hour_offset if isinstance(action, Retry) else 0


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
    """The set EXPLORE samples from, and the set OURS searches.

    Binding constraint (A71): both arms enumerate candidates through this
    function. An estimator trained on one grid and queried on another has zero
    coverage exactly where it is asked to predict.
    """
    return [
        action
        for action in candidate_pairs(case, state)
        if evaluate_gates(case, state, action, ArmMode.ENFORCE).allowed
    ]
