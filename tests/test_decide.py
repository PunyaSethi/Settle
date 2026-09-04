"""CP18 — live case entry. SPEC §9, §10, §12, §16.

DEC-1 is the one that matters. A live demo that ran a second, friendlier policy
would be worth less than no live demo: the point of the screen is not that it
produces a plausible answer, it is that the answer is the one the 10,000-case
run would have produced for the same case. So the test does not check that the
endpoint returns something reasonable — it constructs the case and state by
hand, calls `policy.choose` directly, and requires the same decision, the same
alternatives, the same numbers.

DEC-3 is the demo. Three presets, each chosen so one mechanism is visible, and
each asserted for the behaviour it claims rather than for producing output. A
preset that stopped demonstrating its mechanism would still look fine on screen,
which is exactly why it needs a test.
"""

import json
import re
from pathlib import Path

import pytest

from settle.agent.estimator import load_latest
from settle.agent.policy import choose
from settle.api.decide import DecideRequest, decide
from settle.policy.legal import legal_actions
from settle.schema.enums import ActionType, ArmMode

REPO_ROOT = Path(__file__).resolve().parent.parent
VIEWER = REPO_ROOT / "viewer" / "index.html"

ESTIMATOR = load_latest()
needs_model = pytest.mark.skipif(ESTIMATOR is None, reason="no model in out/")


def call(**overrides) -> dict:
    return decide(DecideRequest(**overrides), ESTIMATOR)


# --------------------------------------------------------------------------
# DEC-1 — the same decision, not a similar one
# --------------------------------------------------------------------------

@needs_model
@pytest.mark.parametrize("overrides", [
    {},
    {"decline_code": "do_not_honour", "tick": 16},
    {"decline_code": "mandate_revoked", "mandate_state": "revoked"},
    {"decline_code": "gateway_timeout", "attempt_number": 3, "attempts_used": 2},
    {"decline_code": "do_not_honour", "promise_date": "2026-01-25"},
    {"decline_code": "authentication_failed", "amount_paise": 990000, "rail": "upi_autopay"},
])
def test_DEC_1_the_endpoint_returns_what_policy_choose_returns(overrides) -> None:
    """Constructed directly and compared, option by option.

    `decide()` is a formatter over `choose()`. If it ever became anything else —
    a filter, a re-ranking, a second estimator call — this fails, and it fails
    on the numbers rather than on the shape.
    """
    request = DecideRequest(**overrides)
    case, state = request.observed(), request.state()

    expected = choose(case, state, legal_actions(case, state), ESTIMATOR, ArmMode.ENFORCE)
    actual = decide(request, ESTIMATOR)

    assert actual["decision"]["reason_code"] == expected.reason_code
    assert actual["decision"]["chosen_type"] == expected.action.type.value
    assert actual["decision"]["p_settle"] == expected.p_success
    assert actual["decision"]["uplift"] == expected.uplift
    assert actual["decision"]["expected_value_paise"] == expected.expected_value
    assert actual["decision"]["economic_stop"] == expected.economic_stop

    # Every option, with the same probability and the same verdict. Sorted by
    # EV for display, so compare as a mapping rather than a sequence.
    served = {a["action_label"]: a for a in actual["alternatives"]}
    assert len(served) == len(expected.alternatives)
    for alt in expected.alternatives:
        payload = alt.action.model_dump(mode="json")
        label = next(k for k in served if k.startswith(payload["type"]))
        matches = [
            a for a in actual["alternatives"] if a["action_type"] == payload["type"]
        ]
        assert matches, payload
        exact = next(
            a for a in matches
            if a["p_settle"] == alt.p_success and a["ev_paise"] == alt.ev_paise
            and a["legal"] == alt.legal and a["block_gate"] == alt.block_gate
        )
        assert exact is not None
        assert label


@needs_model
def test_DEC_1_the_baseline_is_derived_and_not_re_estimated() -> None:
    """`p_settle(do_nothing)` is `p_success - uplift`, exactly.

    It is shown because every EV on the screen is an uplift over it. Calling the
    estimator a second time to get it would be a second number that could drift
    from the one the policy actually subtracted.
    """
    request = DecideRequest(decline_code="do_not_honour")
    case, state = request.observed(), request.state()
    expected = choose(case, state, legal_actions(case, state), ESTIMATOR, ArmMode.ENFORCE)

    result = decide(request, ESTIMATOR)
    baseline = result["decision"]["p_settle_do_nothing"]
    assert baseline == pytest.approx(expected.p_success - expected.uplift, abs=1e-12)

    # And every alternative's uplift is measured against that same baseline.
    for alt in result["alternatives"]:
        assert alt["uplift"] == pytest.approx(alt["p_settle"] - baseline, abs=1e-12)


# --------------------------------------------------------------------------
# DEC-2 — every candidate is fully priced
# --------------------------------------------------------------------------

