"""OURS. SPEC §14.1.

One `choose()` method, like every other arm. Everything that makes it different
is in `settle/agent/policy.py`; the seam is unchanged, which is what §4's "one
gate implementation, one code path" is worth.

INV-11: OURS can never run in OBSERVE. Enforced at construction, so no CLI flag
or config file can hand it the mode.
"""

from __future__ import annotations

from settle.agent.policy import PolicyDecision, choose
from settle.runner.arm import assert_enforce_only
from settle.schema.action import Action
from settle.schema.enums import ArmMode
from settle.schema.observed import ObservedCase
from settle.schema.state import CaseState


class OursArm:
    """The policy arm. Consults gates before choosing, per A72."""

    name = "OURS"

    def __init__(self, estimator, mode: ArmMode = ArmMode.ENFORCE) -> None:
        assert_enforce_only(self.name, mode)
        self.mode = mode
        self.estimator = estimator
        self.decisions: list[PolicyDecision] = []

    def choose(self, case: ObservedCase, state: CaseState, legal: list[Action]) -> Action:
        decision = choose(case, state, legal, self.estimator, self.mode)
        self.decisions.append(decision)
        return decision.action
