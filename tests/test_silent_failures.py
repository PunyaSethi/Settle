"""CP6 — the silent-failure auditor. SPEC §7 — DIFFERENTIATOR.

One test per class, each with a case that triggers it and a case that does not.
The negative half matters as much as the positive: a detector that fires on
everything is as useless as one that fires on nothing, and only the pair
distinguishes a working detector from a constant.
"""

from datetime import datetime, timedelta, timezone

import pytest

from settle.recon.reconcile import ReconciledCase, group_by_case, reconcile, seed_failures
from settle.recon.silent_failures import (
    COMPLIANCE_CLASSES,
    DETECTORS,
    detect_all,
    sf1_marked_recovered_never_settled,
    sf2_settled_but_never_reported,
    sf3_duplicate_outcome_caused_duplicate_contact,
    sf4_promise_logged_then_ignored,
    sf5_dispatch_after_opt_out,
    sf6_dispatch_outside_contact_window,
    sf7_recovered_then_reversed_never_reopened,
)
from settle.schema.enums import SilentFailureClass
from settle.sim.observability import ObservabilityConfig

CONFIG = ObservabilityConfig()


def seeded(class_name: str, index: int = 0):
    """One injected case of a class, arranged the way reconcile sees it."""
    entries, actuals, cases = seed_failures(1)
    case_id = f"{class_name}_{index}"
    reconciled = reconcile(entries, actuals, cases)
    views = group_by_case(entries, cases)
    return views[case_id], reconciled[case_id], cases[case_id]


def clean(case_id: str = "sf1_0"):
    """A case with a record that exhibits nothing."""
    view, _, case = seeded("sf1")
    empty = ReconciledCase(
        case_id=case_id, arm="X", ledger_says_recovered=False,
        actually_settled=False, settled_amount_paise=0,
    )
    return view, empty, case


@pytest.mark.parametrize("failure_class", list(SilentFailureClass))
def test_every_class_has_a_detector(failure_class):
    assert failure_class in DETECTORS
    assert callable(DETECTORS[failure_class])


# --------------------------------------------------------------------------
# SF-1 .. SF-7 — one test each, positive and negative
# --------------------------------------------------------------------------

def test_SF_1_marked_recovered_never_settled():
    """Overstated revenue. INV-1's reason for existing: a `captured` webhook is
    an authorisation, and an authorisation is not money."""
    view, record, case = seeded("sf1")
    assert sf1_marked_recovered_never_settled(view, record, case, CONFIG) is True
    assert record.ledger_says_recovered and not record.actually_settled

    settled = record.model_copy(update={"actually_settled": True})
    assert sf1_marked_recovered_never_settled(view, settled, case, CONFIG) is False

    censored = record.model_copy(update={"censored": True})
    assert sf1_marked_recovered_never_settled(view, censored, case, CONFIG) is False, (
        "a censored outcome is unknown, not a false recovery"
    )


def test_SF_2_settled_but_never_reported_and_chasing_continued():
    """Direct customer harm — the pair to `pay_then_complain` in §8."""
    view, record, case = seeded("sf2")
    assert sf2_settled_but_never_reported(view, record, case, CONFIG) is True
    assert record.actually_settled and not record.ledger_says_recovered

    told = record.model_copy(update={"ledger_says_recovered": True})
    assert sf2_settled_but_never_reported(view, told, case, CONFIG) is False

    unsettled = record.model_copy(update={"actually_settled": False, "settled_at": None})
    assert sf2_settled_but_never_reported(view, unsettled, case, CONFIG) is False


def test_SF_2_reports_contacts_made_after_settlement():
    from settle.recon.silent_failures import contacts_after

    view, record, _ = seeded("sf2")
    assert record.settled_at is not None
    assert len(contacts_after(view, record.settled_at)) == 1
    assert not contacts_after(view, record.settled_at + timedelta(days=30))


def test_SF_3_a_duplicate_outcome_produced_a_duplicate_contact():
    """Harassment, and an INV-4 breach. Both halves are required: a replayed
    webhook alone is not harm, and a repeated key alone is not a replay."""
    view, record, case = seeded("sf3")
    assert sf3_duplicate_outcome_caused_duplicate_contact(view, record, case, CONFIG) is True

    single, empty, case = clean()
    assert sf3_duplicate_outcome_caused_duplicate_contact(single, empty, case, CONFIG) is False


