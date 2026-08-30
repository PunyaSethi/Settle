"""CP4 — the case runner. SPEC §12, §13, §14.1.

RUN-9 is the structural one. The runner must be correct while seeing only what
it is told, so it is not permitted to see anything else: `settle/runner/` imports
nothing from `settle.sim.truth`, and `case_runner.py` imports nothing from
`settle.sim` at all. The `WorldHandle` passes through it unopened.
"""

import ast
import collections
import json
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from settle.audit.chain import Ledger, read_entries, verify_file
from settle.execute.executor import WorldHandle
from settle.policy.stops import CONTACT_BUDGET, DECISION_HORIZON_HOURS
from settle.runner.arm import DoNothingArm, FirstLegalArm
from settle.runner.case_runner import (
    DECISION_CADENCE_HOURS,
    MAX_STEPS_PER_CASE,
    next_interesting_tick,
    run_case,
)
from settle.schema.action import DoNothing, Retry, SendMessage
from settle.schema.enums import ArmMode, Channel, LedgerKind, Rail, StopClass
from settle.schema.state import CaseState, CaseStatus
from settle.sim.generator import generate_batch
from settle.sim.observability import ObservabilityConfig
from settle.sim.streams import Streams

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNNER_DIR = REPO_ROOT / "settle" / "runner"
SEED = 42


@pytest.fixture(scope="module")
def batch():
    return generate_batch(400, SEED)


def drive(generated, arm, ledger, observability=None, initial_state=None) -> CaseState:
    return run_case(
        generated.observed,
        arm,
        WorldHandle(truth=generated.truth, streams=Streams(SEED)),
        observability or ObservabilityConfig(),
        ledger,
        initial_state,
    )


# --------------------------------------------------------------------------
# RUN-1 / RUN-2
# --------------------------------------------------------------------------

def test_RUN_1_every_case_runs_to_a_stop(tmp_path, batch):
    with Ledger(tmp_path / "a.jsonl") as ledger:
        finals = [drive(g, FirstLegalArm(), ledger) for g in batch.cases]
    assert len(finals) == len(batch.cases)
    for final in finals:
        assert final.status is CaseStatus.STOPPED
        assert final.stop_reason
        assert final.stop_class in set(StopClass)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ({"settled": True}, "S1_RECOVERED_SETTLED"),
        ({"attempts_used": 99}, "S3_ATTEMPT_BUDGET_EXHAUSTED"),
        ({"contacts_used": 99}, "S3_CONTACT_BUDGET_EXHAUSTED"),
        ({"tick": DECISION_HORIZON_HOURS}, "S6_DECISION_HORIZON"),
        ({"opted_out": True}, "S4_OPT_OUT"),
        ({"disputed": True}, "S5_DISPUTE_RAISED"),
    ],
)
def test_RUN_1_each_stop_reason_is_reachable_through_the_runner(tmp_path, batch, mutation, expected):
    """A stop nothing can reach is a stop that was never tested."""
    generated = batch.cases[0]
    seeded = CaseState(
        case_id=generated.observed.case_id, arm="FIRST_LEGAL", arm_mode=ArmMode.ENFORCE, **mutation
    )
    with Ledger(tmp_path / f"{expected}.jsonl") as ledger:
        final = drive(generated, FirstLegalArm(), ledger, initial_state=seeded)
    assert final.stop_reason == expected


def test_RUN_1_s2_is_reachable_for_a_dead_instrument_with_no_customer_path(tmp_path, batch):
    dead = next(g for g in batch.cases if g.observed.decline_code == "card_expired")
    seeded = CaseState(
        case_id=dead.observed.case_id, arm="B0", arm_mode=ArmMode.ENFORCE, opted_out=True
    )
    with Ledger(tmp_path / "s2.jsonl") as ledger:
        final = drive(dead, DoNothingArm(), ledger, initial_state=seeded)
    assert final.stop_reason == "S2_DEAD_INSTRUMENT_NO_PATH"


