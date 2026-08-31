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
from settle.schema.enums import ActionType, DebtorBehaviour, IntentType
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

    p = truth.true_recoverability * lift * (PARAMS["p_authorise.base_floor"] + base)

    # Liquidity window: money is there just after payday, not before it.
    if _days_to_payday(at, truth.payday_day) <= PARAMS["world.liquidity_window_days"]:
        p *= 1.0 + payday_lift

    # Daytime attempts clear more often than 03:00 ones.
    if (
        PARAMS["p_authorise.day_window_start_hour"]
        <= at.hour
        <= PARAMS["p_authorise.day_window_end_hour"]
    ):
        p *= 1.0 + hour_lift

    # A rail switch only helps when the current instrument is the problem.
    if isinstance(action, SwitchRail) and action.to is case.rail:
        p *= PARAMS["p_authorise.switch_rail_same_rail_penalty"]
    if isinstance(action, Retry) and action.rail is not case.rail:
        p *= PARAMS["p_authorise.retry_cross_rail_penalty"]
    if isinstance(action, (SendMessage, VoiceCall)) and case.dnd_flag:
        p *= PARAMS["p_authorise.dnd_contact_penalty"]

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


# ---------------------------------------------------------------------------
# Natural recovery. SPEC §14.3, A77.
# ---------------------------------------------------------------------------

def natural_recovery_probability(intent: IntentType) -> float:
    """P(this case cures itself within the window), by intent."""
    return PARAMS[f"natural_recovery.{intent.value}"]


def natural_recovery_day(case: ObservedCase, streams: Streams) -> int:
    """When the self-cure lands, if it does."""
    roll = streams.value(case.case_id, "natural_recovery_day", 0)
    return 1 + int(roll * PARAMS["natural_recovery.max_day"])


def natural_recovery(
    case: ObservedCase, truth: HiddenTruth, tick: int, streams: Streams
) -> bool:
    """Has the case cured itself by `tick`, with no arm involved?

    The customer notices the failed debit and tops up, or pays through another
    route. Nobody contacted them.

    Both draws are addressed at tick 0 and read from a stream shared by every
    arm, so the self-cure is the *same event* whatever the arm did. That is what
    makes §14.3's subtraction mean anything: a case that recovers under B0 is
    not counted for any other arm, and it can only be excluded if it is
    identifiably the same case curing itself.

    Without this path B0 recovers nothing, incremental equals gross, and
    `do_nothing` has no positive expected value for any case — which puts the
    contact-restraint result out of reach by construction.
    """
    if streams.value(case.case_id, "natural_recovery_draw", 0) >= natural_recovery_probability(
        truth.intent_type
    ):
        return False
    return tick >= natural_recovery_day(case, streams) * 24


def natural_recovery_at(
    case: ObservedCase, truth: HiddenTruth, streams: Streams
) -> datetime | None:
    """When the self-cure lands, or None if this case never cures itself."""
    if streams.value(case.case_id, "natural_recovery_draw", 0) >= natural_recovery_probability(
        truth.intent_type
    ):
        return None
    return case.created_at + timedelta(days=natural_recovery_day(case, streams))


# ---------------------------------------------------------------------------
# Mandate re-authorisation. SPEC §6, §9, A86.
# ---------------------------------------------------------------------------
#
# Before A86, `request_mandate_update` was legal, selected, and structurally
# incapable of succeeding. It is contact-bearing, so `execute` never reached
# `attempt()` for it, and nothing anywhere revived a dead mandate. §9 named it
# as the recovery path for `dead_instrument` while the simulator gave that path
# a hard zero — 17% of the batch unwinnable by construction.
#
# The mechanism is deliberately not a coin flip at dispatch. A dispatch sets a
# pending re-authorisation some hours out; the mandate is still dead while that
# runs, and the arm has to decide what to do in the meantime. Only when the
# pending re-authorisation lands does the success draw happen, at the tick it
# lands on.

def mandate_response_delay_h(case: ObservedCase, tick: int, streams: Streams) -> int:
    """Hours between asking for a new mandate and the customer acting on it.

    Uniform on [1, max]. Drawn at the tick of the request, from a stream shared
    by every arm: two arms asking the same customer at the same moment wait the
    same time.
    """
    roll = streams.value(case.case_id, "mandate_response_delay", tick)
    return 1 + int(roll * PARAMS["mandate_update.response_delay_h_max"])


def mandate_revival_probability(intent: IntentType) -> float:
    """P(the customer re-authorises | they were asked), by intent.

    Conditioned on intent because a churned customer does not re-authorise. The
    whole point of asking is that only some customers still want the service,
    and a single global rate would make `intent_type` decorative in the place it
    decides most.
    """
    return PARAMS[f"mandate_update.success_rate.{intent.value}"]


def mandate_revives(
    case: ObservedCase, truth: HiddenTruth, at_tick: int, streams: Streams
) -> bool:
    """Does the pending re-authorisation take, at the tick it lands?

    Drawn from `mandate_revival_draw` at `at_tick`, shared across arms. Two arms
    whose requests land on the same tick get the same answer, which is what
    keeps §14.3's comparison about the policy rather than about luck.
    """
    return streams.value(case.case_id, "mandate_revival_draw", at_tick) < (
        mandate_revival_probability(truth.intent_type)
    )


