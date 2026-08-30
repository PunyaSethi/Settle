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
from datetime import datetime
from typing import Final

from settle.policy.gates import idempotency_key
from settle.sim.generator import PARAMS, behaviour_for
from settle.policy.legal import is_contact, is_debit
from settle.schema.action import Action
from settle.schema.enums import ReportedStatus
from settle.schema.observed import ObservedCase
from settle.schema.outcome import ReportedOutcome
from settle.schema.state import CaseState, as_of
from settle.sim.observability import ObservabilityConfig, report
from settle.sim.streams import Streams
from settle.sim.truth import ActualOutcome, HiddenTruth
from settle.sim.world import attempt, reversal_at

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
