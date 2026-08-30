"""CP6.1 — replies. SPEC §11, §8.

RPL-2 is the important one. Wrongly logging a brush-off as a promise sets G6 and
suppresses contact for weeks: the customer who would have paid never hears from
us again. Missing a real promise costs one wasted message. The asymmetry is why
§11 refuses to treat a hedge as a commitment, and this is the test that holds
the line.
"""

import ast
import collections
from datetime import date, timedelta
from pathlib import Path

import pytest

from settle.audit.chain import Ledger, read_entries
from settle.execute.executor import WorldHandle
from settle.runner.arms.baselines import FixedLadderArm, MaxPressureArm
from settle.runner.case_runner import run_case
from settle.schema.enums import ArmMode, LedgerKind
from settle.schema.state import CaseState
from settle.sim.generator import generate_batch
from settle.sim.observability import ObservabilityConfig
from settle.sim.streams import Streams
from settle.text.classify import (
    Confidence,
    ReplyKind,
    classify_reply,
    escalation_rate,
    find_date_spans,
    normalise,
    parse_date_span,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
ANCHOR = date(2026, 3, 1)
SEED = 42

# The four clips DECISIONS.md records, plus the shapes an inbox actually holds.
FIXTURES = {
    ReplyKind.PROMISE: [
        "Haan dekhiye abhi thoda tight chal raha hai... agle mahine kar dunga..."
        " nahi nahi, pandrah tareekh ko ho jayega",
        "Ek hafte mein bhej dunga bhai, abhi nahi hai mere paas",
        "das din mein kar dunga",
        "पंद्रह तारीख को कर दूंगा",
        "kal kar dunga",
        "teen din mein bhej dunga",
    ],
    ReplyKind.OPT_OUT: [
        "Mujhe baar baar mat call kariye, main khud dekh lunga",
        "STOP",
        "please band karo ye messages",
        "unsubscribe",
        # Both a payment claim and an opt-out. Opt-out wins: it is the only
        # reading where being wrong is not a compliance breach.
        "already paid, stop messaging",
    ],
    ReplyKind.HEDGED: [
        "Haan theek hai, dekhta hoon, baad mein baat karte hain",
        "abhi thoda tight hai, try karunga",
        "dekhte hain",
        "ok will see",
        "shayad",
    ],
    ReplyKind.DISPUTE: [
        "ye galat charge hai maine nahi kiya",
        "I want to dispute this",
        "chargeback kar raha hoon",
    ],
    ReplyKind.PAYMENT_CLAIM: [
        "paise bhej diya",
        "payment done na",
    ],
    ReplyKind.UNCLEAR: ["", "   ", "hmm", "kya", "agle saal kar dunga", "40 tareekh ko kar dunga"],
}


# --------------------------------------------------------------------------
# RPL-1 / RPL-2
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("expected", "text"),
    [(kind, text) for kind, texts in FIXTURES.items() for text in texts],
)
def test_RPL_1_each_kind_is_classified_correctly(expected, text):
    assert classify_reply(text, ANCHOR).kind is expected, text


def test_RPL_1_all_six_kinds_are_reachable():
    kinds = {classify_reply(t, ANCHOR).kind for texts in FIXTURES.values() for t in texts}
    assert kinds == set(ReplyKind)


@pytest.mark.parametrize("text", FIXTURES[ReplyKind.HEDGED])
def test_RPL_2_a_hedged_reply_sets_no_promise_date(text):
    """The one that matters. A brush-off logged as a promise suppresses contact
    for weeks under G6 — a worse failure than missing a real promise, because
    the customer who would have paid never hears from us again."""
    verdict = classify_reply(text, ANCHOR)
    assert verdict.kind is ReplyKind.HEDGED
    assert verdict.promise_date is None


def test_RPL_2_a_hedge_beside_a_date_is_still_not_a_promise():
    """"theek hai, 15 tareekh ko dekhta hoon" is agreement to look, not to pay."""
    for text in ("theek hai, pandrah tareekh ko dekhta hoon", "kal dekhte hain", "try karunga kal"):
        verdict = classify_reply(text, ANCHOR)
        assert verdict.promise_date is None, text
        assert verdict.kind is ReplyKind.HEDGED, text