# ---------------------------------------------------------------------------
# Contact response. SPEC §6, A89.
# ---------------------------------------------------------------------------
#
# Before A89, no contact verb could produce a settlement. `world.attempt()` ran
# for debits only, so a message, a voice call and a human escalation were
# dispatched, priced, gated and logged while being structurally incapable of
# recovering money. Every comparison of contact-heavy against contact-light arms
# made before this point was measuring the absence of a mechanism, not a policy
# difference. A86 fixed one instance of this; A89 fixes the class.
#
# The mechanism mirrors A86 deliberately, and for the same reason it is not a
# coin flip at dispatch: a contact sets a pending customer response some hours
# out, and the arm has to decide what to do while it waits.
#
# What a contact produces is a CUSTOMER-INITIATED payment, not a debit. The
# distinction matters in one direction only — nobody submitted anything to a
# rail, so G3, G4 and G9 have nothing to say about it — and in every other
# respect it is a payment: it runs through `settle()` like any other, so
# `auth_no_settle_rate`, `settlement_lag_h` and `will_reverse` all apply. A
# payment prompted by a message can still be an SF-1, and it can still be
# dropped by the reporting layer into an SF-2.

# The verbs `p_authorise` applies the DND penalty to. Inherited from there
# rather than decided again here: `p_authorise.dnd_contact_penalty` had a PRIORS
# row and was unreachable for exactly the reason A89 exists — the branch that
# read it tested for `SendMessage` and `VoiceCall` inside a function only debits
# ever reached.
_DND_PENALISED_VERBS: Final[frozenset[ActionType]] = frozenset(
    {ActionType.SEND_MESSAGE, ActionType.VOICE_CALL}
)


def contact_response_delay_h(case: ObservedCase, tick: int, streams: Streams) -> int:
    """Hours between a contact going out and the customer acting on it.

    Uniform on [1, max]. Drawn at the tick of the contact, from a stream shared
    by every arm: two arms messaging the same customer at the same moment wait
    the same time.
    """
    roll = streams.value(case.case_id, "contact_response_delay", tick)
    return 1 + int(roll * PARAMS["contact_response.delay_h_max"])


def contact_response_probability(
    case: ObservedCase,
    intent: IntentType,
    behaviour: DebtorBehaviour,
    verb: ActionType,
) -> float:
    """P(this contact is answered with a payment). Deterministic, no draws.

    Three factors, each with its own PRIORS row and its own job:

    * `contact_response.rate[intent]` — a message to someone who has left is not
      a message that gets paid. A single global rate would make `intent_type`
      decorative in the place it decides most.
    * `action_lift[verb]` — the same constant that scales a debit's chance,
      reused rather than duplicated. A voice call outranks an SMS here for the
      same declared reason it does there, and `serve_notice` sits at zero
      because a regulatory notice is not a persuasion.
    * `contact_response.behaviour_multiplier[behaviour]` — §8's debtors. A
      `go_silent` customer is near zero by definition; `pay_then_complain` is
      the one that reliably pays.
    """
    lift = ACTION_LIFT[verb]
    if lift == 0.0:
        return 0.0
    p = (
        PARAMS[f"contact_response.rate.{intent.value}"]
        * lift
        * PARAMS[f"contact_response.behaviour_multiplier.{behaviour.value}"]
    )
    if case.dnd_flag and verb in _DND_PENALISED_VERBS:
        p *= PARAMS["p_authorise.dnd_contact_penalty"]
    return min(max(p, 0.0), 1.0)


def contact_responds(
    case: ObservedCase,
    truth: HiddenTruth,
    behaviour: DebtorBehaviour,
    verb: ActionType,
    at_tick: int,
    streams: Streams,
) -> bool:
    """Does the customer pay, at the tick the response lands?

    Drawn from `contact_response_draw` at `at_tick` — the tick the response is
    *due*, never the tick the runner happens to notice it. Two arms whose
    contacts land on the same tick get the same answer, and an arm that notices
    late gets the same answer it would have got on time (WLD-8).
    """
    return streams.value(case.case_id, "contact_response_draw", at_tick) < (
        contact_response_probability(case, truth.intent_type, behaviour, verb)
    )


def contact_payment(
    case: ObservedCase,
    truth: HiddenTruth,
    behaviour: DebtorBehaviour,
    verb: ActionType,
    at: datetime,
    at_tick: int,
    streams: Streams,
) -> ActualOutcome | None:
    """The payment a contact prompted, or None if the customer did not pay.

    `settle()` is called, not reimplemented. A customer-initiated payment is
    still a payment: it can authorise and never settle, and it can settle and
    reverse. Routing it around the two-step that carries INV-1 would make
    messaging the one channel where money is certain — which is the opposite of
    the failure this project exists to model (WLD-7).
    """
    if not contact_responds(case, truth, behaviour, verb, at_tick, streams):
        return None
    return settle(case, truth, at, at_tick, streams)
