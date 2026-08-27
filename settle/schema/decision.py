"""Decision — one deliberate choice, with its rejected alternatives. SPEC §5.4.

Recording the alternatives and their expected values is what makes a decision
auditable rather than merely logged: a reviewer can see what the policy
considered and declined, not just what it did.
"""

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from settle.schema.action import Action
from settle.schema.enums import ArmMode, ChosenBy

SCHEMA_CONFIG = ConfigDict(strict=True, extra="forbid", frozen=True)


class Alternative(BaseModel):
    """One option the policy considered and did not take. SPEC §5.4.

    A bare triple is unreadable in audit JSONL, which is a reviewed
    deliverable. Recording why a rejected alternative was rejected — and
    whether it was rejected on economics or blocked by a gate — is the
    difference between a decision log and a number dump.
    """

    model_config = SCHEMA_CONFIG

    action: Action
    p_success: float = Field(ge=0.0, le=1.0)
    ev_paise: int
    legal: bool
    block_gate: str | None = None

    @model_validator(mode="after")
    def _block_gate_matches_legal(self) -> "Alternative":
        """`block_gate` names the gate, and only when there was one.

        SPEC §5.4 states the pairing; enforcing it here is what stops an
        illegal alternative reaching the audit log with no reason attached,
        which would be indistinguishable from an economic rejection.
        """
        if self.legal and self.block_gate is not None:
            raise ValueError("a legal alternative was not blocked, so block_gate must be null")
        if not self.legal and self.block_gate is None:
            raise ValueError("an illegal alternative must name the gate that blocked it")
        return self


class Decision(BaseModel):
    """A single decision for a single case at a single instant.

    `expected_value` is paise and may be negative — that is the whole point of
    `do_nothing` being selectable.

    `propensity` is written by the EXPLORE sampler at draw time as
    `1/len(legal_pairs)` (SPEC §10.1) and is null for every other arm. It is
    never recomputed analytically after the fact: legality is joint over action
    and hour, so it does not factor, and any formula would be free to drift
    from the sampler.
    """

    model_config = SCHEMA_CONFIG

    decision_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    at: AwareDatetime
    action: Action
    p_success: float = Field(ge=0.0, le=1.0)
    expected_value: int
    alternatives: list[Alternative] = Field(default_factory=list)
    chosen_by: ChosenBy
    reason_code: str = Field(min_length=1)

    arm: str = Field(min_length=1)
    propensity: float | None = Field(default=None, gt=0.0, le=1.0)
    arm_mode: ArmMode
