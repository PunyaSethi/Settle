"""CP7 — the estimator. SPEC §10.1.

EST-8 and EST-10 are the two that can stop the project. EST-8 asks whether the
features carry signal at all; EST-10 asks whether the signal they carry is the
one we think it is, or the self-cure rate wearing a policy's clothes.
"""

from pathlib import Path

import numpy as np
import pytest

from settle.agent.calibration import (
    MIN_CELL_OBSERVATIONS,
    brier_score,
    coverage_table,
    expected_calibration_error,
    headline,
    reliability_table,
)
from settle.agent.estimator import (
    Estimator,
    build_matrix,
    constant_rate_baseline,
    fit_gbm,
    fit_logistic,
    split_by_case,
)
from settle.agent.train import cell_for, load_rows
from settle.schema.action import Retry

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT = REPO_ROOT / "out"
EXPLORE = OUT / "explore.decisions.jsonl"
LABELS = OUT / "labels.jsonl"
CASES = OUT / "explore.cases.jsonl"

pytestmark = pytest.mark.skipif(
    not (EXPLORE.exists() and LABELS.exists() and CASES.exists()),
    reason="training data absent; run the CP7 regeneration first",
)


@pytest.fixture(scope="module")
def trained():
    rows, y, case_ids = load_rows(EXPLORE, LABELS, CASES)
    train, calib, test = split_by_case(case_ids)
    X = build_matrix(rows)
    gbm = Estimator(fit_gbm(X[train.rows], y[train.rows], X[calib.rows], y[calib.rows]), "GBM")
    lr = Estimator(fit_logistic(X[train.rows], y[train.rows], X[calib.rows], y[calib.rows]), "LR")
    return rows, y, case_ids, (train, calib, test), X, gbm, lr


# --------------------------------------------------------------------------
# EST-3
# --------------------------------------------------------------------------

def test_EST_3_the_split_is_by_case_and_no_case_crosses_a_boundary(trained):
    """Rows from one case share hidden truth. Splitting by row puts siblings
    either side of the boundary, the model scores well by memorising the case,
    and every metric comes out optimistic with nothing warning you."""
    _, _, case_ids, (train, calib, test), _, _, _ = trained
    assert not (train.case_ids & calib.case_ids)
    assert not (calib.case_ids & test.case_ids)
    assert not (train.case_ids & test.case_ids)
    assert train.case_ids | calib.case_ids | test.case_ids == set(case_ids)

    ids = np.asarray(case_ids)
    for a, b in ((train, calib), (train, test), (calib, test)):
        assert not (set(ids[a.rows]) & set(ids[b.rows]))


def test_EST_3_the_assignment_is_stable_across_calls(trained):
    """A hash of the case id, not a shuffle: the same case lands in the same
    split in every process, so a model artifact is reproducible."""
    _, _, case_ids, splits, _, _, _ = trained
    again = split_by_case(case_ids)
    for first, second in zip(splits, again):
        assert first.case_ids == second.case_ids
        assert first.rows == second.rows


# --------------------------------------------------------------------------
# EST-4
# --------------------------------------------------------------------------

def test_EST_4_the_model_artifact_hash_is_stable(trained):
    rows, y, case_ids, (train, calib, _), X, _, _ = trained
    first = Estimator(fit_logistic(X[train.rows], y[train.rows], X[calib.rows], y[calib.rows]), "LR")
    second = Estimator(fit_logistic(X[train.rows], y[train.rows], X[calib.rows], y[calib.rows]), "LR")
    assert first.artifact_hash() == second.artifact_hash()


# --------------------------------------------------------------------------
# EST-5 / EST-6 / EST-7
# --------------------------------------------------------------------------

def test_EST_5_ece_and_brier_are_reported_for_both_models(trained, capsys):
    rows, y, _, (_, _, test), X, gbm, lr = trained
    cells = [cell_for(*rows[i]) for i in test.rows]
    stats = {}
    for model in (gbm, lr):
        p = model.predict_many(X[test.rows])
        stats[model.name] = headline(p.tolist(), y[test.rows].tolist(), cells)
    with capsys.disabled():
        print(f"\n  {'model':<6}{'ECE':>9}{'Brier':>9}")
        for name, s in stats.items():
            print(f"  {name:<6}{s['ece']:>9.4f}{s['brier']:>9.4f}")
    for s in stats.values():
        assert 0.0 <= s["ece"] < 0.25
        assert 0.0 < s["brier"] < 0.5


