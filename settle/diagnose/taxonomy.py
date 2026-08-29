"""Decline code to class. SPEC §9.

A pure lookup, and deliberately nothing more. The LLM is never invoked on a
decline code: a gateway string is structured data, and routing it through a
model would buy nothing while adding latency, cost and non-determinism to a
dictionary access. This module imports nothing from `settle.text`.

Codes the table does not know map to `AMBIGUOUS` and are counted. §9 fails a run
whose unmapped-code rate exceeds 5%, so the count has to be observable rather
than silently folded in — `classify_counted` is what makes that measurable.
"""

from typing import Final, NamedTuple

from settle.schema.enums import ActionType, DeclineClass

# SPEC §9, the Codes column. Structure, not priors — no numbers, no PRIORS rows.
CODE_TO_CLASS: Final[dict[str, DeclineClass]] = {
    "insufficient_funds": DeclineClass.TIME_SHIFTABLE,
    "gateway_timeout": DeclineClass.TRANSIENT,
    "issuer_down": DeclineClass.TRANSIENT,
    "card_expired": DeclineClass.DEAD_INSTRUMENT,
    "mandate_revoked": DeclineClass.DEAD_INSTRUMENT,
    "card_stolen": DeclineClass.DEAD_INSTRUMENT,
    "authentication_failed": DeclineClass.AUTH_ABANDONED,
    "do_not_honour": DeclineClass.AMBIGUOUS,
    "fraud_flagged": DeclineClass.TERMINAL,
}

# SPEC §9, the Viable actions column, read as a whitelist.
#
# Two readings were available. Under a blacklist reading — anything not in the
# Forbidden column is permitted — `dead_instrument` would permit `switch_rail`,
# `voice_call` and `escalate_human`, none of which the Viable column lists.
# `terminal` settles it: its Forbidden column says "everything else", which only
# parses if Viable is the whitelist. So Viable is exhaustive everywhere.
#
# `do_nothing` is added to every class because §5.3 requires it to be selectable
# always — an arm that cannot decline to act is a baseline, not a policy.
VIABLE_ACTIONS: Final[dict[DeclineClass, frozenset[ActionType]]] = {
    DeclineClass.TIME_SHIFTABLE: frozenset({ActionType.DO_NOTHING, ActionType.RETRY}),
    DeclineClass.TRANSIENT: frozenset({ActionType.DO_NOTHING, ActionType.RETRY}),
    DeclineClass.DEAD_INSTRUMENT: frozenset(
        {ActionType.DO_NOTHING, ActionType.REQUEST_MANDATE_UPDATE, ActionType.SEND_MESSAGE}
    ),
    DeclineClass.AUTH_ABANDONED: frozenset(
        {ActionType.DO_NOTHING, ActionType.SEND_MESSAGE, ActionType.SWITCH_RAIL}
    ),
    DeclineClass.AMBIGUOUS: frozenset(
        {ActionType.DO_NOTHING, ActionType.RETRY, ActionType.SEND_MESSAGE}
    ),
    DeclineClass.TERMINAL: frozenset({ActionType.DO_NOTHING, ActionType.ESCALATE_HUMAN}),
}

# SPEC §9, the Forbidden column, restricted to what is expressible as an action
# type. The column also carries conditions on action *parameters* — "same-hour
# retry", "retry same rail", "repeated retry" — which are not properties of a
# verb and cannot live in a type-level table. Those are handled where the
# parameter exists: by the hour grid, by `legal_actions`, and by G4.
FORBIDDEN_ACTIONS: Final[dict[DeclineClass, frozenset[ActionType]]] = {
    DeclineClass.TIME_SHIFTABLE: frozenset(
        {
            ActionType.SEND_MESSAGE,
            ActionType.REQUEST_MANDATE_UPDATE,
            ActionType.VOICE_CALL,
            ActionType.ESCALATE_HUMAN,
        }
    ),
    DeclineClass.TRANSIENT: frozenset(
        {
            ActionType.SEND_MESSAGE,
            ActionType.REQUEST_MANDATE_UPDATE,
            ActionType.VOICE_CALL,
            ActionType.ESCALATE_HUMAN,
        }
    ),
    DeclineClass.DEAD_INSTRUMENT: frozenset({ActionType.RETRY, ActionType.SWITCH_RAIL}),
    DeclineClass.AUTH_ABANDONED: frozenset({ActionType.RETRY}),
    DeclineClass.AMBIGUOUS: frozenset({ActionType.VOICE_CALL, ActionType.ESCALATE_HUMAN}),
    DeclineClass.TERMINAL: frozenset(
        {
            ActionType.RETRY,
            ActionType.SWITCH_RAIL,
            ActionType.SEND_MESSAGE,
            ActionType.REQUEST_MANDATE_UPDATE,
            ActionType.SERVE_NOTICE,
            ActionType.VOICE_CALL,
        }
    ),
}


class Diagnosis(NamedTuple):
    """A classification and whether the code was known.

    `mapped` is not decoration. §9 fails a run whose unmapped-code rate exceeds
    5%, and folding unknown codes into `AMBIGUOUS` without counting them would
    make that threshold unmeasurable.
    """

    decline_class: DeclineClass
    mapped: bool


def classify(decline_code: str) -> DeclineClass:
    """Decline code to class. Unknown codes are `AMBIGUOUS`, per SPEC §9."""
    return CODE_TO_CLASS.get(decline_code, DeclineClass.AMBIGUOUS)


def classify_counted(decline_code: str) -> Diagnosis:
    """As `classify`, but says whether the table knew the code."""
    known = decline_code in CODE_TO_CLASS
    return Diagnosis(CODE_TO_CLASS.get(decline_code, DeclineClass.AMBIGUOUS), known)


def unmapped_rate(decline_codes: list[str]) -> float:
    """Fraction of codes the table does not know. §9 fails above 0.05."""
    if not decline_codes:
        return 0.0
    return sum(1 for code in decline_codes if code not in CODE_TO_CLASS) / len(decline_codes)
