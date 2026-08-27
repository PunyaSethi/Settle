"""ObservedCase — everything the agent is allowed to see. SPEC §5.1.

Exactly the fields in §5.1, no additions. Anything the agent would like to know
but cannot observe belongs in `settle/sim/truth.py`, not here.
"""

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from settle.schema.enums import Language, MandateState, Rail

SCHEMA_CONFIG = ConfigDict(strict=True, extra="forbid", frozen=True)


class ObservedCase(BaseModel):
    """A failed recurring debit as the agent sees it.

    `created_at` is the as_of anchor for every downstream time calculation.
    Never wall clock, never file mtime (SPEC §5.1).

    Money is always paise as `int`. Never float — a rounding error in a
    recovery total is indistinguishable from a bug in the policy.
    """

    model_config = SCHEMA_CONFIG

    case_id: str = Field(min_length=1)
    created_at: AwareDatetime
    customer_id: str = Field(min_length=1)
    amount_paise: int = Field(ge=0)
    rail: Rail
    decline_code: str
    decline_reason: str
    attempt_number: int = Field(ge=1)
    mandate_state: MandateState
    mandate_cap_paise: int | None = Field(default=None, ge=0)
    tenure_months: int = Field(ge=0)
    prior_failures: int = Field(ge=0)
    prior_recoveries: int = Field(ge=0)
    plan_value_paise: int = Field(ge=0)
    # 1..28, the same domain as HiddenTruth.payday_day: this field is an
    # estimate of that one, so it cannot have a wider domain (SPEC §5.1, A39).
    observed_credit_day: int | None = Field(default=None, ge=1, le=28)
    consent_whatsapp: bool
    dnd_flag: bool
    language: Language
