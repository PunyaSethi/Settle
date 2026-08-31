"""The case loop. SPEC §12, §13.

One arm, one case, run until a stop fires. The runner owns the ledger and the
ordering; it decides nothing about the case.

What the runner is not allowed to see
-------------------------------------
It never reads `HiddenTruth` or `ActualOutcome`. It sees a `ReportedOutcome`,
which has already passed through the observability layer and may be a drop, a
duplicate, or an authorisation that will never settle. Being correct under that
condition is the entire thesis, so the constraint is structural rather than
conventional: this module imports nothing from `settle.sim`, and RUN-9 asserts
it by walking the AST. The `WorldHandle` is opaque and passed straight through.

Scheduling
----------
`retry(at_hour_offset=n)` is a commitment to debit in n hours, not a debit now
with a note attached. Until CP5.1 the runner dispatched immediately and used the
offset only as a wake-up hint, which made the whole offset dimension of the
action grid a label rather than a behaviour — an estimator trained on it would
have learned nothing from it.

So a schedulable choice sets `state.scheduled` and the runner sleeps to
`due_tick`. **Gates are re-evaluated when it fires.** Circumstances change
between choosing and firing: the customer may have opted out, promised, or
raised a dispute. An action that fires on a verdict taken three days earlier is
a compliance hole, and it is exactly the shape of bug a replayed webhook would
exploit.

A blocked schedule is logged and cleared, never silently dropped. At most one is
pending; a second choice replaces it and the replacement is logged.

Mandate re-authorisation
------------------------
A dispatched `request_mandate_update` is a request, not an event. It sets
`state.mandate_update_due_tick` some hours out, and the mandate stays dead for
the whole of that wait — which is the point: an arm that asks has to decide what
to do while it waits. When that tick arrives the runner asks the world boundary
whether the customer actually re-authorised.

If they did, the runner advances the case's own `mandate_state` to ACTIVE.
`mandate_state` is an `ObservedCase` field (§5.1) and a merchant genuinely sees
the mandate registry flip, so this is the agent learning something true rather
than being handed hidden truth. G3 then stops blocking and the retry paths §9
closes for a *dead* credential open again for the replacement.

Contact response
----------------
A dispatched contact is a request too. It sets `state.contact_response_due_tick`
some hours out, and when that tick arrives the runner asks the world boundary
whether the customer went and paid of their own accord (A89). Most of the time
they did not, and that is silence rather than a failure — nothing was submitted
to a rail, so there is no decline to report.

Money already in flight is not erased by the agent's decision horizon. When a
stop fires with a response still pending, the runner resolves it before
stopping: the world runs to 60 days (§13.1) and a customer who was going to pay
on day 31 still pays. The draw is addressed at the *due* tick, never at the tick
the runner noticed, so an arm that stops early and an arm that runs on get the
same answer.

Tick advancement
----------------
Stepping hourly across the 30-day decision horizon is 720 iterations per case
per arm — 7.2M for one arm at 10,000 cases, and six arms to run. Almost all of
those ticks are hours in which nothing could possibly have changed, so the
runner jumps to the next tick at which a verdict *could* differ:

  * a gate that blocked and clears with time contributes its clearing tick —
    G1 the next window opening, G2 the minimum-gap or rolling-window expiry,
    G6 the promise date;
  * a pending commitment contributes its `due_tick`;
  * a pending re-authorisation contributes its own, because a revived mandate
    opens debit paths the arm should be given the chance to use;
  * otherwise the runner advances by one day.

A pending *contact response* (A89) deliberately does not. There is nothing for
an arm to decide at the moment a customer pays of their own accord, and waking
for it would insert extra decision points at whatever hour the response happens
to land — mostly hours when G1 shuts the contact window. Measured: it cut
EXPLORE's coverage of every contact verb by about 60% and inflated `do_nothing`,
because the extra ticks fall where contacts are illegal. It would also hand
contact-heavy arms more decisions than contact-light ones for no reason
connected to policy. The response is resolved opportunistically instead —
whenever the runner is next awake past its due tick, when a later contact
replaces it, and at the stop — and because the draw is addressed at the *due*
tick, resolving late gives bit-identically the same answer as resolving on time.

A gate that cannot clear by waiting contributes nothing. G9 with no notice
served is the example: no amount of time opens a notice window, only a
`serve_notice` does, so the runner falls through to the daily cadence and gives
the arm another decision rather than spinning.

The daily cadence lives in `POLICY_PARAMS` with a PRIORS row. It is not
cosmetic: it sets how many decisions an arm gets across the horizon, and
therefore contacts per case, which is a §14.4 headline.

Every step strictly increases the tick, so termination is guaranteed by S6
regardless of what an arm does.
"""