def test_RUN_2_no_case_exceeds_the_decision_horizon(tmp_path, batch):
    with Ledger(tmp_path / "a.jsonl") as ledger:
        for generated in batch.cases:
            final = drive(generated, FirstLegalArm(), ledger)
            assert final.tick <= DECISION_HORIZON_HOURS, final.case_id


def test_RUN_2_tick_advancement_is_strictly_increasing(batch):
    """Termination is guaranteed by S6 only if the tick always moves."""
    generated = batch.cases[3]
    state = CaseState(case_id=generated.observed.case_id, arm="B0", arm_mode=ArmMode.ENFORCE)
    for blocked in ((), ("G1",), ("G2",), ("G6",), ("G9",)):
        for action in (DoNothing(), Retry(at_hour_offset=0, rail=Rail.CARD)):
            assert next_interesting_tick(generated.observed, state, action, blocked) > state.tick


def test_RUN_2_a_pending_commitment_is_the_next_wake_up(batch):
    """A73 moved this. The offset no longer advances the tick by itself — a
    commitment does, and it is `due_tick` that the runner sleeps to. Asserting
    the old behaviour would be asserting the bug OQ-30 was about.
    """
    from settle.schema.state import Scheduled

    generated = batch.cases[3]
    state = CaseState(case_id=generated.observed.case_id, arm="B0", arm_mode=ArmMode.ENFORCE)
    loose = Retry(at_hour_offset=6, rail=Rail.CARD)

    # An offset on a loose action changes nothing; it is not a commitment yet.
    assert next_interesting_tick(generated.observed, state, loose, ()) == DECISION_CADENCE_HOURS
    assert next_interesting_tick(generated.observed, state, DoNothing(), ()) == DECISION_CADENCE_HOURS

    committed = state.model_copy(
        update={"scheduled": Scheduled(action=loose, due_tick=6, scheduled_at=0)}
    )
    assert next_interesting_tick(generated.observed, committed, loose, ()) == 6


def test_RUN_2_a_g1_block_advances_to_the_next_window_opening(batch):
    """Not by one hour. Waiting out the night an hour at a time is 13 wasted
    iterations per case per night."""
    generated = batch.cases[3]
    message = SendMessage(channel=Channel.SMS, template_id="t")
    state = CaseState(
        case_id=generated.observed.case_id, arm="B0", arm_mode=ArmMode.ENFORCE, tick=15
    )
    nxt = next_interesting_tick(generated.observed, state, message, ("G1",))
    assert nxt > state.tick
    assert nxt - state.tick <= 24


# --------------------------------------------------------------------------
# RUN-3
# --------------------------------------------------------------------------

@pytest.mark.parametrize("mode", list(ArmMode))
def test_RUN_3_a_stopped_case_accepts_no_further_dispatch(tmp_path, batch, mode):
    """SPEC §13: post-stop events are recorded and do nothing."""
    generated = batch.cases[1]
    stopped = CaseState(
        case_id=generated.observed.case_id,
        arm="FIRST_LEGAL",
        arm_mode=mode,
        status=CaseStatus.STOPPED,
        stop_reason="S4_OPT_OUT",
        stop_class=StopClass.COMPLIANCE,
    )
    path = tmp_path / f"stopped_{mode.value}.jsonl"
    with Ledger(path) as ledger:
        final = drive(generated, FirstLegalArm(mode), ledger, initial_state=stopped)
    assert final.status is CaseStatus.STOPPED
    kinds = [entry.kind for entry in read_entries(path)]
    assert LedgerKind.DISPATCH not in kinds
    assert kinds == [LedgerKind.STOP]


# --------------------------------------------------------------------------
# RUN-4
# --------------------------------------------------------------------------

