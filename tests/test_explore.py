"""CP5 — EXPLORE. SPEC §10.1, §14.1, A71, A72.

EXP-6 is the checkpoint's real output. If the grid is too large or 30k cases too
few, the estimator will extrapolate exactly where the policy needs it to predict,
and every calibration number will look fine on a held-out set with the same
degenerate coverage. Better to find that out here than after training.
"""

import collections
import subprocess
import sys
from datetime import timedelta, timezone
from pathlib import Path

import pytest

from settle.audit.chain import Ledger, read_entries
from settle.execute.executor import WorldHandle
from settle.policy.gates import IST, evaluate_gates
from settle.policy.legal import CONTACT_BEARING
from settle.policy.params import hour_offsets, max_horizon_h
from settle.runner.arms.explore import (
    EVALUATION_SEED_RANGE,
    EXPLORE_SEED_RANGE,
    ExploreArm,
    gate_passing_pairs,
    is_evaluation_seed,
    is_explore_seed,
)
from settle.runner.case_runner import run_case
from settle.schema.enums import ActionType, ArmMode, LedgerKind
from settle.schema.state import CaseState
from settle.sim.generator import generate_batch
from settle.sim.observability import ObservabilityConfig
from settle.sim.streams import Streams

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPLORE_SEED = 90_000
MIN_OBSERVATIONS = 30


def _pair(action) -> tuple:
    """A grid pair is the whole action.

    Collapsing the channel would make a size-5 set holding both an SMS and a
    WhatsApp message look 40% biased toward `send_message` when 2/5 is exactly
    the uniform answer.
    """
    return (
        action.type.value,
        getattr(action, "at_hour_offset", None),
        getattr(getattr(action, "channel", None), "value", None),
        getattr(getattr(action, "rail", None), "value", None),
        getattr(getattr(action, "to", None), "value", None),
    )


def explore_run(n_cases: int, ledger: Ledger) -> ExploreArm:
    arm = ExploreArm(EXPLORE_SEED)
    batch = generate_batch(n_cases, EXPLORE_SEED)
    streams = Streams(EXPLORE_SEED)
    observability = ObservabilityConfig()
    for generated in batch.cases:
        run_case(
            generated.observed,
            arm,
            WorldHandle(truth=generated.truth, streams=streams),
            observability,
            ledger,
        )
    return arm


@pytest.fixture(scope="module")
def big_run(tmp_path_factory):
    """The 30,000-case EXPLORE run, built once and shared.

    Three slow tests each doing their own run was ~5 minutes of the gate for one
    run's worth of information.
    """
    path = tmp_path_factory.mktemp("explore30k") / "audit.jsonl"
    with Ledger(path) as ledger:
        arm = explore_run(30_000, ledger)
    return arm, path


@pytest.fixture(scope="module")
def small_run(tmp_path_factory):
    path = tmp_path_factory.mktemp("explore") / "audit.jsonl"
    with Ledger(path) as ledger:
        arm = explore_run(1_500, ledger)
    return arm, path


# --------------------------------------------------------------------------
# EXP-1
# --------------------------------------------------------------------------

def test_EXP_1_the_sampler_is_uniform_over_a_fixed_set_size():
    """The draw itself, isolated from which pairs happen to pass.

    Indices are taken from an addressed hash; if that hash were biased, every
    downstream uniformity claim would be false and nothing else would notice.
    """
    from settle.sim.streams import derive_unit_float

    for size in (2, 5, 9, 15):
        counts = collections.Counter()
        for tick in range(20_000):
            draw = derive_unit_float(EXPLORE_SEED, "explore_draw", "case_000001", tick)
            counts[min(int(draw * size), size - 1)] += 1
        expected = 20_000 / size
        assert set(counts) == set(range(size))
        for index, seen in counts.items():
            assert abs(seen - expected) / expected < 0.06, (size, index, seen)