def test_EST_6_the_reliability_table_is_produced(trained):
    rows, y, _, (_, _, test), X, _, lr = trained
    table = reliability_table(lr.predict_many(X[test.rows]).tolist(), y[test.rows].tolist())
    assert len(table) == 10
    populated = [b for b in table if b["n"]]
    assert len(populated) >= 5, "predictions collapse into too few buckets to be a diagram"
    for bucket in populated:
        assert 0.0 <= bucket["predicted"] <= 1.0
        assert 0.0 <= bucket["actual"] <= 1.0
    assert sum(b["n"] for b in table) == len(test.rows)


def test_EST_7_thin_cells_are_named_and_excluded(trained):
    rows, y, _, (_, _, test), X, _, lr = trained
    cells = [cell_for(*rows[i]) for i in test.rows]
    p = lr.predict_many(X[test.rows]).tolist()
    stats = headline(p, y[test.rows].tolist(), cells)

    assert stats["extrapolated_cells"], "no cell was thin, so the threshold is untested"
    assert stats["n_covered"] < stats["n_all"], "nothing was excluded"
    for row in coverage_table(cells, p, y[test.rows].tolist()):
        assert row["extrapolated"] == (row["n"] < MIN_CELL_OBSERVATIONS)
    # The headline figures are computed over covered rows only.
    assert stats["ece"] != stats["ece_all"] or stats["n_covered"] == stats["n_all"]


# --------------------------------------------------------------------------
# EST-8 — is there any signal at all?
# --------------------------------------------------------------------------

def test_EST_8_both_models_beat_a_constant_rate_predictor(trained, capsys):
    """If they do not, the features carry no signal and the problem is upstream
    of the model. This test stops the project rather than shipping a number."""
    rows, y, _, (train, _, test), X, gbm, lr = trained
    yte = y[test.rows].tolist()
    constant = constant_rate_baseline(y[train.rows])
    floor = brier_score([constant] * len(yte), yte)

    results = {m.name: brier_score(m.predict_many(X[test.rows]).tolist(), yte) for m in (gbm, lr)}
    with capsys.disabled():
        print(f"\n  constant-rate Brier {floor:.4f} (base rate {constant:.4f})")
        for name, score in results.items():
            print(f"  {name:<4} Brier {score:.4f}   improvement {1 - score / floor:+.1%}")
    for name, score in results.items():
        assert score < floor, f"{name} does not beat predicting the base rate"


# --------------------------------------------------------------------------
# EST-9 — timing
# --------------------------------------------------------------------------

