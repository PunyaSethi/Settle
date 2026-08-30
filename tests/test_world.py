"""CP6.1 — natural recovery. SPEC §14.3, A77.

Without this path B0 recovers nothing, incremental equals gross, and §14.3's
subtraction protects nothing. It is also what gives `do_nothing` positive
expected value: if inaction never recovers anything then every action dominates
it, and the contact-restraint result is unreachable by construction.
"""

import collections

import pytest

from settle.recon.reconcile import reconcile, run_arm
from settle.schema.enums import IntentType
from settle.sim.generator import PARAMS, generate_batch
from settle.sim.streams import STREAM_NAMES, Streams
from settle.sim.world import natural_recovery, natural_recovery_at, natural_recovery_probability

SEED = 42
HORIZON_TICKS = 60 * 24


@pytest.fixture(scope="module")
def batch():
    return generate_batch(2_000, SEED)


def test_WLD_1_b0_recovers_a_non_zero_fraction(batch):
    """B0 does nothing and still recovers. That is the point of the arm."""
    streams = Streams(SEED)
    cured = [g for g in batch.cases if natural_recovery(g.observed, g.truth, HORIZON_TICKS, streams)]
    rate = len(cured) / len(batch.cases)
    assert rate > 0.05, "B0 recovers nothing, so incremental scoring protects nothing"
    assert 0.15 < rate < 0.32, rate


def test_WLD_1_the_realised_rate_matches_the_declared_parameter(batch):
    """Per intent, because a single global rate would make `intent_type`
    decorative in exactly the place it matters most."""
    streams = Streams(SEED)
    total = collections.Counter(g.truth.intent_type for g in batch.cases)
    cured = collections.Counter(
        g.truth.intent_type
        for g in batch.cases
        if natural_recovery(g.observed, g.truth, HORIZON_TICKS, streams)
    )
    for intent in IntentType:
        declared = natural_recovery_probability(intent)
        n = total[intent]
        if n < 100:
            continue
        realised = cured[intent] / n
        tolerance = max(3.5 * ((declared * (1 - declared) / n) ** 0.5), 0.02)
        assert abs(realised - declared) <= tolerance, (intent.value, realised, declared)


def test_WLD_1_recovery_is_ordered_by_intent(batch):
    """Someone willing and able notices and pays. Someone churned does not."""
    assert natural_recovery_probability(IntentType.WILLING_ABLE) > natural_recovery_probability(
        IntentType.WILLING_BROKE
    )
    assert natural_recovery_probability(IntentType.WILLING_BROKE) > natural_recovery_probability(
        IntentType.CHURNED
    )
    assert natural_recovery_probability(IntentType.CHURNED) < 0.05


def test_WLD_2_the_self_cure_is_identical_across_arms(batch):
    """It does not depend on what the arm did — it happened anyway.

    §14.3 subtracts whatever B0 recovers from every other arm. That subtraction
    is only meaningful if the self-cure is identifiably the *same event*.
    """
    first, second = Streams(SEED), Streams(SEED)
    for generated in batch.cases[:400]:
        for tick in (0, 240, HORIZON_TICKS):
            assert natural_recovery(
                generated.observed, generated.truth, tick, first
            ) == natural_recovery(generated.observed, generated.truth, tick, second)
        assert natural_recovery_at(
            generated.observed, generated.truth, first
        ) == natural_recovery_at(generated.observed, generated.truth, second)


def test_WLD_2_every_arm_reconciles_the_same_self_cures():
    """The end-to-end version: two different arms, the same cases self-curing."""
    cured_by_arm = {}
    for arm_key in ("b0", "b1"):
        entries, actuals, cases, name, _, truths, streams = run_arm(arm_key, 400, SEED)
        reconciled = reconcile(entries, actuals, cases, truths=truths, streams=streams)
        cured_by_arm[name] = {
            case_id
            for case_id in cases
            if natural_recovery_at(cases[case_id], truths[case_id], streams) is not None
        }
    b0, b1 = cured_by_arm["B0"], cured_by_arm["B1"]
    assert b0, "B0 cured nothing"
    assert b0 == b1, "the self-cure set diverged between arms — CRN is broken"


def test_WLD_2_the_draws_come_from_shared_streams():
    assert "natural_recovery_draw" in STREAM_NAMES
    assert "natural_recovery_day" in STREAM_NAMES


def test_WLD_1_every_rate_carries_a_priors_row():
    """INV-10. These move B0's recovery, which is subtracted from every arm."""
    for intent in IntentType:
        assert f"natural_recovery.{intent.value}" in PARAMS
    assert "natural_recovery.max_day" in PARAMS
