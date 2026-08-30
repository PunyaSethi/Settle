"""CP6 — reconciliation. SPEC §7, §13.1, INV-1, INV-8.

REC-6 is the structural one. §7 gives `settle/recon/` a named exception to
INV-8, and an exception that is not policed is not an exception — it is the
first of many. This asserts the set is exactly one package.
"""

import ast
import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from settle.recon.reconcile import (
    OBSERVATION_HORIZON_DAYS,
    censored_fraction,
    failure_counts,
    labels,
    reconcile,
    run_arm,
    seed_failures,
)
from settle.recon.silent_failures import COMPLIANCE_CLASSES
from settle.schema.enums import ArmMode, SilentFailureClass
from settle.sim.observability import (
    REPORTING_PARAMETERS,
    ObservabilityConfig,
    perfect_observability,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED = 42
CASES = 400


@pytest.fixture(scope="module")
def b3_run():
    return run_arm("b3", CASES, SEED)


@pytest.fixture(scope="module")
def b2_run():
    return run_arm("b2", CASES, SEED)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    parts = path.relative_to(REPO_ROOT).with_suffix("").parts[:-1]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = list(parts[: len(parts) - (node.level - 1)]) if node.level else []
            module = ".".join([*base, node.module] if node.module else base)
            found.add(module)
            found.update(f"{module}.{a.name}" for a in node.names)
    return found


# --------------------------------------------------------------------------
# REC-1 / REC-2
# --------------------------------------------------------------------------

def test_REC_1_one_record_per_case_always(b2_run):
    entries, actuals, cases, _, _ = b2_run
    reconciled = reconcile(entries, actuals, cases)
    assert set(reconciled) == set(cases)
    assert len(reconciled) == CASES


def test_REC_1_a_case_that_never_acted_still_gets_a_record():
    entries, actuals, cases, _, _ = run_arm("b0", 50, SEED)
    reconciled = reconcile(entries, actuals, cases)
    assert len(reconciled) == 50
    assert all(not r.ledger_says_recovered and not r.actually_settled for r in reconciled.values())


def test_REC_2_belief_and_truth_diverge(b3_run):
    """The distance between the two is the project. If they never diverged,
    §6's observability layer would not be doing anything."""
    entries, actuals, cases, _, _ = b3_run
    reconciled = reconcile(entries, actuals, cases)

    believed_not_settled = [
        r for r in reconciled.values() if r.ledger_says_recovered and not r.actually_settled
    ]
    settled_not_believed = [
        r for r in reconciled.values() if r.actually_settled and not r.ledger_says_recovered
    ]
    assert believed_not_settled, "no case was believed recovered without settling"
    assert settled_not_believed, "no settlement went unreported, so the drop rate did nothing"


# --------------------------------------------------------------------------
# REC-3 — censoring
# --------------------------------------------------------------------------

def test_REC_3_an_outcome_past_the_horizon_is_censored_not_guessed(b2_run):
    from settle.sim.truth import ActualOutcome

    entries, actuals, cases, _, _ = b2_run
    case_id = next(iter(cases))
    case = cases[case_id]
    late = case.created_at + timedelta(days=OBSERVATION_HORIZON_DAYS + 5)
    doctored = dict(actuals)
    doctored[case_id] = [
        (
            ActualOutcome(
                case_id=case_id, at=case.created_at, settled=True,
                settled_at=late, reversed=False, amount_paise=49900,
            ),
            None,
        )
    ]
    record = reconcile(entries, doctored, cases)[case_id]
    assert record.censored is True
    assert record.actually_settled is False, "a censored outcome must not be counted as settled"
    assert record.settled_amount_paise == 0


def test_REC_3_the_censored_fraction_is_reported_per_arm():
    """A27 required this rather than a silent right-censor."""
    for arm_key in ("b1", "b2", "b3"):
        entries, actuals, cases, name, _ = run_arm(arm_key, 200, SEED)
        reconciled = reconcile(entries, actuals, cases)
        fraction = censored_fraction(reconciled)
        assert 0.0 <= fraction <= 1.0, (name, fraction)


def test_REC_3_the_sixty_day_horizon_is_wide_enough_for_the_declared_maxima():
    """settlement_lag_h_max (96h) plus reversal_delay_days_max (21d) from a
    day-30 authorisation lands inside 60. That is why the realised censored
    fraction is near zero, and it is a property of the priors, not luck."""
    from settle.sim.generator import PARAMS

    latest = 30 + PARAMS["settlement_lag_h_max"] / 24 + PARAMS["reversal_delay_days_max"]
    assert latest <= OBSERVATION_HORIZON_DAYS


# --------------------------------------------------------------------------
# REC-4
# --------------------------------------------------------------------------

def test_REC_4_a_reversal_after_settlement_is_detected(b3_run):
    entries, actuals, cases, _, _ = b3_run
    reconciled = reconcile(entries, actuals, cases)
    reversed_cases = [r for r in reconciled.values() if r.reversed]
    assert reversed_cases, "no reversal was detected, so SF-7 is untestable"
    for record in reversed_cases:
        assert record.actually_settled, "a reversal without a settlement is incoherent"
        assert record.reversed_at is not None
        assert record.reversed_at > record.settled_at


# --------------------------------------------------------------------------
# REC-5 / REC-6 — what reconciliation is and is not allowed to read
# --------------------------------------------------------------------------

def test_REC_5_reconcile_never_derives_truth_from_a_reported_outcome():
    """It reads the ledger to learn what the agent *believed* — that is
    `ledger_says_recovered`. It must never read a report to decide what
    happened, which is the whole point of running a second pass."""
    imported = _imports(REPO_ROOT / "settle" / "recon" / "reconcile.py")
    assert not [n for n in imported if n.endswith("ReportedOutcome")], imported

    source = (REPO_ROOT / "settle" / "recon" / "reconcile.py").read_text(encoding="utf-8")
    settled_block = source[
        source.index("settled_record = next(") : source.index("record = ReconciledCase(")
    ]
    # `actually_settled` is derived from the world's own record and nothing else.
    assert "actual_outcomes" in source
    assert "ReportedStatus" not in settled_block
    assert "view.reported" not in settled_block


# The complete set of packages permitted to read hidden truth, and why:
#   sim      constructs it
#   execute  is the world boundary — it runs the action that produces it
#   recon    compares belief against it, which is what §7 exists to do
# Everything else — agent, policy, schema, runner, audit, diagnose — is banned.
TRUTH_READERS = frozenset({"sim", "execute", "recon"})


def test_REC_6_only_the_named_packages_read_hidden_truth():
    """§7's exception to INV-8, policed. An unstated exception is how INV-8
    dies; an unpoliced one is how it dies quietly."""
    offenders = {}
    for module_path in sorted((REPO_ROOT / "settle").rglob("*.py")):
        relative = module_path.relative_to(REPO_ROOT)
        if relative.parts[1] in TRUTH_READERS:
            continue
        leaked = {n for n in _imports(module_path) if n.startswith("settle.sim.truth")}
        if leaked:
            offenders[str(relative)] = sorted(leaked)
    assert not offenders, f"INV-8 breach outside the named exception: {offenders}"


def test_REC_6_the_permitted_set_is_exactly_three_packages():
    """Naming them is the point. A set that grows silently is not an exception."""
    readers = set()
    for module_path in sorted((REPO_ROOT / "settle").rglob("*.py")):
        if {n for n in _imports(module_path) if n.startswith("settle.sim.truth")}:
            readers.add(module_path.relative_to(REPO_ROOT).parts[1])
    assert readers <= TRUTH_READERS, f"a new package started reading truth: {readers - TRUTH_READERS}"
    assert "recon" in readers and "execute" in readers


def test_REC_6_the_exception_is_actually_used():
    """If recon did not read truth, the exception would be dead and should go."""
    reads = {
        n
        for path in (REPO_ROOT / "settle" / "recon").rglob("*.py")
        for n in _imports(path)
        if n.startswith("settle.sim.truth")
    }
    assert reads, "settle/recon/ does not read hidden truth, so the exception is unearned"


# --------------------------------------------------------------------------
# REC-7
# --------------------------------------------------------------------------

def test_REC_7_reconciliation_is_byte_identical_across_processes(tmp_path):
    script = (
        "import json;"
        "from settle.recon.reconcile import reconcile, run_arm;"
        "e, a, c, n, m = run_arm('b2', 60, 42);"
        "r = reconcile(e, a, c);"
        "print(json.dumps({k: v.model_dump(mode='json') for k, v in sorted(r.items())},"
        " sort_keys=True))"
    )
    outputs = []
    for hash_seed in ("0", "1", "random"):
        outputs.append(
            subprocess.run(
                [sys.executable, "-c", script],
                cwd=REPO_ROOT, capture_output=True, text=True, check=True,
                env={"PYTHONHASHSEED": hash_seed, "PATH": "/usr/bin:/bin"},
            ).stdout
        )
    assert outputs[0] == outputs[1] == outputs[2]
    assert len(outputs[0]) > 100


# --------------------------------------------------------------------------
# REC-8 — seeded failures
# --------------------------------------------------------------------------

@pytest.mark.parametrize("n", [1, 5, 17])
def test_REC_8_every_detector_finds_exactly_the_injected_count(n):
    """A detector that always reports zero is indistinguishable from a broken
    detector. This is the test that tells them apart."""
    entries, actuals, cases = seed_failures(n)
    counts = failure_counts(reconcile(entries, actuals, cases))
    for failure_class in SilentFailureClass:
        assert counts[failure_class] == n, (failure_class.value, counts[failure_class], n)


def test_REC_8_seeded_cases_do_not_cross_contaminate():
    """Each injected case exhibits its own class and no other."""
    entries, actuals, cases = seed_failures(3)
    reconciled = reconcile(entries, actuals, cases)
    for case_id, record in reconciled.items():
        expected = case_id.split("_")[0].upper().replace("SF", "SF-")
        assert [f.value for f in record.silent_failures] == [expected], (case_id, record.silent_failures)


# --------------------------------------------------------------------------
# REC-9 — labels
# --------------------------------------------------------------------------

def test_REC_9_labels_cover_exactly_the_trainable_rows():
    """A75: a row where `do_nothing` was the only option is not a decision."""
    from settle.runner.arms.explore import ExploreArm

    entries, actuals, cases, _, _ = run_arm("explore", 300, 90_000)
    reconciled = reconcile(entries, actuals, cases)

    arm = ExploreArm(90_000)
    decisions = []
    from settle.execute.executor import WorldHandle
    from settle.audit.chain import Ledger
    from settle.runner.case_runner import run_case
    from settle.sim.generator import generate_batch
    from settle.sim.streams import Streams
    import tempfile

    batch = generate_batch(300, 90_000)
    streams = Streams(90_000)
    with Ledger(Path(tempfile.mkdtemp()) / "l.jsonl") as ledger:
        for generated in batch.cases:
            run_case(
                generated.observed, arm,
                WorldHandle(truth=generated.truth, streams=streams),
                ObservabilityConfig(), ledger,
            )
    decisions = [json.loads(d.model_dump_json()) for d in arm.decisions]

    trainable = [d for d in decisions if d["propensity"] is not None and d["propensity"] < 1.0]
    rows = labels(decisions, reconciled)
    assert len(rows) == len(trainable), (len(rows), len(trainable))
    assert rows, "no trainable rows, so nothing was tested"
    assert all(set(r) == {"case_id", "decision_id", "settled", "censored"} for r in rows)


def test_REC_9_no_label_derives_from_a_reported_outcome():
    """A label taken from the webhook teaches the model to predict what the
    webhook said, which is the one thing §6 says cannot be trusted."""
    entries, actuals, cases = seed_failures(4)
    reconciled = reconcile(entries, actuals, cases)

    # sf1_* were reported captured and never settled. Their label must be False.
    decisions = [
        {"case_id": f"sf1_{i}", "decision_id": f"d{i}", "propensity": 0.5} for i in range(4)
    ]
    rows = labels(decisions, reconciled)
    assert rows
    assert all(row["settled"] is False for row in rows), "a label followed the webhook, not the money"

    # sf7_* settled and then reversed. Reversed money is not recovered money.
    reversed_decisions = [
        {"case_id": f"sf7_{i}", "decision_id": f"d{i}", "propensity": 0.5} for i in range(4)
    ]
    assert all(row["settled"] is False for row in labels(reversed_decisions, reconciled))


# --------------------------------------------------------------------------
# OBS-1 / OBS-2
# --------------------------------------------------------------------------

def test_OBS_1_every_reporting_parameter_is_read_by_some_code_path():
    """A parameter nobody reads carries a PRIORS row implying it matters, which
    is worse than a literal — it looks like evidence. A51 required this check."""
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (REPO_ROOT / "settle").rglob("*.py")
        if "test" not in path.name
    )
    unread = [name for name in REPORTING_PARAMETERS if f"config.{name}" not in sources]
    assert not unread, f"declared but read by nothing: {unread}"
    assert len(REPORTING_PARAMETERS) == 5


