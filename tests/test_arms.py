"""CP5 — the arms. SPEC §14.1.

ARM-2 is the one that decides whether the headline is honest. §14.1 says
baselines are given full capability and denied only intelligence; a B2 that
cannot serve an e-mandate notice would lose every enach case to G9 and hand us
a win we did not earn.
"""

from datetime import datetime, timezone

import pytest

from settle.audit.chain import Ledger, read_entries
from settle.execute.executor import WorldHandle
from settle.runner.arm import ARMS, DoNothingArm, FirstLegalArm, assert_enforce_only
from settle.runner.arms.baselines import FixedLadderArm, MaxPressureArm, SingleRetryArm
from settle.runner.arms.explore import ExploreArm
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
