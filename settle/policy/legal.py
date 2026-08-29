"""The legal action set. SPEC §9.

`legal_actions` is the closed set the policy chooses from and EXPLORE samples
from. It excludes actions the decline class forbids. It does NOT exclude actions
a gate would block.

Why the two filters stay separate
---------------------------------
EXPLORE logs `propensity = 1/len(legal_pairs)` at draw time (§10.1). If gates
folded into this set, the denominator would move with a case's contact history,
promise state and opt-out flag — so two cases with the same decline class would
carry different propensities for reasons that have nothing to do with what the
policy could have chosen. The estimator would then be reweighting on the gates
rather than on the action space. Gates filter afterwards, and a blocked
alternative is recorded with the gate that blocked it (`Alternative.block_gate`,
§5.4) rather than vanishing from the set.

LEG-3 asserts the separation directly: changing any gate-relevant field of
`CaseState` must not change what this returns.
"""

from typing import Final

from settle.schema.action import (
    Action,
    DoNothing,
    EscalateHuman,
    RequestMandateUpdate,
    Retry,
    SendMessage,
    ServeNotice,
    SwitchRail,
    VoiceCall,
)
from settle.schema.enums import ActionType, Channel, DeclineClass, Rail
from settle.schema.observed import ObservedCase
from settle.schema.state import CaseState, CaseStatus
from settle.diagnose.taxonomy import classify, viable_actions
from settle.policy.escalation import is_escalation_eligible

# An action the customer perceives. G1, G2 and G7 apply to exactly these and to
# nothing else: a silent retry is a message to the bank, not to a person, and
# gating it on the contact window would suppress the one action §9 calls viable
# for `time_shiftable` at precisely the hour it works.
CONTACT_BEARING: Final[frozenset[ActionType]] = frozenset(
    {
        ActionType.SEND_MESSAGE,
        ActionType.REQUEST_MANDATE_UPDATE,
        ActionType.SERVE_NOTICE,
        ActionType.VOICE_CALL,
        ActionType.ESCALATE_HUMAN,
    }
)

# An action that moves money, or tries to. G3, G4 and G9 apply to these.
DEBIT_BEARING: Final[frozenset[ActionType]] = frozenset(
    {ActionType.RETRY, ActionType.SWITCH_RAIL}
)


def is_contact(action: Action) -> bool:
    """SPEC §12 — whether G1, G2 and G7 apply."""
    return action.type in CONTACT_BEARING


def is_debit(action: Action) -> bool:
    """SPEC §12 — whether G3, G4 and G9 apply."""
    return action.type in DEBIT_BEARING


def message_channels(case: ObservedCase) -> tuple[Channel, ...]:
    """Channels a message may go out on.

    SMS always; WhatsApp only with consent. Consent is a field of the case, not
    of the gate state, which is why it belongs here — and note that no gate in
    §12 covers `consent_whatsapp` or `dnd_flag`. See the CP3 report.
    """
    if case.consent_whatsapp:
        return (Channel.SMS, Channel.WHATSAPP)
    return (Channel.SMS,)


def template_id_for(decline_class: DeclineClass, channel: Channel) -> str:
    """Deterministic placeholder until D3 defines the template library."""
    return f"tpl_{decline_class.value}_{channel.value}"


def legal_actions(case: ObservedCase, state: CaseState) -> list[Action]:
    """The closed set of actions the decline class permits for this case.

    `state` is consulted for one thing only: a stopped case has no legal
    actions (§13, stops are terminal). Every other field of `CaseState` is a
    gate's business, not this function's.
    """
    if state.status is CaseStatus.STOPPED:
        return []

    decline_class = classify(case.decline_code)
    # Eligibility reads only `ObservedCase` (§2.1), so consulting it here does
    # not make this function state-dependent and LEG-3 keeps holding.
    viable = viable_actions(decline_class, is_escalation_eligible(case))
    actions: list[Action] = []

    if ActionType.DO_NOTHING in viable:
        actions.append(DoNothing())

    if ActionType.RETRY in viable:
        actions.append(Retry(at_hour_offset=0, rail=case.rail))

    if ActionType.SWITCH_RAIL in viable:
        actions.extend(SwitchRail(to=rail) for rail in Rail if rail is not case.rail)

    if ActionType.SEND_MESSAGE in viable:
        actions.extend(
            SendMessage(channel=channel, template_id=template_id_for(decline_class, channel))
            for channel in message_channels(case)
        )

    if ActionType.REQUEST_MANDATE_UPDATE in viable:
        actions.extend(
            RequestMandateUpdate(channel=channel) for channel in message_channels(case)
        )

    # §9 lists `serve_notice` as viable for the classes that can debit (A57).
    # It is only meaningful on `enach`, which is the rail G9 governs — serving
    # notice on a card debit would spend a contact and buy nothing.
    if ActionType.SERVE_NOTICE in viable and case.rail is Rail.ENACH:
        actions.extend(
            ServeNotice(channel=channel) for channel in message_channels(case)
        )

    if ActionType.ESCALATE_HUMAN in viable:
        actions.append(EscalateHuman())

    if ActionType.VOICE_CALL in viable:
        actions.append(VoiceCall())

    return actions