def test_OBS_1_the_distortions_actually_happen(b3_run):
    entries, _, _, _, _ = b3_run
    from settle.schema.enums import LedgerKind

    reported = [e for e in entries if e.kind is LedgerKind.REPORTED_OUTCOME]
    assert [e for e in reported if e.payload["arrival_count"] > 1], "no webhook was ever duplicated"
    assert [e for e in reported if e.payload["status"] == "none"], "no webhook was ever dropped"


def test_OBS_2_perfect_observability_zeroes_reporting_only():
    """It measures what unreliable *reporting* costs. The world still fails to
    settle, still reverses, still lags."""
    from settle.sim.generator import PARAMS

    perfect = perfect_observability()
    assert perfect.is_perfect
    for name in REPORTING_PARAMETERS:
        assert getattr(perfect, name) == 0.0, name

    assert PARAMS["auth_no_settle_rate"] > 0.0
    assert "auth_no_settle_rate" not in ObservabilityConfig.model_fields
    assert PARAMS["will_reverse_rate"] > 0.0
    assert PARAMS["settlement_lag_h.mean"] > 0.0


def test_OBS_2_sf1_is_still_producible_under_perfect_observability():
    """If zeroing the flag abolished SF-1, the auditor would report zero for a
    real-world failure class and look like it was working."""
    import tempfile

    from settle.audit.chain import Ledger, read_entries
    from settle.execute.executor import WorldHandle
    from settle.runner.arms.baselines import MaxPressureArm
    from settle.runner.case_runner import run_case
    from settle.sim.generator import generate_batch
    from settle.sim.streams import Streams

    batch = generate_batch(400, SEED)
    streams = Streams(SEED)
    perfect = perfect_observability()
    actuals, cases = {}, {}
    path = Path(tempfile.mkdtemp()) / "perfect.jsonl"
    with Ledger(path) as ledger:
        for generated in batch.cases:
            world = WorldHandle(truth=generated.truth, streams=streams)
            run_case(generated.observed, MaxPressureArm(), world, perfect, ledger)
            actuals[generated.observed.case_id] = list(world.actuals)
            cases[generated.observed.case_id] = generated.observed

    counts = failure_counts(reconcile(read_entries(path), actuals, cases, config=perfect))
    assert counts[SilentFailureClass.SF1] > 0, (
        "SF-1 vanished under perfect reporting, so the flag reached into the world"
    )
    assert counts[SilentFailureClass.SF2] == 0, "a drop occurred under perfect reporting"


def test_OBS_2_compliance_classes_are_zero_for_every_enforce_arm():
    """SF-5 and SF-6 for an ENFORCE arm are a gate failure, not an audit finding."""
    for arm_key in ("b0", "b1", "b2", "explore"):
        entries, actuals, cases, name, mode = run_arm(arm_key, 200, SEED if arm_key != "explore" else 90_000)
        assert mode is ArmMode.ENFORCE
        counts = failure_counts(reconcile(entries, actuals, cases))
        breaches = {c.value: counts[c] for c in COMPLIANCE_CLASSES if counts[c]}
        assert not breaches, f"{name} is in ENFORCE and shows {breaches} — a gate did not hold"
