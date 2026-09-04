"""CP5 — the arms. SPEC §14.1.

ARM-2 is the one that decides whether the headline is honest. §14.1 says
baselines are given full capability and denied only intelligence; a B2 that
cannot serve an e-mandate notice would lose every enach case to G9 and hand us
a win we did not earn.
"""

import ast
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from settle.audit.chain import Ledger, read_entries
from settle.execute.executor import WorldHandle
from settle.runner.arm import ARMS, DoNothingArm, FirstLegalArm, assert_enforce_only
from settle.runner.arms.baselines import FixedLadderArm, MaxPressureArm, SingleRetryArm
from settle.diagnose.taxonomy import classify
from settle.runner.arms.explore import ExploreArm
from settle.runner.arms.hybrid import HybridArm
from settle.runner.arms.ours import OursArm
from settle.runner.case_runner import run_case
from settle.schema.action import ACTION_MODELS, Action
from settle.schema.enums import ActionType, ArmMode, Channel, LedgerKind, Rail
from settle.schema.state import CaseState
from settle.sim.generator import generate_batch
from settle.sim.observability import ObservabilityConfig
from settle.sim.streams import Streams

SEED = 42
EXPLORE_SEED = 90_000


@pytest.fixture(scope="module")
def batch():
    return generate_batch(600, SEED)


def drive(generated, arm, ledger):
    return run_case(
        generated.observed,
        arm,
        WorldHandle(truth=generated.truth, streams=Streams(SEED)),
        ObservabilityConfig(),
        ledger,
    )


def dispatched(path):
    return [e for e in read_entries(path) if e.kind is LedgerKind.DISPATCH]


def build(key):
    cls = ARMS[key]
    if key == "explore":
        return cls(EXPLORE_SEED)
    if key == "first_legal":
        return cls()
    return cls()


# --------------------------------------------------------------------------
# ARM-1
# --------------------------------------------------------------------------

@pytest.mark.parametrize("key", sorted(ARMS))
def test_ARM_1_every_arm_satisfies_the_protocol(key):
    arm = build(key)
    assert isinstance(arm.name, str) and arm.name
    assert isinstance(arm.mode, ArmMode)
    assert callable(arm.choose)


@pytest.mark.parametrize("key", sorted(ARMS))
def test_ARM_1_every_arm_emits_only_closed_set_verbs(key, batch, tmp_path):
    """An arm that could emit something outside §5.3 would be a hole in every
    gate, because gates dispatch on the verb."""
    arm = build(key)
    path = tmp_path / f"{key}.jsonl"
    with Ledger(path) as ledger:
        for generated in batch.cases[:120]:
            drive(generated, arm, ledger)

    for entry in dispatched(path):
        verb = entry.payload["action"]["type"]
        assert verb in {a.value for a in ActionType}, verb
        assert verb != ActionType.DO_NOTHING.value, "do_nothing is never dispatched"


def test_ARM_1_choose_returns_an_action_from_the_closed_set(batch):
    generated = batch.cases[0]
    state = CaseState(case_id=generated.observed.case_id, arm="X", arm_mode=ArmMode.ENFORCE)
    from settle.policy.legal import legal_actions

    legal = legal_actions(generated.observed, state)
    for key in sorted(ARMS):
        chosen = build(key).choose(generated.observed, state, legal)
        assert type(chosen) in ACTION_MODELS, f"{key} emitted {type(chosen)}"


# --------------------------------------------------------------------------
# ARM-2 — B2 has full capability
# --------------------------------------------------------------------------

def test_ARM_2_b2_serves_the_e_mandate_notice_on_enach(batch, tmp_path):
    """Without this B2 loses every enach case to G9 and the win is fake."""
    enach = [g for g in batch.cases if g.observed.rail is Rail.ENACH]
    assert enach, "no enach cases in the batch to test against"

    path = tmp_path / "b2_enach.jsonl"
    with Ledger(path) as ledger:
        for generated in enach:
            drive(generated, FixedLadderArm(), ledger)

    verbs = {e.payload["action"]["type"] for e in dispatched(path)}
    assert ActionType.SERVE_NOTICE.value in verbs, "B2 never served a notice on enach"
    assert ActionType.RETRY.value in verbs, "B2 served notice but never debited"