def test_EXP_1_the_realised_distribution_is_uniform_over_the_passing_set(small_run):
    """Aggregated over real decisions, conditioned on set size.

    Sets differ per case and tick, so the testable claim is that the chosen
    *index* is uniform given how many options there were.
    """
    arm, _ = small_run
    # Uniform over *pairs*, not verbs. The grid expands one retry into eight
    # offsets, so a set of nine is do_nothing plus eight retries and `retry`
    # taking 8/9 of the draws is the uniform answer, not a bias.
    by_size = collections.defaultdict(collections.Counter)
    for decision in arm.decisions:
        size = round(1 / decision.propensity)
        by_size[size][_pair(decision.action)] += 1

    checked = 0
    for size, pairs in by_size.items():
        total = sum(pairs.values())
        if size < 2 or total < 600:
            continue
        checked += 1
        for pair, count in pairs.items():
            share = count / total
            assert share <= (1.0 / size) * 2.0 + 0.03, (size, pair, share)
    assert checked >= 2, "not enough multi-option decisions to test uniformity"


@pytest.mark.slow
def test_EXP_1_uniformity_holds_at_thirty_thousand_cases(big_run):
    arm, _ = big_run
    by_size = collections.defaultdict(collections.Counter)
    for decision in arm.decisions:
        by_size[round(1 / decision.propensity)][_pair(decision.action)] += 1

    for size, pairs in by_size.items():
        total = sum(pairs.values())
        if size < 2 or total < 5_000:
            continue
        for pair, count in pairs.items():
            assert count / total <= (1.0 / size) * 1.6 + 0.01, (size, pair, count / total)


# --------------------------------------------------------------------------
# EXP-2
# --------------------------------------------------------------------------

def test_EXP_2_logged_propensity_is_one_over_the_passing_set():
    """The probability the action was *chosen*, not that it was proposed (A72).

    Driven through `choose()` on states we construct, rather than reconstructed
    from a finished run: mid-run state carries contact history, counters and
    notice windows that change what passes, and a bare tick cannot recover them.
    """
    arm = ExploreArm(EXPLORE_SEED)
    batch = generate_batch(250, EXPLORE_SEED)
    checked = 0
    for generated in batch.cases:
        case = generated.observed
        for tick, attempts, contacts in ((0, 0, 0), (7, 1, 0), (26, 0, 1), (73, 2, 2)):
            state = CaseState(
                case_id=case.case_id,
                arm="EXPLORE",
                arm_mode=ArmMode.ENFORCE,
                tick=tick,
                attempts_used=attempts,
                contacts_used=contacts,
            )
            passing = gate_passing_pairs(case, state)
            before = len(arm.decisions)
            chosen = arm.choose(case, state, [])
            decision = arm.decisions[before]

            assert chosen in passing
            assert decision.action == chosen
            assert decision.propensity == pytest.approx(1.0 / len(passing))
            checked += 1
    assert checked > 500


def test_EXP_2_every_decision_carries_a_propensity(small_run):
    arm, _ = small_run
    assert all(d.propensity is not None and 0.0 < d.propensity <= 1.0 for d in arm.decisions)
    assert all(d.arm == "EXPLORE" and d.arm_mode is ArmMode.ENFORCE for d in arm.decisions)


def test_EXP_2_propensity_would_be_wrong_if_sampled_from_the_legal_set(small_run):
    """The distinction A72 exists for, demonstrated rather than asserted.

    If EXPLORE sampled the legal set, 1/len(legal) would differ from the
    probability the action actually executed, because gates would filter after
    the draw.
    """
    from settle.policy.legal import legal_actions
    from settle.runner.arms.explore import candidate_pairs

    batch = generate_batch(300, EXPLORE_SEED)
    differing = 0
    for generated in batch.cases:
        for tick in (0, 5, 11, 30):
            state = CaseState(
                case_id=generated.observed.case_id,
                arm="EXPLORE",
                arm_mode=ArmMode.ENFORCE,
                tick=tick,
            )
            proposed = len(candidate_pairs(generated.observed, state))
            executed = len(gate_passing_pairs(generated.observed, state))
            if proposed != executed:
                differing += 1
    assert differing > 0, "gates never filtered anything, so the distinction is untested"


