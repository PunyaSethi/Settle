"""Feature construction. SPEC §10.1.

Every feature is something a real merchant could compute at decision time from
its own records. Nothing here reads `HiddenTruth`, and nothing here imports
`settle.sim` — EST-1 walks the AST to prove it. A model that could see
`payday_day` would predict liquidity timing perfectly and teach us nothing about
whether the timing signal is learnable from what a merchant actually has.

`day_of_month_at_dispatch` is the one that carries that question. Payday
clusters on the 1st and the 7th (§5.2), and a model given the day of month at
dispatch can in principle learn the liquidity window without ever seeing the
payday itself. If it learns nothing from it, that is a finding about the
feature set, and it gets reported rather than hidden.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Final

from settle.diagnose.taxonomy import classify
from settle.policy.params import POLICY_PARAMS
from settle.schema.action import Action, Retry, SendMessage, SwitchRail
from settle.schema.enums import ActionType, Channel, DeclineClass, Rail
from settle.schema.observed import ObservedCase

IST: Final = timezone(timedelta(hours=5, minutes=30))

# The agent's own belief about how close to a salary credit still counts as
# liquid. Deliberately NOT `world.liquidity_window_days`: that is the
# simulator's parameter and the agent may not read it (A85). A policy handed
# the generator's own number would demonstrate that we can read our own
# simulator, not that a merchant could learn the effect.
LIQUIDITY_WINDOW_DAYS: Final[int] = int(POLICY_PARAMS["liquidity_window_days_belief"])

# Days in each month, for the distance-to-month-boundary feature. February is
# 28 because `payday_day` is bounded to 1..28 (§5.2), so no salary lands on the
# 29th and leap years cannot change a liquidity window.
_DAYS_IN_MONTH: Final[tuple[int, ...]] = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)

_RAILS: Final = tuple(Rail)
_CLASSES: Final = tuple(DeclineClass)
_ACTIONS: Final = tuple(ActionType)
_CHANNELS: Final = tuple(Channel)

FEATURE_NAMES: Final[tuple[str, ...]] = (
    # --- the case, as the merchant records it ---
    "amount_paise",
    "log_amount",
    "plan_value_paise",
    "attempt_number",
    "tenure_months",
    "prior_failures",
    "prior_recoveries",
    "consent_whatsapp",
    "dnd_flag",
    "observed_credit_day",
    "observed_credit_day_known",
    "mandate_cap_known",
    *(f"rail_{r.value}" for r in _RAILS),
    *(f"class_{c.value}" for c in _CLASSES),
    # --- the action under consideration ---
    *(f"action_{a.value}" for a in _ACTIONS),
    "offset_hours",
    *(f"target_rail_{r.value}" for r in _RAILS),
    *(f"channel_{c.value}" for c in _CHANNELS),
    "channel_none",
    # --- when it would land ---
    "ist_hour_at_dispatch",
    "day_of_month_at_dispatch",
    "days_since_created",
    "inside_contact_window",
    # --- timing, stated cyclically ---
    # `day_of_month_at_dispatch` alone is a linear term through a cyclical
    # effect: probability rises as a salary lands and decays after, then rises
    # again. A monotonic fit through that learns roughly nothing, which is what
    # CP7's two-level worked example looked like. These three state the same
    # information in a shape a linear model can use.
    "days_to_month_start",
    "in_liquidity_window",
    "days_since_last_attempt",
    "has_prior_attempt",
    "hours_to_contact_window",
)


# G1's window, restated as a distance so it is usable as a number rather than a
# flag. `inside_contact_window` answers yes/no; this answers how far off.
_CONTACT_WINDOW_START: Final[int] = 8
_CONTACT_WINDOW_END: Final[int] = 19


def _hours_to_contact_window(hour: int) -> int:
    """Hours from `hour` until G1 next permits a contact. Zero if inside."""
    if _CONTACT_WINDOW_START <= hour < _CONTACT_WINDOW_END:
        return 0
    return (_CONTACT_WINDOW_START - hour) % 24


def days_to_month_start(day: int, month: int) -> int:
    """Distance to the nearest month boundary, 0-15.

    Zero on the 1st and on the last day of the month — both are adjacent to a
    salary credit, and a feature that treated the 31st as maximally far from
    payday would be describing the calendar rather than the customer.
    """
    length = _DAYS_IN_MONTH[month - 1]
    return min(day - 1, length - day + 1, 15)


def action_offset(action: Action) -> int:
    """Hours between the decision and the dispatch. Only `retry` schedules."""
    return action.at_hour_offset if isinstance(action, Retry) else 0


def target_rail(case: ObservedCase, action: Action) -> Rail:
    if isinstance(action, Retry):
        return action.rail
    if isinstance(action, SwitchRail):
        return action.to
    return case.rail


def action_channel(action: Action) -> Channel | None:
    return getattr(action, "channel", None)


def dispatch_moment(case: ObservedCase, action: Action, tick: int) -> datetime:
    """When the action would actually land, in the case's own frame.

    `tick + offset`, never a clock. A feature derived from wall time would make
    the model unreproducible and the ledger unreplayable.
    """
    return case.created_at + timedelta(hours=tick + action_offset(action))


def feature_row(
    case: ObservedCase, action: Action, tick: int, last_attempt_tick: int | None = None
) -> dict[str, float]:
    """One row, from `ObservedCase` + `Action` + `tick` and nothing else.

    `last_attempt_tick` is the tick of the previous debit on this case, which
    the caller reconstructs from the decision stream. It is still not hidden
    truth — a merchant knows when it last tried to charge someone.
    """
    at = dispatch_moment(case, action, tick)
    ist = at.astimezone(IST)
    decline_class = classify(case.decline_code)
    channel = action_channel(action)
    offset = action_offset(action)
    row: dict[str, float] = {
        "amount_paise": float(case.amount_paise),
        "log_amount": float(len(str(case.amount_paise))),
        "plan_value_paise": float(case.plan_value_paise),
        "attempt_number": float(case.attempt_number),
        "tenure_months": float(case.tenure_months),
        "prior_failures": float(case.prior_failures),
        "prior_recoveries": float(case.prior_recoveries),
        "consent_whatsapp": float(case.consent_whatsapp),
        "dnd_flag": float(case.dnd_flag),
        # Unknown is encoded as 0 *and* flagged, so the model can tell "the
        # first of the month" from "we have no idea".
        "observed_credit_day": float(case.observed_credit_day or 0),
        "observed_credit_day_known": float(case.observed_credit_day is not None),
        "mandate_cap_known": float(case.mandate_cap_paise is not None),
        "offset_hours": float(offset),
        "ist_hour_at_dispatch": float(ist.hour),
        "day_of_month_at_dispatch": float(ist.day),
        "days_since_created": float((tick + offset) / 24.0),
        "inside_contact_window": float(8 <= ist.hour < 19),
        "channel_none": float(channel is None),
        "days_to_month_start": float(days_to_month_start(ist.day, ist.month)),
        "in_liquidity_window": float(
            days_to_month_start(ist.day, ist.month) <= LIQUIDITY_WINDOW_DAYS
        ),
        # Measured at the dispatch moment, not at the decision moment. A retry
        # chosen now and fired in 72 hours reaches the bank 72 hours further
        # from the last attempt, and until CP10 this read `tick` alone — so the
        # feature that ranks 2nd of 45 by permutation importance was *identical*
        # across all eight offsets of a retry, which are exactly the candidates
        # the policy has to tell apart (G4).
        "days_since_last_attempt": float(
            0.0 if last_attempt_tick is None else max(0, tick + offset - last_attempt_tick) / 24.0
        ),
        # Hours until G1 would let a contact through, at the dispatch moment.
        # Zero inside the window. It varies across offsets, which `offset_hours`
        # alone does not tell the model anything useful about: 18h and 30h are
        # both "tomorrow", but one lands at 02:00 and the other at 14:00.
        "hours_to_contact_window": float(_hours_to_contact_window(ist.hour)),
        "has_prior_attempt": float(last_attempt_tick is not None),
    }
    for rail in _RAILS:
        row[f"rail_{rail.value}"] = float(case.rail is rail)
        row[f"target_rail_{rail.value}"] = float(target_rail(case, action) is rail)
    for cls in _CLASSES:
        row[f"class_{cls.value}"] = float(decline_class is cls)
    for action_type in _ACTIONS:
        row[f"action_{action_type.value}"] = float(action.type is action_type)
    for chan in _CHANNELS:
        row[f"channel_{chan.value}"] = float(channel is chan)
    return row


def feature_vector(
    case: ObservedCase, action: Action, tick: int, last_attempt_tick: int | None = None
) -> list[float]:
    """The row as an ordered vector, in `FEATURE_NAMES` order."""
    row = feature_row(case, action, tick, last_attempt_tick)
    return [row[name] for name in FEATURE_NAMES]