from datetime import date, datetime, time, timedelta
from typing import Final

from settle.audit.chain import Ledger
from settle.execute.executor import (
    WorldHandle,
    contact_response_delay,
    contact_response_outcome,
    dispatch_key,
    execute,
    mandate_update_delay,
    mandate_update_taken,
)
from settle.policy.gates import (
    CONTACT_WINDOW_START_HOUR_IST,
    FREQUENCY_WINDOW_HOURS,
    MIN_CONTACT_GAP_HOURS,
    after_serve_notice,
    evaluate_gates,
    evaluation_hour,
    target_rail,
)
from settle.policy.legal import is_contact, is_debit, legal_actions
from settle.policy.params import POLICY_PARAMS
from settle.policy.stops import DECISION_HORIZON_HOURS, check_stops
from settle.schema.action import Action, Retry, SwitchRail
from settle.schema.enums import (
    ActionType,
    Actor,
    ArmMode,
    LedgerKind,
    MandateState,
    Rail,
    ReportedStatus,
)
from settle.schema.observed import ObservedCase
from settle.schema.outcome import ReportedOutcome
from settle.text.classify import ReplyKind, ReplyVerdict, classify_reply
from settle.schema.state import CaseState, CaseStatus, Scheduled, as_of

# When the arm has chosen to do nothing and no timer is pending, it reconsiders
# tomorrow. In POLICY_PARAMS with a PRIORS row (A68): it sets how many decisions
# an arm gets across the horizon, and therefore contacts per case.
DECISION_CADENCE_HOURS: Final[int] = int(POLICY_PARAMS["decision_cadence_hours"])

# Reason codes the auditor keys on. SF-4 looks for PROMISE_LOGGED, SF-5 for
# OPTED_OUT, so these strings are a contract with settle/recon/.
_VERDICT_REASONS: Final[dict[ReplyKind, str]] = {
    ReplyKind.OPT_OUT: "OPTED_OUT",
    ReplyKind.DISPUTE: "DISPUTE_RAISED",
    ReplyKind.PROMISE: "PROMISE_LOGGED",
    ReplyKind.PAYMENT_CLAIM: "PAYMENT_CLAIMED",
    ReplyKind.HEDGED: "REPLY_HEDGED",
    ReplyKind.UNCLEAR: "REPLY_UNCLEAR",
}

# Termination is guaranteed by S6, but a bug in tick advancement would spin
# forever rather than fail. This turns that into a loud error.
MAX_STEPS_PER_CASE: Final[int] = DECISION_HORIZON_HOURS + 8


def _tick_of(case: ObservedCase, moment: datetime) -> int:
    """The tick containing `moment`, rounded up so a boundary is not missed."""
    delta = moment - case.created_at
    hours = delta.total_seconds() / 3600.0
    return int(hours) if float(hours).is_integer() else int(hours) + 1


def _tick_of_date(case: ObservedCase, day: date) -> int:
    """First tick on `day`, in the case's own frame."""
    midnight = datetime.combine(day, datetime.min.time(), tzinfo=case.created_at.tzinfo)
    return max(0, _tick_of(case, midnight))


def _next_contact_window_open(case: ObservedCase, state: CaseState) -> int:
    """The next tick at which G1 would permit a contact."""
    hour = evaluation_hour(case, state)
    ahead = (CONTACT_WINDOW_START_HOUR_IST - hour) % 24
    return state.tick + (ahead or 24)


def scheduled_offset(action: Action) -> int:
    """Hours between choosing an action and its firing. A71's offset dimension."""
    return action.at_hour_offset if isinstance(action, Retry) else 0


