"""CP3 — gates. SPEC §12.

One test per gate that blocks and permits, plus the four properties that make
the set trustworthy: all eleven verdicts are returned, OBSERVE records rather
than blocks, a silent retry is not a contact, and G9's window is set by an action
rather than appearing by magic.

There is no `hour` argument. It is derived from `case.created_at + state.tick`,
so an inconsistent (tick, hour) pair is unrepresentable rather than merely
undocumented (A59). Tests that need a particular IST hour set the tick with
`tick_for_hour`.

PURE-1 lives here because `gates.py` is the file most likely to reach for a
clock — every gate is a question about "now", and the answer has to come from
`case.created_at + tick` or the run stops being replayable.
"""

import ast
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from settle.policy.gates import (
    CARD_NETWORK_RETRY_CAP,
    CONTACT_WINDOW_END_HOUR_IST,
    CONTACT_WINDOW_START_HOUR_IST,
    FREQUENCY_CAP_PER_WINDOW,
    GATES,
    IST,
    MIN_CONTACT_GAP_HOURS,
    evaluation_hour,
    after_serve_notice,
    evaluate_gates,
    gate_g1,
    gate_g2,
    gate_g3,
    gate_g4,
    gate_g5,
    gate_g6,
    gate_g7,
    gate_g8,
    gate_g9,
    gate_g10,
    gate_g11,
    idempotency_key,
    ist_hour,
)
from settle.policy.legal import CONTACT_BEARING, DEBIT_BEARING
from settle.schema.action import (
    DoNothing,
    EscalateHuman,
    RequestMandateUpdate,
    Retry,
    SendMessage,
    ServeNotice,
    SwitchRail,
    VoiceCall,
)
from settle.policy.params import class_retry_cap
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

REPO_ROOT = Path(__file__).resolve().parent.parent

# 03:00Z is 08:30 IST, so a tick of N hours lands at IST hour 8+N.
AT = datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)

MESSAGE = SendMessage(channel=Channel.SMS, template_id="tpl")
RETRY_CARD = Retry(at_hour_offset=0, rail=Rail.CARD)
RETRY_ENACH = Retry(at_hour_offset=0, rail=Rail.ENACH)


def tick_for_hour(hour: int, day: int = 0) -> int:
    """Tick that lands on `hour` IST. AT is 03:00Z, which is 08:30 IST."""
    return (hour - 8) % 24 + 24 * day


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
# GAT-1 — G1 contact window
# --------------------------------------------------------------------------

def test_GAT_1_g1_blocks_a_contact_outside_the_window_and_permits_one_inside():
    late = state(tick=tick_for_hour(22))
    assert gate_g1(case(), late, MESSAGE).allowed is False
    assert gate_g1(case(), late, MESSAGE).reason_code == "G1_OUTSIDE_CONTACT_WINDOW"
    assert gate_g1(case(), state(tick=tick_for_hour(3)), MESSAGE).allowed is False
    assert gate_g1(case(), state(tick=tick_for_hour(10)), MESSAGE).allowed is True


def test_GAT_1_the_window_is_half_open_at_both_ends():
    """08:00 is inside; 19:00 is the moment it closes, so 19:xx is outside."""
    at_start = state(tick=tick_for_hour(CONTACT_WINDOW_START_HOUR_IST))
    before_start = state(tick=tick_for_hour(CONTACT_WINDOW_START_HOUR_IST - 1))
    last_hour = state(tick=tick_for_hour(CONTACT_WINDOW_END_HOUR_IST - 1))
    on_close = state(tick=tick_for_hour(CONTACT_WINDOW_END_HOUR_IST))
    assert gate_g1(case(), at_start, MESSAGE).allowed is True
    assert gate_g1(case(), before_start, MESSAGE).allowed is False
    assert gate_g1(case(), last_hour, MESSAGE).allowed is True
    assert gate_g1(case(), on_close, MESSAGE).allowed is False