@needs_model
def test_DEC_2_every_candidate_carries_the_full_pricing() -> None:
    """SPEC §5.4's `Alternative`, rendered. A row missing its gate is a row a
    reader cannot check."""
    for overrides in ({}, {"decline_code": "do_not_honour", "tick": 16},
                      {"decline_code": "mandate_revoked", "mandate_state": "revoked"}):
        result = call(**overrides)
        assert result["alternatives"], overrides
        for alt in result["alternatives"]:
            for key in ("action_label", "action_type", "p_settle", "p_settle_pct",
                        "uplift", "uplift_pct", "ev_paise", "ev_rupees",
                        "cost_rupees", "legal", "block_gate", "chosen"):
                assert key in alt, f"{key} missing from {alt}"
            assert isinstance(alt["legal"], bool)
            assert alt["p_settle_pct"].endswith("%")
            if not alt["legal"]:
                assert alt["block_gate"], "an illegal option names no gate"
            else:
                assert alt["block_gate"] is None
        assert sum(1 for a in result["alternatives"] if a["chosen"]) <= 1
        # Sorted by expected value, so the argmax and what it beat read in order.
        evs = [a["ev_paise"] for a in result["alternatives"]]
        assert evs == sorted(evs, reverse=True)


@needs_model
def test_DEC_2_excluded_verbs_are_distinguished_from_blocked_ones() -> None:
    """Two ways to be unavailable that look identical in a table and mean
    opposite things. §9 exclusion is not a gate block."""
    result = call(decline_code="mandate_revoked", mandate_state="revoked")
    excluded = {v["action_type"] for v in result["diagnosis"]["excluded_verbs"]}
    candidates = {a["action_type"] for a in result["alternatives"]}

    assert ActionType.RETRY.value in excluded
    assert ActionType.RETRY.value not in candidates
    assert excluded.isdisjoint(candidates), "a verb is both excluded and priced"
    for verb in result["diagnosis"]["excluded_verbs"]:
        assert verb["why"], f"{verb['action_type']} excluded with no reason"


# --------------------------------------------------------------------------
# DEC-3 — the three presets demonstrate what they claim
# --------------------------------------------------------------------------

@needs_model
def test_DEC_3_expired_card_has_no_retry_option_at_all() -> None:
    """dead_instrument. The point is absence, not refusal.

    A retry on a revoked mandate is not a blocked retry — §9 never made it a
    candidate. Shown as an exclusion so "no retry row" reads as a diagnosis.
    """
    result = call(decline_code="mandate_revoked", mandate_state="revoked", tick=0)
    assert result["diagnosis"]["decline_class"] == "dead_instrument"

    types = {a["action_type"] for a in result["alternatives"]}
    assert ActionType.RETRY.value not in types, "retry was priced for a dead instrument"
    assert ActionType.RETRY.value in {
        v["action_type"] for v in result["diagnosis"]["excluded_verbs"]
    }
    # And it is not merely blocked — nothing in the table names a gate for it.
    assert not any(
        a["action_type"] == ActionType.RETRY.value for a in result["alternatives"]
    )
    # The class does permit contact, and at 10:00 IST nothing blocks it.
    assert ActionType.REQUEST_MANDATE_UPDATE.value in types
    assert result["state"]["ist_hour"] == 10


@needs_model
def test_DEC_3_at_0200_g1_blocks_contact_and_leaves_the_retry_legal() -> None:
    """The gate that only touches contacts. A debit is silent, so G1 has no
    business stopping it, and a chart of "we contact less" means nothing if the
    quiet hours also stopped the machine working."""
    result = call(decline_code="do_not_honour", tick=16)
    assert result["state"]["ist_hour"] == 2
    assert result["diagnosis"]["decline_class"] == "ambiguous"

    by_type = {}
    for alt in result["alternatives"]:
        by_type.setdefault(alt["action_type"], []).append(alt)

    assert ActionType.SEND_MESSAGE.value in by_type, "no contact candidate to block"
    for alt in by_type[ActionType.SEND_MESSAGE.value]:
        assert alt["legal"] is False
        assert alt["block_gate"] == "G1"

    assert ActionType.RETRY.value in by_type
    assert any(a["legal"] for a in by_type[ActionType.RETRY.value]), (
        "G1 blocked a silent retry, which is not its job"
    )


@needs_model
def test_DEC_3_a_live_promise_suppresses_contact_but_not_the_retry() -> None:
    """G6. Between a logged promise and its date, we stop asking — and keep
    trying the instrument, which costs the customer nothing."""
    result = call(decline_code="do_not_honour", promise_date="2026-01-25", tick=0)
    assert result["state"]["promise_date"] == "2026-01-25"

    by_type = {}
    for alt in result["alternatives"]:
        by_type.setdefault(alt["action_type"], []).append(alt)

    assert ActionType.SEND_MESSAGE.value in by_type, "no contact candidate to suppress"
    for alt in by_type[ActionType.SEND_MESSAGE.value]:
        assert alt["legal"] is False
        assert alt["block_gate"] == "G6", f"suppressed by {alt['block_gate']}, not G6"

    assert any(a["legal"] for a in by_type[ActionType.RETRY.value])

    # Without the promise, the same case contacts freely: the suppression is the
    # promise's doing and not the hour's.
    unpromised = call(decline_code="do_not_honour", tick=0)
    assert any(
        a["legal"] for a in unpromised["alternatives"]
        if a["action_type"] == ActionType.SEND_MESSAGE.value
    )