# --------------------------------------------------------------------------
# EXP-3
# --------------------------------------------------------------------------

def test_EXP_3_explore_never_violates_a_gate(small_run):
    """It samples from what passes, so nothing it chooses can be blocked."""
    _, path = small_run
    checks = [e for e in read_entries(path) if e.kind is LedgerKind.GATE_CHECK]
    assert checks
    blocked = [e for e in checks if e.payload["blocked_by"]]
    assert not blocked, f"{len(blocked)} EXPLORE choices were blocked by gates"
    assert not [e for e in checks if e.payload["violations"]]


@pytest.mark.slow
def test_EXP_3_zero_violations_across_thirty_thousand_cases(big_run):
    _, path = big_run
    blocked = [
        e
        for e in read_entries(path)
        if e.kind is LedgerKind.GATE_CHECK and e.payload["blocked_by"]
    ]
    assert not blocked, f"{len(blocked)} blocked choices at 30k"


# --------------------------------------------------------------------------
# EXP-4
# --------------------------------------------------------------------------

def test_EXP_4_the_explore_ledger_is_byte_identical_across_processes(tmp_path):
    """Training data that is not reproducible is not training data."""
    script = (
        "import sys;"
        "from settle.audit.chain import Ledger;"
        "from settle.execute.executor import WorldHandle;"
        "from settle.runner.arms.explore import ExploreArm;"
        "from settle.runner.case_runner import run_case;"
        "from settle.sim.generator import generate_batch;"
        "from settle.sim.observability import ObservabilityConfig;"
        "from settle.sim.streams import Streams;"
        "arm = ExploreArm(90000); b = generate_batch(60, 90000);"
        "s = Streams(90000); o = ObservabilityConfig(); led = Ledger(sys.argv[1]);"
        "[run_case(g.observed, arm, WorldHandle(truth=g.truth, streams=s), o, led)"
        " for g in b.cases];"
        "led.close();"
        "print(open(sys.argv[1]).read(), end='')"
    )
    outputs = []
    for hash_seed in ("0", "1", "random"):
        target = tmp_path / f"e_{hash_seed}.jsonl"
        outputs.append(
            subprocess.run(
                [sys.executable, "-c", script, str(target)],
                cwd=REPO_ROOT, capture_output=True, text=True, check=True,
                env={"PYTHONHASHSEED": hash_seed, "PATH": "/usr/bin:/bin"},
            ).stdout
        )
    assert outputs[0] == outputs[1] == outputs[2]
    assert outputs[0]


# --------------------------------------------------------------------------
# EXP-5
# --------------------------------------------------------------------------

def test_EXP_5_the_seed_ranges_are_provably_disjoint():
    assert not (set(EVALUATION_SEED_RANGE) & set(EXPLORE_SEED_RANGE))
    assert is_explore_seed(EXPLORE_SEED) and not is_evaluation_seed(EXPLORE_SEED)
    assert is_evaluation_seed(42) and not is_explore_seed(42)


def test_EXP_5_explore_refuses_an_evaluation_seed():
    """Asserted in code, not in a comment. Training on the evaluation seeds is
    not a held-out set, it is a memorisation test."""
    with pytest.raises(ValueError, match="outside EXPLORE_SEED_RANGE"):
        ExploreArm(42)
    with pytest.raises(ValueError):
        ExploreArm(EVALUATION_SEED_RANGE.stop - 1)
    ExploreArm(EXPLORE_SEED_RANGE.start)


def test_EXP_5_the_cli_refuses_a_crossed_seed():
    from settle.runner.run import main

    with pytest.raises(SystemExit):
        main(["--arm", "explore", "--cases", "1", "--seed", "42"])
    with pytest.raises(SystemExit):
        main(["--arm", "b0", "--cases", "1", "--seed", "90000"])


# --------------------------------------------------------------------------
# EXP-6 — coverage. The number that decides whether the estimator is trainable.
# --------------------------------------------------------------------------