def test_RUN_4_the_same_seed_produces_a_byte_identical_ledger_across_processes(tmp_path):
    """Arm comparison is meaningless if a run is not reproducible."""
    script = (
        "import sys;"
        "from settle.audit.chain import Ledger;"
        "from settle.execute.executor import WorldHandle;"
        "from settle.runner.arm import FirstLegalArm;"
        "from settle.runner.case_runner import run_case;"
        "from settle.sim.generator import generate_batch;"
        "from settle.sim.observability import ObservabilityConfig;"
        "from settle.sim.streams import Streams;"
        "b = generate_batch(40, 42); s = Streams(42); o = ObservabilityConfig();"
        "led = Ledger(sys.argv[1]);"
        "[run_case(g.observed, FirstLegalArm(), WorldHandle(truth=g.truth, streams=s), o, led)"
        " for g in b.cases];"
        "led.close();"
        "print(open(sys.argv[1]).read(), end='')"
    )
    outputs = []
    for hash_seed in ("0", "1", "random"):
        target = tmp_path / f"run_{hash_seed}.jsonl"
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
# RUN-5
# --------------------------------------------------------------------------

def test_RUN_5_b0_dispatches_nothing_and_still_terminates_every_case(tmp_path, batch):
    """§14.3 subtracts whatever B0 recovers. If B0 acted, the subtraction would
    be measuring our own policy against itself."""
    path = tmp_path / "b0.jsonl"
    with Ledger(path) as ledger:
        finals = [drive(g, DoNothingArm(), ledger) for g in batch.cases]

    assert all(final.status is CaseStatus.STOPPED for final in finals)
    entries = read_entries(path)
    assert entries
    assert not [e for e in entries if e.kind is LedgerKind.DISPATCH]
    assert all(final.contacts_used == 0 and final.attempts_used == 0 for final in finals)
    assert all(final.dispatched_keys == frozenset() for final in finals)
    verify_file(path)


# --------------------------------------------------------------------------
# RUN-6
# --------------------------------------------------------------------------

def test_RUN_6_gate_blocks_are_logged_with_reason_codes_and_counted(tmp_path, batch):
    path = tmp_path / "blocks.jsonl"
    with Ledger(path) as ledger:
        for generated in batch.cases:
            drive(generated, FirstLegalArm(), ledger)

    checks = [e for e in read_entries(path) if e.kind is LedgerKind.GATE_CHECK]
    assert checks
    blocked = [e for e in checks if e.payload["blocked_by"]]
    assert blocked, "no gate ever fired — a gate set that never blocks is untested"

    for entry in blocked:
        assert entry.reason_code == entry.payload["blocked_by"][0]
        assert entry.payload["allowed"] is False

    fired = {gate for e in blocked for gate in e.payload["blocked_by"]}
    assert len(fired) >= 3, f"only {fired} ever fired"


# --------------------------------------------------------------------------
# RUN-7
# --------------------------------------------------------------------------

def test_RUN_7_observe_records_violations_and_proceeds_enforce_blocks(tmp_path, batch):
    subset = batch.cases[:120]

    enforce_path = tmp_path / "enforce.jsonl"
    with Ledger(enforce_path) as ledger:
        for generated in subset:
            drive(generated, FirstLegalArm(ArmMode.ENFORCE), ledger)

    observe_path = tmp_path / "observe.jsonl"
    with Ledger(observe_path) as ledger:
        for generated in subset:
            drive(generated, FirstLegalArm(ArmMode.OBSERVE), ledger)

    enforce = read_entries(enforce_path)
    observe = read_entries(observe_path)

    # ENFORCE: a block means no dispatch and no violation recorded.
    for entry in (e for e in enforce if e.kind is LedgerKind.GATE_CHECK):
        assert entry.payload["violations"] == []
        assert entry.payload["allowed"] is not bool(entry.payload["blocked_by"])

    # OBSERVE: the same blocks are recorded as violations and permitted anyway.
    violations = [
        e for e in observe if e.kind is LedgerKind.GATE_CHECK and e.payload["violations"]
    ]
    assert violations, "OBSERVE recorded no violations, so B3 could not breach anything"
    for entry in violations:
        assert entry.payload["allowed"] is True
        assert entry.payload["violations"] == entry.payload["blocked_by"]

    observe_dispatches = len([e for e in observe if e.kind is LedgerKind.DISPATCH])
    enforce_dispatches = len([e for e in enforce if e.kind is LedgerKind.DISPATCH])
    assert observe_dispatches > enforce_dispatches, "OBSERVE did not actually permit more"


