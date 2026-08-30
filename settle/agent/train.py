"""Train the estimator. SPEC §10.1.

    python -m settle.agent.train --explore out/explore.decisions.jsonl \
        --labels out/labels.jsonl --out out/model.pkl

Training rows come from the EXPLORE arm only, and labels come from
reconciliation against `ActualOutcome`. A label taken from `ReportedOutcome`
would teach the model to predict what the webhook said, which is the one thing
§6 says cannot be trusted.
"""

from __future__ import annotations

import argparse
import json
import pickle
from pathlib import Path
from typing import Final

import numpy as np

from settle.agent.calibration import headline, reliability_table
from settle.agent.estimator import (
    Estimator,
    build_matrix,
    constant_rate_baseline,
    fit_gbm,
    fit_logistic,
    split_by_case,
)
from settle.agent.features import IST, action_offset, dispatch_moment
from settle.diagnose.taxonomy import classify
from settle.schema.action import Action
from settle.schema.decision import Decision
from settle.schema.observed import ObservedCase


def load_rows(explore_path: Path, labels_path: Path, cases_path: Path):
    """Join EXPLORE's decisions to reconciliation's labels, by decision id.

    Cases are read from a file of `ObservedCase` rows, not rebuilt from a seed
    through the generator. Regenerating them would put `settle.sim` inside the
    agent package — an INV-8 breach that EST-1 catches — and it would also be a
    lie about the input: a merchant has a file of cases, not a simulator.
    """
    labels = {
        row["decision_id"]: row
        for row in (json.loads(line) for line in labels_path.read_text().splitlines() if line.strip())
    }
    cases: dict[str, ObservedCase] = {}
    for line in cases_path.read_text().splitlines():
        if line.strip():
            case = ObservedCase.model_validate_json(line)
            cases[case.case_id] = case

    # The tick of the previous debit on each case, reconstructed from the
    # decision stream in order. Not hidden truth: a merchant knows when it last
    # tried to charge someone.
    last_debit: dict[str, int] = {}
    debit_verbs = {"retry", "switch_rail"}

    rows, ys, case_ids = [], [], []
    for line in explore_path.read_text().splitlines():
        if not line.strip():
            continue
        decision = Decision.model_validate_json(line)
        tick = int(decision.decision_id.split(":")[1])
        previous = last_debit.get(decision.case_id)
        if decision.action.type.value in debit_verbs:
            last_debit[decision.case_id] = tick

        label = labels.get(decision.decision_id)
        case = cases.get(decision.case_id)
        if label is None or case is None:
            continue
        rows.append((case, decision.action, tick, previous))
        ys.append(int(label["settled"]))
        case_ids.append(decision.case_id)
    return rows, np.asarray(ys), case_ids


