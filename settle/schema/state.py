"""CaseState — what the gates read. SPEC §5.7.

Gates and stops are pure functions. That is only possible because every quantity
they need is a recorded field here rather than something derived by scanning the
ledger: a gate that had to walk history would depend on how much history it was
handed, and two arms at the same point would disagree.

The evaluation instant is `case.created_at + tick hours`. It comes from the
case's own anchor, never from a clock, which is what makes a run replayable from
the ledger and what GEN-5/PURE-1 enforce structurally.

Immutability
------------
The model is frozen and its containers are immutable: `contact_history` is a
`tuple`, `dispatched_keys` a `frozenset`. §5.7 writes them as `list` and `set`,
but a frozen model holding a mutable container is only half frozen, and a `set`
of strings serialises in an order that varies with `PYTHONHASHSEED` — which
would make two runs of the same batch disagree. `dispatched_keys` therefore
serialises sorted. See the CP3 report.
"""

from datetime import date, datetime, timedelta
from enum import Enum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_serializer

from settle.schema.action import Action
from settle.schema.enums import ArmMode, StopClass

SCHEMA_CONFIG = ConfigDict(strict=True, extra="forbid", frozen=True)


class CaseStatus(str, Enum):
    """SPEC §5.7. A stopped case is terminal — §13 admits no resurrection."""

    OPEN = "open"
    STOPPED = "stopped"


class Scheduled(BaseModel):
    """One action committed to a future tick. SPEC §5.7.

    A commitment, not a dispatch. `retry(at_hour_offset=72)` means "debit in
    three days", and firing it now would make the offset a label rather than a
    behaviour — which is what it was until this model existed.

    At most one is pending per case. A second choice replaces it and the
    replacement is logged, because a queue of scheduled actions is a queue of
    decisions taken under circumstances that no longer hold.
    """

    model_config = SCHEMA_CONFIG

    action: Action
    due_tick: int = Field(ge=0)
    scheduled_at: int = Field(ge=0)


class CaseState(BaseModel):
    """Everything a gate or stop is allowed to know about a case's progress."""

    model_config = SCHEMA_CONFIG

    case_id: str = Field(min_length=1)
    arm: str = Field(min_length=1)
    arm_mode: ArmMode

    status: CaseStatus = CaseStatus.OPEN
    stop_reason: str | None = None
    stop_class: StopClass | None = None

    # Retries only. A rail switch is a change of instrument, not a retry, and
    # counting it against a class retry budget makes `switch_rail` unusable for
    # `auth_abandoned` — the one class whose recovery path it is (A67).
    attempts_used: int = Field(default=0, ge=0)
    rail_switches_used: int = Field(default=0, ge=0)
    # G4 counts submissions to the card network, whichever verb produced them:
    # a retry on card and a switch *to* card are both submissions, a switch away
    # is not (A70). Separate from the two above because G4 and G10 are counting
    # different things.
    card_submissions_used: int = Field(default=0, ge=0)
    contacts_used: int = Field(default=0, ge=0)
    contact_history: tuple[AwareDatetime, ...] = ()
    last_contact_at: AwareDatetime | None = None

    opted_out: bool = False
    disputed: bool = False

    promise_date: date | None = None
    promise_logged_at: AwareDatetime | None = None

    notice_window_until: AwareDatetime | None = None

    dispatched_keys: frozenset[str] = frozenset()

    # S1. A settlement record, never an authorisation (INV-1). Recorded here
    # rather than passed to `check_stops` because a caller-supplied bool is
    # inference by another name, and §5.7's rule is that state transitions are
    # recorded, not inferred.
    settled: bool = False
    settled_at: AwareDatetime | None = None

    # At most one pending commitment. Gates are re-evaluated when it comes due:
    # a customer can opt out, promise or dispute between the choice and the
    # firing, and an action that fires without re-gating is a compliance hole.
    scheduled: Scheduled | None = None

    tick: int = Field(default=0, ge=0)

    @field_serializer("dispatched_keys")
    def _sorted_keys(self, keys: frozenset[str]) -> list[str]:
        """Sorted, because a set's iteration order is not a property of its value.

        Left unsorted, the same batch would serialise differently in two
        processes and any hash taken over this state would disagree with itself.
        """
        return sorted(keys)


def as_of(created_at: datetime, state: CaseState) -> datetime:
    """The instant a gate evaluates against. SPEC §5.7.

    Derived from the case's `created_at` anchor and the recorded tick. Never a
    clock: that is the whole reason gates can be replayed.
    """
    return created_at + timedelta(hours=state.tick)