# --------------------------------------------------------------------------
# RUN-8 — marked `slow`, opt in with `pytest -m slow` (A69)
# --------------------------------------------------------------------------

@pytest.mark.slow
@pytest.mark.parametrize("arm_class", [DoNothingArm, FirstLegalArm])
def test_RUN_8_ten_thousand_cases_complete_without_error(tmp_path, arm_class, capsys):
    full = generate_batch(10_000, SEED)
    streams = Streams(SEED)
    observability = ObservabilityConfig()
    arm = arm_class()

    path = tmp_path / f"{arm.name}.jsonl"
    started = time.perf_counter()
    with Ledger(path) as ledger:
        for generated in full.cases:
            final = run_case(
                generated.observed,
                arm,
                WorldHandle(truth=generated.truth, streams=streams),
                observability,
                ledger,
            )
            assert final.status is CaseStatus.STOPPED
    elapsed = time.perf_counter() - started

    verify_file(path)
    with capsys.disabled():
        print(f"\n  RUN-8 {arm.name}: 10,000 cases in {elapsed:.2f}s "
              f"({elapsed / 10_000 * 1000:.3f} ms/case)")


# --------------------------------------------------------------------------
# RUN-9
# --------------------------------------------------------------------------

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


def test_RUN_9_the_runner_never_imports_hidden_truth():
    """The loop must be correct while seeing only what it is told."""
    offenders = {}
    for module_path in sorted(RUNNER_DIR.rglob("*.py")):
        leaked = {n for n in _imports(module_path) if n.startswith("settle.sim.truth")}
        if leaked:
            offenders[str(module_path.relative_to(REPO_ROOT))] = sorted(leaked)
    assert not offenders, f"the runner can reach hidden truth: {offenders}"


def test_RUN_9_the_case_loop_touches_no_simulator_at_all():
    """Stronger than RUN-9's letter. `run.py` builds the batch and hands the
    runner an opaque WorldHandle; the loop itself knows of no simulator."""
    leaked = {n for n in _imports(RUNNER_DIR / "case_runner.py") if n.startswith("settle.sim")}
    assert not leaked, f"case_runner.py imports {leaked}"


def test_RUN_9_detects_a_planted_violation():
    planted = RUNNER_DIR / "_truth_probe.py"
    planted.write_text("from settle.sim.truth import HiddenTruth\n", encoding="utf-8")
    try:
        assert {n for n in _imports(planted) if n.startswith("settle.sim.truth")}
    finally:
        planted.unlink()


def test_RUN_9_the_runner_never_reads_an_actual_outcome():
    """`ActualOutcome` is what the money did. The runner sees ReportedOutcome.

    Checked against imported names rather than raw text, so a docstring that
    explains the rule does not read as a breach of it.
    """
    for module_path in sorted(RUNNER_DIR.rglob("*.py")):
        imported = _imports(module_path)
        assert not [n for n in imported if n.endswith("ActualOutcome")], module_path.name
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert "ActualOutcome" not in names | attrs, module_path.name


def test_RUN_8_the_cadence_is_a_recorded_parameter_not_a_literal():
    """A68. It sets how many decisions an arm gets across the horizon, and
    therefore contacts per case — a §14.4 headline, so INV-10 covers it."""
    from settle.policy.params import POLICY_PARAMS

    assert DECISION_CADENCE_HOURS == int(POLICY_PARAMS["decision_cadence_hours"])


