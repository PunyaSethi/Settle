"""CP3 — diagnosis and the legal action set. SPEC §9.

The expected code-to-class mapping is written out longhand here rather than
imported from `taxonomy`. A test that reads the same table it is checking
asserts only that a dict equals itself; this one asserts that the code agrees
with SPEC §9.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from settle.diagnose.taxonomy import (
    CODE_TO_CLASS,
    FORBIDDEN_ACTIONS,
    VIABLE_ACTIONS,
    classify,
    classify_counted,
    unmapped_rate,
)
from settle.policy.legal import legal_actions
from settle.schema.action import ACTION_MODELS, Retry, SwitchRail
from settle.schema.enums import (
    ActionType,
    ArmMode,
    Channel,
    DeclineClass,
    Language,
    MandateState,
    Rail,
)
from settle.schema.observed import ObservedCase
from settle.schema.state import CaseState

AT = datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)

# SPEC §9, the Codes column, transcribed by hand.
SPEC_SECTION_9 = {
    "insufficient_funds": DeclineClass.TIME_SHIFTABLE,
    "gateway_timeout": DeclineClass.TRANSIENT,
    "issuer_down": DeclineClass.TRANSIENT,
    "card_expired": DeclineClass.DEAD_INSTRUMENT,
    "mandate_revoked": DeclineClass.DEAD_INSTRUMENT,
    "card_stolen": DeclineClass.DEAD_INSTRUMENT,
    "authentication_failed": DeclineClass.AUTH_ABANDONED,
    "do_not_honour": DeclineClass.AMBIGUOUS,
    "fraud_flagged": DeclineClass.TERMINAL,
}

UNMAPPED = ("issuer_error_91", "npci_rc_u69", "acquirer_declined", "", "INSUFFICIENT_FUNDS")


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


# --------------------------------------------------------------------------
# TAX-1
# --------------------------------------------------------------------------

@pytest.mark.parametrize(("code", "expected"), sorted(SPEC_SECTION_9.items()))
def test_TAX_1_every_declared_code_maps_to_its_declared_class(code, expected):
    assert classify(code) is expected
    assert classify_counted(code) == (expected, True)


def test_TAX_1_the_table_carries_exactly_the_codes_spec_section_9_lists():
    assert dict(CODE_TO_CLASS) == SPEC_SECTION_9


def test_TAX_1_every_class_is_reachable_from_some_code():
    """A class no code maps to is a branch the batch can never exercise."""
    assert set(CODE_TO_CLASS.values()) == set(DeclineClass)


# --------------------------------------------------------------------------
# TAX-2
# --------------------------------------------------------------------------

@pytest.mark.parametrize("code", UNMAPPED)
def test_TAX_2_unmapped_code_becomes_ambiguous_and_is_counted_as_unmapped(code):
    assert classify(code) is DeclineClass.AMBIGUOUS
    diagnosis = classify_counted(code)
    assert diagnosis.decline_class is DeclineClass.AMBIGUOUS
    assert diagnosis.mapped is False


def test_TAX_2_unmapped_is_distinguishable_from_a_genuine_do_not_honour():
    """Both land on AMBIGUOUS. Only one of them counts against §9's 5% gate."""
    assert classify_counted("do_not_honour").mapped is True
    assert classify_counted("npci_rc_u69").mapped is False


def test_TAX_2_unmapped_rate_is_measurable():
    assert unmapped_rate([]) == 0.0
    assert unmapped_rate(list(SPEC_SECTION_9)) == 0.0
    assert unmapped_rate(["npci_rc_u69"]) == 1.0
    assert unmapped_rate(["insufficient_funds"] * 49 + ["npci_rc_u69"]) == pytest.approx(0.02)


# --------------------------------------------------------------------------
# TAX-3 — the table is internally coherent
# --------------------------------------------------------------------------

@pytest.mark.parametrize("decline_class", list(DeclineClass))
def test_TAX_3_no_class_forbids_its_own_viable_actions(decline_class):
    viable = VIABLE_ACTIONS[decline_class]
    forbidden = FORBIDDEN_ACTIONS[decline_class]
    assert viable, f"{decline_class.value} has no viable action at all"
    overlap = viable & forbidden
    assert not overlap, (
        f"{decline_class.value} both permits and forbids "
        f"{sorted(a.value for a in overlap)} — the §9 table contradicts itself"
    )


@pytest.mark.parametrize("decline_class", list(DeclineClass))
def test_TAX_3_every_class_leaves_a_real_option_beyond_doing_nothing(decline_class):
    """`do_nothing` is always viable, so a class whose only option is `do_nothing`
    has no recovery path at all and should have been a stop, not a class."""
    beyond_nothing = VIABLE_ACTIONS[decline_class] - {ActionType.DO_NOTHING}
    assert beyond_nothing, f"{decline_class.value} offers nothing but do_nothing"


