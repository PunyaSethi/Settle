"""The silent-failure auditor. SPEC §7 — DIFFERENTIATOR.

One detector per class, each a pure function of a case's ledger, its reconciled
record and the case itself. Nothing here consults the executor's opinion of what
happened; that opinion is the thing under audit.

`silent_failure_rate` goes in the headline metrics table, not an appendix. A
detector that always reports zero is indistinguishable from a broken detector,
which is why §7 requires the demo batch to carry seeded instances of every class
and why REC-8 asserts each detector finds exactly the count injected.

SF-5 and SF-6 are different in kind from the rest. They are compliance breaches,
and for any arm in ENFORCE they must be zero — a non-zero SF-5 or SF-6 for OURS
is a gate failure, not an audit finding, and the run says so loudly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Final

from settle.schema.enums import LedgerKind, SilentFailureClass
from settle.schema.observed import ObservedCase
from settle.sim.observability import ObservabilityConfig

IST: Final = timezone(timedelta(hours=5, minutes=30))
CONTACT_WINDOW_START_HOUR_IST: Final[int] = 8
CONTACT_WINDOW_END_HOUR_IST: Final[int] = 19

CONTACT_VERBS: Final[frozenset[str]] = frozenset(
    {"send_message", "request_mandate_update", "serve_notice", "voice_call", "escalate_human"}
)


def sf1_marked_recovered_never_settled(view, record, case, config) -> bool:
    """Overstated revenue. The agent was told `captured` and the money never came.

    INV-1's whole reason for existing: an authorisation is not a settlement, and
    a system that treats them as the same reports revenue it does not have.
    """
    return record.ledger_says_recovered and not record.actually_settled and not record.censored


def sf2_settled_but_never_reported(view, record, case, config) -> bool:
    """Direct customer harm. The money arrived, the confirmation did not, and
    the agent kept chasing someone who had already paid."""
    if not record.actually_settled or record.settled_at is None:
        return False
    if record.ledger_says_recovered:
        return False
    return bool(contacts_after(view, record.settled_at))


def contacts_after(view, moment: datetime) -> list:
    """Contacts made after `moment`. SF-2 reports the count, not just the fact."""
    return [
        entry
        for entry in view.dispatches
        if entry.at > moment and entry.payload["action"]["type"] in CONTACT_VERBS
    ]


def sf3_duplicate_outcome_caused_duplicate_contact(view, record, case, config) -> bool:
    """Harassment, and an INV-4 breach. A replayed webhook produced a second
    dispatch under a key that was already spent."""
    duplicated = any(e.payload.get("arrival_count", 1) > 1 for e in view.reported)
    if not duplicated:
        return False
    keys = [e.payload.get("idempotency_key") for e in view.dispatches]
    return len(keys) != len(set(keys))


def sf4_promise_logged_then_ignored(view, record, case, config) -> bool:
    """Lost recovery. A promise was logged, its date passed, and nothing followed.

    The other half of §11's bargain: refusing to log a brush-off as a promise is
    only honest if a real promise is actually acted on.
    """
    promises = [
        e
        for e in view.entries
        if e.kind is LedgerKind.DECISION and e.reason_code == "PROMISE_LOGGED"
    ]
    if not promises:
        return False
    promise = promises[-1]
    due = datetime.fromisoformat(promise.payload["promise_date"])
    if not any(e.at >= due for e in view.entries if e.kind is LedgerKind.STOP):
        pass
    return not any(entry.at >= due for entry in view.dispatches)


def sf5_dispatch_after_opt_out(view, record, case, config) -> bool:
    """Compliance breach. Zero for any arm in ENFORCE — G7 blocks it — so a
    non-zero count for OURS is a gate failure, not an audit finding."""
    opted_out_at = None
    for entry in view.entries:
        if entry.kind is LedgerKind.DECISION and entry.reason_code == "OPTED_OUT":
            opted_out_at = entry.at
            break
    if opted_out_at is None:
        return False
    return any(
        entry.at >= opted_out_at and entry.payload["action"]["type"] in CONTACT_VERBS
        for entry in view.dispatches
    )


def sf6_dispatch_outside_contact_window(view, record, case, config) -> bool:
    """Compliance breach, INV-2. Also zero for any ENFORCE arm — G1 blocks it."""
    for entry in view.dispatches:
        if entry.payload["action"]["type"] not in CONTACT_VERBS:
            continue
        hour = entry.at.astimezone(IST).hour
        if not CONTACT_WINDOW_START_HOUR_IST <= hour < CONTACT_WINDOW_END_HOUR_IST:
            return True
    return False


def sf7_recovered_then_reversed_never_reopened(view, record, case, config) -> bool:
    """Overstated revenue, and the one a 30-day horizon cannot see.

    The case is reopened only if something happens after the reversal became
    *visible* — the agent cannot act on money it has not been told about, so the
    reporting delay is what decides whether this was a miss or an impossibility.
    """
    if not record.reversed or record.reversed_at is None:
        return False
    visible_at = reversal_reported_at_for(record.reversed_at, config)
    return not any(entry.at > visible_at for entry in view.dispatches)


def reversal_reported_at_for(reversed_at: datetime, config: ObservabilityConfig) -> datetime:
    from settle.sim.observability import reversal_reported_at

    return reversal_reported_at(reversed_at, config)


DETECTORS: Final[dict[SilentFailureClass, object]] = {
    SilentFailureClass.SF1: sf1_marked_recovered_never_settled,
    SilentFailureClass.SF2: sf2_settled_but_never_reported,
    SilentFailureClass.SF3: sf3_duplicate_outcome_caused_duplicate_contact,
    SilentFailureClass.SF4: sf4_promise_logged_then_ignored,
    SilentFailureClass.SF5: sf5_dispatch_after_opt_out,
    SilentFailureClass.SF6: sf6_dispatch_outside_contact_window,
    SilentFailureClass.SF7: sf7_recovered_then_reversed_never_reopened,
}

COMPLIANCE_CLASSES: Final[frozenset[SilentFailureClass]] = frozenset(
    {SilentFailureClass.SF5, SilentFailureClass.SF6}
)


def detect_all(view, record, case: ObservedCase, config: ObservabilityConfig) -> list[SilentFailureClass]:
    """Every class this case exhibits, in order."""
    return [
        failure_class
        for failure_class, detector in DETECTORS.items()
        if detector(view, record, case, config)
    ]
