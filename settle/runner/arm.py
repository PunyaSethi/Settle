"""The arm seam. SPEC §14.1.

An arm is one method: given a case, its state, and the legal actions, choose
one. Baselines, EXPLORE, OURS and LLM-STRAT all implement this and nothing else
changes — which is what makes §4's "one gate implementation, one code path"
true rather than aspirational.

An arm cannot reach around this interface. It receives the legal set, not the
case's hidden truth, and it returns an action, not a dispatch.
"""

from typing import Protocol

from settle.schema.action import Action, DoNothing
from settle.schema.enums import ActionType
from settle.schema.enums import ArmMode
from settle.schema.observed import ObservedCase
from settle.schema.state import CaseState


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


ARMS: dict[str, type] = {"b0": DoNothingArm, "first_legal": FirstLegalArm}