def test_RUN_8_a_switch_to_card_counts_as_a_card_network_submission():
    """A70. G4 caps traffic to a network; the verb that produced it is irrelevant."""
    from settle.policy.gates import CARD_NETWORK_RETRY_CAP, gate_g4
    from settle.schema.action import SwitchRail

    generated = generate_batch(1, SEED).cases[0]
    case = generated.observed.model_copy(update={"rail": Rail.UPI_AUTOPAY})
    base = CaseState(case_id=case.case_id, arm="B0", arm_mode=ArmMode.ENFORCE)

    to_card = SwitchRail(to=Rail.CARD)
    away = SwitchRail(to=Rail.ENACH)

    at_cap = base.model_copy(update={"card_submissions_used": CARD_NETWORK_RETRY_CAP})
    assert gate_g4(case, at_cap, to_card).allowed is False
    assert gate_g4(case, at_cap, away).allowed is True

    # Retries and switches that never touched card do not fill the counter.
    busy_elsewhere = base.model_copy(
        update={"attempts_used": 99, "rail_switches_used": 99, "card_submissions_used": 0}
    )
    assert gate_g4(case, busy_elsewhere, to_card).allowed is True


# --------------------------------------------------------------------------
# SCD — scheduling. SPEC §5.7, A73.
# --------------------------------------------------------------------------

from settle.policy.params import hour_offsets  # noqa: E402
from settle.schema.action import ServeNotice  # noqa: E402
from settle.schema.enums import DeclineClass  # noqa: E402
from settle.schema.state import Scheduled  # noqa: E402


class _FixedRetryArm:
    """Always the same retry at the same offset. A probe, not a policy."""

    name = "PROBE"

    def __init__(self, offset: int, mode: ArmMode = ArmMode.ENFORCE) -> None:
        self.offset = offset
        self.mode = mode

    def choose(self, case, state, legal):
        for action in legal:
            if isinstance(action, Retry):
                return Retry(at_hour_offset=self.offset, rail=action.rail)
        return DoNothing()


def _tick_of(case, moment) -> int:
    return round((moment - case.created_at).total_seconds() / 3600)


def _retryable(batch, rail=Rail.CARD):
    return [
        g
        for g in batch.cases
        if g.observed.rail is rail and g.observed.decline_code == "insufficient_funds"
    ]


def _scheduled_then_dispatched(entries, case):
    """Pair each SCHEDULED decision with the dispatch that followed it."""
    pending = None
    pairs = []
    for entry in entries:
        if entry.case_id != case.case_id:
            continue
        if entry.kind is LedgerKind.DECISION and entry.reason_code == "SCHEDULED":
            pending = entry
        elif entry.kind is LedgerKind.DISPATCH and pending is not None:
            pairs.append((pending, entry))
            pending = None
    return pairs


def test_SCD_1_a_scheduled_retry_dispatches_at_tick_plus_offset(tmp_path, batch):
    """OQ-30. Until A73 the offset was a label: the runner dispatched at once
    and used it only as a wake-up hint, so an estimator would have learned
    nothing from the grid's offset dimension."""
    candidates = _retryable(batch)
    assert candidates
    path = tmp_path / "scd1.jsonl"
    with Ledger(path) as ledger:
        for generated in candidates[:40]:
            drive(generated, _FixedRetryArm(72), ledger)

    entries = read_entries(path)
    checked = 0
    for generated in candidates[:40]:
        for scheduled, dispatched in _scheduled_then_dispatched(entries, generated.observed):
            chosen_at = _tick_of(generated.observed, scheduled.at)
            fired_at = _tick_of(generated.observed, dispatched.at)
            assert scheduled.payload["offset_hours"] == 72
            assert scheduled.payload["due_tick"] == chosen_at + 72
            assert fired_at == chosen_at + 72, (chosen_at, fired_at)
            checked += 1
    assert checked, "nothing was scheduled, so nothing was tested"


