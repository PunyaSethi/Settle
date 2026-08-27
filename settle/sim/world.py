"""The response model. SPEC §5.2, §5.5.

Given `(case, truth, action, hour, tick)`, decide what actually happened. Every
draw comes from an addressed stream (SPEC §14.2) and nothing reads a clock —
GEN-5 enforces both, because a single `datetime.now()` in here would make GEN-1
false the next day and nobody would notice until the numbers stopped matching.

The two-step that carries INV-1 lives here: an action can authorise without
settling, and a settlement can reverse. `ReportedOutcome` is what the agent
will be told; `ActualOutcome` is what the money did.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

from pydantic import BaseModel, ConfigDict

from settle.schema.action import Action, Retry, SendMessage, SwitchRail, VoiceCall
from settle.schema.enums import ActionType
from settle.schema.observed import ObservedCase
from settle.sim.generator import PARAMS
from settle.sim.streams import Streams
from settle.sim.truth import ActualOutcome, HiddenTruth

# How much each verb moves P(authorise) relative to the case's own
# recoverability. These live in PARAMS with PRIORS rows, not as literals here:
# they decide whether a retry outperforms a message, which puts them upstream
# of every rupee in §14.4. INV-10 covers any number that can move a metric.
ACTION_LIFT: Final[dict[ActionType, float]] = {
    action_type: PARAMS[f"action_lift.{action_type.value}"] for action_type in ActionType
}


class AttemptResult(BaseModel):
    """What one action actually did."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    case_id: str
    authorised: bool
    p_authorise: float
    actual: ActualOutcome | None = None


def _days_to_payday(at: datetime, payday_day: int) -> int:
    """Whole days from `at` to the next `payday_day`, using `at` only.

    No wall clock: `at` is derived from the case's `created_at` anchor.
    """
    day = at.day
    if day <= payday_day:
        return payday_day - day
    return (28 - day) + payday_day


def p_authorise(case: ObservedCase, truth: HiddenTruth, action: Action, at: datetime) -> float:
    """P(authorise | case, action, hour). Deterministic, no draws."""
    lift = ACTION_LIFT[action.type]
    if lift == 0.0:
        return 0.0

    params = truth.response_fn_params
    base = params.get("base", 0.2)
    payday_lift = params.get("payday_lift", 0.0)
    hour_lift = params.get("hour_lift", 0.0)

    p = truth.true_recoverability * lift * (0.5 + base)

    # Liquidity window: money is there just after payday, not before it.
    if _days_to_payday(at, truth.payday_day) <= 1:
        p *= 1.0 + payday_lift

    # Daytime attempts clear more often than 03:00 ones.
    if 9 <= at.hour <= 20:
        p *= 1.0 + hour_lift

    # A rail switch only helps when the current instrument is the problem.
    if isinstance(action, SwitchRail) and action.to is case.rail:
        p *= 0.5
    if isinstance(action, Retry) and action.rail is not case.rail:
        p *= 0.9
    if isinstance(action, (SendMessage, VoiceCall)) and case.dnd_flag:
        p *= 0.6

    return min(max(p, 0.0), 1.0)


def attempt(
    case: ObservedCase,
    truth: HiddenTruth,
    action: Action,
    at: datetime,
    tick: int,
    streams: Streams,
) -> AttemptResult:
    """Run one action against the world and return what actually happened."""
    p = p_authorise(case, truth, action, at)
    if p == 0.0:
        return AttemptResult(case_id=case.case_id, authorised=False, p_authorise=0.0)

    authorised = streams.value(case.case_id, "action_outcome", tick) < p
    if not authorised:
        return AttemptResult(case_id=case.case_id, authorised=False, p_authorise=p)

    return AttemptResult(
        case_id=case.case_id,
        authorised=True,
        p_authorise=p,
        actual=settle(case, truth, at, tick, streams),
    )


def settle(
    case: ObservedCase,
    truth: HiddenTruth,
    authorised_at: datetime,
    tick: int,
    streams: Streams,
) -> ActualOutcome:
    """Authorisation to settlement, and possibly back again.

    Two independent ways an authorisation fails to become money: the
    instrument's own disposition (`truth.will_settle`) and the bank-level
    auth-no-settle rate. Both are needed — the first is a property of the
    customer, the second a property of the rails — and INV-1 exists because
    either can bite after a `captured` webhook has already arrived.

    Neither reads the reporting layer, and this module does not import it. That
    separation is the point: `--perfect-observability` must not be able to make
    authorisation equivalent to settlement, because SF-1 is a fact about money,
    not about webhooks (SPEC §6).
    """
    settle_roll = streams.value(case.case_id, "settle_roll", tick)
    settled = truth.will_settle and settle_roll >= PARAMS["auth_no_settle_rate"]

    if not settled:
        return ActualOutcome(
            case_id=case.case_id, at=authorised_at, settled=False, amount_paise=None
        )

    settled_at = authorised_at + timedelta(hours=truth.settlement_lag_h)
    return ActualOutcome(
        case_id=case.case_id,
        at=authorised_at,
        settled=True,
        settled_at=settled_at,
        reversed=truth.will_reverse,
        amount_paise=case.amount_paise,
    )


def reversal_at(
    case: ObservedCase,
    settled_at: datetime,
    tick: int,
    streams: Streams,
    max_delay_days: int,
) -> datetime:
    """When a reversal lands.

    Whether it reverses is `truth.will_reverse`; the `reversal_roll` stream
    decides when. The delay routinely exceeds the 30-day decision horizon,
    which is precisely why §13.1 scores at 60 (SF-7).
    """
    roll = streams.value(case.case_id, "reversal_roll", tick)
    return settled_at + timedelta(days=1 + int(roll * max_delay_days))