def test_EST_9_the_probability_varies_with_the_offset(trained, capsys):
    """If it does not, the model learned nothing about timing and the
    liquidity-window claim in §9 has no support from the estimator."""
    from settle.policy.params import hour_offsets

    rows, _, _, (_, _, test), _, _, lr = trained
    offsets = hour_offsets()
    spreads = []
    example = None
    for index in test.rows[:3000]:
        case, action, tick, last_attempt = rows[index]
        if not isinstance(action, Retry):
            continue
        probs = [
            lr.predict_proba(case, Retry(at_hour_offset=o, rail=action.rail), tick, last_attempt)
            for o in offsets
        ]
        spreads.append(max(probs) - min(probs))
        if example is None:
            example = (case.case_id, tick, probs)

    assert spreads, "no retry rows in the test split"
    spreads.sort()
    median = spreads[len(spreads) // 2]
    with capsys.disabled():
        case_id, tick, probs = example
        print(f"\n  {case_id} tick {tick}")
        print("    " + "  ".join(f"{o:>5}h" for o in offsets))
        print("    " + "  ".join(f"{p:>6.3f}" for p in probs))
        print(f"  spread over {len(spreads):,} rows: median {median:.4f} "
              f"p90 {spreads[int(len(spreads) * 0.9)]:.4f} max {spreads[-1]:.4f}")
    assert median > 0.0, "the probability is identical at every offset — timing is dead"


# --------------------------------------------------------------------------
# EST-10 — the natural-recovery confound
# --------------------------------------------------------------------------

def test_EST_10_the_self_cure_confound_is_measured(trained, capsys):
    """A case that self-cures settles whatever the arm did, so its rows teach
    the model that every action "worked". Measured, reported, and not silently
    dropped."""
    from settle.sim.generator import generate_batch
    from settle.sim.streams import Streams
    from settle.sim.world import natural_recovery_at

    rows, y, case_ids, (_, _, test), X, _, lr = trained
    batch = generate_batch(30_000, 90_000)
    streams = Streams(90_000)
    self_cured = {
        g.observed.case_id
        for g in batch.cases
        if natural_recovery_at(g.observed, g.truth, streams) is not None
    }

    ids = np.asarray(case_ids)
    cured_mask = np.asarray([cid in self_cured for cid in ids])
    overall = y.mean()
    without = y[~cured_mask].mean()
    within = y[cured_mask].mean()

    te = np.asarray(test.rows)
    p = lr.predict_many(X[te])
    te_cured = cured_mask[te]

    with capsys.disabled():
        print(f"\n  self-cured cases        {len(self_cured):,}/30,000 = {len(self_cured)/30_000:.1%}")
        print(f"  rows from self-cured    {cured_mask.sum():,}/{len(y):,} = {cured_mask.mean():.1%}")
        print(f"  base rate overall       {overall:.4f}")
        print(f"  base rate self-cured    {within:.4f}")
        print(f"  base rate NOT self-cured{without:>9.4f}")
        print(f"  test Brier, self-cured      {brier_score(p[te_cured].tolist(), y[te][te_cured].tolist()):.4f}")
        print(f"  test Brier, not self-cured  {brier_score(p[~te_cured].tolist(), y[te][~te_cured].tolist()):.4f}")

    assert within > without, "self-cured rows do not label True more often — check the join"
    assert cured_mask.mean() > 0.0


def test_EST_10_the_labels_come_from_reconciliation_not_reporting():
    """A label from `ReportedOutcome` teaches the model to predict what the
    webhook said, which is the one thing §6 says cannot be trusted."""
    import ast

    source = (REPO_ROOT / "settle" / "recon" / "reconcile.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    labels_fn = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "labels"
    )
    # Docstring stripped: a comment explaining the rule is not a breach of it.
    body = ast.unparse(ast.Module(body=labels_fn.body[1:], type_ignores=[]))
    assert "actually_settled" in body
    assert "ReportedOutcome" not in body
    assert "arrival_count" not in body
    assert "status" not in body


# --------------------------------------------------------------------------
# EXP-7 — oversampling must not lie about the propensity
# --------------------------------------------------------------------------

class DoNothingProbe:
    """Stands in for `do_nothing` when priming the coverage counter."""

    from settle.schema.enums import ActionType as _AT

    type = _AT.DO_NOTHING


def test_EXP_7_the_logged_propensity_is_the_actual_sampling_probability():
    """The contract oversampling could break. If the draw is weighted while the
    log still claims `1/len(passing)`, every IPS estimate is wrong — and wrong
    in the direction of whichever cell we chose to boost.
    """
    from settle.runner.arms.explore import ExploreArm, gate_passing_pairs
    from settle.schema.enums import ArmMode
    from settle.schema.state import CaseState
    from settle.sim.generator import generate_batch

    arm = ExploreArm(90_000, oversample=True)
    batch = generate_batch(400, 90_000)

    # Prime the counter so some cells are already at target and others are not.
    # With an empty counter every cell is under target, every weight is equal,
    # and the draw is uniform — the weighting only bites once cells fill, which
    # is worth exercising rather than assuming.
    from settle.runner.arms.explore import coverage_cell

    seeded_case = batch.cases[0].observed
    arm.cell_counts[coverage_cell(seeded_case, DoNothingProbe(), 0)] = 10_000

    checked = weighted = 0
    for generated in batch.cases:
        case = generated.observed
        for tick in (0, 5, 9, 14, 26, 40, 74):
            state = CaseState(case_id=case.case_id, arm="EXPLORE", arm_mode=ArmMode.ENFORCE, tick=tick)
            passing = gate_passing_pairs(case, state)
            if not passing:
                continue
            weights = arm._weights(case, state, passing)
            before = len(arm.decisions)
            chosen = arm.choose(case, state, [])
            logged = arm.decisions[before].propensity

            expected = weights[passing.index(chosen)] / sum(weights)
            assert logged == pytest.approx(expected), (case.case_id, tick, logged, expected)
            if len(set(weights)) > 1:
                weighted += 1
                assert logged != pytest.approx(1.0 / len(passing)), (
                    "a weighted draw still logged a uniform propensity"
                )
            checked += 1
    assert checked > 500
    assert weighted > 0, "no draw was ever actually weighted, so the test proves nothing"


def test_EXP_7_propensity_matches_the_realised_frequency_per_cell():
    """The statistical version: over many draws with the same weights, the
    fraction of times an action is chosen matches the propensity it logged."""
    import collections

    from settle.runner.arms.explore import ExploreArm, gate_passing_pairs
    from settle.schema.enums import ArmMode
    from settle.schema.state import CaseState
    from settle.sim.generator import generate_batch

    arm = ExploreArm(90_000, oversample=False)  # uniform, so the target is exact
    batch = generate_batch(3_000, 90_000)
    chosen_counts: collections.Counter = collections.Counter()
    logged: dict[int, float] = {}
    trials = 0
    for generated in batch.cases:
        case = generated.observed
        for tick in (9, 11, 13):
            state = CaseState(
                case_id=case.case_id, arm="EXPLORE", arm_mode=ArmMode.ENFORCE, tick=tick
            )
            passing = gate_passing_pairs(case, state)
            if len(passing) != 5:
                continue
            before = len(arm.decisions)
            chosen = arm.choose(case, state, [])
            index = passing.index(chosen)
            chosen_counts[index] += 1
            logged[index] = arm.decisions[before].propensity
            trials += 1

    assert trials > 200, f"only {trials} five-option decisions to test against"
    for index, count in chosen_counts.items():
        realised = count / trials
        assert abs(realised - logged[index]) < 0.06, (index, realised, logged[index])


def test_EXP_7_oversampling_actually_shifts_the_distribution():
    """If it did not, the propensity question would be moot and so would Part B."""
    from settle.runner.arms.explore import ExploreArm

    plain = ExploreArm(90_000, oversample=False)
    boosted = ExploreArm(90_000, oversample=True)
    assert plain.oversample is False and boosted.oversample is True
    assert boosted.target > 0


# --------------------------------------------------------------------------
# EST-12 — the row served is the row trained. SPEC §10.1.
# --------------------------------------------------------------------------
#
# Until CP9.1 `settle/agent/policy.py` called `predict_pairs` without
# `last_attempt_tick`, so the estimator saw `None` on every serve-time call
# while `train.py` reconstructed the real value from the decision stream.
# `days_since_last_attempt` and `has_prior_attempt` therefore meant different
# things in training and in use, and `has_prior_attempt` ranked 4th of 45 by
# permutation importance — the model was being asked its questions in a
# different shape than it learned them.

def test_EST_12_the_serve_row_is_byte_identical_to_the_train_row():
    """Same inputs, same row. `build_matrix` is what `train.py` feeds the
    model; `feature_vector` is what the estimator builds at serve time. If those
    two ever diverge, every calibration number is measured on one shape and
    used on another."""
    import numpy as np

    from settle.agent.estimator import build_matrix
    from settle.agent.features import feature_vector
    from settle.policy.grid import candidate_pairs
    from settle.schema.action import DoNothing
    from settle.schema.enums import ArmMode
    from settle.schema.state import CaseState
    from settle.sim.generator import generate_batch

    batch = generate_batch(40, 42)
    rows = []
    for generated in batch.cases:
        case = generated.observed
        for tick, last in ((0, None), (24, 0), (96, 24), (400, 96)):
            state = CaseState(
                case_id=case.case_id, arm="OURS", arm_mode=ArmMode.ENFORCE,
                tick=tick, last_attempt_tick=last,
            )
            for action in [DoNothing(), *candidate_pairs(case, state)]:
                rows.append((case, action, tick, last))

    trained = build_matrix(rows)
    served = np.asarray([feature_vector(*row) for row in rows])
    assert trained.shape == served.shape
    assert np.array_equal(trained, served), "the train and serve rows differ"


def test_EST_12_the_policy_passes_the_last_attempt_tick():
    """The bug itself. A policy that omits the argument gets `None` for every
    case forever, and the two features built from it are dead at serve time
    while alive in training."""
    import ast

    tree = ast.parse((REPO_ROOT / "settle" / "agent" / "policy.py").read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "predict_pairs"
    ]
    assert calls, "the policy no longer calls predict_pairs"
    for call in calls:
        passed = len(call.args) + len(call.keywords)
        assert passed >= 4, (
            f"predict_pairs called with {passed} arguments at line {call.lineno}; "
            "last_attempt_tick is not being passed and the model sees None"
        )


def test_EST_12_the_runner_records_the_dispatch_tick_not_the_choice_tick():
    """`retry(at_hour_offset=72)` is a commitment that fires three days later
    (A73). The bank sees it when it is submitted, so state records the firing
    tick — and `train.py` must add the offset to match, or the two sides of
    EST-12 measure different events."""
    import inspect

    from settle.agent import train
    from settle.runner import case_runner

    recorded = inspect.getsource(case_runner._apply_dispatch)
    assert 'update["last_attempt_tick"] = state.tick' in recorded

    reconstructed = inspect.getsource(train.load_rows)
    assert "last_debit[decision.case_id] = tick + action_offset(decision.action)" in reconstructed


def test_EST_12_a_dispatched_debit_leaves_a_last_attempt_tick():
    """End to end: after a retry fires, the state the policy reads carries the
    tick, so the serve-time row stops being the `None` row."""
    import tempfile

    from settle.audit.chain import Ledger
    from settle.execute.executor import WorldHandle
    from settle.runner.arm import FirstLegalArm
    from settle.runner.case_runner import run_case
    from settle.sim.generator import generate_batch
    from settle.sim.observability import ObservabilityConfig
    from settle.sim.streams import Streams

    batch = generate_batch(300, 42)
    streams, config = Streams(42), ObservabilityConfig()
    path = Path(tempfile.mkdtemp()) / "a.jsonl"
    finals = []
    with Ledger(path) as ledger:
        for generated in batch.cases:
            finals.append(
                run_case(
                    generated.observed,
                    FirstLegalArm(),
                    WorldHandle(truth=generated.truth, streams=streams),
                    config,
                    ledger,
                )
            )

    debited = [f for f in finals if f.attempts_used or f.rail_switches_used]
    assert debited, "no case debited, so there is nothing to record"
    assert all(f.last_attempt_tick is not None for f in debited), (
        "a case dispatched a debit and recorded no attempt tick"
    )
    assert all(f.last_attempt_tick is None for f in finals if f not in debited)


# --------------------------------------------------------------------------
# EST-13 — resolution, the thing uplift ECE cannot see. SPEC §10.1 (A92).
# --------------------------------------------------------------------------
#
# CP10's finding. A84 selects on the calibration of the uplift, and uplift ECE
# bins by predicted uplift before comparing against a matched control rate — so
# a model that returns the same number for every candidate still gets binned and
# still scores well. Isotonic calibration took the median within-decision uplift
# spread from 6.2 points to 1.7 and made 21.0% of multi-option decisions
# perfectly flat, while moving uplift ECE by 0.0017. The policy cannot rank what
# the scorer will not separate, and the metric did not notice.

def _probe_decisions(n=120):
    """Real candidate grids, the way the policy will meet them."""
    from settle.policy.grid import candidate_pairs
    from settle.schema.enums import ArmMode
    from settle.schema.state import CaseState
    from settle.sim.generator import generate_batch

    out = []
    for generated in generate_batch(400, 42).cases:
        case = generated.observed
        for tick, last in ((0, None), (24, 0), (120, 24)):
            state = CaseState(
                case_id=case.case_id, arm="OURS", arm_mode=ArmMode.ENFORCE,
                tick=tick, last_attempt_tick=last,
            )
            pairs = candidate_pairs(case, state)
            if len(pairs) > 1:
                out.append((case, tick, last, pairs))
        if len(out) >= n:
            break
    return out[:n]


class _Flat:
    """Returns one probability for everything. The degenerate case."""

    def predict_pairs(self, case, actions, tick, last_attempt_tick=None):
        import numpy as np

        return np.full(len(actions), 0.5)


def test_EST_13_a_flat_scorer_is_caught():
    """The guard has to fire on the thing it exists to catch, or it is
    decoration. A model that scores every option identically has resolved
    nothing, whatever its ECE says."""
    from settle.agent.estimator import has_usable_resolution, uplift_resolution

    resolution = uplift_resolution(_Flat(), _probe_decisions())
    assert resolution["flat_rate"] == 1.0
    assert resolution["median"] == 0.0
    assert not has_usable_resolution(resolution)


def test_EST_13_isotonic_costs_resolution_and_the_metric_does_not_notice():
    """The measurement CP10 turns on, run in miniature.

    Isotonic is monotone, so it can only tie candidates, never reorder them.
    Ties are the damage: a step function with a few dozen levels cannot express
    a difference smaller than one step, and a retry clears S7 at roughly 0.03%
    uplift.
    """
    import numpy as np

    from settle.agent.estimator import (
        Estimator, calibrate, fit_gbm, uplift_resolution,
    )

    rows, y, case_ids = load_rows(EXPLORE, LABELS, CASES)
    X = build_matrix(rows)
    train, calib, _ = split_by_case(case_ids)
    base = fit_gbm(X[train.rows], y[train.rows], X[calib.rows], y[calib.rows], calibrated=False)
    isotonic = calibrate(base, X[calib.rows], y[calib.rows])

    probe = _probe_decisions()
    raw_res = uplift_resolution(Estimator(base, "raw"), probe)
    iso_res = uplift_resolution(Estimator(isotonic, "iso"), probe)

    assert iso_res["flat_rate"] > raw_res["flat_rate"], (
        "isotonic did not flatten anything here; the CP10 finding no longer reproduces"
    )
    assert iso_res["median"] < raw_res["median"]
    # Monotone: it may tie candidates but must never invert them.
    for case, tick, last, actions in probe[:40]:
        from settle.schema.action import DoNothing

        r = Estimator(base, "raw").predict_pairs(case, [DoNothing(), *actions], tick, last)
        c = Estimator(isotonic, "iso").predict_pairs(case, [DoNothing(), *actions], tick, last)
        order = np.argsort(r)
        assert np.all(np.diff(c[order]) >= -1e-12), (
            "isotonic inverted the ordering, which a monotone map cannot do"
        )


def test_EST_13_the_shipped_model_clears_the_floor():
    """Whatever ships must be rankable. This is the guard as a gate, not as a
    diagnostic."""
    import pickle

    from settle.agent.estimator import (
        Estimator, has_usable_resolution, latest_model_path, uplift_resolution,
    )

    path = latest_model_path(REPO_ROOT / "out")
    if path is None:
        pytest.skip("no trained model")
    payload = pickle.loads(path.read_bytes())
    estimator = Estimator(payload["models"][payload["winner"]], payload["winner"])
    resolution = uplift_resolution(estimator, _probe_decisions())
    assert has_usable_resolution(resolution), (
        f"the shipped model is flat on {resolution['flat_rate']:.1%} of decisions"
    )


def test_EST_13_selection_records_why_a_model_was_rejected():
    """The decision has to be visible in the artifact, not just in a log line
    somebody may or may not have read."""
    import pickle

    from settle.agent.estimator import latest_model_path

    path = latest_model_path(REPO_ROOT / "out")
    if path is None:
        pytest.skip("no trained model")
    payload = pickle.loads(path.read_bytes())
    selection = payload.get("selection", {})
    assert "resolution" in selection, "the artifact does not record resolution"
    assert "rejected_on_resolution" in selection
    for name, resolution in selection["resolution"].items():
        assert {"median", "p90", "flat_rate", "n"} <= set(resolution), name
