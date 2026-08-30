"""Reconciliation. SPEC §7, §13.1, INV-1.

Runs at `observation_horizon_days` (60), not at the decision horizon (30).
Settlements land late and reversals land later; a reconciler that stopped at 30
days could not see the tail it exists to audit (A76).

It does not trust the executor's account of events. The ledger says what the
agent believed; `ActualOutcome` says what the money did; this module reports
both and the distance between them.

Right-censoring is reported, never guessed (A27). An outcome landing past day 60
is marked `censored` and excluded from the recovery figures rather than assumed
one way or the other.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, Iterable

from pydantic import BaseModel, ConfigDict

from settle.schema.enums import LedgerKind, ReportedStatus, SilentFailureClass
from settle.schema.ledger import LedgerEntry
from settle.schema.observed import ObservedCase
from settle.sim.observability import ObservabilityConfig, reversal_reported_at
from settle.sim.truth import ActualOutcome

OBSERVATION_HORIZON_DAYS: Final[int] = 60

# What the executor hands over: what happened, and when a reversal landed.
ActualRecord = tuple[ActualOutcome, "datetime | None"]


class CaseView(BaseModel):
    """One case's ledger, arranged so a detector can be a pure function."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, arbitrary_types_allowed=True)

    case_id: str
    created_at: datetime
    entries: tuple[LedgerEntry, ...]

    @property
    def dispatches(self) -> tuple[LedgerEntry, ...]:
        return tuple(e for e in self.entries if e.kind is LedgerKind.DISPATCH)

    @property
    def reported(self) -> tuple[LedgerEntry, ...]:
        return tuple(e for e in self.entries if e.kind is LedgerKind.REPORTED_OUTCOME)

    @property
    def gate_checks(self) -> tuple[LedgerEntry, ...]:
        return tuple(e for e in self.entries if e.kind is LedgerKind.GATE_CHECK)

    @property
    def believed_recovered(self) -> bool:
        """What the agent believed. A `captured` webhook is not a settlement
        (INV-1), which is exactly why SF-1 exists."""
        return any(
            e.payload.get("status") == ReportedStatus.CAPTURED.value for e in self.reported
        )