def test_GAT_1_ist_is_the_only_timezone_that_counts():
    """18:00 UTC is 23:30 IST. INV-2 is about the customer's clock, not ours."""
    assert ist_hour(datetime(2026, 1, 1, 18, 0, tzinfo=timezone.utc)) == 23
    assert ist_hour(datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)) == 8

    # AT is 03:00Z, so tick 15 lands at 18:00Z — inside a UTC working day and
    # firmly outside the customer's.
    late = state(tick=15)
    assert evaluation_hour(case(), late) == 23
    assert gate_g1(case(), late, MESSAGE).allowed is False


def test_GAT_1_the_hour_is_derived_from_the_tick_and_cannot_disagree_with_it():
    """A59: an inconsistent (tick, hour) pair must be unrepresentable.

    With a separate `hour` argument, G2 could measure its rolling window against
    one instant while G1 judged the window against another, and nothing would
    catch it.
    """
    import inspect

    for gate in GATES:
        assert list(inspect.signature(gate).parameters) == ["case", "state", "action"]
    assert "hour" not in inspect.signature(evaluate_gates).parameters
    for hour in range(24):
        assert evaluation_hour(case(), state(tick=tick_for_hour(hour))) == hour


# --------------------------------------------------------------------------
# GAT-2 — G2 frequency cap
# --------------------------------------------------------------------------

def test_GAT_2_g2_blocks_the_fourth_contact_in_a_week_and_permits_the_third():
    three = tuple(AT + timedelta(hours=h) for h in (0, 24, 48))
    blocked = state(tick=72, contact_history=three, last_contact_at=three[-1])
    assert gate_g2(case(), blocked, MESSAGE).allowed is False
    assert gate_g2(case(), blocked, MESSAGE).reason_code == "G2_FREQUENCY_CAP"

    two = three[:2]
    permitted = state(tick=72, contact_history=two, last_contact_at=two[-1])
    assert gate_g2(case(), permitted, MESSAGE).allowed is True


def test_GAT_2_the_window_rolls_rather_than_resetting():
    """Contacts older than 168h stop counting."""
    stale = tuple(AT + timedelta(hours=h) for h in (0, 1, 2))
    later = state(tick=200, contact_history=stale, last_contact_at=stale[-1])
    assert len(stale) >= FREQUENCY_CAP_PER_WINDOW
    assert gate_g2(case(), later, MESSAGE).allowed is True


def test_GAT_2_the_minimum_gap_blocks_even_when_under_the_weekly_cap():
    recent = state(tick=1, contact_history=(AT,), last_contact_at=AT)
    verdict = gate_g2(case(), recent, MESSAGE)
    assert verdict.allowed is False
    assert verdict.reason_code == "G2_MINIMUM_GAP"

    rested = state(
        tick=MIN_CONTACT_GAP_HOURS, contact_history=(AT,), last_contact_at=AT
    )
    assert gate_g2(case(), rested, MESSAGE).allowed is True


# --------------------------------------------------------------------------
# GAT-3 — G3 mandate validity
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "mandate_state", [MandateState.EXPIRED, MandateState.REVOKED, MandateState.NONE]
)
def test_GAT_3_g3_blocks_a_debit_without_a_live_mandate(mandate_state):
    verdict = gate_g3(case(mandate_state=mandate_state), state(), RETRY_CARD)
    assert verdict.allowed is False
    assert verdict.reason_code == f"G3_MANDATE_{mandate_state.value.upper()}"


def test_GAT_3_g3_permits_a_debit_on_an_active_mandate_and_ignores_contacts():
    assert gate_g3(case(), state(), RETRY_CARD).allowed is True
    assert gate_g3(case(mandate_state=MandateState.REVOKED), state(), MESSAGE).allowed is True
    assert (
        gate_g3(case(mandate_state=MandateState.REVOKED), state(), MESSAGE).reason_code
        == "G3_NOT_A_DEBIT"
    )


# --------------------------------------------------------------------------
# GAT-4 — G4 card-network retry cap
# --------------------------------------------------------------------------

def test_GAT_4_g4_blocks_a_card_retry_at_the_cap_and_permits_one_below_it():
    at_cap = state(attempts_used=CARD_NETWORK_RETRY_CAP)
    assert gate_g4(case(), at_cap, RETRY_CARD).allowed is False
    assert gate_g4(case(), at_cap, RETRY_CARD).reason_code == "G4_CARD_RETRY_CAP"
    assert gate_g4(case(), state(attempts_used=CARD_NETWORK_RETRY_CAP - 1), RETRY_CARD).allowed


