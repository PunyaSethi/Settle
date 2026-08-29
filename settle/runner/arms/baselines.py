"""Baselines B1, B2, B3. SPEC §14.1.

Given full capability and denied only intelligence. Same channels, same
templates, the same e-mandate notice — a baseline crippled by omission produces
a fake win, and it is the first thing a judge will check.

B2 is the rival. It is the fixed dunning ladder most production systems
actually run, and if `settle` cannot beat it there is no result.
"""

from typing import Final

from settle.policy.params import hour_offsets
from settle.schema.action import Action, DoNothing, Retry
from settle.schema.enums import ActionType, ArmMode, Channel, Rail
from settle.schema.observed import ObservedCase
from settle.schema.state import CaseState


# Where a verb is available on more than one channel, take the richer one.
# `legal_actions` lists SMS first, so an arm that took the literal first match
# would never use WhatsApp even with consent — crippling by omission, which
# §14.1 forbids just as firmly as denying the notice.
_CHANNEL_PREFERENCE: Final[tuple[Channel, ...]] = (Channel.WHATSAPP, Channel.SMS, Channel.VOICE)


def _first(legal: list[Action], *types: ActionType) -> Action | None:
    """The best legal action matching any of `types`, in preference order."""
    for wanted in types:
        matching = [action for action in legal if action.type is wanted]
        if not matching:
            continue
        for channel in _CHANNEL_PREFERENCE:
            for action in matching:
                if getattr(action, "channel", None) is channel:
                    return action
        return matching[0]
    return None


def _schedule(action: Action, offset: int) -> Action:
    """Put a retry on the declared grid. A71 — nothing invents its own offsets."""
    if isinstance(action, Retry):
        return Retry(at_hour_offset=offset, rail=action.rail)
    return action


class SingleRetryArm:
    """B1 — one retry at the first legal opportunity, then nothing.

    The cheapest thing anyone does. It exists to separate "retrying at all"
    from "retrying well": if B1 recovers most of what OURS recovers, the
    intelligence is not earning its keep.
    """

    name = "B1"
    mode = ArmMode.ENFORCE

    def choose(self, case: ObservedCase, state: CaseState, legal: list[Action]) -> Action:
        if state.attempts_used > 0:
            return DoNothing()
        # On enach a debit outside a notice window is illegal, so B1 must be
        # able to open one. Denying it that would be crippling by omission.
        if case.rail is Rail.ENACH and state.notice_window_until is None:
            notice = _first(legal, ActionType.SERVE_NOTICE)
            if notice is not None:
                return notice
        retry = _first(legal, ActionType.RETRY)
        return _schedule(retry, 0) if retry is not None else DoNothing()


class FixedLadderArm:
    """B2 — three retries at fixed offsets plus generic messages. The rival.

    Ignores the decline class entirely, which is the intelligence it is denied:
    it will keep retrying a revoked mandate for as long as the gates let it.
    It is denied nothing else. It serves the e-mandate notice on enach, it uses
    WhatsApp where consent exists, and its retries sit on the same declared grid
    OURS will search.
    """

    name = "B2"
    mode = ArmMode.ENFORCE

    # Retry, message, retry, message, retry, message. The shape of every
    # dunning ladder in production.
    LADDER: Final[tuple[tuple[ActionType, ...], ...]] = (
        (ActionType.RETRY, ActionType.REQUEST_MANDATE_UPDATE, ActionType.SEND_MESSAGE),
        (ActionType.SEND_MESSAGE, ActionType.REQUEST_MANDATE_UPDATE),
        (ActionType.RETRY, ActionType.SEND_MESSAGE),
        (ActionType.SEND_MESSAGE, ActionType.REQUEST_MANDATE_UPDATE),
        (ActionType.RETRY, ActionType.SEND_MESSAGE),
        (ActionType.SEND_MESSAGE, ActionType.REQUEST_MANDATE_UPDATE),
    )

    def __init__(self) -> None:
        offsets = hour_offsets()
        # Fixed, class-blind, and on the declared grid: now, next morning, two days.
        self.retry_offsets = (offsets[0], offsets[2], offsets[4])

    def choose(self, case: ObservedCase, state: CaseState, legal: list[Action]) -> Action:
        if case.rail is Rail.ENACH and state.notice_window_until is None:
            notice = _first(legal, ActionType.SERVE_NOTICE)
            if notice is not None:
                return notice

        step = state.attempts_used + state.contacts_used
        if step >= len(self.LADDER):
            return DoNothing()

        chosen = _first(legal, *self.LADDER[step])
        if chosen is None:
            return DoNothing()
        return _schedule(chosen, self.retry_offsets[min(state.attempts_used, 2)])


class MaxPressureArm:
    """B3 — every legal action at every opportunity, gates in OBSERVE.

    It will generate violations. That is its purpose: it is the upper bound on
    what an unguarded system does, and the number OURS's compliance column is
    measured against. §13.2 relaxes the compliance stops for it so it can
    actually reach G7 and G8 rather than being stopped before they are consulted.
    """

    name = "B3"
    mode = ArmMode.OBSERVE

    def choose(self, case: ObservedCase, state: CaseState, legal: list[Action]) -> Action:
        acting = [a for a in legal if a.type is not ActionType.DO_NOTHING]
        if not acting:
            return DoNothing()
        # Rotate, so it exercises every verb the class permits rather than
        # hammering one of them.
        step = state.attempts_used + state.contacts_used + state.rail_switches_used
        return _schedule(acting[step % len(acting)], 0)