class ReconciledCase(BaseModel):
    """What the agent believed, what happened, and the distance between them."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    case_id: str
    arm: str
    ledger_says_recovered: bool
    actually_settled: bool
    settled_amount_paise: int
    settled_at: datetime | None = None
    reversed: bool = False
    reversed_at: datetime | None = None
    silent_failures: list[SilentFailureClass] = []
    censored: bool = False


def group_by_case(entries: Iterable[LedgerEntry], cases: dict[str, ObservedCase]) -> dict[str, CaseView]:
    grouped: dict[str, list[LedgerEntry]] = {}
    for entry in entries:
        grouped.setdefault(entry.case_id, []).append(entry)
    return {
        case_id: CaseView(
            case_id=case_id,
            created_at=cases[case_id].created_at,
            entries=tuple(sorted(rows, key=lambda e: e.seq)),
        )
        for case_id, rows in grouped.items()
        if case_id in cases
    }


def reconcile(
    ledger: Iterable[LedgerEntry],
    actual_outcomes: dict[str, list[ActualRecord]],
    cases: dict[str, ObservedCase],
    *,
    horizon_days: int = OBSERVATION_HORIZON_DAYS,
    config: ObservabilityConfig | None = None,
) -> dict[str, ReconciledCase]:
    """One record per case. SPEC §7.

    `horizon_days` is the observation horizon, not the decision horizon. The
    agent stopped acting at 30 days; the money kept moving.
    """
    from settle.recon.silent_failures import detect_all

    config = config or ObservabilityConfig()
    views = group_by_case(ledger, cases)
    reconciled: dict[str, ReconciledCase] = {}

    for case_id, case in cases.items():
        view = views.get(
            case_id, CaseView(case_id=case_id, created_at=case.created_at, entries=())
        )
        horizon = case.created_at + timedelta(days=horizon_days)
        records = actual_outcomes.get(case_id, [])

        settled_record = next(
            (
                (outcome, reversed_at)
                for outcome, reversed_at in records
                if outcome.settled and outcome.settled_at is not None
            ),
            None,
        )

        censored = False
        settled = False
        settled_at: datetime | None = None
        amount = 0
        reversed_flag = False
        reversed_at: datetime | None = None

        if settled_record is not None:
            outcome, raw_reversed_at = settled_record
            if outcome.settled_at is not None and outcome.settled_at > horizon:
                # Landed past the window. Reported as censored, never guessed.
                censored = True
            else:
                settled = True
                settled_at = outcome.settled_at
                amount = outcome.amount_paise or 0
                if raw_reversed_at is not None:
                    if raw_reversed_at > horizon:
                        censored = True
                    else:
                        reversed_flag = True
                        reversed_at = raw_reversed_at

        record = ReconciledCase(
            case_id=case_id,
            arm=view.entries[0].arm if view.entries else "",
            ledger_says_recovered=view.believed_recovered,
            actually_settled=settled,
            settled_amount_paise=amount,
            settled_at=settled_at,
            reversed=reversed_flag,
            reversed_at=reversed_at,
            censored=censored,
        )
        reconciled[case_id] = record.model_copy(
            update={"silent_failures": detect_all(view, record, case, config)}
        )

    return reconciled


def labels(decisions: Iterable[dict], reconciled: dict[str, ReconciledCase]) -> list[dict]:
    """Training labels. SPEC §10.1, Part E.

    The `settled` column comes from reconciliation and from nothing else. A
    label derived from `ReportedOutcome` would teach the estimator to predict
    what the webhook said, which is the one thing §6 says cannot be trusted —
    and the model would then be confidently wrong in exactly the cases the
    project exists to catch.

    Only rows where the choice set had more than one member are labelled (A75).
    A row where `do_nothing` was the only option is not a decision.
    """
    rows = []
    for decision in decisions:
        propensity = decision.get("propensity")
        if propensity is None or propensity >= 1.0:
            continue
        record = reconciled.get(decision["case_id"])
        if record is None:
            continue
        rows.append(
            {
                "case_id": decision["case_id"],
                "decision_id": decision["decision_id"],
                "settled": record.actually_settled and not record.reversed,
                "censored": record.censored,
            }
        )
    return rows


def censored_fraction(reconciled: dict[str, ReconciledCase]) -> float:
    if not reconciled:
        return 0.0
    return sum(1 for r in reconciled.values() if r.censored) / len(reconciled)


def run_arm(arm_key: str, n_cases: int, seed: int):
    """One arm over a batch, returning everything reconciliation needs."""
    import tempfile

    from settle.audit.chain import Ledger, read_entries
    from settle.execute.executor import WorldHandle
    from settle.runner.arm import ARMS
    from settle.runner.case_runner import run_case
    from settle.schema.enums import ArmMode
    from settle.sim.generator import generate_batch
    from settle.sim.streams import Streams

    arm_class = ARMS[arm_key]
    arm = arm_class(90_000) if arm_key == "explore" else arm_class()
    batch = generate_batch(n_cases, seed)
    streams = Streams(seed)
    config = ObservabilityConfig()

    actuals: dict[str, list[ActualRecord]] = {}
    cases: dict[str, ObservedCase] = {}
    path = Path(tempfile.mkdtemp()) / f"{arm_key}.jsonl"
    with Ledger(path) as ledger:
        for generated in batch.cases:
            world = WorldHandle(truth=generated.truth, streams=streams)
            run_case(generated.observed, arm, world, config, ledger)
            actuals[generated.observed.case_id] = list(world.actuals)
            cases[generated.observed.case_id] = generated.observed
    return read_entries(path), actuals, cases, arm.name, arm.mode


def failure_counts(reconciled: dict[str, ReconciledCase]) -> dict[SilentFailureClass, int]:
    counts = {cls: 0 for cls in SilentFailureClass}
    for record in reconciled.values():
        for failure in record.silent_failures:
            counts[failure] += 1
    return counts


def print_table(n_cases: int, seed: int, seeded: int = 0, labels_path: Path | None = None) -> None:
    """The project's headline artifact. SPEC §7, §14.4."""
    from settle.recon.silent_failures import COMPLIANCE_CLASSES
    from settle.schema.enums import ArmMode

    classes = list(SilentFailureClass)
    header = "  arm   " + " ".join(f"{c.value:>6}" for c in classes) + "   censored%  believed  settled"
    print(f"\nsilent failures — {n_cases:,} cases, seed {seed}, horizon {OBSERVATION_HORIZON_DAYS}d")
    print(header)
    print("  " + "-" * (len(header) - 2))

    for arm_key in ("b0", "b1", "b2", "b3", "explore"):
        entries, actuals, cases, name, mode = run_arm(arm_key, n_cases, seed)
        reconciled = reconcile(entries, actuals, cases)
        counts = failure_counts(reconciled)
        believed = sum(1 for r in reconciled.values() if r.ledger_says_recovered)
        settled = sum(1 for r in reconciled.values() if r.actually_settled)
        row = " ".join(f"{counts[c]:>6,}" for c in classes)
        print(f"  {name:<6}{row}   {censored_fraction(reconciled):>8.2%}  {believed:>8,} {settled:>8,}")

        if mode is ArmMode.ENFORCE:
            breaches = {c: counts[c] for c in COMPLIANCE_CLASSES if counts[c]}
            if breaches:
                print(f"         *** GATE FAILURE: {name} is in ENFORCE and shows {breaches} ***")
                print("             This is not an audit finding. A gate did not hold.")

    if seeded:
        entries, actuals, cases = seed_failures(seeded)
        reconciled = reconcile(entries, actuals, cases)
        counts = failure_counts(reconciled)
        print(f"\n  seeded {seeded} of each class — detector found:")
        for cls in classes:
            mark = "ok" if counts[cls] == seeded else "MISMATCH"
            print(f"    {cls.value}  injected {seeded:>3}  found {counts[cls]:>3}   {mark}")