def test_GAT_4_g4_is_a_card_rule_and_follows_the_action_not_the_case():
    """A switch away from card escapes the cap; a switch to card does not."""
    at_cap = state(attempts_used=CARD_NETWORK_RETRY_CAP)
    assert gate_g4(case(rail=Rail.CARD), at_cap, SwitchRail(to=Rail.UPI_AUTOPAY)).allowed is True
    assert gate_g4(case(rail=Rail.UPI_AUTOPAY), at_cap, SwitchRail(to=Rail.CARD)).allowed is False
    assert gate_g4(case(rail=Rail.ENACH), at_cap, RETRY_ENACH).allowed is True


# --------------------------------------------------------------------------
# GAT-5 — G5 idempotency
# --------------------------------------------------------------------------

def test_GAT_5_g5_blocks_a_key_already_dispatched_and_permits_an_unseen_one():
    subject, base = case(), state()
    key = idempotency_key(subject, base, MESSAGE)
    assert gate_g5(subject, base, MESSAGE).allowed is True
    seen = state(dispatched_keys=frozenset({key}))
    assert gate_g5(subject, seen, MESSAGE).allowed is False
    assert gate_g5(subject, seen, MESSAGE).reason_code == "G5_DUPLICATE_KEY"


def test_GAT_5_the_key_is_stable_and_distinguishes_what_it_should():
    subject, base = case(), state()
    assert idempotency_key(subject, base, MESSAGE) == idempotency_key(subject, base, MESSAGE)
    # The hour is a function of the tick, so the tick is the only time component.
    assert idempotency_key(subject, base, MESSAGE) != idempotency_key(
        subject, state(tick=1), MESSAGE
    )
    assert idempotency_key(subject, base, MESSAGE) != idempotency_key(
        subject, base, SendMessage(channel=Channel.WHATSAPP, template_id="tpl")
    )
    assert idempotency_key(subject, base, MESSAGE) != idempotency_key(
        case(case_id="case-2"), base, MESSAGE
    )


def test_GAT_5_do_nothing_has_no_key_to_collide():
    seen = state(dispatched_keys=frozenset({idempotency_key(case(), state(), MESSAGE)}))
    verdict = gate_g5(case(), seen, DoNothing())
    assert verdict.allowed is True
    assert verdict.reason_code == "G5_NOT_DISPATCHED"


# --------------------------------------------------------------------------
# GAT-6 — G6 promise suppression
# --------------------------------------------------------------------------

def test_GAT_6_g6_blocks_a_contact_before_the_promise_date_and_permits_one_after():
    promised = state(promise_date=date(2026, 1, 10), promise_logged_at=AT, tick=24)
    verdict = gate_g6(case(), promised, MESSAGE)
    assert verdict.allowed is False
    assert verdict.reason_code == "G6_PROMISE_SUPPRESSION"

    elapsed = state(promise_date=date(2026, 1, 2), promise_logged_at=AT, tick=24 * 3)
    assert gate_g6(case(), elapsed, MESSAGE).allowed is True


def test_GAT_6_the_promise_date_itself_is_not_suppressed():
    """INV-7 suppresses contact *between* the promise and its date, not on it."""
    on_the_day = state(promise_date=date(2026, 1, 2), promise_logged_at=AT, tick=24)
    assert gate_g6(case(), on_the_day, MESSAGE).allowed is True


def test_GAT_6_a_silent_retry_is_not_suppressed_by_a_promise():
    promised = state(promise_date=date(2026, 1, 10), promise_logged_at=AT, tick=24)
    assert gate_g6(case(), promised, RETRY_CARD).allowed is True


# --------------------------------------------------------------------------
# GAT-7 — G7 opt-out
# --------------------------------------------------------------------------