def cell_for(case: ObservedCase, action: Action, tick: int, last_attempt: int | None = None) -> tuple:
    """(action_type, hour_bucket, decline_class) — the coverage cell."""
    at = dispatch_moment(case, action, tick).astimezone(IST)
    return (action.type.value, at.hour // 4, classify(case.decline_code).value)


def _feature_importance(model, X_test, y_test, sample: int = 3000) -> None:
    """Permutation importance, model-agnostic. Answers "which did it use", not
    "which does it have a coefficient for"."""
    from sklearn.inspection import permutation_importance

    from settle.agent.features import FEATURE_NAMES

    n = min(sample, len(y_test))
    result = permutation_importance(
        model.model, X_test[:n], y_test[:n], n_repeats=5, random_state=0, scoring="neg_brier_score"
    )
    ranked = sorted(zip(FEATURE_NAMES, result.importances_mean), key=lambda kv: -kv[1])
    timing = (
        "day_of_month_at_dispatch",
        "days_to_month_start",
        "in_liquidity_window",
        "days_since_last_attempt",
    )
    print(f"\nfeature importance (permutation, neg-Brier, n={n:,})")
    print("  top 8 overall")
    for name, value in ranked[:8]:
        print(f"    {name:<28}{value:>+10.5f}")
    print("  the four timing features")
    lookup = dict(ranked)
    for name in timing:
        rank = [n for n, _ in ranked].index(name) + 1
        print(f"    {name:<28}{lookup[name]:>+10.5f}   rank {rank}/{len(ranked)}")


def _timing_spread(model, rows, test_rows) -> None:
    """EST-9. If the probability does not move with the offset, the model has
    learned nothing about timing and §9's liquidity-window claim is unsupported.
    Reported plainly either way."""
    from settle.policy.params import hour_offsets
    from settle.schema.action import Retry

    offsets = hour_offsets()
    spreads = []
    worked = None
    for index in test_rows[:4000]:
        case, action, tick, last_attempt = rows[index]
        if not isinstance(action, Retry):
            continue
        probs = [
            model.predict_proba(case, Retry(at_hour_offset=o, rail=action.rail), tick, last_attempt)
            for o in offsets
        ]
        spreads.append(max(probs) - min(probs))
        if worked is None:
            worked = (case, tick, probs)
    if not spreads:
        print("  no retry rows in the test split")
        return

    case, tick, probs = worked
    print(f"  worked example: {case.case_id}, tick {tick}, {case.decline_code}")
    print("    " + "  ".join(f"{o:>5}h" for o in offsets))
    print("    " + "  ".join(f"{p:>6.3f}" for p in probs))
    spreads.sort()
    print(f"  spread over {len(spreads):,} retry rows: "
          f"median {spreads[len(spreads)//2]:.4f}  p90 {spreads[int(len(spreads)*0.9)]:.4f}  "
          f"max {spreads[-1]:.4f}")
    if spreads[len(spreads) // 2] < 0.005:
        print("  FINDING: the probability barely moves with the offset. The model has")
        print("           learned little about timing; §9's liquidity claim is unsupported.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="settle.agent.train", description=__doc__)
    parser.add_argument("--explore", type=Path, default=Path("out/explore.decisions.jsonl"))
    parser.add_argument("--labels", type=Path, default=Path("out/labels.jsonl"))
    parser.add_argument("--out", type=Path, default=Path("out/model.pkl"))
    parser.add_argument("--cases", type=Path, default=Path("out/explore.cases.jsonl"))
    args = parser.parse_args(argv)

    rows, y, case_ids = load_rows(args.explore, args.labels, args.cases)
    print(f"training rows      {len(rows):,} from {len(set(case_ids)):,} cases")
    print(f"base rate          {y.mean():.4f}")

    train, calib, test = split_by_case(case_ids)
    print(f"split (by case)    train {len(train.rows):,} / calib {len(calib.rows):,} / test {len(test.rows):,}")
    print(f"                   cases {len(train.case_ids):,} / {len(calib.case_ids):,} / {len(test.case_ids):,}")

    X = build_matrix(rows)
    Xtr, ytr = X[train.rows], y[train.rows]
    Xca, yca = X[calib.rows], y[calib.rows]
    Xte, yte = X[test.rows], y[test.rows]

    models = {
        "GBM": Estimator(fit_gbm(Xtr, ytr, Xca, yca), "GBM"),
        "LR": Estimator(fit_logistic(Xtr, ytr, Xca, yca), "LR"),
    }
    cells = [cell_for(*rows[i]) for i in test.rows]
    constant = constant_rate_baseline(ytr)
    do_nothing_mask = np.asarray([rows[i][1].type.value == "do_nothing" for i in test.rows])

    results = {}
    print(f"\n{'model':<10} {'ECE':>8} {'Brier':>8} {'ECE(all)':>9} {'Brier(all)':>11}")
    for name, model in models.items():
        p = model.predict_many(Xte)
        stats = headline(p.tolist(), yte.tolist(), cells)
        results[name] = (model, p, stats)
        print(f"{name:<10} {stats['ece']:>8.4f} {stats['brier']:>8.4f} "
              f"{stats['ece_all']:>9.4f} {stats['brier_all']:>11.4f}")

    from settle.agent.calibration import brier_score, expected_calibration_error
    constant_brier = brier_score([constant] * len(yte), yte.tolist())
    print(f"{'CONSTANT':<10} {'—':>8} {constant_brier:>8.4f}  (base rate {constant:.4f})")

    # §10.2 subtracts p_settle(do_nothing) from every action, so a miscalibrated
    # do_nothing term poisons every EV the policy computes. Reported separately.
    print(f"\ndo_nothing rows only (A82: the term every EV subtracts)")
    print(f"  {'model':<6}{'n':>8}{'ECE':>9}{'Brier':>9}{'base':>8}")
    for name, (_, p, _) in results.items():
        pn, yn = p[do_nothing_mask].tolist(), yte[do_nothing_mask].tolist()
        print(f"  {name:<6}{len(pn):>8,}{expected_calibration_error(pn, yn):>9.4f}"
              f"{brier_score(pn, yn):>9.4f}{np.mean(yn):>8.4f}")

    winner = min(results, key=lambda n: results[n][2]["ece"])
    model, p_win, stats = results[winner]

    # EST-6 — the reliability diagram, as numbers.
    print(f"\nreliability ({winner}, held-out test)")
    print(f"  {'bucket':<12}{'n':>8}{'predicted':>11}{'actual':>9}{'gap':>8}")
    for bucket in reliability_table(p_win.tolist(), yte.tolist()):
        if not bucket["n"]:
            print(f"  {bucket['bucket']:<12}{0:>8}{'—':>11}{'—':>9}{'—':>8}")
            continue
        gap = bucket["predicted"] - bucket["actual"]
        print(f"  {bucket['bucket']:<12}{bucket['n']:>8,}{bucket['predicted']:>11.3f}"
              f"{bucket['actual']:>9.3f}{gap:>+8.3f}")

    # EST-7 — coverage, with thin cells named rather than quietly averaged in.
    thin = [row for row in stats["coverage"] if row["extrapolated"]]
    covered = [row for row in stats["coverage"] if not row["extrapolated"]]
    print(f"\ncoverage (action, 4h bucket, decline class) — threshold n >= 50")
    print(f"  covered cells      {len(covered)}   rows {stats['n_covered']:,}")
    print(f"  EXTRAPOLATED       {len(thin)}   rows {stats['n_all'] - stats['n_covered']:,}"
          f"  (excluded from the headline figures)")
    for row in sorted(thin, key=lambda r: -r["n"])[:12]:
        action, bucket, cls = row["cell"]
        print(f"    {action:<22} {bucket*4:02d}-{bucket*4+3:02d}  {cls:<16} n={row['n']:>3}")
    if len(thin) > 12:
        print(f"    ... and {len(thin) - 12} more")

    # Which features the model actually used, on the timing question.
    _feature_importance(model, Xte, yte)

    # EST-9 — does the probability move with the offset at all?
    print(f"\ntiming signal ({winner}) — same case, same verb, eight offsets")
    _timing_spread(model, rows, test.rows)

    print(f"\nshipping: {winner} (lower ECE on held-out test)")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("wb") as handle:
        pickle.dump({"winner": winner, "models": {n: m.model for n, (m, _, _) in results.items()}}, handle)
    print(f"model -> {args.out}   hash {results[winner][0].artifact_hash()[:16]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