@pytest.mark.parametrize("offset", [o for o in hour_offsets() if o > 0])
def test_SCD_2_the_realised_gap_matches_every_declared_offset(tmp_path, batch, offset):
    candidates = _retryable(batch)[:25]
    path = tmp_path / f"scd2_{offset}.jsonl"
    with Ledger(path) as ledger:
        for generated in candidates:
            drive(generated, _FixedRetryArm(offset), ledger)

    entries = read_entries(path)
    gaps = []
    for generated in candidates:
        for scheduled, dispatched in _scheduled_then_dispatched(entries, generated.observed):
            gaps.append(
                _tick_of(generated.observed, dispatched.at)
                - _tick_of(generated.observed, scheduled.at)
            )
    assert gaps, f"offset {offset} never fired"
    assert set(gaps) == {offset}, (offset, collections.Counter(gaps))


def test_SCD_3_a_state_change_between_scheduling_and_firing_prevents_dispatch(tmp_path, batch):
    """The reason A73 re-gates on arrival.

    An opt-out in ENFORCE stops the case before the commitment can fire; a
    dispute in OBSERVE, where the compliance stops are relaxed, reaches the
    gate itself and G8 blocks the debit. Either way the commitment does not
    dispatch on a verdict taken days earlier.
    """
    generated = _retryable(batch)[0]
    case = generated.observed
    commitment = Scheduled(
        action=Retry(at_hour_offset=48, rail=case.rail), due_tick=48, scheduled_at=0
    )

    opted_out = CaseState(
        case_id=case.case_id, arm="PROBE", arm_mode=ArmMode.ENFORCE,
        tick=48, scheduled=commitment, opted_out=True,
    )
    path = tmp_path / "scd3_optout.jsonl"
    with Ledger(path) as ledger:
        final = drive(generated, _FixedRetryArm(48), ledger, initial_state=opted_out)
    assert final.stop_reason == "S4_OPT_OUT"
    assert not [e for e in read_entries(path) if e.kind is LedgerKind.DISPATCH]

    disputed = CaseState(
        case_id=case.case_id, arm="PROBE", arm_mode=ArmMode.OBSERVE,
        tick=48, scheduled=commitment, disputed=True,
    )
    path = tmp_path / "scd3_dispute.jsonl"
    with Ledger(path) as ledger:
        drive(generated, _FixedRetryArm(48, ArmMode.OBSERVE), ledger, initial_state=disputed)

    entries = read_entries(path)
    due = [e for e in entries if e.kind is LedgerKind.GATE_CHECK and e.payload.get("scheduled")]
    assert due, "the commitment never came due"
    assert "G8" in due[0].payload["blocked_by"]


def test_SCD_4_a_blocked_commitment_is_logged_and_control_returns_to_the_arm(tmp_path, batch):
    """Logged and cleared, never silently dropped."""
    generated = _retryable(batch)[0]
    case = generated.observed
    at_cap = CaseState(
        case_id=case.case_id, arm="PROBE", arm_mode=ArmMode.ENFORCE, tick=30,
        card_submissions_used=99,
        scheduled=Scheduled(
            action=Retry(at_hour_offset=30, rail=Rail.CARD), due_tick=30, scheduled_at=0
        ),
    )
    path = tmp_path / "scd4.jsonl"
    with Ledger(path) as ledger:
        final = drive(generated, _FixedRetryArm(30), ledger, initial_state=at_cap)

    entries = read_entries(path)
    blocked = [e for e in entries if e.reason_code == "SCHEDULE_BLOCKED"]
    assert blocked, "the block was not logged"
    assert "G4" in blocked[0].payload["blocked_by"]
    assert blocked[0].payload["due_tick"] == 30

    # Cleared, and the arm was asked again afterwards.
    assert final.scheduled is None
    after = [e for e in entries if e.seq > blocked[0].seq and e.kind is LedgerKind.GATE_CHECK]
    assert after, "control never returned to the arm"
    assert not after[0].payload.get("scheduled")


