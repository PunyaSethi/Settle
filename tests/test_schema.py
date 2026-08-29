"""CP1 — schema freeze. SPEC §5.

Test IDs are embedded in the test names so `scripts/gate.sh` can report which
named IDs actually ran. A checkpoint that runs green but silently skipped a
named test is not a checkpoint that passed.
"""

import ast
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from settle.schema.action import (
    ACTION_MODELS,
    DoNothing,
    EscalateHuman,
    RequestMandateUpdate,
    Retry,
    SendMessage,
    ServeNotice,
    SwitchRail,
    VoiceCall,
)
from settle.schema.canonical import canonical_json
from settle.schema.decision import Alternative, Decision
from settle.schema.enums import (
    Actor,
    ArmMode,
    Channel,
    ChosenBy,
    IntentType,
    Language,
    LedgerKind,
    MandateState,
    Rail,
    ReportedStatus,
)
from settle.schema.ledger import LedgerEntry
from settle.schema.observed import ObservedCase
from settle.schema.outcome import ReportedOutcome
from settle.sim.truth import ActualOutcome, HiddenTruth

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = REPO_ROOT / "settle" / "schema"

AT = datetime(2026, 8, 27, 10, 30, tzinfo=timezone.utc)
IST = timezone(timedelta(hours=5, minutes=30))


def observed_case(**overrides) -> dict:
    """Minimal valid ObservedCase payload. Optional fields stay unset."""
    payload = dict(
        case_id="case-1",
        created_at=AT,
        customer_id="cust-1",
        amount_paise=49900,
        rail=Rail.UPI_AUTOPAY,
        decline_code="insufficient_funds",
        decline_reason="Insufficient funds in the account",
        attempt_number=1,
        mandate_state=MandateState.ACTIVE,
        tenure_months=7,
        prior_failures=1,
        prior_recoveries=0,
        plan_value_paise=49900,
        consent_whatsapp=True,
        dnd_flag=False,
        language=Language.HINGLISH,
    )
    payload.update(overrides)
    return payload


def minimal_instances() -> dict[str, BaseModel]:
    """One minimal valid instance of every frozen model in SPEC §5."""
    return {
        "ObservedCase": ObservedCase(**observed_case()),
        "DoNothing": DoNothing(),
        "Retry": Retry(at_hour_offset=18, rail=Rail.UPI_AUTOPAY),
        "SwitchRail": SwitchRail(to=Rail.CARD),
        "SendMessage": SendMessage(channel=Channel.WHATSAPP, template_id="tpl_nudge_hi"),
        "RequestMandateUpdate": RequestMandateUpdate(channel=Channel.SMS),
        "ServeNotice": ServeNotice(channel=Channel.SMS),
        "Alternative": Alternative(
            action=VoiceCall(),
            p_success=0.44,
            ev_paise=-400,
            legal=False,
            block_gate="G1",
        ),
        "EscalateHuman": EscalateHuman(),
        "VoiceCall": VoiceCall(),
        "Decision": Decision(
            decision_id="dec-1",
            case_id="case-1",
            at=AT,
            action=Retry(at_hour_offset=18, rail=Rail.UPI_AUTOPAY),
            p_success=0.41,
            expected_value=17_800,
            alternatives=[
                Alternative(action=DoNothing(), p_success=0.19, ev_paise=0, legal=True),
                Alternative(
                    action=VoiceCall(),
                    p_success=0.44,
                    ev_paise=-400,
                    legal=False,
                    block_gate="G1",
                ),
            ],
            chosen_by=ChosenBy.MODEL,
            reason_code="TIME_SHIFTABLE_LIQUIDITY_WINDOW",
            arm="OURS",
            arm_mode=ArmMode.ENFORCE,
        ),
        "ReportedOutcome": ReportedOutcome(
            case_id="case-1",
            at=AT,
            status=ReportedStatus.CAPTURED,
            payment_id="pay_QxTzE9mXfKp2Ab",
            amount_paise=49900,
            arrival_count=1,
        ),
        "LedgerEntry": LedgerEntry(
            seq=0,
            case_id="case-1",
            at=AT,
            kind=LedgerKind.DECISION,
            actor=Actor.POLICY,
            payload={"reason": "liquidity window"},
            reason_code="DECIDED",
            prev_hash="0" * 64,
            hash="a" * 64,
            arm="OURS",
        ),
        "HiddenTruth": HiddenTruth(
            case_id="case-1",
            true_recoverability=0.63,
            intent_type=IntentType.WILLING_BROKE,
            patience_budget=4,
            payday_day=1,
            response_fn_params={"base": 0.2, "payday_lift": 0.35},
            will_settle=True,
            settlement_lag_h=38,
            will_reverse=False,
        ),
        "ActualOutcome": ActualOutcome(
            case_id="case-1",
            at=AT,
            settled=True,
            settled_at=AT + timedelta(hours=38),
            reversed=False,
            amount_paise=49900,
        ),
    }