def test_ARM_2_b2_uses_every_channel_available_to_ours(batch, tmp_path):
    """Same channels, same templates. Denying WhatsApp would be crippling by
    omission — §14.1 denies baselines intelligence, not capability."""
    path = tmp_path / "b2.jsonl"
    with Ledger(path) as ledger:
        for generated in batch.cases:
            drive(generated, FixedLadderArm(), ledger)

    channels = {
        e.payload["action"].get("channel")
        for e in dispatched(path)
        if "channel" in e.payload["action"]
    }
    assert Channel.SMS.value in channels
    assert Channel.WHATSAPP.value in channels, "B2 never used WhatsApp"


def test_ARM_2_b2_ignores_the_decline_class(batch, tmp_path):
    """The intelligence it is denied. It keeps retrying wherever gates allow."""
    path = tmp_path / "b2_classes.jsonl"
    with Ledger(path) as ledger:
        for generated in batch.cases:
            drive(generated, FixedLadderArm(), ledger)

    retries = [e for e in dispatched(path) if e.payload["action"]["type"] == "retry"]
    cases_retried = {e.case_id for e in retries}
    assert len(cases_retried) > 20
    # Retries sit on the declared grid, not on offsets B2 invented.
    from settle.policy.params import hour_offsets

    assert {e.payload["action"]["at_hour_offset"] for e in retries} <= set(hour_offsets())


# --------------------------------------------------------------------------
# ARM-3 — B3, OBSERVE, and INV-11
# --------------------------------------------------------------------------

def test_ARM_3_b3_in_observe_produces_violations(batch, tmp_path):
    path = tmp_path / "b3_observe.jsonl"
    arm = MaxPressureArm()
    assert arm.mode is ArmMode.OBSERVE
    with Ledger(path) as ledger:
        for generated in batch.cases[:200]:
            drive(generated, arm, ledger)

    violations = [
        e
        for e in read_entries(path)
        if e.kind is LedgerKind.GATE_CHECK and e.payload["violations"]
    ]
    assert violations, "B3 produced no violations, so it is not testing the gates"
    fired = {gate for e in violations for gate in e.payload["violations"]}
    assert len(fired) >= 2, f"only {fired} ever fired"


def test_ARM_3_the_same_arm_in_enforce_produces_none(batch, tmp_path):
    """One gate implementation, one code path (§4). Only the binding differs."""

    class EnforcedMaxPressure(MaxPressureArm):
        mode = ArmMode.ENFORCE

    path = tmp_path / "b3_enforce.jsonl"
    with Ledger(path) as ledger:
        for generated in batch.cases[:200]:
            drive(generated, EnforcedMaxPressure(), ledger)

    for entry in read_entries(path):
        if entry.kind is LedgerKind.GATE_CHECK:
            assert entry.payload["violations"] == []


def test_ARM_3_inv_11_ours_can_never_be_constructed_in_observe():
    """Enforced where arms are constructed, so a CLI flag cannot do it."""
    with pytest.raises(ValueError, match="INV-11"):
        assert_enforce_only("OURS", ArmMode.OBSERVE)
    with pytest.raises(ValueError, match="INV-11"):
        assert_enforce_only("ours", ArmMode.OBSERVE)
    assert_enforce_only("OURS", ArmMode.ENFORCE)
    assert_enforce_only("B3", ArmMode.OBSERVE)


def test_ARM_3_b3_is_the_only_arm_that_runs_in_observe():
    observing = {key for key in ARMS if build(key).mode is ArmMode.OBSERVE}
    assert observing == {"b3"}, observing


# --------------------------------------------------------------------------
# ARM-4 — B1
# --------------------------------------------------------------------------

