"""The arm seam. SPEC §14.1.

An arm is one method: given a case, its state, and the legal actions, choose
one. Baselines, EXPLORE, OURS and LLM-STRAT all implement this and nothing else
changes — which is what makes §4's "one gate implementation, one code path"
true rather than aspirational.

An arm cannot reach around this interface. It receives the legal set, not the
case's hidden truth, and it returns an action, not a dispatch.

Gates before choosing
---------------------
An arm may consult gates before it chooses (A72). EXPLORE must, or its logged
propensity describes the wrong distribution: sampling from the legal set and
letting gates block afterwards makes the executed distribution non-uniform in a
way `1/len(legal)` does not capture, and every IPS estimate built on it is wrong.
OURS needs the same visibility to populate `Alternative.legal` and
`Alternative.block_gate` (§5.4).

Consulting gates is not bypassing them. The runner evaluates them again, in the
one implementation §4 requires, and its verdict is the one that binds.
"""

from typing import Protocol

from settle.schema.action import Action, DoNothing
from settle.schema.enums import ActionType
from settle.schema.enums import ArmMode
from settle.schema.observed import ObservedCase
from settle.schema.state import CaseState
from settle.runner.arms.baselines import FixedLadderArm, MaxPressureArm, SingleRetryArm
from settle.runner.arms.explore import ExploreArm


class Arm(Protocol):
    """The whole interface. Deliberately one method."""

    name: str
    mode: ArmMode

    def choose(
        self, case: ObservedCase, state: CaseState, legal: list[Action]
    ) -> Action: ...


class DoNothingArm:
    """B0 — the natural-recovery baseline. SPEC §14.1.

    Not a placeholder. §14.3 subtracts whatever B0 recovers, because roughly a
    fifth of at-risk value returns on its own and counting it is the easiest way
    for a recovery product to flatter itself.
    """

    name = "B0"
    mode = ArmMode.ENFORCE

    def choose(self, case: ObservedCase, state: CaseState, legal: list[Action]) -> Action:
        return DoNothing()


class FirstLegalArm:
    """The first legal action that actually does something.

    A smoke test for the runner, not a baseline: it has no policy and its
    numbers mean nothing. It exists so the loop can be exercised end to end by
    an arm that dispatches.

    `legal[0]` is always `do_nothing` — §5.3 requires it to be viable for every
    class and `legal_actions` lists it first — so an arm that took the literal
    first element would be `DoNothingArm` under another name and would exercise
    no dispatch path at all.
    """

    name = "FIRST_LEGAL"
    mode = ArmMode.ENFORCE

    def __init__(self, mode: ArmMode = ArmMode.ENFORCE) -> None:
        self.mode = mode

    def choose(self, case: ObservedCase, state: CaseState, legal: list[Action]) -> Action:
        for action in legal:
            if action.type is not ActionType.DO_NOTHING:
                return action
        return DoNothing()


def assert_enforce_only(name: str, mode: ArmMode) -> None:
    """INV-11: OURS can never run in OBSERVE.

    Enforced where arms are constructed rather than left to the arm registry,
    so a future OURS cannot be handed OBSERVE by a CLI flag.
    """
    if name.upper() == "OURS" and mode is ArmMode.OBSERVE:
        raise ValueError("INV-11: arm OURS can never run in OBSERVE mode")


ARMS: dict[str, type] = {
    "b0": DoNothingArm,
    "b1": SingleRetryArm,
    "b2": FixedLadderArm,
    "b3": MaxPressureArm,
    "explore": ExploreArm,
    "first_legal": FirstLegalArm,
}
