"""HYBRID — route by decline class. SPEC §14.1.

The margin is concentrated. At 10,000 cases OURS beats B2 by 47.67 points on
`auth_abandoned` and loses on `transient`, `time_shiftable`, `ambiguous` and
`dead_instrument`. The obvious question follows immediately: what if each class
went to whichever arm wins it?

This is that question, answered. It is a measurement, not a product.

It composes; it does not reimplement
------------------------------------
`HybridArm` holds one `OursArm` and one `FixedLadderArm` and delegates. There is
no third policy here and no new parameter — if there were, the result would
measure the tuning rather than the routing, and the number would be worth
nothing. ARM-7 asserts the composition literally: HYBRID's decisions on
`auth_abandoned` are identical to OURS's on the same cases, and its
`time_shiftable` decisions are identical to B2's.

Routing is per case, not per decision
-------------------------------------
A case's decline class never changes, so a case belongs to exactly one delegate
for its whole life. That is what makes the identity in ARM-7 possible at all: a
router that switched arms mid-case would produce a state trajectory neither
source arm ever visits, and "composes two arms" would stop being true the moment
the first case reached its second decision.

The router reads `classify(case.decline_code)` — a pure function of an
`ObservedCase` field. It never touches `CaseState`, so the routing decision is
the same on tick 0 and tick 700.

Not the submitted policy
------------------------
OURS is. HYBRID exists to measure the ceiling of class-based routing and to
report it rather than leave a reader wondering whether we tried. Its cost is in
contacts, and contacts are the thing the project set out not to spend.
"""

from __future__ import annotations

from typing import Final

from settle.diagnose.taxonomy import classify
from settle.runner.arm import assert_enforce_only
from settle.runner.arms.baselines import FixedLadderArm
from settle.runner.arms.ours import OursArm
from settle.schema.action import Action
from settle.schema.enums import ArmMode, DeclineClass
from settle.schema.observed import ObservedCase
from settle.schema.state import CaseState

__all__ = ["OURS_CLASSES", "HybridArm"]

# The classes OURS wins, and the only ones it is given. One entry, from one
# measurement: `auth_abandoned` at +47.67 points. Everything else goes to the
# ladder, including the four classes OURS loses and `terminal`, where the two
# arms tie at zero and the tie is broken toward the incumbent.
#
# This is the whole of the tuning surface, and it is a lookup rather than a
# threshold on purpose. A cutoff — "route to OURS where it beat B2 by more than
# k points" — would have a k in it, and k would have been chosen against the
# result it produces.
OURS_CLASSES: Final[frozenset[DeclineClass]] = frozenset({DeclineClass.AUTH_ABANDONED})


class HybridArm:
    """OURS on `auth_abandoned`, the fixed ladder everywhere else."""

    name = "HYBRID"

    def __init__(self, estimator, mode: ArmMode = ArmMode.ENFORCE) -> None:
        # INV-11 reaches HYBRID through what it contains. `OursArm` refuses
        # OBSERVE, and handing it this arm's mode is what makes the refusal
        # transitive rather than something HYBRID has to remember. Asserted
        # here too so the error names the arm the caller actually constructed.
        assert_enforce_only("OURS", mode)
        if mode is not ArmMode.ENFORCE:
            raise ValueError(
                "INV-11: arm HYBRID contains OURS and can never run in OBSERVE mode"
            )
        self.mode = mode
        self.ours = OursArm(estimator, mode)
        self.ladder = FixedLadderArm()

    def route(self, case: ObservedCase):
        """Which delegate owns this case. A pure function of one observed field."""
        return self.ours if classify(case.decline_code) in OURS_CLASSES else self.ladder

    def choose(self, case: ObservedCase, state: CaseState, legal: list[Action]) -> Action:
        return self.route(case).choose(case, state, legal)

    @property
    def decisions(self):
        """The policy decisions OURS made, for the cases it was given.

        Exposed so `report.py` can trace HYBRID exactly as it traces OURS. The
        ladder contributes none: it has no estimator and considers no
        alternatives, which is the difference the per-class table is measuring.
        """
        return self.ours.decisions