# --------------------------------------------------------------------------
# SCH-1
# --------------------------------------------------------------------------

def test_SCH_1_every_model_instantiates_from_a_minimal_valid_payload():
    instances = minimal_instances()
    assert len(instances) == 15, "a frozen model was added or removed without a test update"
    for name, instance in instances.items():
        assert isinstance(instance, BaseModel), name


def test_SCH_1_optional_fields_default_rather_than_requiring_a_value():
    case = ObservedCase(**observed_case())
    assert case.mandate_cap_paise is None
    assert case.observed_credit_day is None
    assert Decision(
        decision_id="d",
        case_id="c",
        at=AT,
        action=DoNothing(),
        p_success=0.0,
        expected_value=0,
        chosen_by=ChosenBy.HEURISTIC,
        reason_code="NOOP",
        arm="B0",
        arm_mode=ArmMode.ENFORCE,
    ).propensity is None


# --------------------------------------------------------------------------
# SCH-2
# --------------------------------------------------------------------------

def test_SCH_2_negative_amount_rejected():
    with pytest.raises(ValidationError):
        ObservedCase(**observed_case(amount_paise=-1))
    with pytest.raises(ValidationError):
        ReportedOutcome(
            case_id="c", at=AT, status=ReportedStatus.CAPTURED, amount_paise=-1, arrival_count=1
        )


def test_SCH_2_unknown_enum_value_rejected():
    payload = json.dumps(
        {**observed_case(created_at=AT.isoformat()), "rail": "upi_intent"}, default=str
    )
    with pytest.raises(ValidationError):
        ObservedCase.model_validate_json(payload)


def test_SCH_2_naive_datetime_rejected():
    with pytest.raises(ValidationError):
        ObservedCase(**observed_case(created_at=datetime(2026, 8, 27, 10, 30)))
    with pytest.raises(ValidationError):
        ActualOutcome(case_id="c", at=datetime(2026, 8, 27), settled=False)


def test_SCH_2_float_where_int_required_rejected():
    for bad in (49900.5, 49900.0):
        with pytest.raises(ValidationError):
            ObservedCase(**observed_case(amount_paise=bad))
    with pytest.raises(ValidationError):
        ObservedCase.model_validate_json(
            json.dumps({**observed_case(created_at=AT.isoformat()), "amount_paise": 49900.5}, default=str)
        )


def test_SCH_2_unknown_field_rejected():
    with pytest.raises(ValidationError):
        ObservedCase(**observed_case(true_recoverability=0.9))


def test_SCH_2_probability_bounds_enforced():
    for bad in (-0.01, 1.01):
        with pytest.raises(ValidationError):
            HiddenTruth(
                case_id="c",
                true_recoverability=bad,
                intent_type=IntentType.CHURNED,
                patience_budget=0,
                payday_day=1,
                will_settle=False,
                settlement_lag_h=0,
                will_reverse=False,
            )


# --------------------------------------------------------------------------
# SCH-3 — INV-8, enforced structurally
# --------------------------------------------------------------------------