def test_TAX_3_do_nothing_is_viable_for_every_class():
    """SPEC §5.3: an arm that cannot decline to act is a baseline, not a policy."""
    for decline_class in DeclineClass:
        assert ActionType.DO_NOTHING in VIABLE_ACTIONS[decline_class]


# --------------------------------------------------------------------------
# LEG-1
# --------------------------------------------------------------------------

@pytest.mark.parametrize("code", ["card_expired", "mandate_revoked", "card_stolen"])
@pytest.mark.parametrize("rail", list(Rail))
def test_LEG_1_dead_instrument_yields_no_retry_ever(code, rail):
    actions = legal_actions(case(decline_code=code, rail=rail), state())
    assert not [a for a in actions if isinstance(a, Retry)]
    assert not [a for a in actions if isinstance(a, SwitchRail)]
    assert ActionType.RETRY not in {a.type for a in actions}


def test_LEG_1_dead_instrument_still_offers_a_path_to_a_new_mandate():
    """Forbidding retry must not leave the class with nothing to do."""
    actions = legal_actions(case(decline_code="card_expired"), state())
    assert ActionType.REQUEST_MANDATE_UPDATE in {a.type for a in actions}


# --------------------------------------------------------------------------
# LEG-2
# --------------------------------------------------------------------------

@pytest.mark.parametrize("code", sorted(SPEC_SECTION_9) + list(UNMAPPED))
@pytest.mark.parametrize("rail", list(Rail))
def test_LEG_2_legal_actions_is_always_a_subset_of_the_closed_verb_set(code, rail):
    actions = legal_actions(case(decline_code=code, rail=rail), state())
    assert actions, f"{code} on {rail.value} has no legal action, not even do_nothing"
    for action in actions:
        assert type(action) in ACTION_MODELS
        assert action.type in ActionType
        assert action.type in VIABLE_ACTIONS[classify(code)] or action.type is ActionType.SERVE_NOTICE


def test_LEG_2_a_stopped_case_has_no_legal_actions():
    from settle.schema.state import CaseStatus

    assert legal_actions(case(), state(status=CaseStatus.STOPPED, stop_reason="S4")) == []


def test_LEG_2_no_legal_action_is_ever_forbidden_by_its_class():
    for code in list(SPEC_SECTION_9) + list(UNMAPPED):
        decline_class = classify(code)
        for rail in Rail:
            for action in legal_actions(case(decline_code=code, rail=rail), state()):
                assert action.type not in FORBIDDEN_ACTIONS[decline_class], (
                    f"{code}/{rail.value} offered {action.type.value}, "
                    f"which §9 forbids for {decline_class.value}"
                )


# --------------------------------------------------------------------------
# LEG-3 — the two filters stay separate
# --------------------------------------------------------------------------

GATE_RELEVANT_STATE = [
    {"opted_out": True},
    {"disputed": True},
    {"promise_date": date(2026, 3, 1), "promise_logged_at": AT},
    {"contact_history": (AT, AT + timedelta(hours=1), AT + timedelta(hours=2))},
    {"last_contact_at": AT},
    {"attempts_used": 99},
    {"contacts_used": 99},
    {"dispatched_keys": frozenset({"deadbeef"})},
    {"notice_window_until": AT + timedelta(days=3)},
    {"tick": 700},
]


@pytest.mark.parametrize("mutation", GATE_RELEVANT_STATE)
@pytest.mark.parametrize("code", ["insufficient_funds", "card_expired", "do_not_honour"])
def test_LEG_3_gate_state_never_changes_the_legal_action_set(mutation, code):
    """EXPLORE logs propensity as 1/len(legal_pairs) at draw time (§10.1).

    If gate state moved this denominator, two cases with the same decline class
    would carry different propensities for reasons unrelated to what the policy
    could have chosen, and the estimator would reweight on the gates instead of
    on the action space.
    """
    subject = case(decline_code=code)
    baseline = legal_actions(subject, state())
    mutated = legal_actions(subject, state(**mutation))
    assert mutated == baseline, f"{list(mutation)} changed the legal set"


def test_LEG_3_gates_do_however_block_actions_the_legal_set_still_offers():
    """The separation is only meaningful if the second filter actually bites."""
    from settle.policy.gates import evaluate_gates
    from settle.schema.action import SendMessage

    subject = case(decline_code="card_expired")
    opted_out = state(opted_out=True)
    action = SendMessage(channel=Channel.SMS, template_id="t")

    assert action.type in {a.type for a in legal_actions(subject, opted_out)}
    result = evaluate_gates(subject, opted_out, action, 10, ArmMode.ENFORCE)
    assert not result.allowed
    assert "G7" in result.blocked_by