def test_DEC_3_the_presets_in_the_page_match_the_cases_under_test() -> None:
    """The screen's presets are the ones asserted above.

    Parsed from the page rather than trusted: a preset edited to something that
    no longer shows its mechanism would leave these tests passing against cases
    nobody can reach from the UI.
    """
    html = VIEWER.read_text(encoding="utf-8")
    block = html[html.index("const DECIDE_PRESETS"): html.index("function renderDecide")]

    assert '"mandate_revoked"' in block and '"revoked"' in block
    assert block.count('"do_not_honour"') >= 2
    assert "tick: 16" in block
    assert '"2026-01-25"' in block
    # Each carries a stated reason; a preset with no explanation is a bookmark.
    assert block.count("why:") == 3


# --------------------------------------------------------------------------
# DEC-4 — nonsense is refused, never coerced
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    {"amount_paise": 0},
    {"amount_paise": -1},
    {"attempt_number": 0},
    {"observed_credit_day": 0},
    {"observed_credit_day": 29},
    {"rail": "carrier_pigeon"},
    {"mandate_state": "sort_of_active"},
    {"language": "klingon"},
    {"tick": -1},
    {"attempts_used": -1},
    {"promise_date": "not-a-date"},
    {"case_id": ""},
    {"plan_value_paise": 0},
    {"unknown_field": 1},
])
def test_DEC_4_invalid_input_is_rejected(bad) -> None:
    """Refused rather than priced. A decision computed from nonsense is worse
    than an error, because it looks like a decision."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DecideRequest(**bad)


@needs_model
def test_DEC_4_the_endpoint_reports_the_field_and_the_reason() -> None:
    """A judge who typed 0 into amount should read which field and why."""
    import asyncio

    from settle.api.decide import policy_decide

    class _Request:
        pass

    response = asyncio.run(policy_decide({"amount_paise": 0, "attempt_number": 0}))
    assert response.status_code == 422
    payload = json.loads(bytes(response.body))
    assert payload["reason_code"] == "INVALID_CASE"
    fields = {e["field"] for e in payload["errors"]}
    assert "amount_paise" in fields and "attempt_number" in fields
    for error in payload["errors"]:
        assert error["message"], "an error with no message"


@needs_model
def test_DEC_4_valid_input_is_not_coerced_on_the_way_through() -> None:
    """What was typed is what was priced. The response echoes the case back so
    a reader can check the decision belongs to the case they entered."""
    result = call(amount_paise=123456, attempt_number=4, rail="enach",
                  decline_code="issuer_down", dnd_flag=True)
    assert result["case"]["amount_rupees"] == "1,234.56"
    assert result["case"]["attempt_number"] == 4
    assert result["case"]["rail"] == "enach"
    assert result["case"]["decline_code"] == "issuer_down"
    assert result["case"]["dnd_flag"] is True


# --------------------------------------------------------------------------
# DEC-5 — the file:// screens are untouched
# --------------------------------------------------------------------------

def test_DEC_5_screen_4_says_it_needs_the_api_from_file_protocol() -> None:
    """The endpoint is unreachable from `file://` and the screen says so, the
    same way the voice lab does. A screen that failed silently would read as a
    broken demo."""
    html = VIEWER.read_text(encoding="utf-8")
    block = html[html.index("function renderDecide"): html.index("function renderDecision")]

    assert 'location.protocol !== "file:"' in block
    assert "needs the API" in block
    assert "uvicorn settle.api.app:app" in block
    # It returns before building the form, rather than rendering a dead one.
    assert "return;" in block


def test_DEC_5_screens_1_and_2_do_not_depend_on_the_endpoint() -> None:
    """VIW-4's property, restated here because CP18 is what could break it.

    Screens 1 and 2 render from data the page already carries. Only screens 3
    and 4 fetch, and both guard on the protocol first.
    """
    html = VIEWER.read_text(encoding="utf-8")
    batch = html[html.index("function renderBatch"): html.index("function renderTrace")]
    trace = html[html.index("function renderTrace"): html.index("function caseDetail")]

    for name, block in (("renderBatch", batch), ("renderTrace", trace)):
        assert "fetch(" not in block, f"{name} fetches, so file:// would break"
        assert "/policy/decide" not in block


def test_DEC_5_screen_4_reuses_screen_2s_alternatives_table() -> None:
    """One renderer, so the two screens cannot disagree.

    They show the same shape because they come from the same policy. Two
    renderers would be free to drift, and the first time they did, the live
    screen would quietly be telling a judge something the batch did not.
    """
    html = VIEWER.read_text(encoding="utf-8")
    assert html.count("function alternativesTable") == 1, "more than one renderer"
    # Called from the case trace and from the live decision, and nowhere builds
    # a second table with the same columns.
    assert html.count("alternativesTable(") >= 3
    assert html.count('"Blocked by"') == 1, "a second table duplicates the columns"