def test_GAT_7_g7_blocks_a_contact_after_opt_out_and_permits_one_before():
    assert gate_g7(case(), state(opted_out=True), MESSAGE).allowed is False
    assert gate_g7(case(), state(opted_out=True), MESSAGE).reason_code == "G7_OPTED_OUT"
    assert gate_g7(case(), state(), MESSAGE).allowed is True


@pytest.mark.parametrize("channel", list(Channel))
def test_GAT_7_opt_out_is_honoured_on_every_channel(channel):
    """INV-3. G7 tests the action's nature, never its channel, so a new channel
    in §5.3 cannot open a hole here."""
    opted = state(opted_out=True)
    for action in (
        SendMessage(channel=channel, template_id="t"),
        RequestMandateUpdate(channel=channel),
        ServeNotice(channel=channel),
    ):
        assert gate_g7(case(), opted, action).allowed is False
    assert gate_g7(case(), opted, VoiceCall()).allowed is False
    assert gate_g7(case(), opted, EscalateHuman()).allowed is False


# --------------------------------------------------------------------------
# GAT-8 — G8 dispute freeze
# --------------------------------------------------------------------------

def test_GAT_8_g8_freezes_collection_while_a_dispute_stands():
    disputed = state(disputed=True)
    assert gate_g8(case(), disputed, RETRY_CARD).allowed is False
    assert gate_g8(case(), disputed, MESSAGE).allowed is False
    assert gate_g8(case(), disputed, MESSAGE).reason_code == "G8_DISPUTE_FREEZE"
    assert gate_g8(case(), state(), RETRY_CARD).allowed is True


def test_GAT_8_escalation_stays_open_so_the_freeze_can_end():
    """Freezing the resolution path would make the freeze permanent."""
    disputed = state(disputed=True)
    assert gate_g8(case(), disputed, EscalateHuman()).allowed is True
    assert gate_g8(case(), disputed, DoNothing()).allowed is True


# --------------------------------------------------------------------------
# GAT-9 / GAT-13 — G9 e-mandate notice
# --------------------------------------------------------------------------

def test_GAT_9_g9_blocks_an_enach_debit_with_no_notice_and_permits_other_rails():
    enach = case(rail=Rail.ENACH)
    verdict = gate_g9(enach, state(), RETRY_ENACH)
    assert verdict.allowed is False
    assert verdict.reason_code == "G9_NO_NOTICE_SERVED"
    assert gate_g9(case(), state(), RETRY_CARD).allowed is True
    assert gate_g9(case(), state(), MESSAGE).reason_code == "G9_NOT_A_DEBIT"


def test_GAT_13_serve_notice_opens_the_window_and_the_window_expires():
    enach = case(rail=Rail.ENACH)
    before = state()
    assert before.notice_window_until is None

    after = after_serve_notice(enach, before)
    assert after.notice_window_until is not None

    inside = after.model_copy(update={"tick": 48})
    assert gate_g9(enach, inside, RETRY_ENACH).allowed is True
    assert gate_g9(enach, inside, RETRY_ENACH).reason_code == "G9_INSIDE_NOTICE_WINDOW"

    outside = after.model_copy(update={"tick": 24 * 4})
    verdict = gate_g9(enach, outside, RETRY_ENACH)
    assert verdict.allowed is False
    assert verdict.reason_code == "G9_NOTICE_WINDOW_EXPIRED"


def test_GAT_13_a_notice_served_later_covers_a_later_debit():
    """The window runs from when the notice was served, not from case creation."""
    enach = case(rail=Rail.ENACH)
    late = after_serve_notice(enach, state(tick=200))
    assert gate_g9(enach, late.model_copy(update={"tick": 224}), RETRY_ENACH).allowed is True
    assert gate_g9(enach, late.model_copy(update={"tick": 300}), RETRY_ENACH).allowed is False


# --------------------------------------------------------------------------
# GAT-10 — every verdict, not just the first block
# --------------------------------------------------------------------------

def test_GAT_10_evaluate_gates_returns_all_eleven_verdicts():
    result = evaluate_gates(case(), state(tick=tick_for_hour(10)), MESSAGE, ArmMode.ENFORCE)
    assert len(result.verdicts) == 11 == len(GATES)
    assert [v.gate for v in result.verdicts] == [f"G{n}" for n in range(1, 12)]