def test_SF_3_is_zero_when_g5_holds():
    """A duplicate webhook that produced no duplicate dispatch is G5 working,
    not a silent failure."""
    view, record, case = seeded("sf4")  # has a dispatch, no duplicate report
    assert sf3_duplicate_outcome_caused_duplicate_contact(view, record, case, CONFIG) is False


def test_SF_4_a_promise_was_logged_and_then_ignored():
    """The other half of §11's bargain: refusing to log a brush-off as a promise
    is only honest if a real promise is acted on."""
    view, record, case = seeded("sf4")
    assert sf4_promise_logged_then_ignored(view, record, case, CONFIG) is True

    none_logged, empty, case = clean()
    assert sf4_promise_logged_then_ignored(none_logged, empty, case, CONFIG) is False


def test_SF_5_a_dispatch_after_opt_out():
    view, record, case = seeded("sf5")
    assert sf5_dispatch_after_opt_out(view, record, case, CONFIG) is True

    never_opted, empty, case = clean()
    assert sf5_dispatch_after_opt_out(never_opted, empty, case, CONFIG) is False


def test_SF_6_a_dispatch_outside_the_contact_window():
    view, record, case = seeded("sf6")
    assert sf6_dispatch_outside_contact_window(view, record, case, CONFIG) is True

    daytime, record, case = seeded("sf4")  # its contact is at 10:30 IST
    assert sf6_dispatch_outside_contact_window(daytime, record, case, CONFIG) is False


def test_SF_6_ignores_silent_retries():
    """A retry is a message to the bank, not to a person. Counting it against
    the contact window would report a violation that never happened."""
    view, record, case = seeded("sf6")
    doctored = view.model_copy(
        update={
            "entries": tuple(
                e.model_copy(
                    update={"payload": {**e.payload, "action": {"type": "retry", "rail": "card"}}}
                )
                if e.kind.value == "dispatch"
                else e
                for e in view.entries
            )
        }
    )
    assert sf6_dispatch_outside_contact_window(doctored, record, case, CONFIG) is False


def test_SF_7_recovered_then_reversed_and_never_reopened():
    """The class a 30-day horizon cannot see. §13.1 scores at 60 for this."""
    view, record, case = seeded("sf7")
    assert sf7_recovered_then_reversed_never_reopened(view, record, case, CONFIG) is True
    assert record.reversed and record.reversed_at is not None

    not_reversed = record.model_copy(update={"reversed": False, "reversed_at": None})
    assert sf7_recovered_then_reversed_never_reopened(view, not_reversed, case, CONFIG) is False


def test_SF_7_a_reopened_case_is_not_a_silent_failure():
    """Reopening is measured from when the reversal became *visible*. The agent
    cannot act on money it has not been told about, so the reporting delay is
    what separates a miss from an impossibility."""
    from settle.schema.enums import Actor, LedgerKind
    from settle.schema.ledger import LedgerEntry
    from settle.sim.observability import reversal_reported_at

    view, record, case = seeded("sf7")
    assert record.reversed_at is not None
    after = reversal_reported_at(record.reversed_at, CONFIG) + timedelta(hours=1)
    reopened = view.model_copy(
        update={
            "entries": view.entries
            + (
                LedgerEntry(
                    seq=999, case_id=view.case_id, at=after, kind=LedgerKind.DISPATCH,
                    actor=Actor.SYSTEM,
                    payload={"action": {"type": "retry", "rail": "card"}, "idempotency_key": "z"},
                    reason_code="DISPATCH_INTENT", prev_hash="0" * 64, hash="0" * 64, arm="X",
                ),
            )
        }
    )
    assert sf7_recovered_then_reversed_never_reopened(reopened, record, case, CONFIG) is False


# --------------------------------------------------------------------------
# The compliance classes are different in kind
# --------------------------------------------------------------------------

def test_the_compliance_classes_are_named_and_are_exactly_two():
    assert COMPLIANCE_CLASSES == {SilentFailureClass.SF5, SilentFailureClass.SF6}


def test_detect_all_returns_every_class_a_case_exhibits():
    entries, actuals, cases = seed_failures(2)
    reconciled = reconcile(entries, actuals, cases)
    for case_id, record in reconciled.items():
        expected = case_id.split("_")[0].upper().replace("SF", "SF-")
        assert [f.value for f in record.silent_failures] == [expected]


def test_a_clean_case_exhibits_nothing():
    """The detectors must be capable of saying no."""
    view, empty, case = clean()
    empty_view = view.model_copy(update={"entries": ()})
    assert detect_all(empty_view, empty, case, CONFIG) == []
