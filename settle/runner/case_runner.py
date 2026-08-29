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

Tick advancement
----------------
Stepping hourly across the 30-day decision horizon is 720 iterations per case
per arm — 7.2M for one arm at 10,000 cases, and six arms to run. Almost all of
those ticks are hours in which nothing could possibly have changed, so the
runner jumps to the next tick at which a verdict *could* differ:

  * a gate that blocked and clears with time contributes its clearing tick —
    G1 the next window opening, G2 the minimum-gap or rolling-window expiry,
    G6 the promise date;
  * a scheduled `retry(at_hour_offset=n)` contributes `tick + n`;
  * otherwise the runner advances by one day.

A gate that cannot clear by waiting contributes nothing. G9 with no notice
served is the example: no amount of time opens a notice window, only a
`serve_notice` does, so the runner falls through to the daily cadence and gives
the arm another decision rather than spinning.

The daily cadence is a unit, not a tuned parameter — the agent reconsiders once
a day when it has chosen to do nothing. If it ever becomes a knob, it needs a
PRIORS row (see §21).

Every step strictly increases the tick, so termination is guaranteed by S6
regardless of what an arm does.
"""

from datetime import date, datetime, timedelta
from typing import Final

from settle.audit.chain import Ledger
from settle.execute.executor import WorldHandle, dispatch_key, execute
from settle.policy.gates import (
    CONTACT_WINDOW_START_HOUR_IST,
    FREQUENCY_WINDOW_HOURS,
    MIN_CONTACT_GAP_HOURS,
    after_serve_notice,
    evaluate_gates,
    evaluation_hour,
)
from settle.policy.legal import is_contact, is_debit, legal_actions
from settle.policy.stops import DECISION_HORIZON_HOURS, check_stops
from settle.schema.action import Action, Retry, SwitchRail
from settle.schema.enums import ActionType, Actor, ArmMode, LedgerKind, ReportedStatus
from settle.schema.observed import ObservedCase
from settle.schema.outcome import ReportedOutcome
from settle.schema.state import CaseState, CaseStatus, as_of

# One day. A unit rather than a tuned parameter: when the arm has chosen to do
# nothing and no timer is pending, it reconsiders tomorrow.
DECISION_CADENCE_HOURS: Final[int] = 24

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

    if isinstance(action, Retry) and action.at_hour_offset:
        candidates.add(state.tick + action.at_hour_offset)

    future = {tick for tick in candidates if tick > state.tick}
    proposed = min(future) if future else state.tick + DECISION_CADENCE_HOURS
    return max(proposed, state.tick + 1)


def _apply_dispatch(case: ObservedCase, state: CaseState, action: Action, key: str) -> CaseState:
    """Record what the dispatch consumed. §5.7 — recorded, never inferred."""
    at = as_of(case.created_at, state)
    update: dict = {"dispatched_keys": state.dispatched_keys | {key}}

    if action.type is ActionType.RETRY:
        update["attempts_used"] = state.attempts_used + 1
    elif isinstance(action, SwitchRail):
        # A67: a switch is a change of instrument, not a retry.
        update["rail_switches_used"] = state.rail_switches_used + 1

    if is_contact(action):
        update["contacts_used"] = state.contacts_used + 1
        update["contact_history"] = state.contact_history + (at,)
        update["last_contact_at"] = at

    updated = state.model_copy(update=update)
    if action.type is ActionType.SERVE_NOTICE:
        updated = after_serve_notice(case, updated)
    return updated


def _apply_outcome(state: CaseState, outcome: ReportedOutcome) -> CaseState:
    """What a reported outcome is allowed to change.

    Not `settled`. INV-1 requires a settlement record, and a `captured` webhook
    is an authorisation. Only reconciliation may set that field, which is why
    S1 cannot fire from anything the runner sees.
    """
    return state


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

    for _ in range(MAX_STEPS_PER_CASE):
        # 1. stops
        stop = check_stops(case, state, arm.mode)
        if stop is not None:
            state = state.model_copy(
                update={
                    "status": CaseStatus.STOPPED,
                    "stop_reason": stop.reason_code,
                    "stop_class": stop.stop_class,
                }
            )
            log(LedgerKind.STOP, Actor.POLICY, {"stop": stop.stop}, stop.reason_code)
            return state

        # 2-3. the legal set, and the arm's choice from it
        legal = legal_actions(case, state)
        action = arm.choose(case, state, legal)

        # 4. gates — all eleven, always
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

        # 6. WRITE-AHEAD. The key is built, the entry is written and flushed,
        #    and only then does anything touch the world (INV-5).
        key = dispatch_key(case, state, action)
        log(
            LedgerKind.DISPATCH,
            Actor.SYSTEM,
            {"action": action.model_dump(mode="json"), "idempotency_key": key},
            "DISPATCH_INTENT",
        )
        outcome = execute(action, case, state, world, observability)

        # 7. record what the dispatch consumed, and what we were told
        state = _apply_dispatch(case, state, action, key)
        log(
            LedgerKind.REPORTED_OUTCOME,
            Actor.SYSTEM,
            {
                "status": outcome.status.value,
                "arrival_count": outcome.arrival_count,
                "payment_id": outcome.payment_id,
            },
            f"REPORTED_{outcome.status.value.upper()}",
        )
        state = _apply_outcome(state, outcome)

        # 8. advance
        state = state.model_copy(
            update={"tick": next_interesting_tick(case, state, action, result.blocked_by)}
        )

    raise RuntimeError(
        f"{case.case_id} did not stop within {MAX_STEPS_PER_CASE} steps — "
        "tick advancement is not making progress"
    )