def test_GAT_10_a_multiply_blocked_action_reports_every_gate_that_fired():
    """Short-circuiting would make `Alternative.block_gate` (§5.4) a guess."""
    hopeless = state(
        opted_out=True,
        disputed=True,
        promise_date=date(2026, 6, 1),
        promise_logged_at=AT,
        contact_history=tuple(AT + timedelta(hours=h) for h in (0, 24, 48)),
        last_contact_at=AT + timedelta(hours=48),
        tick=tick_for_hour(23, day=2),
    )
    result = evaluate_gates(case(), hopeless, MESSAGE, ArmMode.ENFORCE)
    assert len(result.verdicts) == 11
    assert set(result.blocked_by) >= {"G1", "G2", "G6", "G7", "G8"}
    assert result.first_block == "G1"
    assert result.allowed is False


def test_GAT_10_a_clean_case_blocks_on_nothing():
    result = evaluate_gates(case(), state(tick=tick_for_hour(10)), MESSAGE, ArmMode.ENFORCE)
    assert result.allowed is True
    assert result.blocked_by == ()
    assert result.first_block is None
    assert all(v.allowed for v in result.verdicts)


# --------------------------------------------------------------------------
# GAT-11 — ENFORCE blocks, OBSERVE records
# --------------------------------------------------------------------------

def test_GAT_11_observe_permits_a_g1_violation_and_records_it_enforce_blocks_it():
    late = state(tick=tick_for_hour(23))
    enforce = evaluate_gates(case(), late, MESSAGE, ArmMode.ENFORCE)
    assert enforce.allowed is False
    assert enforce.blocked_by == ("G1",)
    assert enforce.violations == ()

    observe = evaluate_gates(case(), late, MESSAGE, ArmMode.OBSERVE)
    assert observe.allowed is True
    assert observe.blocked_by == ("G1",)
    assert observe.violations == ("G1",)


def test_GAT_11_the_gates_themselves_are_identical_in_both_modes():
    """SPEC §4: one implementation, one code path. Only the verdict binds differently."""
    args = (case(), state(opted_out=True, tick=tick_for_hour(23)), MESSAGE)
    enforce = evaluate_gates(*args, ArmMode.ENFORCE)
    observe = evaluate_gates(*args, ArmMode.OBSERVE)
    assert enforce.verdicts == observe.verdicts
    assert enforce.blocked_by == observe.blocked_by


def test_GAT_11_observe_records_every_violation_not_only_the_first():
    result = evaluate_gates(
        case(), state(opted_out=True, tick=tick_for_hour(23)), MESSAGE, ArmMode.OBSERVE
    )
    assert set(result.violations) >= {"G1", "G7"}


# --------------------------------------------------------------------------
# GAT-12 — a silent retry is not a contact
# --------------------------------------------------------------------------

@pytest.mark.parametrize("action", [RETRY_CARD, SwitchRail(to=Rail.UPI_AUTOPAY), DoNothing()])
def test_GAT_12_a_silent_retry_is_not_subject_to_g1_g2_or_g7(action):
    """§9 calls a liquidity-window retry the best move for `time_shiftable`.

    Gating it on the contact window would forbid it at 03:00 — exactly when a
    salary credit lands — and would spend a human's patience on a message the
    human never receives.
    """
    hostile = state(
        opted_out=True,
        contact_history=tuple(AT + timedelta(hours=h) for h in (0, 1, 2)),
        last_contact_at=AT + timedelta(hours=2),
        tick=tick_for_hour(3),
    )
    for gate in (gate_g1, gate_g2, gate_g7):
        verdict = gate(case(), hostile, action)
        assert verdict.allowed is True, f"{verdict.gate} applied to {action.type.value}"
        assert verdict.reason_code.endswith("NOT_A_CONTACT")


def test_GAT_12_the_contact_and_debit_sets_are_explicit_and_disjoint():
    assert CONTACT_BEARING & DEBIT_BEARING == frozenset()
    assert ActionType.DO_NOTHING not in CONTACT_BEARING | DEBIT_BEARING
    assert CONTACT_BEARING | DEBIT_BEARING | {ActionType.DO_NOTHING} == set(ActionType)


