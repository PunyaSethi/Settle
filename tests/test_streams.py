"""CP2 — indexed random streams. SPEC §14.2.

STR-3 is the one that matters. It is the property that makes arm comparison
mean anything: two arms that took different routes to the same tick must read
the same number there. Without it, every headline in §14.4 is noise.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from settle.sim.streams import (
    MAX_TICK,
    OBSERVATION_HORIZON_DAYS,
    STREAM_NAMES,
    STREAM_TICK_UNITS,
    TICKS_PER_DAY,
    Streams,
    stream_value,
    tick_for,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED = 42


# --------------------------------------------------------------------------
# STR-1
# --------------------------------------------------------------------------

def test_STR_1_value_is_a_pure_function_of_seed_case_name_and_tick():
    first = stream_value(SEED, "case-1", "action_outcome", 7)
    assert stream_value(SEED, "case-1", "action_outcome", 7) == first
    assert 0.0 <= first < 1.0

    # Changing any one coordinate changes the address.
    assert stream_value(SEED + 1, "case-1", "action_outcome", 7) != first
    assert stream_value(SEED, "case-2", "action_outcome", 7) != first
    assert stream_value(SEED, "case-1", "settle_roll", 7) != first
    assert stream_value(SEED, "case-1", "action_outcome", 8) != first


def test_STR_1_reading_other_addresses_does_not_disturb_this_one():
    before = stream_value(SEED, "case-1", "reply_draw", 3)
    for tick in range(MAX_TICK + 1 - 200, MAX_TICK + 1):
        for name in STREAM_NAMES:
            stream_value(SEED, "case-99", name, tick)
    assert stream_value(SEED, "case-1", "reply_draw", 3) == before


def test_STR_1_value_is_identical_in_a_separate_process():
    """Python's built-in hash() is salted per process. blake2b is not.

    This is the test that catches someone swapping hashlib for hash().
    """
    script = (
        "from settle.sim.streams import stream_value;"
        "print(repr([stream_value(42, 'case-1', n, 11) for n in "
        "('action_outcome','settle_roll','reply_draw')]))"
    )
    runs = [
        subprocess.run(
            [sys.executable, "-c", script],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
        ).stdout
        for seed in ("0", "1", "random")
    ]
    assert runs[0] == runs[1] == runs[2]
    assert runs[0] == repr(
        [stream_value(42, "case-1", n, 11) for n in ("action_outcome", "settle_roll", "reply_draw")]
    ) + "\n"


def test_STR_1_unknown_stream_name_is_rejected_rather_than_silently_hashed():
    with pytest.raises(ValueError, match="unknown stream"):
        stream_value(SEED, "case-1", "reply_drwa", 0)


# --------------------------------------------------------------------------
# STR-2
# --------------------------------------------------------------------------

def test_STR_2_reading_tick_14_directly_equals_reading_it_after_ticks_0_to_13():
    direct = stream_value(SEED, "case-1", "action_outcome", 14)

    walked = None
    for tick in range(15):
        walked = stream_value(SEED, "case-1", "action_outcome", tick)

    assert walked == direct


def test_STR_2_the_whole_prefix_is_order_independent():
    forwards = [stream_value(SEED, "case-1", "patience_draw", t) for t in range(20)]
    backwards = [stream_value(SEED, "case-1", "patience_draw", t) for t in reversed(range(20))]
    assert list(reversed(backwards)) == forwards


# --------------------------------------------------------------------------
# STR-3 — the CRN property
# --------------------------------------------------------------------------

def _arm_reads(streams: Streams, case_id: str, action_count: int, probe_tick: int) -> float:
    """An arm that burns `action_count` draws before probing `probe_tick`."""
    for tick in range(action_count):
        streams.value(case_id, "action_outcome", tick)
    return streams.value(case_id, "action_outcome", probe_tick)


def test_STR_3_arms_with_different_action_counts_read_the_same_value_at_a_tick():
    streams = Streams(SEED)
    probe = 30
    values = {n: _arm_reads(streams, "case-1", n, probe) for n in (0, 3, 7, 19)}
    assert len(set(values.values())) == 1, (
        "an arm's action count changed what it observed at the same tick — "
        "common random numbers are broken and arm comparison is noise"
    )


def test_STR_3_holds_across_every_named_stream():
    streams = Streams(SEED)
    for name in STREAM_NAMES:
        busy = [streams.value("case-1", name, t) for t in range(11)][10]
        idle = Streams(SEED).value("case-1", name, 10)
        assert busy == idle, name


def test_STR_3_a_sequential_prng_would_have_failed_this():
    """The negative control: show the property is not free.

    A seeded sequential PRNG desyncs the moment two arms draw a different
    number of times. If this ever starts passing, someone has replaced the
    addressed streams with consumption and STR-3 has stopped meaning anything.
    """
    import random

    def sequential_arm(action_count: int) -> float:
        rng = random.Random(SEED)
        for _ in range(action_count):
            rng.random()
        return rng.random()

    assert sequential_arm(3) != sequential_arm(7)


# --------------------------------------------------------------------------
# STR-4
# --------------------------------------------------------------------------

def test_STR_4_ticks_are_addressable_to_day_60():
    assert OBSERVATION_HORIZON_DAYS == 60
    assert MAX_TICK == 60 * TICKS_PER_DAY - 1 == 1439

    last_day, last_hour = OBSERVATION_HORIZON_DAYS - 1, TICKS_PER_DAY - 1
    assert tick_for(last_day, last_hour) == MAX_TICK
    assert 0.0 <= stream_value(SEED, "case-1", "reversal_roll", MAX_TICK) < 1.0


def test_STR_4_day_30_is_not_the_end_of_the_address_space():
    """A 30-day address space would silently right-censor the settlement tail."""
    thirty_day_max = 30 * TICKS_PER_DAY - 1
    assert MAX_TICK > thirty_day_max
    assert 0.0 <= stream_value(SEED, "case-1", "settle_roll", thirty_day_max + 1) < 1.0


def test_STR_4_out_of_range_addresses_are_refused():
    with pytest.raises(ValueError):
        stream_value(SEED, "case-1", "settle_roll", MAX_TICK + 1)
    with pytest.raises(ValueError):
        stream_value(SEED, "case-1", "settle_roll", -1)
    with pytest.raises(ValueError):
        tick_for(OBSERVATION_HORIZON_DAYS, 0)


def test_STR_4_every_named_stream_declares_its_tick_unit():
    assert set(STREAM_TICK_UNITS) == set(STREAM_NAMES)
    assert len(STREAM_NAMES) == 7
    assert all(unit.startswith("per ") for unit in STREAM_TICK_UNITS.values())