def test_ARM_4_b1_dispatches_at_most_one_retry_per_case(batch, tmp_path):
    path = tmp_path / "b1.jsonl"
    with Ledger(path) as ledger:
        for generated in batch.cases:
            drive(generated, SingleRetryArm(), ledger)

    retries_per_case = {}
    for entry in dispatched(path):
        if entry.payload["action"]["type"] == "retry":
            retries_per_case[entry.case_id] = retries_per_case.get(entry.case_id, 0) + 1

    assert retries_per_case, "B1 never retried anything"
    assert max(retries_per_case.values()) == 1, retries_per_case


def test_ARM_4_b1_can_still_open_a_notice_window_on_enach(batch, tmp_path):
    """Denying B1 the notice would mean it never retries an enach case at all,
    which is crippling by omission rather than denial of intelligence."""
    enach = [g for g in batch.cases if g.observed.rail is Rail.ENACH]
    path = tmp_path / "b1_enach.jsonl"
    with Ledger(path) as ledger:
        for generated in enach:
            drive(generated, SingleRetryArm(), ledger)

    verbs = {e.payload["action"]["type"] for e in dispatched(path)}
    assert ActionType.SERVE_NOTICE.value in verbs
    assert ActionType.RETRY.value in verbs


def test_ARM_4_b1_does_nothing_once_it_has_retried(batch):
    generated = batch.cases[0]
    spent = CaseState(
        case_id=generated.observed.case_id, arm="B1", arm_mode=ArmMode.ENFORCE, attempts_used=1
    )
    from settle.policy.legal import legal_actions

    legal = legal_actions(generated.observed, spent)
    assert SingleRetryArm().choose(generated.observed, spent, legal).type is ActionType.DO_NOTHING


# --------------------------------------------------------------------------
# ARM-5 / ARM-6 / ARM-7 — HYBRID. CP17, SPEC §14.1.
#
# HYBRID composes two arms that already exist. These assert the composition by
# running the sources on the same cases and comparing what came out — not by
# reading the router, which would test the code against itself and pass on a
# router that was wrong in the same way twice.
# --------------------------------------------------------------------------

def _estimator():
    import pickle

    from settle.agent.estimator import Estimator, latest_model_path

    payload = pickle.loads(Path(latest_model_path()).read_bytes())
    return Estimator(payload["models"][payload["winner"]], payload["winner"])


def _dispatch_log(cases, arm, path):
    """Every dispatch each case produced, keyed by case, as comparable JSON."""
    with Ledger(path) as ledger:
        for generated in cases:
            drive(generated, arm, ledger)
    entries = read_entries(path)
    out: dict[str, list] = {g.observed.case_id: [] for g in cases}
    for entry in entries:
        if entry.kind is LedgerKind.DISPATCH:
            # Seq is a per-file counter and differs between arms that dispatched
            # different totals, so it is deliberately not compared. What has to
            # match is the action, its key, and when it fired.
            out[entry.case_id].append(
                (entry.at.isoformat(), json.dumps(entry.payload, sort_keys=True))
            )
    return out


def _by_class(cases, name):
    return [g for g in cases if classify(g.observed.decline_code).value == name]


# OURS is the expensive arm and these three tests all need the same three logs,
# so they are built once. A smaller batch than the module fixture: 250 cases
# carries every decline class and the tests are comparisons, not measurements —
# nothing here reads a rate.
HYBRID_CASES = 250


@pytest.fixture(scope="module")
def composed(tmp_path_factory):
    """OURS, B2 and HYBRID over one batch, under one seed. Built once."""
    batch = generate_batch(HYBRID_CASES, SEED)
    directory = tmp_path_factory.mktemp("hybrid")
    estimator = _estimator()
    return (
        batch.cases,
        _dispatch_log(batch.cases, OursArm(estimator), directory / "ours.jsonl"),
        _dispatch_log(batch.cases, FixedLadderArm(), directory / "b2.jsonl"),
        _dispatch_log(batch.cases, HybridArm(estimator), directory / "hybrid.jsonl"),
    )