def test_GAT_12_serve_notice_is_a_contact_because_the_customer_receives_it():
    """A55/A34: the notice is a real SMS, so it costs G2 budget and patience."""
    assert ActionType.SERVE_NOTICE in CONTACT_BEARING
    late = state(tick=tick_for_hour(23))
    assert gate_g1(case(), late, ServeNotice(channel=Channel.SMS)).allowed is False


# --------------------------------------------------------------------------
# PURE-1
# --------------------------------------------------------------------------

CP3_FILES = [
    "settle/schema/state.py",
    "settle/diagnose/__init__.py",
    "settle/diagnose/taxonomy.py",
    "settle/policy/__init__.py",
    "settle/policy/gates.py",
    "settle/policy/stops.py",
    "settle/policy/legal.py",
    "settle/policy/escalation.py",
    "settle/policy/params.py",
    "tests/test_taxonomy.py",
    "tests/test_gates.py",
    "tests/test_stops.py",
]

BANNED_CALLS = {
    ("datetime", "now"),
    ("datetime", "utcnow"),
    ("date", "today"),
    ("time", "time"),
    ("time", "time_ns"),
    ("time", "monotonic"),
    ("time", "perf_counter"),
    ("os", "urandom"),
    ("uuid", "uuid4"),
}
BANNED_MODULES = {"random", "secrets", "time", "os"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package_parts = path.relative_to(REPO_ROOT).with_suffix("").parts[:-1]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = list(package_parts[: len(package_parts) - (node.level - 1)]) if node.level else []
            module = ".".join([*base, node.module] if node.module else base)
            found.add(module)
            found.update(f"{module}.{alias.name}" for alias in node.names)
    return found


def _impurities(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            if isinstance(owner, ast.Name) and (owner.id, node.func.attr) in BANNED_CALLS:
                found.add(f"{owner.id}.{node.func.attr}()")
            elif isinstance(owner, ast.Attribute) and (owner.attr, node.func.attr) in BANNED_CALLS:
                found.add(f"{owner.attr}.{node.func.attr}()")
    for name in _imports(path):
        root = name.split(".")[0]
        if root in BANNED_MODULES:
            found.add(f"import {name}")
        if name == "settle.sim" or name.startswith("settle.sim."):
            found.add(f"import {name}")
    return found


@pytest.mark.parametrize("relative", CP3_FILES)
def test_PURE_1_no_cp3_file_reads_a_clock_or_reaches_for_hidden_truth(relative):
    """Every function in CP3 is a function of its arguments and nothing else.

    A clock would make a gate unreplayable from the ledger; `settle.sim` would
    put hidden truth one import from the policy (INV-8).
    """
    path = REPO_ROOT / relative
    assert path.exists(), relative
    offenders = _impurities(path)
    assert not offenders, f"{relative}: {sorted(offenders)}"


def test_PURE_1_detects_planted_violations():
    """A purity check that has never fired proves nothing."""
    planted = REPO_ROOT / "settle" / "policy" / "_purity_probe.py"
    planted.write_text(
        "import random\n"
        "from datetime import datetime\n"
        "from settle.sim.truth import HiddenTruth\n"
        "def f():\n"
        "    return datetime.now(), random.random()\n",
        encoding="utf-8",
    )
    try:
        found = _impurities(planted)
        assert "datetime.now()" in found
        assert "import random" in found
        assert "import settle.sim.truth" in found
    finally:
        planted.unlink()


def test_PURE_1_gates_are_deterministic_across_repeated_calls():
    """The AST check is static. This one is behavioural."""
    args = (case(), state(tick=5, contact_history=(AT,), last_contact_at=AT), MESSAGE)
    first = evaluate_gates(*args, ArmMode.ENFORCE)
    for _ in range(50):
        assert evaluate_gates(*args, ArmMode.ENFORCE) == first


def test_PURE_1_gates_never_mutate_the_state_they_are_given():
    subject = case()
    before = state(tick=5, contact_history=(AT,), last_contact_at=AT, dispatched_keys=frozenset({"k"}))
    snapshot = before.model_dump_json()
    evaluate_gates(subject, before, MESSAGE, ArmMode.ENFORCE)
    after_serve_notice(subject, before)
    assert before.model_dump_json() == snapshot


# --------------------------------------------------------------------------
# GAT-14 — G10 class retry budget
# --------------------------------------------------------------------------

def test_GAT_14_g10_caps_ambiguous_at_one_retry_and_permits_the_first():
    """§9 permits one retry at a different hour, then a message.

    Before G10 that sentence was unenforced: `ambiguous` retries ran until G4 or
    S3 fired, which is a different rule than the one §9 states.
    """
    ambiguous = case(decline_code="do_not_honour")
    assert class_retry_cap(DeclineClass.AMBIGUOUS) == 1

    assert gate_g10(ambiguous, state(attempts_used=0), RETRY_CARD).allowed is True
    verdict = gate_g10(ambiguous, state(attempts_used=1), RETRY_CARD)
    assert verdict.allowed is False
    assert verdict.reason_code == "G10_CLASS_RETRY_CAP"


def test_GAT_14_g10_is_distinct_from_g4s_card_network_cap():
    """One ambiguous retry is still legal under G4, which caps at four."""
    ambiguous = case(decline_code="do_not_honour")
    at_class_cap = state(attempts_used=1)
    assert gate_g4(ambiguous, at_class_cap, RETRY_CARD).allowed is True
    assert gate_g10(ambiguous, at_class_cap, RETRY_CARD).allowed is False

    time_shiftable = case(decline_code="insufficient_funds")
    assert gate_g10(time_shiftable, at_class_cap, RETRY_CARD).allowed is True
    assert CARD_NETWORK_RETRY_CAP > class_retry_cap(DeclineClass.AMBIGUOUS)


def test_GAT_14_g10_applies_to_retries_and_not_to_rail_switches():
    """Capping a switch here would forbid what §9 calls viable for auth_abandoned."""
    auth = case(decline_code="authentication_failed")
    assert class_retry_cap(DeclineClass.AUTH_ABANDONED) == 0
    verdict = gate_g10(auth, state(attempts_used=3), SwitchRail(to=Rail.UPI_AUTOPAY))
    assert verdict.allowed is True
    assert verdict.reason_code == "G10_NOT_A_RETRY"
    assert gate_g10(auth, state(), MESSAGE).allowed is True


# --------------------------------------------------------------------------
# GAT-15 — G11 TRAI DND registry
# --------------------------------------------------------------------------

def test_GAT_15_g11_blocks_a_voice_call_to_a_dnd_registered_number():
    verdict = gate_g11(case(dnd_flag=True), state(), VoiceCall())
    assert verdict.allowed is False
    assert verdict.reason_code == "G11_DND_REGISTERED"
    assert gate_g11(case(dnd_flag=False), state(), VoiceCall()).allowed is True


@pytest.mark.parametrize("channel", [Channel.SMS, Channel.WHATSAPP])
def test_GAT_15_g11_does_not_block_transactional_messages(channel):
    """DND covers unsolicited commercial contact. A message to an existing
    customer about a failed payment on their own subscription is exempt —
    ASSERTED, and recorded in Known Limitations."""
    on_dnd = case(dnd_flag=True)
    for action in (
        SendMessage(channel=channel, template_id="t"),
        RequestMandateUpdate(channel=channel),
        ServeNotice(channel=channel),
    ):
        verdict = gate_g11(on_dnd, state(), action)
        assert verdict.allowed is True
        assert verdict.reason_code == "G11_NOT_A_VOICE_CALL"


def test_GAT_15_dnd_is_no_longer_a_field_the_policy_ignores():
    """Before G11, `dnd_flag` shaped the simulated world but no gate read it —
    a compliance rule that affects outcomes with nothing enforcing it."""
    result = evaluate_gates(
        case(dnd_flag=True, amount_paise=99900, attempt_number=3),
        state(tick=tick_for_hour(10)),
        VoiceCall(),
        ArmMode.ENFORCE,
    )
    assert result.allowed is False
    assert "G11" in result.blocked_by