def _coverage(decisions):
    grid = collections.Counter()
    hourly = collections.Counter()
    for decision in decisions:
        action = decision.action
        verb = action.type
        offset = getattr(action, "at_hour_offset", None)
        grid[(verb, offset)] += 1
        hourly[(verb, decision.at.astimezone(IST).hour // 4)] += 1
    return grid, hourly


def _reachable(verb: ActionType, bucket: int) -> bool:
    """A contact outside 08:00-19:00 IST is illegal, so its cell is not thin —
    it is unreachable, and the estimator must never be queried there."""
    if verb not in CONTACT_BEARING:
        return True
    return bucket in (2, 3, 4)


@pytest.mark.slow
def test_EXP_6_coverage_of_the_action_grid_at_thirty_thousand_cases(big_run, capsys):
    arm, _ = big_run
    grid, hourly = _coverage(arm.decisions)
    offsets = hour_offsets()
    verbs = sorted({verb for verb, _ in hourly}, key=lambda v: v.value)

    lines = [f"\n  EXPLORE decisions: {len(arm.decisions):,}", "", "  retry x offset"]
    lines.append("    " + " ".join(f"{o:>7}" for o in offsets))
    lines.append("    " + " ".join(f"{grid[(ActionType.RETRY, o)]:>7,}" for o in offsets))
    lines += ["", f"  action x 4h IST bucket   (. = unreachable, G1 shuts the window)"]
    lines.append(f"    {'verb':<24}" + " ".join(f"{b*4:02d}-{b*4+3:02d}" for b in range(6)))
    for verb in verbs:
        cells = []
        for bucket in range(6):
            cells.append(
                f"{hourly[(verb, bucket)]:>5,}" if _reachable(verb, bucket) else f"{'.':>5}"
            )
        lines.append(f"    {verb.value:<24}" + " ".join(cells))

    reachable = [(v, b) for v in verbs for b in range(6) if _reachable(v, b)]
    covered = [cell for cell in reachable if hourly[cell] >= MIN_OBSERVATIONS]
    retry_cells = [(ActionType.RETRY, o) for o in offsets]
    retry_covered = [c for c in retry_cells if grid[c] >= MIN_OBSERVATIONS]

    lines += [
        "",
        f"  retry x offset      {len(retry_covered)}/{len(retry_cells)} cells >= {MIN_OBSERVATIONS} obs",
        f"  reachable cells     {len(covered)}/{len(reachable)} cells >= {MIN_OBSERVATIONS} obs",
        f"  unreachable cells   {len(verbs) * 6 - len(reachable)} (contacts outside the window)",
    ]
    with capsys.disabled():
        print("\n".join(lines))

    assert len(retry_covered) == len(retry_cells), "the offset grid is not fully covered"
    assert len(covered) == len(reachable), [c for c in reachable if hourly[c] < MIN_OBSERVATIONS]


def test_EXP_6_the_grid_is_shared_and_bounded():
    """A71's binding constraint. An estimator trained on one grid and queried on
    another has zero coverage exactly where it is asked to predict."""
    from settle.policy.stops import DECISION_HORIZON_HOURS
    from settle.runner.arms.baselines import FixedLadderArm
    from settle.runner.arms.explore import expand_grid
    from settle.schema.action import Retry
    from settle.schema.enums import Rail

    offsets = hour_offsets()
    assert offsets == tuple(sorted(set(offsets))), "the grid has duplicates or is unsorted"
    assert max_horizon_h() <= DECISION_HORIZON_HOURS
    assert all(0 <= o < max_horizon_h() for o in offsets)

    # Both dimensions §9 needs: a different hour of day, and a later day.
    assert any(o % 24 != 0 for o in offsets), "no offset shifts the hour of day"
    assert any(o >= 48 for o in offsets), "no offset reaches a later day"

    expanded = expand_grid(Retry(at_hour_offset=0, rail=Rail.CARD))
    assert [a.at_hour_offset for a in expanded] == list(offsets)
    assert set(FixedLadderArm().retry_offsets) <= set(offsets), "B2 invented its own offsets"
