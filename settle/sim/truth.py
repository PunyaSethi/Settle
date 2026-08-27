"""Hidden truth. SPEC §5.2 and §5.5.

NEVER import this module from `settle/agent/`, `settle/policy/` or
`settle/schema/`.

INV-8 says hidden truth is never readable by any module under `settle/agent/`.
That invariant is enforced structurally rather than by discipline: these models
live here, outside `settle/schema/`, so an agent module cannot reach them by
importing a contract. SCH-3 walks the AST of every module under
`settle/schema/` and fails the build if one of them imports `settle.sim`.

The simulator constructs these. The observability layer (SPEC §6) decides how
much of `ActualOutcome` ever becomes a `ReportedOutcome`, and reconciliation
(SPEC §7) compares the two from outside the agent. That comparison is the
project.
"""

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from settle.schema.enums import IntentType

TRUTH_CONFIG = ConfigDict(strict=True, extra="forbid", frozen=True)


class HiddenTruth(BaseModel):
    """What is actually true about a case. SPEC §5.2.

    `true_recoverability` is the quantity the estimator is trying to
    approximate and must never observe.

    `will_settle` and `will_reverse` are the two facts that make INV-1 more
    than pedantry: an authorisation is not a settlement, and a settlement is
    not final.
    """

    model_config = TRUTH_CONFIG

    case_id: str = Field(min_length=1)
    true_recoverability: float = Field(ge=0.0, le=1.0)
    intent_type: IntentType
    patience_budget: int = Field(ge=0)
    payday_day: int = Field(ge=1, le=28)
    response_fn_params: dict[str, float] = Field(default_factory=dict)
    will_settle: bool
    settlement_lag_h: int = Field(ge=0)
    will_reverse: bool


class ActualOutcome(BaseModel):
    """What the money actually did. SPEC §5.5.

    Compare against `ReportedOutcome` to detect the silent failures of §7. The
    agent never sees this; only `settle/recon/` does.
    """

    model_config = TRUTH_CONFIG

    case_id: str = Field(min_length=1)
    at: AwareDatetime
    settled: bool
    settled_at: AwareDatetime | None = None
    reversed: bool = False
    amount_paise: int | None = Field(default=None, ge=0)
