"""Indexed random streams. SPEC §14.2.

Every draw in the simulation is addressed, not consumed:

    value(case_id, stream_name, tick) -> float in [0, 1)

The value is derived by hashing `(master_seed, case_id, stream_name, tick)`.
Nothing is drawn from a sequential PRNG, and that is the whole point.

Why addressing rather than consumption
--------------------------------------
Common random numbers exist so that every arm faces identical luck and the
difference between arms is the policy rather than the draws. A seeded
sequential PRNG does not give you that. An arm that takes seven actions has
consumed seven values by the time it reaches a decision that an arm taking
three actions reaches after three — from that point on the two arms are reading
different numbers, and every comparison downstream is noise dressed up as a
result.

Addressing removes the coupling between an arm's action count and the
randomness it observes. Tick N of a stream is the same number for every arm,
forever, regardless of how it got there.

`hashlib` rather than `hash()`
------------------------------
Python's built-in `hash()` is salted per process by default. A batch seeded
identically would produce different numbers in two runs, which is exactly the
failure GEN-1 exists to catch. BLAKE2b has no such behaviour: the same bytes
give the same digest on any machine, in any process, on any run.
"""

import hashlib
from typing import Final

# SPEC §13.1 — the world keeps running for 60 days, not the 30 the agent acts
# over. Streams are sized to the observation horizon so that settlements and
# reversals landing in the tail are drawn from the same addresses for every arm.
OBSERVATION_HORIZON_DAYS: Final[int] = 60

# G1 constrains contact to whole hours, so an hour is the finest resolution any
# policy can act at. One address per hour across the observation horizon is
# therefore enough to address anything that can happen.
TICKS_PER_DAY: Final[int] = 24
MAX_TICK: Final[int] = OBSERVATION_HORIZON_DAYS * TICKS_PER_DAY - 1

# SPEC §14.2 — the named streams, each with its stated tick unit.
STREAM_TICK_UNITS: Final[dict[str, str]] = {
    "action_outcome": "per action attempt",
    "settle_roll": "per authorisation",
    "reversal_roll": "per settlement",
    "webhook_drop": "per reported outcome",
    "webhook_dup": "per reported outcome",
    "reply_draw": "per contact",
    "patience_draw": "per contact",
    # Reporting-layer ordering. It belongs in the shared space for the same
    # reason `webhook_drop` does: every arm must face the identical distortion
    # on the same case, or the comparison is measuring luck (OQ-34).
    "out_of_order": "per reported outcome",
    # Whether and when a case cures itself, with no arm involved. Shared so the
    # self-cure is identical across arms — that is what makes §14.3's
    # incremental subtraction mean anything.
    "natural_recovery_draw": "per case",
    "natural_recovery_day": "per case",
    # A86 — whether a dispatched `request_mandate_update` is acted on, and how
    # long the customer takes to act. Shared for the same reason as the rest: a
    # customer who would re-authorise does so whichever arm asked, so two arms
    # requesting an update at the same tick must get the same answer.
    #
    # Two addresses rather than one. The success is drawn at the tick the
    # re-authorisation lands and the delay at the tick it was requested, so a
    # single stream would make the delay of a second request the same number
    # that decided the first request's success whenever they coincide. Nothing
    # in the domain couples those, and a coupling nobody declared is exactly
    # what §14.2 exists to remove.
    "mandate_revival_draw": "per pending re-authorisation",
    "mandate_response_delay": "per mandate update dispatched",
    # A89 — whether a contacted customer goes and pays of their own accord, and
    # how long they take. Shared for the same reason as the mandate pair: a
    # customer who would respond to a message does so whichever arm sent it, so
    # two arms contacting at the same tick must get the same answer. Without
    # that, "B2 contacts more and recovers more" would be partly a statement
    # about which arm drew the luckier numbers.
    #
    # Two addresses, not one, for the reason stated above: the response is drawn
    # at the tick it lands and the delay at the tick of the contact, and one
    # stream would couple a later contact's delay to an earlier contact's
    # outcome whenever they coincide.
    "contact_response_draw": "per pending customer response",
    "contact_response_delay": "per contact dispatched",
}
STREAM_NAMES: Final[tuple[str, ...]] = tuple(STREAM_TICK_UNITS)

_MANTISSA_BITS = 53


def derive_unit_float(*parts: object) -> float:
    """Hash `parts` to a float in [0, 1).

    Parts are length-prefixed before hashing, so no two distinct tuples can
    encode to the same bytes — `("ab", "c")` and `("a", "bc")` are different
    addresses and must stay that way.

    The top 53 bits of the digest become the mantissa, which is exactly the
    precision a Python float has. Taking fewer bits would leave the low end of
    the range unreachable; taking more would be discarded silently.
    """
    encoded = [str(part).encode("utf-8") for part in parts]
    payload = b"".join(len(blob).to_bytes(4, "big") + blob for blob in encoded)
    digest = hashlib.blake2b(payload, digest_size=8).digest()
    return (int.from_bytes(digest, "big") >> (64 - _MANTISSA_BITS)) / (1 << _MANTISSA_BITS)


def tick_for(day: int, hour: int = 0) -> int:
    """Address of `hour` on `day`, both zero-based, within the observation horizon."""
    if not 0 <= day < OBSERVATION_HORIZON_DAYS:
        raise ValueError(f"day {day} is outside the {OBSERVATION_HORIZON_DAYS}-day observation horizon")
    if not 0 <= hour < TICKS_PER_DAY:
        raise ValueError(f"hour {hour} is not an hour of the day")
    return day * TICKS_PER_DAY + hour


def stream_value(master_seed: int, case_id: str, stream_name: str, tick: int) -> float:
    """The value at one stream address. Pure, total, and process-independent."""
    if stream_name not in STREAM_TICK_UNITS:
        raise ValueError(
            f"unknown stream {stream_name!r}. A typo here silently breaks common "
            f"random numbers, so the set is closed: {', '.join(STREAM_NAMES)}"
        )
    if isinstance(tick, bool) or not isinstance(tick, int):
        raise TypeError(f"tick must be int, got {type(tick).__name__}")
    if not 0 <= tick <= MAX_TICK:
        raise ValueError(
            f"tick {tick} is outside [0, {MAX_TICK}], the {OBSERVATION_HORIZON_DAYS}-day "
            "observation horizon at hourly resolution"
        )
    return derive_unit_float(master_seed, case_id, stream_name, tick)


class Streams:
    """A batch's streams, bound to one master seed.

    Holds no mutable state. Two `Streams` with the same seed are
    interchangeable, and reading one never affects what another returns.
    """

    __slots__ = ("master_seed",)

    def __init__(self, master_seed: int) -> None:
        if isinstance(master_seed, bool) or not isinstance(master_seed, int):
            raise TypeError(f"master_seed must be int, got {type(master_seed).__name__}")
        self.master_seed = master_seed

    def value(self, case_id: str, stream_name: str, tick: int) -> float:
        return stream_value(self.master_seed, case_id, stream_name, tick)

    def __repr__(self) -> str:
        return f"Streams(master_seed={self.master_seed})"