def test_RPL_2_a_promise_needs_a_date_that_survives_validation():
    """Future, inside the horizon, and a real day of the month. §11: disagreement
    becomes a confirmation turn, not a silent guess."""
    assert classify_reply("40 tareekh ko kar dunga", ANCHOR).kind is ReplyKind.UNCLEAR
    assert classify_reply("agle saal kar dunga", ANCHOR).kind is ReplyKind.UNCLEAR
    far = classify_reply("60 din mein kar dunga", ANCHOR)
    assert far.kind is ReplyKind.UNCLEAR, "a commitment past the horizon is not a commitment"


def test_RPL_2_the_locator_and_the_parser_are_separate():
    """§11's contract: something locates a span, deterministic code evaluates it.
    The classifier never constructs a date itself."""
    spans = find_date_spans(normalise("pandrah tareekh ko, nahi 20 tareekh"))
    assert len(spans) == 2
    assert parse_date_span(*spans[0], ANCHOR) == date(2026, 3, 15)
    assert parse_date_span(*spans[-1], ANCHOR) == date(2026, 3, 20)


def test_RPL_2_a_self_correction_resolves_to_the_last_span():
    """Clip 1: "agle mahine... nahi nahi, pandrah tareekh". The speaker corrected
    themselves and the parser has to agree with them, not with their first draft."""
    verdict = classify_reply(
        "agle mahine kar dunga... nahi nahi, pandrah tareekh ko ho jayega", ANCHOR
    )
    assert verdict.promise_date == date(2026, 3, 15)


def test_RPL_2_devanagari_numerals_parse():
    """gpt-transcribe returns Devanagari for Hindi speech whatever the `language`
    parameter says, so a parser assuming ASCII digits silently finds nothing."""
    assert classify_reply("१५ tareekh ko kar dunga", ANCHOR).promise_date == date(2026, 3, 15)
    assert classify_reply("पंद्रह तारीख को कर दूंगा", ANCHOR).promise_date == date(2026, 3, 15)


# --------------------------------------------------------------------------
# RPL-3 / RPL-4 — the verdicts reach CaseState
# --------------------------------------------------------------------------

def _drive(generated, arm, ledger, initial_state=None):
    return run_case(
        generated.observed, arm,
        WorldHandle(truth=generated.truth, streams=Streams(SEED)),
        ObservabilityConfig(), ledger, initial_state,
    )


@pytest.fixture(scope="module")
def b2_entries(tmp_path_factory):
    path = tmp_path_factory.mktemp("replies") / "a.jsonl"
    batch = generate_batch(1_200, SEED)
    streams = Streams(SEED)
    finals = []
    with Ledger(path) as ledger:
        for generated in batch.cases:
            finals.append(
                run_case(
                    generated.observed, FixedLadderArm(),
                    WorldHandle(truth=generated.truth, streams=streams),
                    ObservabilityConfig(), ledger,
                )
            )
    return read_entries(path), finals


def test_RPL_3_an_opt_out_sets_the_flag_and_S4_fires(b2_entries):
    entries, finals = b2_entries
    opted = [f for f in finals if f.opted_out]
    assert opted, "no case ever opted out, so G7 and S4 are untested on real data"
    assert [f for f in opted if f.stop_reason == "S4_OPT_OUT"]
    assert [e for e in entries if e.reason_code == "OPTED_OUT"]


def test_RPL_3_no_contact_follows_an_opt_out_in_enforce(b2_entries):
    """SF-5 is the audit for this, and for an ENFORCE arm it must be zero."""
    from settle.recon.reconcile import failure_counts, reconcile, run_arm
    from settle.schema.enums import SilentFailureClass

    e, a, c, _, _, t, s = run_arm("b2", 600, SEED)
    counts = failure_counts(reconcile(e, a, c, truths=t, streams=s))
    assert counts[SilentFailureClass.SF5] == 0, "a contact followed an opt-out under ENFORCE"


def test_RPL_4_a_promise_sets_the_date_and_G6_suppresses_contact(b2_entries):
    entries, finals = b2_entries
    promised = [f for f in finals if f.promise_date is not None]
    assert promised, "no promise was ever logged"
    assert [e for e in entries if e.reason_code == "PROMISE_LOGGED"]

    blocks = collections.Counter(
        gate for e in entries if e.kind is LedgerKind.GATE_CHECK for gate in e.payload["blocked_by"]
    )
    assert blocks["G6"] > 0, "G6 never fired, so promise suppression is untested"


