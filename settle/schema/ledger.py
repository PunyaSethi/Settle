"""LedgerEntry — append-only, hash-chained. SPEC §5.6.

Written to `out/audit.jsonl`. The chain is what lets reconciliation contradict
the executor: an entry cannot be revised after the fact without breaking every
hash that follows it.

INV-5: the audit entry is written BEFORE dispatch, never after. That ordering
is a property of the writer, not of this model, but it is the reason `kind` has
separate `decision`, `gate_check` and `dispatch` members.
"""

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from settle.schema.enums import Actor, LedgerKind

SCHEMA_CONFIG = ConfigDict(strict=True, extra="forbid", frozen=True)


class LedgerEntry(BaseModel):
    """One link in the audit chain.

    `hash` is `sha256(prev_hash + canonical_json(entry_without_hash))`
    (SPEC §5.6). The chain is verified by `python -m settle.audit.verify`.
    """

    model_config = SCHEMA_CONFIG

    seq: int = Field(ge=0)
    case_id: str = Field(min_length=1)
    at: AwareDatetime
    kind: LedgerKind
    actor: Actor
    payload: dict[str, object] = Field(default_factory=dict)
    reason_code: str
    prev_hash: str
    hash: str

    arm: str = Field(min_length=1)
