"""CP3 — stops. SPEC §13, §13.2.

STP-7 is the one that matters. The compliance/terminal split exists so that B3
can actually breach INV-3: if S4 fired in OBSERVE, it would stop the case before
G7 was ever consulted, and the unguarded baseline would report structurally zero
opt-out violations while appearing to test them. A baseline that cannot fail is
not a baseline.
"""

from datetime import datetime, timezone

import pytest

from settle.policy.stops import (
    ATTEMPT_BUDGET,
    CONTACT_BUDGET,
    DECISION_HORIZON_HOURS,
    accepts_dispatch,
    check_stops,
)
from settle.policy.legal import legal_actions
from settle.schema.enums import ArmMode, Language, MandateState, Rail, StopClass
from settle.schema.observed import ObservedCase
from settle.schema.state import CaseState, CaseStatus

AT = datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)


def case(**overrides) -> ObservedCase:
    payload = dict(
        case_id="case-1",
        created_at=AT,
        customer_id="cust-1",
        amount_paise=49900,
        rail=Rail.CARD,
        decline_code="insufficient_funds",
        decline_reason="Insufficient funds",
        attempt_number=1,
        mandate_state=MandateState.ACTIVE,
        tenure_months=7,
        prior_failures=1,
        prior_recoveries=0,
        plan_value_paise=49900,
        consent_whatsapp=True,
        dnd_flag=False,
        language=Language.EN,
    )
    payload.update(overrides)
    return ObservedCase(**payload)


def state(**overrides) -> CaseState:
    payload = dict(case_id="case-1", arm="OURS", arm_mode=ArmMode.ENFORCE)
    payload.update(overrides)
    return CaseState(**payload)


def test_no_stop_fires_on_a_clean_open_case():
    assert check_stops(case(), state(), ArmMode.ENFORCE) is None


# --------------------------------------------------------------------------
# STP-1 — S1 recovered
# --------------------------------------------------------------------------

def test_STP_1_s1_fires_only_on_a_settlement_never_an_authorisation():
    """INV-1. `settled` arrives from reconciliation, not from what the agent did."""
    assert check_stops(case(), state(), ArmMode.ENFORCE, settled=False) is None

    verdict = check_stops(case(), state(), ArmMode.ENFORCE, settled=True)
    assert verdict is not None
    assert verdict.stop == "S1"
    assert verdict.stop_class is StopClass.TERMINAL_STATE
    assert verdict.reason_code == "S1_RECOVERED_SETTLED"


def test_STP_1_s1_outranks_every_other_stop():
    """Money that arrived is the end of the case, whatever else is true."""
    messy = state(opted_out=True, disputed=True, attempts_used=ATTEMPT_BUDGET, tick=DECISION_HORIZON_HOURS)
    assert check_stops(case(), messy, ArmMode.ENFORCE, settled=True).stop == "S1"


# --------------------------------------------------------------------------
# STP-2 — S2 dead instrument, no customer path
# --------------------------------------------------------------------------

@pytest.mark.parametrize("code", ["card_expired", "mandate_revoked", "card_stolen"])
def test_STP_2_s2_fires_when_a_dead_instrument_has_no_customer_left_to_ask(code):
    verdict = check_stops(case(decline_code=code), state(opted_out=True), ArmMode.ENFORCE)
    assert verdict is not None
    assert verdict.stop == "S2"
    assert verdict.stop_class is StopClass.TERMINAL_STATE


def test_STP_2_a_dead_instrument_alone_is_not_terminal():
    """It is still one `request_mandate_update` away from recovery."""
    assert check_stops(case(decline_code="card_expired"), state(), ArmMode.ENFORCE) is None


def test_STP_2_opt_out_alone_is_not_s2():
    """On a live instrument, opting out is S4 — a compliance stop, not a dead end."""
    verdict = check_stops(case(decline_code="insufficient_funds"), state(opted_out=True), ArmMode.ENFORCE)
    assert verdict.stop == "S4"


# --------------------------------------------------------------------------
# STP-3 — S3 budget exhausted
# --------------------------------------------------------------------------

def test_STP_3_s3_fires_on_the_attempt_budget():
    assert check_stops(case(), state(attempts_used=ATTEMPT_BUDGET - 1), ArmMode.ENFORCE) is None
    verdict = check_stops(case(), state(attempts_used=ATTEMPT_BUDGET), ArmMode.ENFORCE)
    assert verdict.stop == "S3"
    assert verdict.reason_code == "S3_ATTEMPT_BUDGET_EXHAUSTED"


def test_STP_3_s3_fires_on_the_contact_budget_independently():
    assert check_stops(case(), state(contacts_used=CONTACT_BUDGET - 1), ArmMode.ENFORCE) is None
    verdict = check_stops(case(), state(contacts_used=CONTACT_BUDGET), ArmMode.ENFORCE)
    assert verdict.stop == "S3"
    assert verdict.reason_code == "S3_CONTACT_BUDGET_EXHAUSTED"


# --------------------------------------------------------------------------
# STP-4 — S4 opt-out
# --------------------------------------------------------------------------

def test_STP_4_s4_is_a_compliance_stop_and_fires_on_opt_out():
    verdict = check_stops(case(), state(opted_out=True), ArmMode.ENFORCE)
    assert verdict.stop == "S4"
    assert verdict.stop_class is StopClass.COMPLIANCE
    assert verdict.reason_code == "S4_OPT_OUT"