def test_RPL_4_a_silent_retry_is_still_permitted_under_a_promise():
    """G6 suppresses *contact*. A retry is a message to the bank, not the person,
    and forbidding it would waste the very window the promise points at."""
    from settle.policy.gates import gate_g6
    from settle.schema.action import Retry, SendMessage
    from settle.schema.enums import Channel, Rail

    batch = generate_batch(20, SEED)
    case = batch.cases[0].observed
    state = CaseState(
        case_id=case.case_id, arm="B2", arm_mode=ArmMode.ENFORCE,
        promise_date=case.created_at.date() + timedelta(days=10), tick=24,
    )
    assert gate_g6(case, state, SendMessage(channel=Channel.SMS, template_id="t")).allowed is False
    assert gate_g6(case, state, Retry(at_hour_offset=0, rail=Rail.CARD)).allowed is True


def test_RPL_4_a_dispute_sets_the_flag_and_S5_fires(b2_entries):
    entries, finals = b2_entries
    disputed = [f for f in finals if f.disputed or f.stop_reason == "S5_DISPUTE_RAISED"]
    assert disputed
    assert [e for e in entries if e.reason_code == "DISPUTE_RAISED"]


# --------------------------------------------------------------------------
# RPL-5 — escalation
# --------------------------------------------------------------------------

def test_RPL_5_unclear_replies_are_counted_and_the_rate_is_reported(b2_entries):
    entries, _ = b2_entries
    verdicts = collections.Counter(
        e.payload["kind"]
        for e in entries
        if e.kind is LedgerKind.DECISION and "kind" in e.payload
    )
    assert verdicts, "no reply verdict was logged"
    total = sum(verdicts.values())
    rate = verdicts.get("unclear", 0) / total
    assert 0.0 <= rate < 0.5, f"escalation rate {rate:.1%} — the deterministic path is not carrying its share"


def test_RPL_5_every_verdict_is_logged_whether_or_not_it_changed_state(b2_entries):
    """A reply that was read and deliberately ignored is a decision."""
    entries, _ = b2_entries
    logged = [e for e in entries if e.kind is LedgerKind.DECISION and "kind" in e.payload]
    assert [e for e in logged if e.payload["changed_state"] is False], "no ignored reply was logged"
    assert [e for e in logged if e.payload["changed_state"] is True]
    for entry in logged:
        assert entry.payload["confidence"] in ("high", "low")


def test_RPL_5_the_escalation_rate_helper_agrees():
    verdicts = [classify_reply(t, ANCHOR) for texts in FIXTURES.values() for t in texts]
    unclear = sum(1 for v in verdicts if v.kind is ReplyKind.UNCLEAR)
    assert escalation_rate(verdicts) == pytest.approx(unclear / len(verdicts))
    assert escalation_rate([]) == 0.0


# --------------------------------------------------------------------------
# RPL-6 — purity
# --------------------------------------------------------------------------

def test_RPL_6_classification_is_pure():
    """No clock, no randomness, no network — and no LLM. The escalation seam
    exists so that a dictionary lookup never becomes a network call."""
    path = REPO_ROOT / "settle" / "text" / "classify.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for banned in ("random", "time", "os", "requests", "httpx", "openai", "urllib", "socket"):
        assert banned not in imported, f"classify.py imports {banned}"

    source = path.read_text(encoding="utf-8")
    for banned in ("datetime.now", "date.today", "utcnow"):
        assert banned not in source


def test_RPL_6_the_same_text_always_classifies_the_same():
    for texts in FIXTURES.values():
        for text in texts:
            first = classify_reply(text, ANCHOR)
            for _ in range(20):
                assert classify_reply(text, ANCHOR) == first


def test_RPL_6_the_anchor_is_the_case_not_the_clock():
    """A promise parsed against wall time resolves differently on a replay and
    the ledger stops reproducing."""
    early = classify_reply("kal kar dunga", date(2026, 1, 5))
    late = classify_reply("kal kar dunga", date(2026, 7, 5))
    assert early.promise_date == date(2026, 1, 6)
    assert late.promise_date == date(2026, 7, 6)


