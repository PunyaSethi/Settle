"""Action — the closed verb set. SPEC §5.3.

Modelled as a discriminated union rather than one model with optional
parameters, so that "closed verb set" is a property of the type rather than a
property of a validator someone can forget to run.

Actions carry no amount field. Partial or reduced debits against
`mandate_cap_paise` are deliberately out of scope (SPEC §5.3): a continuous
amount dimension multiplies the action space and the estimator's training
burden. Asserted by SCH-6.
"""

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

from settle.schema.enums import ActionType, Channel, Rail

SCHEMA_CONFIG = ConfigDict(strict=True, extra="forbid", frozen=True)


class DoNothing(BaseModel):
    """First-class action. Any arm that cannot emit it is a baseline, not a
    policy (SPEC §5.3)."""

    model_config = SCHEMA_CONFIG
    type: Literal[ActionType.DO_NOTHING] = ActionType.DO_NOTHING


class Retry(BaseModel):
    """`retry(at_hour_offset: int, rail: enum)`.

    The offset is relative to the decision's `at`, not to wall clock.
    """

    model_config = SCHEMA_CONFIG
    type: Literal[ActionType.RETRY] = ActionType.RETRY
    at_hour_offset: int = Field(ge=0)
    rail: Rail


class SwitchRail(BaseModel):
    """`switch_rail(to: enum)`."""

    model_config = SCHEMA_CONFIG
    type: Literal[ActionType.SWITCH_RAIL] = ActionType.SWITCH_RAIL
    to: Rail


class SendMessage(BaseModel):
    """`send_message(channel: enum, template_id: str)`."""

    model_config = SCHEMA_CONFIG
    type: Literal[ActionType.SEND_MESSAGE] = ActionType.SEND_MESSAGE
    channel: Channel
    template_id: str = Field(min_length=1)


class RequestMandateUpdate(BaseModel):
    """`request_mandate_update(channel: enum)`."""

    model_config = SCHEMA_CONFIG
    type: Literal[ActionType.REQUEST_MANDATE_UPDATE] = ActionType.REQUEST_MANDATE_UPDATE
    channel: Channel


class ServeNotice(BaseModel):
    """`serve_notice(channel: Channel)`.

    An explicit verb, not an executor side effect. Under G9 a served notice is
    a full contact costing G2 budget and patience_budget (SPEC §20). If the
    executor served notices implicitly the policy would never price them, and
    the notice-then-debit sequencing G9 exists to force would be invisible to
    the decision. On `enach` the agent must choose to spend a contact on notice
    before it can legally debit outside an active window.
    """

    model_config = SCHEMA_CONFIG
    type: Literal[ActionType.SERVE_NOTICE] = ActionType.SERVE_NOTICE
    channel: Channel


class EscalateHuman(BaseModel):
    """`escalate_human`."""

    model_config = SCHEMA_CONFIG
    type: Literal[ActionType.ESCALATE_HUMAN] = ActionType.ESCALATE_HUMAN


class VoiceCall(BaseModel):
    """`voice_call` — high-value slice only (SPEC §5.3)."""

    model_config = SCHEMA_CONFIG
    type: Literal[ActionType.VOICE_CALL] = ActionType.VOICE_CALL


Action = Annotated[
    Union[
        DoNothing,
        Retry,
        SwitchRail,
        SendMessage,
        RequestMandateUpdate,
        ServeNotice,
        EscalateHuman,
        VoiceCall,
    ],
    Field(discriminator="type"),
]

ACTION_MODELS: tuple[type[BaseModel], ...] = (
    DoNothing,
    Retry,
    SwitchRail,
    SendMessage,
    RequestMandateUpdate,
    ServeNotice,
    EscalateHuman,
    VoiceCall,
)
