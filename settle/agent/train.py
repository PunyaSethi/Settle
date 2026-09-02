"""Train the estimator. SPEC §10.1.

    python -m settle.agent.train --explore out/explore.decisions.jsonl \
        --labels out/labels.jsonl --out out/

Training rows come from the EXPLORE arm only, and labels come from
reconciliation against `ActualOutcome`. A label taken from `ReportedOutcome`
would teach the model to predict what the webhook said, which is the one thing
§6 says cannot be trusted.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import pickle
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

import numpy as np

from settle.agent.calibration import headline, reliability_table
from settle.agent.estimator import MAX_FLAT_DECISION_RATE
from settle.agent.estimator import LATEST_POINTER
from settle.agent.estimator import (
    Estimator,
    build_matrix,
    constant_rate_baseline,
    fit_gbm,
    fit_logistic,
    has_usable_resolution,
    split_by_case,
    uplift_calibration,
    uplift_resolution,
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

    # The tick at which the previous debit on each case was *dispatched*,
    # reconstructed from the decision stream in order. Not hidden truth: a
    # merchant knows when it last tried to charge someone.
    #
    # Dispatched, not chosen. `retry(at_hour_offset=72)` is a commitment that
    # fires three days later (A73), and the bank sees it when it is submitted.
    # `CaseState.last_attempt_tick` records the firing tick, so this must add
    # the offset or the two sides of EST-12 would be measuring different events.
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
            last_debit[decision.case_id] = tick + action_offset(decision.action)

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


def _feature_importance(model, X_test, y_test, sample: int = 3000) -> dict:
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
    order = [n for n, _ in ranked]
    for name in timing:
        rank = order.index(name) + 1
        print(f"    {name:<28}{lookup[name]:>+10.5f}   rank {rank}/{len(ranked)}")
    return {
        "n_features": len(ranked),
        "ranks": {
            name: {"rank": order.index(name) + 1, "importance": lookup[name]}
            for name in timing
        },
    }


def _timing_spread(model, rows, test_rows) -> dict:
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
        return {"n_offsets": len(offsets), "offsets": list(offsets), "spread": None}

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
    return {
        "n_offsets": len(offsets),
        "offsets": list(offsets),
        "spread": {
            "n_retry_rows": len(spreads),
            "median": spreads[len(spreads) // 2],
            "p90": spreads[int(len(spreads) * 0.9)],
            "max": spreads[-1],
        },
    }


# Resolution is a property of the decisions the policy will actually face, so it
# is measured on real candidate grids rather than on training rows. The cases
# come from the held-out test split — which `load_rows` already read off disk —
# and never from the generator: `settle/agent/` may not import `settle.sim`
# (INV-8, EST-1, GEN-2), and a probe that reached for a simulator would be the
# agent package deciding it is allowed to build its own world.
probe_size: Final[int] = 600
PROBE_TICKS: Final[tuple[tuple[int, int | None], ...]] = (
    (0, None), (24, 0), (120, 24), (336, 120),
)


def _resolution_probe(rows, test_rows, n: int = probe_size) -> list[tuple]:
    """`(case, tick, last_attempt_tick, actions)` for multi-option decisions."""
    from settle.policy.grid import candidate_pairs
    from settle.schema.enums import ArmMode
    from settle.schema.state import CaseState

    seen: dict[str, ObservedCase] = {}
    for index in test_rows:
        case = rows[index][0]
        seen.setdefault(case.case_id, case)

    out: list[tuple] = []
    for case in seen.values():
        for tick, last in PROBE_TICKS:
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



# The two timing hypotheses, and why they are written here rather than measured
# downstream. Until CP13.2 these figures existed only in this module's stdout,
# the README quoted them from a training log, and the quoted values were stale —
# they predated A93 and reproduced nothing. A number a test cannot check is a
# number that drifts, so training writes them.
LIQUIDITY_FEATURES: Final[tuple[str, ...]] = (
    "day_of_month_at_dispatch",
    "days_to_month_start",
    "in_liquidity_window",
)
RECENCY_FEATURES: Final[tuple[str, ...]] = ("days_since_last_attempt",)

# What the README carried until CP13.1, kept so the correction stays auditable
# rather than becoming a silent edit.
SUPERSEDED_TIMING: Final[dict] = {
    "note": (
        "What the README carried until CP13.1, from a training log predating "
        "A93. Recorded so the correction is auditable rather than a silent edit."
    ),
    "median_spread_points": 3.7,
    "rank_range": "26-37",
    "n_features": 45,
}


def write_model_report(path: Path, winner: str, model_name: str,
                       importance: dict, timing: dict) -> None:
    """Write the timing block of `out/model_report.json`, preserving the rest.

    Merged rather than overwritten, and the reason is INV-8. This module is in
    `settle/agent/`, which may not import `settle.sim`, so it cannot run an arm
    and cannot produce the SF-2 decomposition that shares this file — that block
    is written by the evaluation side, which is allowed to build a world. A
    training run that clobbered it would be the agent package deleting evidence
    it is not entitled to generate.

    Same reason `liquidity.sensitivity` is left alone: it comes from the
    parameter sweep, which is an evaluation artefact.
    """
    report: dict = {}
    if path.exists():
        with contextlib.suppress(json.JSONDecodeError, OSError):
            report = json.loads(path.read_text(encoding="utf-8"))

    ranks = importance["ranks"]
    liquidity_ranks = {n: ranks[n] for n in LIQUIDITY_FEATURES if n in ranks}
    recency_ranks = {n: ranks[n] for n in RECENCY_FEATURES if n in ranks}

    report.setdefault("about", (
        "Figures the README quotes that out/metrics.json does not produce. "
        "The timing block is written by settle.agent.train at training time; "
        "the SF-2 decomposition is written by the evaluation side, which is the "
        "only part permitted to run an arm."
    ))
    report["generated_at"] = datetime.now(tz=timezone.utc).isoformat()
    report["model"] = model_name
    report["winner"] = winner

    block = report.setdefault("retry_timing", {})
    block["summary"] = (
        "Two hypotheses were tested and they came apart. Liquidity timing is "
        "withdrawn; recency survived. Reporting them together as one set of "
        "'timing features' understated one and overstated the other."
    )
    block["n_features"] = importance["n_features"]
    block["n_offsets"] = timing["n_offsets"]
    block["offsets"] = timing["offsets"]

    liquidity = block.setdefault("liquidity", {})
    liquidity["verdict"] = "withdrawn — SPEC §10.1, A83"
    liquidity["hypothesis"] = "retries near payday recover more"
    liquidity["feature_ranks"] = liquidity_ranks
    liquidity["rank_best"] = min((v["rank"] for v in liquidity_ranks.values()), default=None)
    liquidity["rank_worst"] = max((v["rank"] for v in liquidity_ranks.values()), default=None)

    recency = block.setdefault("recency", {})
    recency["verdict"] = "survived"
    recency["hypothesis"] = "how long since the last attempt matters"
    recency["feature_ranks"] = recency_ranks
    recency["rank_best"] = min((v["rank"] for v in recency_ranks.values()), default=None)
    recency["offset_spread"] = timing["spread"]

    block["superseded_figures"] = SUPERSEDED_TIMING

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"model report -> {path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="settle.agent.train", description=__doc__)
    parser.add_argument("--explore", type=Path, default=Path("out/explore.decisions.jsonl"))
    parser.add_argument("--labels", type=Path, default=Path("out/labels.jsonl"))
    # A directory, not a file. The artifact is content-addressed inside it.
    parser.add_argument("--out", type=Path, default=Path("out"))
    parser.add_argument("--cases", type=Path, default=Path("out/explore.cases.jsonl"))
    # Deliberately not derived from --out: the report is a committed deliverable
    # checked by CHT-3, while --out is where model artifacts land, and a run
    # that trained into a scratch directory should still be able to refresh it.
    parser.add_argument("--model-report", type=Path, default=Path("out/model_report.json"))
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

    # Four candidates, because the calibrator is part of the model. CP10
    # measured isotonic erasing 15.1% of decisions' resolution while moving
    # uplift ECE by 0.0003, so "GBM" and "GBM, isotonically calibrated" are two
    # different answers to the policy's question and both have to be on the
    # table (A92).
    models = {
        "GBM+iso": Estimator(fit_gbm(Xtr, ytr, Xca, yca), "GBM+iso"),
        "GBM": Estimator(fit_gbm(Xtr, ytr, Xca, yca, calibrated=False), "GBM"),
        "LR+iso": Estimator(fit_logistic(Xtr, ytr, Xca, yca), "LR+iso"),
        "LR": Estimator(fit_logistic(Xtr, ytr, Xca, yca, calibrated=False), "LR"),
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

    # --- model selection. SPEC §10.1 (A84) --------------------------------
    #
    # On the calibration of the *uplift*, not of the probability. §10.2
    # subtracts `p_settle(do_nothing)` from every action, so the quantity the
    # policy is sensitive to is the difference and not either term alone. A
    # model can win overall and lose the difference, and shipping it would mean
    # selecting on a number the policy never uses.
    #
    # A84 has said this since CP8. Until CP9.1 the code selected on overall ECE
    # and `uplift_calibration` was called by nothing — so the rule was a claim in
    # a document with no code path, and at CP9 the two disagreed: LR won overall
    # and GBM won the difference. The decision is printed here rather than left
    # implicit in a `min()`.
    uplift = {name: uplift_calibration(model, rows, X, y, test.rows)
              for name, (model, _, _) in results.items()}
    probe = _resolution_probe(rows, test.rows)
    resolution = {name: uplift_resolution(model, probe)
                  for name, (model, _, _) in results.items()}

    print("\n model selection — A84 (calibration of the uplift) x A92 (resolution of it)")
    print(f"  {'model':<10}{'ECE overall':>13}{'ECE uplift':>12}{'spread':>10}"
          f"{'flat':>8}{'usable':>9}")
    for name in results:
        r = resolution[name]
        print(
            f"  {name:<10}{results[name][2]['ece']:>13.4f}{uplift[name]['ece_uplift']:>12.4f}"
            f"{r['median']:>10.4f}{r['flat_rate']:>8.1%}"
            f"{('yes' if has_usable_resolution(r) else 'NO'):>9}"
        )
    print(f"  resolution probed on {probe_size:,} real multi-option decisions;"
          f" a model flat on more than {MAX_FLAT_DECISION_RATE:.0%} of them is not selectable")

    usable = [n for n in results if has_usable_resolution(resolution[n])]
    rejected = [n for n in results if n not in usable]
    if rejected:
        print(f"  rejected on resolution: {', '.join(rejected)}"
              f" — a scorer that returns one number for every option is a constant,"
              f" whatever its calibration says (A92)")
    if not usable:
        raise SystemExit(
            "every candidate failed the resolution floor. The policy has nothing to rank with; "
            "fix the features or the calibrator before shipping a model."
        )

    winner = min(usable, key=lambda name: uplift[name]["ece_uplift"])
    overall_winner = min(results, key=lambda name: results[name][2]["ece"])
    if overall_winner != winner:
        print(
            f"\n  the criteria disagree: {overall_winner} wins overall ECE"
            f" ({results[overall_winner][2]['ece']:.4f} vs {results[winner][2]['ece']:.4f}),"
            f" {winner} wins the uplift"
            f" ({uplift[winner]['ece_uplift']:.4f} vs {uplift[overall_winner]['ece_uplift']:.4f})."
        )
        print("  §10.2 uses the difference and nothing else, so the uplift winner ships.")
        print(f"  The cost is stated rather than hidden: the shipped model's probability"
              f" level is calibrated to {results[winner][2]['ece']:.4f} ECE, not"
              f" {results[overall_winner][2]['ece']:.4f}.")
    else:
        print(f"\n  both criteria agree on {winner}.")

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
    importance = _feature_importance(model, Xte, yte)

    # EST-9 — does the probability move with the offset at all?
    print(f"\ntiming signal ({winner}) — same case, same verb, eight offsets")
    timing = _timing_spread(model, rows, test.rows)

    print(f"\nshipping: {winner} (lower uplift ECE on held-out test — A84)")

    # --- the artifact. SPEC §10.1, CP9.1 D2 -------------------------------
    #
    # Content-addressed, and never overwritten. The CP8-to-CP9 comparison is
    # unrecoverable because retraining replaced `out/model.pkl` in place, so the
    # world change and the model change could not be separated afterwards. A
    # run that cannot be re-measured against its predecessor is a run whose
    # numbers cannot be attributed.
    payload = {
        "winner": winner,
        "models": {n: m.model for n, (m, _, _) in results.items()},
        "selection": {
            "criterion": "uplift_ece, subject to a resolution floor (A84 x A92)",
            "uplift": {n: uplift[n]["ece_uplift"] for n in uplift},
            "overall": {n: results[n][2]["ece"] for n in results},
            "resolution": {n: resolution[n] for n in resolution},
            "rejected_on_resolution": rejected,
        },
        "rows": len(rows),
        "cases": len(set(case_ids)),
    }
    blob = pickle.dumps(payload)
    sha = hashlib.sha256(blob).hexdigest()[:12]
    out_dir = args.out if args.out.suffix == "" else args.out.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    artifact = out_dir / f"model_{sha}.pkl"
    artifact.write_bytes(blob)
    (out_dir / LATEST_POINTER).write_text(artifact.name + "\n", encoding="utf-8")
    print(f"model -> {artifact}")
    print(f"latest -> {out_dir / LATEST_POINTER}   ({artifact.name})")

    write_model_report(args.model_report, winner, artifact.name, importance, timing)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
