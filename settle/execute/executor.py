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

from dataclasses import dataclass
from datetime import datetime

from settle.policy.gates import idempotency_key
from settle.policy.legal import is_debit
from settle.schema.action import Action
from settle.schema.enums import ReportedStatus
from settle.schema.observed import ObservedCase
from settle.schema.outcome import ReportedOutcome
from settle.schema.state import CaseState, as_of
from settle.sim.observability import ObservabilityConfig
from settle.sim.streams import Streams
from settle.sim.truth import HiddenTruth
from settle.sim.world import attempt


@dataclass(frozen=True)
class WorldHandle:
    """Everything the executor needs to run an action against the world.

    Opaque to the runner, which passes it straight through without inspecting
    it. That is what lets `settle/runner/` stay free of `settle.sim.truth`
    (RUN-9) while still driving a simulated world.
    """

    truth: HiddenTruth
    streams: Streams


def dispatch_key(case: ObservedCase, state: CaseState, action: Action) -> str:
    """INV-4. Re-exported so the runner can build it before it writes."""
    return idempotency_key(case, state, action)


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
        return _report(case, at, settled_amount=None, authorised=False, world=world,
                       observability=observability, tick=state.tick)

    result = attempt(case, world.truth, action, at, state.tick, world.streams)
    return _report(
        case,
        at,
        settled_amount=case.amount_paise if result.authorised else None,
        authorised=result.authorised,
        world=world,
        observability=observability,
        tick=state.tick,
    )


def _report(
    case: ObservedCase,
    at: datetime,
    *,
    settled_amount: int | None,
    authorised: bool,
    world: WorldHandle,
    observability: ObservabilityConfig,
    tick: int,
) -> ReportedOutcome:
    """Push an outcome through the observability layer. SPEC §6.

    A dropped webhook does not become a failure — it becomes silence. The agent
    cannot tell the two apart, and that is exactly the condition §7's SF-2
    describes: chasing a customer who has already paid.
    """
    if not authorised:
        return ReportedOutcome(
            case_id=case.case_id, at=at, status=ReportedStatus.FAILED, arrival_count=1
        )

    if world.streams.value(case.case_id, "webhook_drop", tick) < observability.webhook_drop_rate:
        return ReportedOutcome(
            case_id=case.case_id, at=at, status=ReportedStatus.NONE, arrival_count=1
        )

    duplicated = (
        world.streams.value(case.case_id, "webhook_dup", tick)
        < observability.webhook_duplicate_rate
    )
    return ReportedOutcome(
        case_id=case.case_id,
        at=at,
        status=ReportedStatus.CAPTURED,
        payment_id=f"pay_{case.case_id}_{tick}",
        amount_paise=settled_amount,
        arrival_count=2 if duplicated else 1,
    )
