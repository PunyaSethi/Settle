"""Escalation eligibility. SPEC §2.1.

A pure function of `ObservedCase`, and it lives here rather than in the
simulator for a structural reason.

A46 required any policy needing this flag to recompute it from observables
rather than be handed it from the sim side. That was impossible as written: the
rule lived in `settle/sim/generator.py`, and a policy module importing
`settle.sim` is an INV-8 breach. The dependency now runs sim -> policy —
`generator.py` imports this — and never the other way.

`settle/policy/` imports nothing from `settle.sim`. GEN-8 asserts it of this
module specifically, PURE-1 of the package.
"""

from typing import Final

from settle.schema.observed import ObservedCase

# SPEC §2 — "high value, retries exhausted". Both halves are observable, which
# is the whole point: a rule the policy cannot recompute is a channel.
# ASSERTED, with rows in PRIORS.md under Sampled parameters, where
# `settle.sim.generator.PARAMS` reads them back out of this module.
ESCALATION_MIN_AMOUNT_PAISE: Final[int] = 74_900
ESCALATION_MIN_ATTEMPT_NUMBER: Final[int] = 2


def is_escalation_eligible(case: ObservedCase) -> bool:
    """SPEC §2's escalation slice, from observables alone.

    High value, retries exhausted. Nothing here reads `CaseState`, so
    `legal_actions` can consult it without becoming state-dependent and LEG-3
    keeps holding.
    """
    return (
        case.amount_paise >= ESCALATION_MIN_AMOUNT_PAISE
        and case.attempt_number >= ESCALATION_MIN_ATTEMPT_NUMBER
    )
