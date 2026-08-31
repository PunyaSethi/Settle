"""CP8 — the OURS arm, end to end. SPEC §14.1, §14.3, §14.4.

OUR-3 and OUR-4 are the comparison. They are deliberately weak thresholds: the
point is that the claims hold at all, not that they hold by a chosen margin.
Tuning the policy until a threshold passed would be the thing this whole
checkpoint structure exists to prevent.
"""

import collections
import pickle
import tempfile
from pathlib import Path

import pytest

from settle.agent.estimator import Estimator, latest_model_path
from settle.audit.chain import Ledger, read_entries
from settle.execute.executor import WorldHandle
from settle.recon.reconcile import failure_counts, reconcile
from settle.runner.arm import DoNothingArm
from settle.runner.arms.baselines import FixedLadderArm
from settle.runner.arms.ours import OursArm
from settle.runner.case_runner import run_case
from settle.schema.enums import ArmMode, LedgerKind, SilentFailureClass
from settle.sim.generator import generate_batch
from settle.sim.observability import ObservabilityConfig
from settle.sim.streams import Streams

REPO_ROOT = Path(__file__).resolve().parent.parent
MODEL = latest_model_path(REPO_ROOT / "out")
N, SEED = 400, 42
CONTACT_VERBS = {
    "send_message", "request_mandate_update", "serve_notice", "voice_call", "escalate_human"
}

pytestmark = pytest.mark.skipif(MODEL is None, reason="no trained model; run CP7 training")


@pytest.fixture(scope="module")
def estimator():
    payload = pickle.loads(MODEL.read_bytes())
    return Estimator(payload["models"][payload["winner"]], payload["winner"])


def run_arm(arm, batch):
    """One arm over the batch, under common random numbers."""
    streams, config = Streams(SEED), ObservabilityConfig()
    actuals, cases, truths, finals = {}, {}, {}, []
    path = Path(tempfile.mkdtemp()) / "a.jsonl"
    with Ledger(path) as ledger:
        for generated in batch.cases:
            world = WorldHandle(truth=generated.truth, streams=streams)
            finals.append(run_case(generated.observed, arm, world, config, ledger))
            actuals[generated.observed.case_id] = list(world.actuals)
            cases[generated.observed.case_id] = generated.observed
            truths[generated.observed.case_id] = generated.truth
    entries = read_entries(path)
    return entries, reconcile(entries, actuals, cases, truths=truths, streams=streams), finals


@pytest.fixture(scope="module")
def comparison(estimator):
    batch = generate_batch(N, SEED)
    return {
        "OURS": run_arm(OursArm(estimator), batch),
        "B0": run_arm(DoNothingArm(), batch),
        "B2": run_arm(FixedLadderArm(), batch),
    }


def recovered(reconciled):
    return {c for c, r in reconciled.items() if r.actually_settled and not r.reversed}


def contacts(entries):
    return [
        e
        for e in entries
        if e.kind is LedgerKind.DISPATCH and e.payload["action"]["type"] in CONTACT_VERBS
    ]


# --------------------------------------------------------------------------
# OUR-1
# --------------------------------------------------------------------------

def test_OUR_1_ours_cannot_be_constructed_in_observe(estimator):
    """INV-11, enforced at construction so no flag or config can do it."""
    with pytest.raises(ValueError, match="INV-11"):
        OursArm(estimator, ArmMode.OBSERVE)
    assert OursArm(estimator).mode is ArmMode.ENFORCE


def test_OUR_1_ours_satisfies_the_arm_protocol(estimator):
    arm = OursArm(estimator)
    assert arm.name == "OURS" and callable(arm.choose)


# --------------------------------------------------------------------------
# OUR-2
# --------------------------------------------------------------------------

def test_OUR_2_zero_compliance_violations(comparison):
    """SF-5 and SF-6 for an ENFORCE arm are a gate failure, not an audit
    finding. A non-zero count here means a gate did not hold."""
    _, reconciled, _ = comparison["OURS"]
    counts = failure_counts(reconciled)
    assert counts[SilentFailureClass.SF5] == 0, "a contact followed an opt-out"
    assert counts[SilentFailureClass.SF6] == 0, "a contact fell outside 08:00-19:00 IST"


def test_OUR_2_no_dispatch_was_ever_blocked(comparison):
    """The policy consults gates before choosing (A72), so nothing it picks
    should be refused when the runner evaluates them again."""
    entries, _, _ = comparison["OURS"]
    blocked = [
        e
        for e in entries
        if e.kind is LedgerKind.GATE_CHECK
        and e.payload["blocked_by"]
        and not e.payload.get("scheduled")
    ]
    assert not blocked, f"{len(blocked)} choices were blocked at the moment of choosing"


# --------------------------------------------------------------------------
# OUR-3 / OUR-4
# --------------------------------------------------------------------------

def test_OUR_3_ours_beats_b0_on_incremental_recovery(comparison, capsys):
    """§14.3: a case that also recovers under B0 is not counted. Roughly a fifth
    of at-risk value returns on its own, and counting it is the easiest way for
    a recovery product to flatter itself."""
    _, ours, _ = comparison["OURS"]
    _, b0, _ = comparison["B0"]
    baseline = recovered(b0)
    incremental = recovered(ours) - baseline

    with capsys.disabled():
        print(f"\n  B0 self-cure  {len(baseline)}/{N}")
        print(f"  OURS total    {len(recovered(ours))}/{N}")
        print(f"  incremental   {len(incremental)}/{N} = {len(incremental)/N:.1%}")
    assert incremental, "OURS recovered nothing B0 did not"


def test_OUR_4_ours_uses_strictly_fewer_contacts_per_case_than_b2(comparison, capsys):
    ours_entries, ours_rec, _ = comparison["OURS"]
    b2_entries, b2_rec, _ = comparison["B2"]
    ours_contacts = len(contacts(ours_entries)) / N
    b2_contacts = len(contacts(b2_entries)) / N

    with capsys.disabled():
        print(f"\n  contacts per case   OURS {ours_contacts:.3f}   B2 {b2_contacts:.3f}")
        print(f"  incremental cases   OURS {len(recovered(ours_rec)):>4}   B2 {len(recovered(b2_rec)):>4}")
    assert ours_contacts < b2_contacts, "OURS is not more restrained than the fixed ladder"


def test_OUR_4_restraint_is_deliberate_not_incapacity(comparison, estimator, capsys):
    """A policy that never acts is not restrained, it is broken. OURS must be
    choosing `do_nothing` on purpose and acting when the uplift pays."""
    arm = OursArm(estimator)
    batch = generate_batch(120, SEED)
    run_arm(arm, batch)

    reasons = collections.Counter(d.reason_code for d in arm.decisions)
    with capsys.disabled():
        print(f"\n  decision reasons  {dict(reasons)}")
    assert reasons["EV_ARGMAX"] > 0, "OURS never acted at all"
    assert reasons["DO_NOTHING_DOMINATES"] > 0, "OURS never declined"

    acting = [d.uplift for d in arm.decisions if d.reason_code == "EV_ARGMAX"]
    declining = [d.uplift for d in arm.decisions if d.reason_code == "DO_NOTHING_DOMINATES"]
    assert sum(acting) / len(acting) > sum(declining) / len(declining), (
        "OURS acts on lower uplift than it declines — the argmax is inverted"
    )
