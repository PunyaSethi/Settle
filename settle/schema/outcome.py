"""ReportedOutcome — what the agent is told. SPEC §5.5.

`ActualOutcome` is deliberately NOT here. It lives in `settle/sim/truth.py`.
The split is the project: the agent reads `ReportedOutcome` only, and
reconciliation compares the two from outside the agent.

`arrival_count` exists because real webhooks arrive more than once.
"""

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from settle.schema.enums import ReportedStatus

SCHEMA_CONFIG = ConfigDict(strict=True, extra="forbid", frozen=True)


class ReportedOutcome(BaseModel):
    """An outcome as reported through the observability layer (SPEC §6).

    A `captured` status is not a settlement. INV-1 forbids marking a case
    RECOVERED on the strength of this model alone.
    """

    model_config = SCHEMA_CONFIG

    case_id: str = Field(min_length=1)
    at: AwareDatetime
    status: ReportedStatus
    payment_id: str | None = None
    amount_paise: int | None = Field(default=None, ge=0)
    arrival_count: int = Field(ge=1)