# --------------------------------------------------------------------------
# STP-5 — S5 dispute
# --------------------------------------------------------------------------

def test_STP_5_s5_is_a_compliance_stop_and_fires_on_a_dispute():
    verdict = check_stops(case(), state(disputed=True), ArmMode.ENFORCE)
    assert verdict.stop == "S5"
    assert verdict.stop_class is StopClass.COMPLIANCE
    assert verdict.reason_code == "S5_DISPUTE_RAISED"


# --------------------------------------------------------------------------
# STP-6 — S6 decision horizon
# --------------------------------------------------------------------------

def test_STP_6_s6_fires_at_the_thirty_day_decision_horizon():
    assert DECISION_HORIZON_HOURS == 30 * 24
    assert check_stops(case(), state(tick=DECISION_HORIZON_HOURS - 1), ArmMode.ENFORCE) is None
    verdict = check_stops(case(), state(tick=DECISION_HORIZON_HOURS), ArmMode.ENFORCE)
    assert verdict.stop == "S6"
    assert verdict.stop_class is StopClass.TERMINAL_STATE


def test_STP_6_the_decision_horizon_is_not_the_observation_horizon():
    """SPEC §13.1: the agent stops at 30 days, the world runs to 60. A single
    horizon would score a day-32 settlement as a loss and hide SF-7."""
    assert DECISION_HORIZON_HOURS < 60 * 24


# --------------------------------------------------------------------------
# STP-7 — the compliance / terminal split
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("mutation", "expected_stop"),
    [({"opted_out": True}, "S4"), ({"disputed": True}, "S5")],
)
def test_STP_7_compliance_stops_are_relaxed_in_observe(mutation, expected_stop):
    """B3 must be able to reach G7 and G8 in order to violate them."""
    assert check_stops(case(), state(**mutation), ArmMode.ENFORCE).stop == expected_stop
    assert check_stops(case(), state(**mutation), ArmMode.OBSERVE) is None


@pytest.mark.parametrize(
    ("mutation", "kwargs", "expected_stop"),
    [
        ({}, {"settled": True}, "S1"),
        ({"attempts_used": ATTEMPT_BUDGET}, {}, "S3"),
        ({"contacts_used": CONTACT_BUDGET}, {}, "S3"),
        ({"tick": DECISION_HORIZON_HOURS}, {}, "S6"),
    ],
)
def test_STP_7_terminal_state_stops_bind_in_observe_too(mutation, kwargs, expected_stop):
    """OBSERVE relaxes compliance, not physics. B3 is unguarded, not immortal."""
    for mode in (ArmMode.ENFORCE, ArmMode.OBSERVE):
        verdict = check_stops(case(), state(**mutation), mode, **kwargs)
        assert verdict is not None, f"{expected_stop} did not fire in {mode.value}"
        assert verdict.stop == expected_stop
        assert verdict.stop_class is StopClass.TERMINAL_STATE


def test_STP_7_s2_is_terminal_and_survives_observe_despite_needing_opt_out():
    """S2 reads `opted_out`, but it is a dead end rather than a compliance rule,
    so OBSERVE does not relax it."""
    dead = case(decline_code="card_expired")
    for mode in (ArmMode.ENFORCE, ArmMode.OBSERVE):
        verdict = check_stops(dead, state(opted_out=True), mode)
        assert verdict.stop == "S2"
        assert verdict.stop_class is StopClass.TERMINAL_STATE


def test_STP_7_s7_is_deliberately_absent():
    """S7 needs an EV, so it needs the estimator. It belongs in the policy at D3,
    not in a module whose value is being a pure function of its arguments."""
    import settle.policy.stops as stops_module

    source = stops_module.__doc__ or ""
    assert "S7" in source
    for mode in (ArmMode.ENFORCE, ArmMode.OBSERVE):
        for mutation in ({}, {"attempts_used": 1}, {"contacts_used": 1}):
            verdict = check_stops(case(), state(**mutation), mode)
            assert verdict is None or verdict.stop != "S7"


# --------------------------------------------------------------------------
# STP-8 — a stopped case is terminal
# --------------------------------------------------------------------------

@pytest.mark.parametrize("mode", list(ArmMode))
def test_STP_8_a_stopped_case_accepts_no_further_dispatch_in_any_mode(mode):
    """SPEC §13: post-stop events are recorded and do nothing."""
    stopped = state(
        status=CaseStatus.STOPPED, stop_reason="S4_OPT_OUT", stop_class=StopClass.COMPLIANCE
    )
    assert accepts_dispatch(stopped) is False
    assert legal_actions(case(), stopped) == []


@pytest.mark.parametrize("mode", list(ArmMode))
def test_STP_8_check_stops_reports_the_recorded_stop_rather_than_re_deriving_it(mode):
    """State transitions are recorded, never inferred (§5.7). A case stopped for
    S5 must not come back as S4 because the flags happen to suit."""
    stopped = state(
        status=CaseStatus.STOPPED,
        stop_reason="S5_DISPUTE_RAISED",
        stop_class=StopClass.COMPLIANCE,
        opted_out=True,
    )
    verdict = check_stops(case(), stopped, mode)
    assert verdict is not None
    assert verdict.stop == "S5_DISPUTE_RAISED"
    assert verdict.reason_code == "ALREADY_STOPPED"


def test_STP_8_an_open_case_does_accept_dispatch():
    assert accepts_dispatch(state()) is True
    assert legal_actions(case(), state()) != []
