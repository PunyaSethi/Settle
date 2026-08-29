"""The hash-chained ledger. SPEC §5.6, INV-5, INV-6.

`hash = sha256(prev_hash + canonical_json(entry_without_hash))`. Every entry
references the one before it, so an entry cannot be revised after the fact
without breaking every hash that follows.

Append-only, and structurally so
--------------------------------
There is no update path and no delete path — not "we don't call them", but "the
methods do not exist" (LED-3). A ledger with an `UPDATE` is not a ledger, and
the reconciliation pass in §7 exists precisely so the executor's own account of
events can be contradicted.

Write-ahead
-----------
INV-5: the audit entry for a dispatch is written BEFORE the dispatch executes,
and flushed. If the process dies mid-dispatch there is a record of intent.
Writing afterwards loses it, and the next run contacts the customer again — the
SF-3 harassment case, caused by the audit system meant to prevent it.
"""

import hashlib
import json
from pathlib import Path
from typing import Any, Final

from settle.schema.canonical import canonical_json
from settle.schema.enums import Actor, LedgerKind
from settle.schema.ledger import LedgerEntry

# The chain's anchor. An all-zero hash is unmistakably "no predecessor" rather
# than an empty string that could be confused with a serialisation bug.
GENESIS_HASH: Final[str] = "0" * 64


def entry_hash(prev_hash: str, entry: LedgerEntry) -> str:
    """SPEC §5.6, exactly.

    The entry is hashed without its own `hash` field — including it would be
    circular — and with `prev_hash` prepended so the chain is a chain rather
    than a set of independently verifiable records.
    """
    payload = entry.model_dump(mode="python")
    payload.pop("hash", None)
    return hashlib.sha256(prev_hash.encode("ascii") + canonical_json(payload)).hexdigest()


class Ledger:
    """An append-only JSONL ledger.

    Deliberately exposes exactly one mutation: `append`. There is no `update`,
    no `delete`, no `truncate` and no way to reach the underlying handle.
    """

    __slots__ = ("_path", "_handle", "_seq", "_prev_hash")

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._path.open("a", encoding="utf-8")
        self._seq = 0
        self._prev_hash = GENESIS_HASH

    @property
    def path(self) -> Path:
        return self._path

    @property
    def seq(self) -> int:
        """The seq the next entry will take."""
        return self._seq

    @property
    def head(self) -> str:
        return self._prev_hash

    def append(
        self,
        *,
        case_id: str,
        at,
        kind: LedgerKind,
        actor: Actor,
        payload: dict[str, Any],
        reason_code: str,
        arm: str,
    ) -> LedgerEntry:
        """Write one entry and flush it.

        Flushed rather than buffered because INV-5 is about what survives a
        crash. An entry sitting in a userspace buffer when the process dies is
        an entry that was never written.
        """
        unhashed = LedgerEntry(
            seq=self._seq,
            case_id=case_id,
            at=at,
            kind=kind,
            actor=actor,
            payload=payload,
            reason_code=reason_code,
            prev_hash=self._prev_hash,
            hash="",
            arm=arm,
        )
        entry = unhashed.model_copy(update={"hash": entry_hash(self._prev_hash, unhashed)})

        self._handle.write(entry.model_dump_json() + "\n")
        self._handle.flush()

        self._seq += 1
        self._prev_hash = entry.hash
        return entry

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def read_entries(path: str | Path) -> list[LedgerEntry]:
    """Every entry in a ledger file, in order."""
    with Path(path).open(encoding="utf-8") as handle:
        return [LedgerEntry.model_validate_json(line) for line in handle if line.strip()]


class ChainBreak(Exception):
    """A ledger that does not verify. Carries the seq so it can be found."""

    def __init__(self, seq: int, reason: str) -> None:
        super().__init__(f"chain broken at seq {seq}: {reason}")
        self.seq = seq
        self.reason = reason


def verify_entries(entries: list[LedgerEntry]) -> None:
    """Re-derive every hash. Raises `ChainBreak` at the first failure.

    Checks three things, because a chain can break in three ways: a seq out of
    order, a `prev_hash` that does not match the previous entry, and a `hash`
    that does not match its own content.
    """
    prev_hash = GENESIS_HASH
    for position, entry in enumerate(entries):
        if entry.seq != position:
            raise ChainBreak(entry.seq, f"expected seq {position}, found {entry.seq}")
        if entry.prev_hash != prev_hash:
            raise ChainBreak(entry.seq, "prev_hash does not match the preceding entry")
        recomputed = entry_hash(prev_hash, entry)
        if entry.hash != recomputed:
            raise ChainBreak(entry.seq, "entry content does not match its hash")
        prev_hash = entry.hash


def verify_file(path: str | Path) -> int:
    """Verify a ledger on disk. Returns the number of entries checked."""
    entries = read_entries(path)
    verify_entries(entries)
    return len(entries)