# --------------------------------------------------------------------------
# RPL-7 — it all happens in a real run
# --------------------------------------------------------------------------

def test_RPL_7_promises_opt_outs_and_disputes_all_occur_at_non_zero_rates(b2_entries):
    entries, _ = b2_entries
    kinds = collections.Counter(
        e.payload["kind"] for e in entries if e.kind is LedgerKind.DECISION and "kind" in e.payload
    )
    for kind in ("promise", "opt_out", "dispute", "hedged"):
        assert kinds[kind] > 0, f"{kind} never occurred in 1,200 cases"


def test_RPL_7_G6_G7_and_G8_all_fire_at_least_once(tmp_path):
    """G7 needs OBSERVE to be reachable: in ENFORCE, S4 stops the case before
    the gate is consulted, which is exactly what §13.2 says and why B3 exists."""
    batch = generate_batch(800, SEED)
    streams = Streams(SEED)
    path = tmp_path / "b3.jsonl"
    with Ledger(path) as ledger:
        for generated in batch.cases:
            run_case(
                generated.observed, MaxPressureArm(),
                WorldHandle(truth=generated.truth, streams=streams),
                ObservabilityConfig(), ledger,
            )
    blocks = collections.Counter(
        gate
        for e in read_entries(path)
        if e.kind is LedgerKind.GATE_CHECK
        for gate in e.payload["blocked_by"]
    )
    for gate in ("G6", "G7", "G8"):
        assert blocks[gate] > 0, f"{gate} never fired: {dict(blocks)}"


# --------------------------------------------------------------------------
# RPL-8 — a payment claim is a claim
# --------------------------------------------------------------------------

PAYMENT_CLAIMS = [
    "maine to kar diya tha payment gaya kyu nahi",
    "ho gaya na payment? check karo",
    "bhej diya hai screenshot bhejun kya",
    "paise bhej diya",
]


@pytest.mark.parametrize("text", PAYMENT_CLAIMS)
def test_RPL_8_a_payment_claim_never_marks_a_case_recovered(text):
    """INV-1. The customer saying they paid is not a settlement record.

    A claim is the *least* reliable signal in the system — it is unverified,
    self-reported, and frequently sincere but wrong: the debit failed and the
    customer is describing a payment that never left their account. Only
    reconciliation against a settlement may set `settled`.
    """
    from settle.schema.observed import ObservedCase
    from settle.runner.case_runner import _apply_outcome
    from settle.schema.outcome import ReportedOutcome
    from settle.schema.enums import ReportedStatus

    case: ObservedCase = generate_batch(1, SEED).cases[0].observed
    state = CaseState(case_id=case.case_id, arm="B2", arm_mode=ArmMode.ENFORCE)
    outcome = ReportedOutcome(
        case_id=case.case_id, at=case.created_at, status=ReportedStatus.FAILED,
        arrival_count=1, reply_text=text,
    )
    updated, verdict = _apply_outcome(case, state, outcome)

    assert verdict is not None
    assert updated.settled is False, "a payment claim set `settled`"
    assert updated.settled_at is None
    assert updated == state.model_copy(update={}), "a payment claim changed state at all"


def test_RPL_8_no_reply_verdict_can_set_settled():
    """Across every kind, not just the claim. `settled` has exactly one source."""
    from settle.runner.case_runner import _VERDICT_REASONS, _apply_outcome
    from settle.schema.enums import ReportedStatus
    from settle.schema.outcome import ReportedOutcome

    case = generate_batch(1, SEED).cases[0].observed
    state = CaseState(case_id=case.case_id, arm="B2", arm_mode=ArmMode.ENFORCE)
    for text in [t for texts in FIXTURES.values() for t in texts] + PAYMENT_CLAIMS:
        outcome = ReportedOutcome(
            case_id=case.case_id, at=case.created_at, status=ReportedStatus.CAPTURED,
            arrival_count=1, reply_text=text,
        )
        updated, _ = _apply_outcome(case, state, outcome)
        assert updated.settled is False, text
    assert set(_VERDICT_REASONS) == set(ReplyKind)