def next_interesting_tick(
    case: ObservedCase, state: CaseState, action: Action, blocked_by: tuple[str, ...]
) -> int:
    """The next tick at which a verdict could differ. Strictly greater than now."""
    candidates: set[int] = set()

    if "G1" in blocked_by:
        candidates.add(_next_contact_window_open(case, state))

    if "G2" in blocked_by:
        if state.last_contact_at is not None:
            candidates.add(
                _tick_of(case, state.last_contact_at + timedelta(hours=MIN_CONTACT_GAP_HOURS))
            )
        if state.contact_history:
            oldest = min(state.contact_history)
            candidates.add(_tick_of(case, oldest + timedelta(hours=FREQUENCY_WINDOW_HOURS)))

    if "G6" in blocked_by and state.promise_date is not None:
        candidates.add(_tick_of_date(case, state.promise_date))

    if state.scheduled is not None:
        candidates.add(state.scheduled.due_tick)

    # A86. The mandate can come back while the arm is doing nothing, and the
    # case looks entirely different afterwards, so it is worth waking for.
    if state.mandate_update_due_tick is not None:
        candidates.add(state.mandate_update_due_tick)


    future = {tick for tick in candidates if tick > state.tick}
    proposed = min(future) if future else state.tick + DECISION_CADENCE_HOURS
    # A commitment due past the horizon simply never fires; S6 stops the case
    # first. Letting the tick run past the horizon to reach it would break the
    # one bound the loop actually guarantees.
    return max(min(proposed, DECISION_HORIZON_HOURS), state.tick + 1)


def _apply_dispatch(case: ObservedCase, state: CaseState, action: Action, key: str) -> CaseState:
    """Record what the dispatch consumed. §5.7 — recorded, never inferred."""
    at = as_of(case.created_at, state)
    update: dict = {"dispatched_keys": state.dispatched_keys | {key}}

    if action.type is ActionType.RETRY:
        update["attempts_used"] = state.attempts_used + 1
    elif isinstance(action, SwitchRail):
        # A67: a switch is a change of instrument, not a retry.
        update["rail_switches_used"] = state.rail_switches_used + 1

    if is_debit(action):
        # The feature `days_since_last_attempt` is built from this. Recorded at
        # the tick the debit actually fires, which for a scheduled retry is its
        # `due_tick` and not the tick it was chosen at — the bank saw it when it
        # was submitted. `train.py` reconstructs the same quantity by adding the
        # offset to the decision tick (EST-12).
        update["last_attempt_tick"] = state.tick

    # A70: G4 counts submissions to the card network, whichever verb produced
    # them. A retry on card and a switch to card are both submissions.
    if is_debit(action) and target_rail(case, action) is Rail.CARD:
        update["card_submissions_used"] = state.card_submissions_used + 1

    if is_contact(action):
        update["contacts_used"] = state.contacts_used + 1
        update["contact_history"] = state.contact_history + (at,)
        update["last_contact_at"] = at

    updated = state.model_copy(update=update)
    if action.type is ActionType.SERVE_NOTICE:
        updated = after_serve_notice(case, updated)
    return updated


def _apply_outcome(
    case: ObservedCase, state: CaseState, outcome: ReportedOutcome
) -> tuple[CaseState, ReplyVerdict | None]:
    """What a reported outcome is allowed to change.

    Not `settled`. INV-1 requires a settlement record, and a `captured` webhook
    is an authorisation. Only reconciliation may set that field.

    A reply, however, changes the world the gates operate in — and it is the
    only thing that does. Until CP6.1 nothing read the text the debtors were
    already generating, so `opted_out` and `promise_date` were never set, G6 and
    G7 could never fire on a real case, and SF-4 and SF-5 were unreachable
    except by seeding.
    """
    if outcome.reply_text is None:
        return state, None

    verdict = classify_reply(outcome.reply_text, case.created_at.date())
    if verdict.kind is ReplyKind.OPT_OUT:
        return state.model_copy(update={"opted_out": True}), verdict
    if verdict.kind is ReplyKind.DISPUTE:
        return state.model_copy(update={"disputed": True}), verdict
    if verdict.kind is ReplyKind.PROMISE and verdict.promise_date is not None:
        return (
            state.model_copy(
                update={
                    "promise_date": verdict.promise_date,
                    "promise_logged_at": as_of(case.created_at, state),
                }
            ),
            verdict,
        )
    # hedged, unclear, payment_claim — read, and deliberately acted on by
    # setting nothing. §11: a brush-off logged as a promise suppresses contact
    # for weeks, which is the worse failure.
    return state, verdict