def main(argv: list[str] | None = None) -> int:
    """Run every arm, reconcile, and print the silent-failure table."""
    parser = argparse.ArgumentParser(prog="settle.recon.reconcile", description=__doc__)
    parser.add_argument("--cases", type=int, default=2_000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--seed-failures", type=int, default=0)
    parser.add_argument("--labels", type=Path, default=None)
    args = parser.parse_args(argv)
    print_table(args.cases, args.seed, args.seed_failures, args.labels)
    return 0



# ---------------------------------------------------------------------------
# Seeded failures. SPEC §7 — the demo batch carries deliberate instances of
# every class, because a detector that always reports zero is indistinguishable
# from a broken one.
# ---------------------------------------------------------------------------

def seed_failures(n: int):
    """Inject `n` known instances of each of SF-1 through SF-7.

    Returns `(ledger_entries, actuals, cases)` shaped exactly as a real run's,
    so the detectors cannot tell the difference and REC-8 can assert the counts.
    """
    from datetime import timezone

    from settle.schema.enums import Actor, Language, MandateState, Rail
    from settle.schema.ledger import LedgerEntry

    anchor = datetime(2026, 3, 1, 3, 0, tzinfo=timezone.utc)  # 08:30 IST
    entries: list[LedgerEntry] = []
    actuals: dict[str, list[ActualRecord]] = {}
    cases: dict[str, ObservedCase] = {}
    seq = 0

    def case_for(case_id: str) -> ObservedCase:
        return ObservedCase(
            case_id=case_id, created_at=anchor, customer_id="cust",
            amount_paise=49900, rail=Rail.CARD, decline_code="insufficient_funds",
            decline_reason="x", attempt_number=1, mandate_state=MandateState.ACTIVE,
            tenure_months=3, prior_failures=0, prior_recoveries=0, plan_value_paise=49900,
            consent_whatsapp=True, dnd_flag=False, language=Language.EN,
        )

    def add(case_id, kind, actor, payload, reason, hours):
        nonlocal seq
        entries.append(
            LedgerEntry(
                seq=seq, case_id=case_id, at=anchor + timedelta(hours=hours), kind=kind,
                actor=actor, payload=payload, reason_code=reason,
                prev_hash="0" * 64, hash="0" * 64, arm="SEEDED",
            )
        )
        seq += 1

    def contact(case_id, hours, key="k0", verb="send_message"):
        add(case_id, LedgerKind.DISPATCH, Actor.SYSTEM,
            {"action": {"type": verb, "channel": "sms", "template_id": "t"},
             "idempotency_key": key}, "DISPATCH_INTENT", hours)

    for index in range(n):
        # SF-1: believed captured, never settled.
        cid = f"sf1_{index}"; cases[cid] = case_for(cid); actuals[cid] = []
        add(cid, LedgerKind.REPORTED_OUTCOME, Actor.SYSTEM,
            {"status": "captured", "arrival_count": 1, "payment_id": "pay_x"}, "REPORTED_CAPTURED", 1)

        # SF-2: settled, never reported, and contact continued afterwards.
        cid = f"sf2_{index}"; cases[cid] = case_for(cid)
        settled_at = anchor + timedelta(hours=10)
        actuals[cid] = [(ActualOutcome(case_id=cid, at=anchor, settled=True, settled_at=settled_at,
                                       reversed=False, amount_paise=49900), None)]
        contact(cid, 30)

        # SF-3: a replayed webhook produced a second dispatch under a spent key.
        cid = f"sf3_{index}"; cases[cid] = case_for(cid); actuals[cid] = []
        add(cid, LedgerKind.REPORTED_OUTCOME, Actor.SYSTEM,
            {"status": "failed", "arrival_count": 2, "payment_id": None}, "REPORTED_FAILED", 1)
        contact(cid, 2, key="dup"); contact(cid, 3, key="dup")

        # SF-4: promise logged, date passed, nothing followed.
        cid = f"sf4_{index}"; cases[cid] = case_for(cid); actuals[cid] = []
        add(cid, LedgerKind.DECISION, Actor.POLICY,
            {"promise_date": (anchor + timedelta(days=5)).isoformat()}, "PROMISE_LOGGED", 1)
        contact(cid, 2)

        # SF-5: contact after opt-out.
        cid = f"sf5_{index}"; cases[cid] = case_for(cid); actuals[cid] = []
        add(cid, LedgerKind.DECISION, Actor.POLICY, {}, "OPTED_OUT", 4)
        contact(cid, 30)

        # SF-6: contact at 02:30 IST.
        cid = f"sf6_{index}"; cases[cid] = case_for(cid); actuals[cid] = []
        contact(cid, 18)

        # SF-7: settled, reversed, never reopened.
        cid = f"sf7_{index}"; cases[cid] = case_for(cid)
        settled_at = anchor + timedelta(days=2)
        reversed_at = anchor + timedelta(days=9)
        actuals[cid] = [(ActualOutcome(case_id=cid, at=anchor, settled=True, settled_at=settled_at,
                                       reversed=True, amount_paise=49900), reversed_at)]
        contact(cid, 5)

    return entries, actuals, cases

if __name__ == "__main__":
    raise SystemExit(main())
