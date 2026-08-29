"""CP4 — the case runner. SPEC §12, §13, §14.1.

RUN-9 is the structural one. The runner must be correct while seeing only what
it is told, so it is not permitted to see anything else: `settle/runner/` imports
nothing from `settle.sim.truth`, and `case_runner.py` imports nothing from
`settle.sim` at all. The `WorldHandle` passes through it unopened.
"""

import ast
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


def test_RUN_2_a_scheduled_retry_advances_to_its_own_offset(batch):
    generated = batch.cases[3]
    state = CaseState(case_id=generated.observed.case_id, arm="B0", arm_mode=ArmMode.ENFORCE)
    scheduled = Retry(at_hour_offset=6, rail=Rail.CARD)
    assert next_interesting_tick(generated.observed, state, scheduled, ()) == 6
    assert next_interesting_tick(generated.observed, state, DoNothing(), ()) == DECISION_CADENCE_HOURS


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
# RUN-8
# --------------------------------------------------------------------------

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