def test_ARM_5_hybrid_routes_auth_abandoned_to_ours_and_the_rest_to_b2(composed):
    """Compared against each source arm on the same cases, class by class.

    A router asserted by inspection is a router asserted against itself. This
    runs OURS and B2 over the whole batch, runs HYBRID over the same batch under
    the same streams, and requires each class's dispatches to match exactly one
    of them.
    """
    cases, ours, b2, hybrid = composed

    seen: dict[str, set[str]] = {}
    for generated in cases:
        cid = generated.observed.case_id
        cls = classify(generated.observed.decline_code).value
        matches = set()
        if hybrid[cid] == ours[cid]:
            matches.add("OURS")
        if hybrid[cid] == b2[cid]:
            matches.add("B2")
        assert matches, f"{cid} ({cls}): HYBRID matched neither source arm"
        seen.setdefault(cls, set()).update(matches)

    # auth_abandoned must be OURS and must be distinguishable from B2 — if the
    # two agreed everywhere, the routing claim would be untestable.
    assert "OURS" in seen["auth_abandoned"]
    assert "B2" not in seen["auth_abandoned"] or len(seen["auth_abandoned"]) > 1

    for cls, matched in seen.items():
        if cls == "auth_abandoned":
            continue
        assert "B2" in matched, f"{cls} did not follow the ladder"


def test_ARM_6_hybrid_in_observe_raises():
    """INV-11 reaches HYBRID through what it contains: it holds OURS."""
    estimator = _estimator()
    with pytest.raises(ValueError, match="INV-11"):
        HybridArm(estimator, ArmMode.OBSERVE)

    # And it is constructible in the mode it is allowed to run in.
    assert HybridArm(estimator, ArmMode.ENFORCE).mode is ArmMode.ENFORCE
    assert HybridArm(estimator).mode is ArmMode.ENFORCE


def test_ARM_7_hybrid_decisions_are_byte_identical_to_the_arm_that_owns_the_class(
    composed,
):
    """What "composes two arms" has to mean.

    Not "similar to", not "as good as" — the same bytes. A router that produced
    a third behaviour on either class would make the per-class comparison
    meaningless, because HYBRID's rows would no longer be OURS's and B2's rows
    rearranged.
    """
    cases, ours, b2, hybrid = composed

    auth = _by_class(cases, "auth_abandoned")
    shiftable = _by_class(cases, "time_shiftable")
    assert auth and shiftable, "the batch lacks a class this test needs"

    for generated in auth:
        cid = generated.observed.case_id
        assert hybrid[cid] == ours[cid], f"{cid}: auth_abandoned did not match OURS"

    for generated in shiftable:
        cid = generated.observed.case_id
        assert hybrid[cid] == b2[cid], f"{cid}: time_shiftable did not match B2"

    # Not vacuous: the two sources must actually differ somewhere, or "identical
    # to whichever owns the class" would be true of any router at all.
    differ = [
        g.observed.case_id for g in cases
        if ours[g.observed.case_id] != b2[g.observed.case_id]
    ]
    assert differ, "OURS and B2 dispatched identically on every case"
    assert any(
        classify(g.observed.decline_code).value == "auth_abandoned"
        for g in cases if g.observed.case_id in set(differ)
    ), "OURS and B2 never differ on auth_abandoned, so the routing is unobservable"


def test_ARM_7_hybrid_reimplements_neither_arm():
    """It holds the two arms and delegates. No third policy, no new parameter.

    Checked structurally as well as behaviourally: a `choose` that grew its own
    logic would still pass the comparison tests on the day it was written and
    drift the week after.
    """
    source = (Path(__file__).resolve().parent.parent
              / "settle" / "runner" / "arms" / "hybrid.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    choose = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "choose"
    )
    # One statement: return the delegate's answer.
    assert len(choose.body) == 1, "HYBRID.choose grew logic of its own"
    assert isinstance(choose.body[0], ast.Return)

    # It imports both arms and defines no policy constant beyond the class list.
    assert "OursArm" in source and "FixedLadderArm" in source
    for banned in ("expected_value", "predict", "hour_offsets", "POLICY_PARAMS"):
        assert banned not in source, f"hybrid.py reaches for {banned}"