def test_SCD_5_a_second_commitment_replaces_the_first_and_is_logged(tmp_path, batch):
    """A queue of scheduled actions is a queue of decisions taken under
    circumstances that no longer hold."""
    generated = _retryable(batch)[0]
    case = generated.observed
    stale = CaseState(
        case_id=case.case_id, arm="PROBE", arm_mode=ArmMode.ENFORCE, tick=0,
        scheduled=Scheduled(
            action=Retry(at_hour_offset=168, rail=Rail.CARD), due_tick=600, scheduled_at=0
        ),
    )
    path = tmp_path / "scd5.jsonl"
    with Ledger(path) as ledger:
        drive(generated, _FixedRetryArm(18), ledger, initial_state=stale)

    replaced = [e for e in read_entries(path) if e.reason_code == "SCHEDULE_REPLACED"]
    assert replaced, "the replacement was not logged"
    assert replaced[0].payload["replaced_due_tick"] == 600
    assert replaced[0].payload["due_tick"] == 18
    assert replaced[0].payload["replaced"]["at_hour_offset"] == 168


@pytest.mark.parametrize("offset", [0, 6, 72, 168])
def test_SCD_6_scheduling_does_not_break_termination(tmp_path, batch, offset):
    path = tmp_path / f"scd6_{offset}.jsonl"
    with Ledger(path) as ledger:
        for generated in batch.cases[:200]:
            final = drive(generated, _FixedRetryArm(offset), ledger)
            assert final.status is CaseStatus.STOPPED
            assert final.tick <= DECISION_HORIZON_HOURS


def test_SCD_6_a_commitment_due_past_the_horizon_never_fires(tmp_path, batch):
    """S6 stops the case first. Letting the tick run past the horizon to reach
    a commitment would break the one bound the loop guarantees."""
    generated = _retryable(batch)[0]
    case = generated.observed
    late = CaseState(
        case_id=case.case_id, arm="PROBE", arm_mode=ArmMode.ENFORCE,
        tick=DECISION_HORIZON_HOURS - 10,
        scheduled=Scheduled(
            action=Retry(at_hour_offset=168, rail=Rail.CARD),
            due_tick=DECISION_HORIZON_HOURS + 158,
            scheduled_at=DECISION_HORIZON_HOURS - 10,
        ),
    )
    path = tmp_path / "scd6_late.jsonl"
    with Ledger(path) as ledger:
        final = drive(generated, _FixedRetryArm(168), ledger, initial_state=late)
    assert final.stop_reason == "S6_DECISION_HORIZON"
    assert final.tick <= DECISION_HORIZON_HOURS
    assert not [e for e in read_entries(path) if e.kind is LedgerKind.DISPATCH]


def test_SCD_7_the_ledger_is_byte_identical_with_scheduling_active(tmp_path):
    script = (
        "import sys;"
        "from settle.audit.chain import Ledger;"
        "from settle.execute.executor import WorldHandle;"
        "from settle.runner.arms.baselines import FixedLadderArm;"
        "from settle.runner.case_runner import run_case;"
        "from settle.sim.generator import generate_batch;"
        "from settle.sim.observability import ObservabilityConfig;"
        "from settle.sim.streams import Streams;"
        "b = generate_batch(120, 42); s = Streams(42); o = ObservabilityConfig();"
        "led = Ledger(sys.argv[1]);"
        "[run_case(g.observed, FixedLadderArm(), WorldHandle(truth=g.truth, streams=s), o, led)"
        " for g in b.cases];"
        "led.close();"
        "print(open(sys.argv[1]).read(), end='')"
    )
    outputs = []
    for hash_seed in ("0", "1", "random"):
        target = tmp_path / f"scd7_{hash_seed}.jsonl"
        outputs.append(
            subprocess.run(
                [sys.executable, "-c", script, str(target)],
                cwd=REPO_ROOT, capture_output=True, text=True, check=True,
                env={"PYTHONHASHSEED": hash_seed, "PATH": "/usr/bin:/bin"},
            ).stdout
        )
    assert outputs[0] == outputs[1] == outputs[2]
    assert "SCHEDULED" in outputs[0], "the fixture produced no scheduling to compare"
