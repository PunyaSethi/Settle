"""The executor. SPEC §6, §5.5, INV-4, INV-5.

The only module that touches the world. It decides nothing: it receives an
action that has already passed gates and performs it.

Ordering, and why it is not negotiable
--------------------------------------
INV-5: build the idempotency key, write the audit entry, then dispatch. The
runner owns the ledger and performs the write, so this module exposes the key
derivation and the dispatch, and the ordering is enforced where the ledger lives
(EXE-2). Writing the entry afterwards would mean a process that dies mid-
dispatch leaves no record of intent, and the next run contacts the customer
again — SF-3 harassment, caused by the audit system meant to prevent it.

What the agent is told
----------------------
`execute` returns a `ReportedOutcome`, never an `ActualOutcome`. The observability
layer (§6) drops and duplicates on the way out, so `status == "none"` means "we
heard nothing", which is not the same as "nothing happened". That gap is the
project.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

from settle.policy.gates import idempotency_key
from settle.sim.generator import PARAMS, behaviour_for
from settle.policy.legal import is_contact, is_debit
from settle.schema.action import Action
from settle.schema.enums import ActionType, ReportedStatus
from settle.schema.observed import ObservedCase
from settle.schema.outcome import ReportedOutcome
from settle.schema.state import CaseState, as_of
from settle.sim.observability import ObservabilityConfig, report
from settle.sim.streams import Streams
from settle.sim.truth import ActualOutcome, HiddenTruth
from settle.sim.world import (
    attempt,
    contact_payment,
    contact_response_delay_h,
    mandate_response_delay_h,
    mandate_revives,
    reversal_at,
)

REVERSAL_DELAY_DAYS_MAX: Final[int] = int(PARAMS["reversal_delay_days_max"])


@dataclass(frozen=True)
class WorldHandle:
    """Everything the executor needs to run an action against the world.

    Opaque to the runner, which passes it straight through without inspecting
    it. That is what lets `settle/runner/` stay free of `settle.sim.truth`
    (RUN-9) while still driving a simulated world.
    """

    truth: HiddenTruth
    streams: Streams
    # Where the world's own account of events is recorded, for reconciliation to
    # read afterwards. The runner passes this through without opening it — it is
    # entitled to `ReportedOutcome` and nothing else (RUN-9).
    actuals: list[tuple[ActualOutcome, datetime | None]] = field(default_factory=list)


def dispatch_key(case: ObservedCase, state: CaseState, action: Action) -> str:
    """INV-4. Re-exported so the runner can build it before it writes."""
    return idempotency_key(case, state, action)


def _reply_for(case: ObservedCase, state: CaseState, world: WorldHandle) -> str | None:
    """What the customer says back to a contact. SPEC §8, §11.

    Behaviour is derived from the batch seed rather than handed over, so the
    executor never needs the whole `GeneratedCase` — and the agent never sees
    the behaviour that produced the words, only the words.
    """
    from settle.sim.debtors import reply, reply_text

    behaviour = behaviour_for(world.streams.master_seed, case.case_id)
    spoken = reply(
        case.case_id, world.truth, behaviour, state.contacts_used, state.tick, world.streams
    )
    return reply_text(spoken, case.case_id, state.tick, world.streams) or None


def execute(
    action: Action,
    case: ObservedCase,
    state: CaseState,
    world: WorldHandle,
    observability: ObservabilityConfig,
) -> ReportedOutcome:
    """Perform `action` and return what the agent is subsequently told.

    Only a debit can produce an outcome. A message is dispatched and reported as
    `none` — there is nothing for a gateway to say about it.
    """
    at = as_of(case.created_at, state)

    if not is_debit(action):
        return ReportedOutcome(
            case_id=case.case_id, at=at, status=ReportedStatus.NONE, arrival_count=1,
            reply_text=_reply_for(case, state, world),
        )

    result = attempt(case, world.truth, action, at, state.tick, world.streams)
    if not result.authorised or result.actual is None:
        return ReportedOutcome(
            case_id=case.case_id, at=at, status=ReportedStatus.FAILED, arrival_count=1
        )

    actual = result.actual
    reversed_when: datetime | None = None
    if actual.settled and actual.reversed and actual.settled_at is not None:
        reversed_when = reversal_at(
            case, actual.settled_at, state.tick, world.streams, REVERSAL_DELAY_DAYS_MAX
        )
    world.actuals.append((actual, reversed_when))

    return report(
        actual,
        case_id=case.case_id,
        tick=state.tick,
        config=observability,
        streams=world.streams,
        authorised_at=at,
    )


# ---------------------------------------------------------------------------
# Mandate re-authorisation. SPEC §6, §9, A86.
# ---------------------------------------------------------------------------
#
# A pending re-authorisation is world state, so the two questions about it are
# answered here and nowhere else. `settle/runner/` may not import `settle.sim`
# (RUN-9) — it holds a `WorldHandle` it never opens — so the runner asks these
# two functions instead of reaching into the simulator itself.

def mandate_update_delay(case: ObservedCase, state: CaseState, world: WorldHandle) -> int:
    """How many hours until the requested re-authorisation lands, if it does.

    Drawn at the tick the request was dispatched. The mandate stays dead for the
    whole of it, which is what stops A86 from being a coin flip at dispatch.
    """
    return mandate_response_delay_h(case, state.tick, world.streams)


def mandate_update_taken(case: ObservedCase, world: WorldHandle, due_tick: int) -> bool:
    """Did the customer actually re-authorise, at the tick it landed?

    Conditioned on intent inside the world model: a churned customer does not
    re-authorise. Shared across arms, so this is a fact about the customer
    rather than about which arm happened to ask.

    Addressed at `due_tick`, not at whatever tick the runner noticed. The runner
    wakes for the due tick but can be carried past it — `next_interesting_tick`
    clamps to the decision horizon — and an arm that noticed late would
    otherwise draw a different number from one that noticed on time, which is
    exactly the coupling §14.2 exists to remove.
    """
    return mandate_revives(case, world.truth, due_tick, world.streams)


# ---------------------------------------------------------------------------
# Contact response. SPEC §6, A89.
# ---------------------------------------------------------------------------
#
# A pending customer response is world state, so the questions about it are
# answered here. `settle/runner/` may not import `settle.sim` (RUN-9), and the
# debtor behaviour that modulates the response is `settle/sim/debtors.py`
# territory the runner must never see — the agent gets the words a debtor says,
# never the behaviour that produced them.

def contact_response_delay(case: ObservedCase, state: CaseState, world: WorldHandle) -> int:
    """How many hours until a dispatched contact could be answered with money."""
    return contact_response_delay_h(case, state.tick, world.streams)


def contact_response_outcome(
    case: ObservedCase,
    world: WorldHandle,
    verb: ActionType,
    due_tick: int,
    observability: ObservabilityConfig,
) -> ReportedOutcome | None:
    """Resolve one pending contact response, and report what the agent hears.

    Returns `None` when the customer did not pay — which is most of the time,
    and is silence rather than a failure: nothing was submitted to a rail, so
    there is no decline for a gateway to report.

    When they did pay, the outcome is recorded on the handle for reconciliation
    and pushed through the same reporting layer a debit's outcome goes through.
    That is deliberate: a customer-initiated payment can be dropped on the way
    back, leaving the agent chasing someone who has already paid (SF-2), and it
    can be duplicated into a second dispatch (SF-3). Routing it around §6 would
    make messaging the one channel with perfect observability.
    """
    behaviour = behaviour_for(world.streams.master_seed, case.case_id)
    at = case.created_at + timedelta(hours=due_tick)
    actual = contact_payment(
        case, world.truth, behaviour, verb, at, due_tick, world.streams
    )
    if actual is None:
        return None

    reversed_when: datetime | None = None
    if actual.settled and actual.reversed and actual.settled_at is not None:
        reversed_when = reversal_at(
            case, actual.settled_at, due_tick, world.streams, REVERSAL_DELAY_DAYS_MAX
        )
    world.actuals.append((actual, reversed_when))

    return report(
        actual,
        case_id=case.case_id,
        tick=due_tick,
        config=observability,
        streams=world.streams,
        authorised_at=at,
    )