def _imported_modules(path: Path) -> set[str]:
    """Every absolute module name imported by `path`, relative imports resolved."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package_parts = path.relative_to(REPO_ROOT).with_suffix("").parts[:-1]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = list(package_parts[: len(package_parts) - (node.level - 1)])
            else:
                base = []
            module = ".".join([*base, node.module] if node.module else base)
            found.add(module)
            found.update(f"{module}.{alias.name}" for alias in node.names)
    return found


def test_SCH_3_schema_package_never_imports_settle_sim():
    modules = sorted(SCHEMA_DIR.rglob("*.py"))
    assert modules, "no modules found under settle/schema/"
    offenders = {}
    for module_path in modules:
        leaked = {
            name
            for name in _imported_modules(module_path)
            if name == "settle.sim" or name.startswith("settle.sim.")
        }
        if leaked:
            offenders[str(module_path.relative_to(REPO_ROOT))] = sorted(leaked)
    assert not offenders, f"INV-8 breach — settle/schema/ imports hidden truth: {offenders}"


def test_SCH_3_detects_a_planted_violation():
    """The detector must be able to fail. One that always passes is not a test."""
    planted = SCHEMA_DIR / "_inv8_probe.py"
    planted.write_text("from settle.sim.truth import HiddenTruth\n", encoding="utf-8")
    try:
        leaked = {n for n in _imported_modules(planted) if n.startswith("settle.sim")}
        assert leaked, "SCH-3 would not have caught a direct import of hidden truth"
    finally:
        planted.unlink()


def test_SCH_3_hidden_truth_is_not_reachable_from_settle_schema():
    import settle.schema as schema_pkg

    for name in ("HiddenTruth", "ActualOutcome"):
        assert not hasattr(schema_pkg, name), f"{name} must not be exported from settle.schema"


# --------------------------------------------------------------------------
# SCH-4
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(minimal_instances()))
def test_SCH_4_json_round_trip_is_lossless(name):
    original = minimal_instances()[name]
    restored = type(original).model_validate_json(original.model_dump_json())
    assert restored == original
    assert restored.model_dump_json() == original.model_dump_json()


def test_SCH_4_round_trip_preserves_the_action_discriminator():
    decision = minimal_instances()["Decision"]
    restored = Decision.model_validate_json(decision.model_dump_json())
    assert isinstance(restored.action, Retry)
    assert [type(alt.action) for alt in restored.alternatives] == [DoNothing, VoiceCall]


# --------------------------------------------------------------------------
# SCH-5
# --------------------------------------------------------------------------

def test_SCH_5_equal_objects_built_independently_hash_to_identical_bytes():
    for name, first in minimal_instances().items():
        second = minimal_instances()[name]
        assert first is not second
        assert canonical_json(first) == canonical_json(second), name


def test_SCH_5_dict_insertion_order_does_not_change_the_bytes():
    forwards = {"alpha": 1, "beta": {"x": 1, "y": 2}, "gamma": [3, 4]}
    backwards = {"gamma": [3, 4], "beta": {"y": 2, "x": 1}, "alpha": 1}
    assert canonical_json(forwards) == canonical_json(backwards)
    assert canonical_json(forwards) == b'{"alpha":1,"beta":{"x":1,"y":2},"gamma":[3,4]}'


def test_SCH_5_same_instant_in_a_different_timezone_hashes_the_same():
    utc = ReportedOutcome(case_id="c", at=AT, status=ReportedStatus.NONE, arrival_count=1)
    ist = ReportedOutcome(
        case_id="c", at=AT.astimezone(IST), status=ReportedStatus.NONE, arrival_count=1
    )
    assert utc.at == ist.at
    assert canonical_json(utc) == canonical_json(ist)


def test_SCH_5_output_is_ascii_and_whitespace_free():
    blob = canonical_json({"note": "pandrah tareekh — पंद्रह", "n": 1})
    blob.decode("ascii")
    assert b"\\u" in blob
    # No whitespace between tokens. Spaces inside a string value are content.
    assert b'", "' not in blob and b'": "' not in blob
    assert not any(ws in blob for ws in (b"\n", b"\t", b"\r"))


def test_SCH_5_refuses_encodings_that_would_not_be_byte_stable():
    from decimal import Decimal

    for value in (
        {"at": datetime(2026, 8, 27, 10, 30)},
        {"amount": Decimal("499.00")},
        {"tags": {"a", "b"}},
        {"p": float("nan")},
    ):
        with pytest.raises(TypeError):
            canonical_json(value)


# --------------------------------------------------------------------------
# SCH-6
# --------------------------------------------------------------------------

AMOUNT_LIKE = ("amount", "paise", "rupee", "value", "sum", "quantum", "partial")


def test_SCH_6_no_action_can_express_an_amount():
    for model in ACTION_MODELS:
        for field_name in model.model_fields:
            lowered = field_name.lower()
            assert not any(token in lowered for token in AMOUNT_LIKE), (
                f"{model.__name__}.{field_name} looks like an amount; SPEC §5.3 "
                "puts partial debits out of scope"
            )


def test_SCH_6_action_set_is_closed_and_is_exactly_the_seven_verbs():
    from settle.schema.enums import ActionType

    declared = {model.model_fields["type"].default for model in ACTION_MODELS}
    assert declared == set(ActionType)
    assert len(ACTION_MODELS) == 8


def test_SCH_6_an_amount_cannot_be_smuggled_in_as_an_extra_field():
    with pytest.raises(ValidationError):
        Retry(at_hour_offset=1, rail=Rail.CARD, amount_paise=100)


# --------------------------------------------------------------------------
# SCH-7
# --------------------------------------------------------------------------

def test_SCH_7_serve_notice_is_in_the_closed_verb_set_and_carries_a_channel():
    from settle.schema.enums import ActionType

    assert ServeNotice in ACTION_MODELS
    assert ServeNotice.model_fields["type"].default is ActionType.SERVE_NOTICE
    assert set(ServeNotice.model_fields) == {"type", "channel"}

    notice = ServeNotice(channel=Channel.SMS)
    assert notice.channel is Channel.SMS
    with pytest.raises(ValidationError):
        ServeNotice()


def test_SCH_7_serve_notice_survives_the_discriminated_union():
    decision = Decision(
        decision_id="dec-notice",
        case_id="case-enach",
        at=AT,
        action=ServeNotice(channel=Channel.SMS),
        p_success=0.0,
        expected_value=-15,
        chosen_by=ChosenBy.HEURISTIC,
        reason_code="G9_NOTICE_BEFORE_DEBIT",
        arm="OURS",
        arm_mode=ArmMode.ENFORCE,
    )
    restored = Decision.model_validate_json(decision.model_dump_json())
    assert isinstance(restored.action, ServeNotice)
    assert restored == decision


def test_SCH_7_channel_is_exactly_sms_whatsapp_voice():
    assert [c.value for c in Channel] == ["sms", "whatsapp", "voice"]
    with pytest.raises(ValidationError):
        ServeNotice.model_validate_json('{"type":"serve_notice","channel":"email"}')


# --------------------------------------------------------------------------
# SCH-8
# --------------------------------------------------------------------------

def test_SCH_8_alternative_round_trips_and_preserves_block_gate():
    blocked = Alternative(
        action=VoiceCall(), p_success=0.44, ev_paise=-400, legal=False, block_gate="G2"
    )
    restored = Alternative.model_validate_json(blocked.model_dump_json())
    assert restored == blocked
    assert restored.legal is False
    assert restored.block_gate == "G2"
    assert json.loads(blocked.model_dump_json())["block_gate"] == "G2"


def test_SCH_8_block_gate_survives_nested_inside_a_decision():
    decision = minimal_instances()["Decision"]
    restored = Decision.model_validate_json(decision.model_dump_json())
    economic, gated = restored.alternatives
    assert economic.legal is True and economic.block_gate is None
    assert gated.legal is False and gated.block_gate == "G1"


def test_SCH_8_a_legal_alternative_needs_no_gate_and_defaults_to_none():
    alt = Alternative(action=DoNothing(), p_success=0.19, ev_paise=0, legal=True)
    assert alt.block_gate is None
    assert canonical_json(alt) == canonical_json(
        Alternative(action=DoNothing(), p_success=0.19, ev_paise=0, legal=True, block_gate=None)
    )


# --------------------------------------------------------------------------
# SCH-9
# --------------------------------------------------------------------------

def test_SCH_9_illegal_alternative_must_name_the_gate():
    with pytest.raises(ValidationError):
        Alternative(action=VoiceCall(), p_success=0.4, ev_paise=-400, legal=False)
    with pytest.raises(ValidationError):
        Alternative(
            action=VoiceCall(), p_success=0.4, ev_paise=-400, legal=False, block_gate=None
        )


def test_SCH_9_legal_alternative_must_not_name_a_gate():
    with pytest.raises(ValidationError):
        Alternative(action=DoNothing(), p_success=0.1, ev_paise=0, legal=True, block_gate="G2")


def test_SCH_9_the_pairing_is_enforced_on_json_input_too():
    ok = Alternative(action=DoNothing(), p_success=0.1, ev_paise=0, legal=True)
    assert Alternative.model_validate_json(ok.model_dump_json()) == ok
    with pytest.raises(ValidationError):
        Alternative.model_validate_json(
            json.dumps(
                {
                    "action": {"type": "do_nothing"},
                    "p_success": 0.1,
                    "ev_paise": 0,
                    "legal": False,
                    "block_gate": None,
                }
            )
        )


# --------------------------------------------------------------------------
# SCH-10 — canonical_json and frozen collections
# --------------------------------------------------------------------------

def test_SCH_10_a_frozenset_encodes_sorted_so_a_case_state_can_be_hashed():
    """A61. Without this, `CaseState` could not enter the audit chain at all.

    A set's iteration order is not a property of its value — it varies with
    PYTHONHASHSEED — so the encoder sorts rather than trusting iteration.
    """
    assert canonical_json({"k": frozenset({"c", "a", "b"})}) == b'{"k":["a","b","c"]}'
    assert canonical_json({"k": frozenset({"a", "b", "c"})}) == canonical_json(
        {"k": frozenset({"c", "b", "a"})}
    )
    assert canonical_json(frozenset()) == b"[]"


def test_SCH_10_a_mutable_set_is_still_refused():
    """A mutable container inside a frozen contract is only half frozen."""
    with pytest.raises(TypeError, match="mutable sets"):
        canonical_json({"k": {"a", "b"}})


def test_SCH_10_case_state_round_trips_and_hashes_deterministically():
    from settle.schema.state import CaseState

    first = CaseState(
        case_id="case-1",
        arm="OURS",
        arm_mode=ArmMode.ENFORCE,
        contact_history=(AT,),
        last_contact_at=AT,
        dispatched_keys=frozenset({"beta", "alpha"}),
        settled=True,
        settled_at=AT,
        tick=7,
    )
    second = CaseState.model_validate_json(first.model_dump_json())
    assert second == first
    assert canonical_json(first) == canonical_json(second)
    assert b'"dispatched_keys":["alpha","beta"]' in canonical_json(first)
