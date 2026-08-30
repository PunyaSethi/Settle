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
        case, action, tick = rows[index]
        if not isinstance(action, Retry):
            continue
        probs = [
            lr.predict_proba(case, Retry(at_hour_offset=o, rail=action.rail), tick)
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