def test_RPL_8_the_runner_never_writes_settled_at_all():
    """Structural, not behavioural. `settled` is reconciliation's field."""
    source = (REPO_ROOT / "settle" / "runner" / "case_runner.py").read_text(encoding="utf-8")
    assert '"settled"' not in source
    assert "settled=True" not in source


# --------------------------------------------------------------------------
# RPL-9 — the escalation rate, measured honestly
# --------------------------------------------------------------------------

CORPUS_PATH = REPO_ROOT / "fixtures" / "replies_adversarial.jsonl"

# Measured at CP7.0 against a corpus written independently of the classifier.
# These are a ratchet, not a target: the classifier may only get better.
BASELINE_AGREEMENT = 8
BASELINE_ESCALATION = 13
CORPUS_SIZE = 18


def _corpus():
    import json

    return [json.loads(line) for line in CORPUS_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_RPL_9_the_adversarial_corpus_is_well_formed():
    rows = _corpus()
    assert len(rows) == CORPUS_SIZE
    kinds = {k.value for k in ReplyKind}
    for row in rows:
        assert row["expected"] in kinds, row
        assert row["text"].strip(), row
        assert row["gloss"] and row["note"], row
    assert len({row["text"] for row in rows}) == CORPUS_SIZE


def test_RPL_9_the_escalation_rate_is_measured_and_reported(capsys):
    """The 0.00% from CP6.1 was an artefact: the debtors spoke from a phrase
    bank this classifier's author wrote. A rate measured against text the
    classifier's own author produced is not a measurement.
    """
    rows = _corpus()
    agree, unclear, disagreements = 0, 0, []
    for row in rows:
        verdict = classify_reply(row["text"], ANCHOR)
        got = verdict.kind.value
        agree += got == row["expected"]
        unclear += got == ReplyKind.UNCLEAR.value
        if got != row["expected"]:
            disagreements.append((row["id"], row["expected"], got, row["text"]))

    with capsys.disabled():
        print(f"\n  adversarial corpus: {len(rows)} replies written independently")
        print(f"    agreement with intent  {agree}/{len(rows)} = {agree / len(rows):.1%}")
        print(f"    escalation rate        {unclear}/{len(rows)} = {unclear / len(rows):.1%}")
        for case_id, expected, got, text in disagreements:
            print(f"    {case_id:>2}  wanted {expected:<14} got {got:<14} {text[:44]}")

    # A ratchet. Falling below the measured baseline is a regression; rising
    # above it is the work. Never tune the classifier to satisfy this.
    assert agree >= BASELINE_AGREEMENT, f"agreement regressed: {agree} < {BASELINE_AGREEMENT}"
    assert unclear <= BASELINE_ESCALATION, f"escalation regressed: {unclear} > {BASELINE_ESCALATION}"


def test_RPL_9_a_contact_instruction_is_indistinguishable_from_a_promise():
    """Recorded because it is currently true, not because it is acceptable.

    "message me after the 8th, I'll do it then" and "I'll pay on the 8th"
    classify identically. The first is an instruction about *contact*; treating
    it as a promise suppresses contact under G6 and then records a broken
    promise under SF-4 when no payment arrives — one misreading producing two
    wrong numbers.
    """
    instruction = classify_reply("aath tareekh ke baad msg karna tab kar dunga", ANCHOR)
    promise = classify_reply("aath tareekh ko kar dunga", ANCHOR)
    assert instruction.kind is promise.kind is ReplyKind.PROMISE
    assert instruction.promise_date == promise.promise_date

    # The corpus entry escapes only through a spelling variant the pattern
    # happens not to match. That is luck, not discrimination.
    assert classify_reply("aath tareek kebaad msg karna tab kar dunga", ANCHOR).kind is ReplyKind.UNCLEAR


def test_RPL_9_cancellation_is_not_detected_as_an_opt_out():
    """Ruling 1, recorded as it stands. `cancel kardo` is a subscription
    cancellation; the schema has one `opted_out` flag serving both meanings, and
    the classifier currently detects neither."""
    for text in ("cancel kardo", "cancel karde bhai", "subscription cancel kar do"):
        assert classify_reply(text, ANCHOR).kind is ReplyKind.UNCLEAR, text
    assert classify_reply("STOP", ANCHOR).kind is ReplyKind.OPT_OUT