def run_case(
    case: ObservedCase,
    arm,
    world: WorldHandle,
    observability,
    ledger: Ledger,
    initial_state: CaseState | None = None,
) -> CaseState:
    """Run one case to a stop and return its final state."""
    state = initial_state or CaseState(case_id=case.case_id, arm=arm.name, arm_mode=arm.mode)

    def log(kind: LedgerKind, actor: Actor, payload: dict, reason_code: str) -> None:
        ledger.append(
            case_id=case.case_id,
            at=as_of(case.created_at, state),
            kind=kind,
            actor=actor,
            payload=payload,
            reason_code=reason_code,
            arm=arm.name,
        )

    def resolve_contact_response(current: CaseState) -> CaseState:
        """Settle up a pending customer response. A89.

        Addressed at the *due* tick rather than at the tick the runner noticed,
        so an arm that stops early and an arm that runs on get the same answer
        for the same case (WLD-8).

        A payment is reported through the same §6 layer a debit's outcome goes
        through, so it can be dropped into an SF-2 or duplicated into an SF-3.
        It does not set `settled` — INV-1 gives that to reconciliation alone.
        """
        due_tick = current.contact_response_due_tick
        verb = current.contact_response_verb
        cleared = current.model_copy(
            update={"contact_response_due_tick": None, "contact_response_verb": None}
        )
        if due_tick is None or verb is None:
            return cleared

        outcome = contact_response_outcome(case, world, verb, due_tick, observability)
        ledger.append(
            case_id=case.case_id,
            at=case.created_at + timedelta(hours=due_tick),
            kind=LedgerKind.EVENT,
            actor=Actor.SYSTEM,
            payload={"verb": verb.value, "paid": outcome is not None, "due_tick": due_tick},
            reason_code="CONTACT_PAID" if outcome else "CONTACT_IGNORED",
            arm=arm.name,
        )
        if outcome is None:
            # Silence, not a decline. Nothing reached a rail.
            return cleared

        ledger.append(
            case_id=case.case_id,
            at=case.created_at + timedelta(hours=due_tick),
            kind=LedgerKind.REPORTED_OUTCOME,
            actor=Actor.SYSTEM,
            payload={
                "status": outcome.status.value,
                "arrival_count": outcome.arrival_count,
                "payment_id": outcome.payment_id,
                "customer_initiated": True,
            },
            reason_code=f"REPORTED_{outcome.status.value.upper()}",
            arm=arm.name,
        )
        updated, _ = _apply_outcome(case, cleared, outcome)
        return updated

    def dispatch(current: CaseState, action: Action) -> CaseState:
        """WRITE-AHEAD. The key is built from the tick the action fires at, the
        entry is written and flushed, and only then does anything touch the
        world (INV-5, INV-4, §12 G5)."""
        key = dispatch_key(case, current, action)
        ledger.append(
            case_id=case.case_id,
            at=as_of(case.created_at, current),
            kind=LedgerKind.DISPATCH,
            actor=Actor.SYSTEM,
            payload={"action": action.model_dump(mode="json"), "idempotency_key": key},
            reason_code="DISPATCH_INTENT",
            arm=arm.name,
        )
        outcome = execute(action, case, current, world, observability)
        updated = _apply_dispatch(case, current, action, key)

        # A86. Asking for a new mandate is a request, not an event. It lands
        # some hours later, and the mandate is dead for all of them. A second
        # request while one is outstanding replaces it — the customer has one
        # link in front of them, not a queue.
        if action.type is ActionType.REQUEST_MANDATE_UPDATE:
            due = current.tick + mandate_update_delay(case, current, world)
            updated = updated.model_copy(update={"mandate_update_due_tick": due})
            ledger.append(
                case_id=case.case_id,
                at=as_of(case.created_at, current),
                kind=LedgerKind.DECISION,
                actor=Actor.SYSTEM,
                payload={"due_tick": due, "channel": getattr(action, "channel").value},
                reason_code="MANDATE_UPDATE_REQUESTED",
                arm=arm.name,
            )

        # A89. Every contact can be answered with a payment the customer makes
        # themselves. It lands hours later, and a second contact replaces the
        # pending one — a customer answers the most recent message, not a queue.
        if is_contact(action):
            # An outstanding chance is taken, not discarded. The draw is
            # addressed at its own due tick, so resolving it now gives exactly
            # the answer it would have given on time — and dropping it would
            # quietly penalise whichever arm contacts most, which is the arm
            # whose viability this checkpoint exists to test.
            if updated.contact_response_due_tick is not None:
                updated = resolve_contact_response(updated)
            due = current.tick + contact_response_delay(case, current, world)
            updated = updated.model_copy(
                update={
                    "contact_response_due_tick": due,
                    "contact_response_verb": action.type,
                }
            )
            ledger.append(
                case_id=case.case_id,
                at=as_of(case.created_at, current),
                kind=LedgerKind.DECISION,
                actor=Actor.SYSTEM,
                payload={"due_tick": due, "verb": action.type.value},
                reason_code="CONTACT_RESPONSE_PENDING",
                arm=arm.name,
            )
        ledger.append(
            case_id=case.case_id,
            at=as_of(case.created_at, current),
            kind=LedgerKind.REPORTED_OUTCOME,
            actor=Actor.SYSTEM,
            payload={
                "status": outcome.status.value,
                "arrival_count": outcome.arrival_count,
                "payment_id": outcome.payment_id,
            },
            reason_code=f"REPORTED_{outcome.status.value.upper()}",
            arm=arm.name,
        )

        updated, verdict = _apply_outcome(case, updated, outcome)
        if verdict is not None:
            # Logged whether or not it changed anything. A reply that was read
            # and deliberately ignored is a decision and belongs in the trace.
            payload: dict = {
                "kind": verdict.kind.value,
                "confidence": verdict.confidence.value,
                "matched_span": verdict.matched_span,
                "changed_state": verdict.kind.value in ("opt_out", "dispute", "promise"),
            }
            reason = _VERDICT_REASONS[verdict.kind]
            if verdict.promise_date is not None:
                payload["promise_date"] = datetime.combine(
                    verdict.promise_date, time.min, tzinfo=case.created_at.tzinfo
                ).isoformat()
            ledger.append(
                case_id=case.case_id,
                at=as_of(case.created_at, current),
                kind=LedgerKind.DECISION,
                actor=Actor.SYSTEM,
                payload=payload,
                reason_code=reason,
                arm=arm.name,
            )
        return updated


    for _ in range(MAX_STEPS_PER_CASE):
        # 1. stops
        stop = check_stops(case, state, arm.mode)
        if stop is not None:
            # A89. Money already in flight is not erased by the agent's decision
            # horizon. The world runs to 60 days (§13.1), so a customer who was
            # going to pay on day 31 still pays — dropping it here would
            # understate every contact-bearing arm by exactly the contacts it
            # made near the end.
            if state.contact_response_due_tick is not None:
                state = resolve_contact_response(state)
            state = state.model_copy(
                update={
                    "status": CaseStatus.STOPPED,
                    "stop_reason": stop.reason_code,
                    "stop_class": stop.stop_class,
                }
            )
            log(LedgerKind.STOP, Actor.POLICY, {"stop": stop.stop}, stop.reason_code)
            return state

        # 1b. a requested re-authorisation that has come due. A86.
        #     The world boundary answers; the runner never asks the simulator
        #     directly (RUN-9). A success advances the case's own
        #     `mandate_state`, which is an observable — the mandate registry
        #     really does flip — so G3 stops blocking and §9's retry ban on a
        #     *dead* credential no longer describes this case.
        if (
            state.mandate_update_due_tick is not None
            and state.tick >= state.mandate_update_due_tick
        ):
            taken = mandate_update_taken(case, world, state.mandate_update_due_tick)
            state = state.model_copy(
                update={
                    "mandate_update_due_tick": None,
                    "mandate_revived": state.mandate_revived or taken,
                }
            )
            if taken:
                case = case.model_copy(update={"mandate_state": MandateState.ACTIVE})
            log(
                LedgerKind.EVENT,
                Actor.SYSTEM,
                {
                    "re_authorised": taken,
                    "mandate_state": case.mandate_state.value,
                },
                "MANDATE_REVIVED" if taken else "MANDATE_UPDATE_IGNORED",
            )

        # 1c. a customer response that has come due. A89.
        if (
            state.contact_response_due_tick is not None
            and state.tick >= state.contact_response_due_tick
        ):
            state = resolve_contact_response(state)

        # 2. a commitment that has come due. Gates are evaluated again: the
        #    verdict that authorised it may be days old.
        if state.scheduled is not None and state.tick >= state.scheduled.due_tick:
            due = state.scheduled
            result = evaluate_gates(case, state, due.action, arm.mode)
            log(
                LedgerKind.GATE_CHECK,
                Actor.POLICY,
                {
                    "action": due.action.type.value,
                    "blocked_by": list(result.blocked_by),
                    "violations": list(result.violations),
                    "allowed": result.allowed,
                    "scheduled": True,
                    "scheduled_at": due.scheduled_at,
                    "due_tick": due.due_tick,
                },
                result.first_block or "GATES_PASSED",
            )
            state = state.model_copy(update={"scheduled": None})

            if not result.allowed:
                # Logged and cleared, never silently dropped. Control returns to
                # the arm, which decides again with the state as it now stands.
                log(
                    LedgerKind.DECISION,
                    Actor.POLICY,
                    {
                        "action": due.action.model_dump(mode="json"),
                        "due_tick": due.due_tick,
                        "blocked_by": list(result.blocked_by),
                    },
                    "SCHEDULE_BLOCKED",
                )
                state = state.model_copy(
                    update={"tick": next_interesting_tick(case, state, due.action, result.blocked_by)}
                )
                continue

            state = dispatch(state, due.action)
            state = state.model_copy(
                update={"tick": next_interesting_tick(case, state, due.action, ())}
            )
            continue

        # 3-4. the legal set, the arm's choice, and all eleven gates
        legal = legal_actions(case, state)
        action = arm.choose(case, state, legal)
        result = evaluate_gates(case, state, action, arm.mode)
        log(
            LedgerKind.GATE_CHECK,
            Actor.POLICY,
            {
                "action": action.type.value,
                "blocked_by": list(result.blocked_by),
                "violations": list(result.violations),
                "allowed": result.allowed,
            },
            result.first_block or "GATES_PASSED",
        )

        # 5. blocked in ENFORCE: nothing dispatches, advance and reconsider
        if not result.allowed:
            state = state.model_copy(
                update={"tick": next_interesting_tick(case, state, action, result.blocked_by)}
            )
            continue

        if action.type is ActionType.DO_NOTHING:
            state = state.model_copy(update={"tick": next_interesting_tick(case, state, action, ())})
            continue

        # 6. a schedulable choice is a commitment, not a dispatch
        offset = scheduled_offset(action)
        if offset > 0:
            commitment = Scheduled(
                action=action, due_tick=state.tick + offset, scheduled_at=state.tick
            )
            if state.scheduled is not None:
                log(
                    LedgerKind.DECISION,
                    Actor.POLICY,
                    {
                        "replaced": state.scheduled.action.model_dump(mode="json"),
                        "replaced_due_tick": state.scheduled.due_tick,
                        "action": action.model_dump(mode="json"),
                        "due_tick": commitment.due_tick,
                    },
                    "SCHEDULE_REPLACED",
                )
            log(
                LedgerKind.DECISION,
                Actor.POLICY,
                {
                    "action": action.model_dump(mode="json"),
                    "due_tick": commitment.due_tick,
                    "offset_hours": offset,
                },
                "SCHEDULED",
            )
            state = state.model_copy(update={"scheduled": commitment})
            state = state.model_copy(
                update={"tick": next_interesting_tick(case, state, action, ())}
            )
            continue

        # 7. immediate
        state = dispatch(state, action)
        state = state.model_copy(
            update={"tick": next_interesting_tick(case, state, action, result.blocked_by)}
        )

    raise RuntimeError(
        f"{case.case_id} did not stop within {MAX_STEPS_PER_CASE} steps — "
        "tick advancement is not making progress"
    )
