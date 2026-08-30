"""Adversarial debtors. SPEC §8 — DIFFERENTIATOR.

Six behaviours layered on top of `intent_type`. They shape replies and patience
and nothing else: a behaviour must never be readable by the agent, which is why
this module lives under `settle/sim/` and GEN-2 fails the build if anything
under `settle/agent/`, `settle/policy/` or `settle/schema/` imports it.

The behaviours exist because a simulation of cooperative payers measures
nothing. `pay_then_complain` in particular is the pair to SF-2: a customer who
has already paid, whose confirmation never arrived, and who reports harassment
when contacted again.
"""

from __future__ import annotations

from enum import Enum
from typing import Final

from pydantic import BaseModel, ConfigDict

from settle.schema.enums import DebtorBehaviour
from settle.sim.generator import PARAMS, pick_from_mix
from settle.sim.streams import Streams
from settle.sim.truth import HiddenTruth


class ReplyKind(str, Enum):
    """What a contact got back. Text generation is §11's problem, not this one."""

    SILENCE = "silence"
    HEDGED = "hedged"
    PROMISE = "promise"
    OPT_OUT = "opt_out"
    DISPUTE = "dispute"
    COMPLAINT = "complaint"


class Reply(BaseModel):
    """One reply, plus what it cost in patience."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    kind: ReplyKind
    promise_in_days: int | None = None
    patience_spent: int = 1


# P(reply kind | behaviour) lives in PARAMS with PRIORS rows. It drives the
# promise-kept rate and the opt-outs-induced count, both of which are headline
# metrics in §14.4, so INV-10 applies (SPEC §15).
REPLY_MIX: Final[dict[DebtorBehaviour, dict[str, float]]] = {
    behaviour: {
        key.rsplit(".", 1)[1]: weight
        for key, weight in PARAMS.items()
        if key.startswith(f"reply_mix.{behaviour.value}.")
    }
    for behaviour in DebtorBehaviour
}

# Contacts before `go_silent` stops answering and `opt_out_midway` opts out,
# and what a complaint costs in patience. Both live in PARAMS with PRIORS rows:
# they move the opt-outs-induced count, which is a §14.4 headline (SPEC §15).
DISENGAGE_AFTER_CONTACTS: Final[int] = int(PARAMS["debtor.disengage_after_contacts"])
COMPLAINT_PATIENCE_COST: Final[int] = int(PARAMS["patience.complaint_cost"])


def reply(
    case_id: str,
    truth: HiddenTruth,
    behaviour: DebtorBehaviour,
    contact_index: int,
    tick: int,
    streams: Streams,
) -> Reply:
    """What this debtor says to contact number `contact_index`.

    Reads `reply_draw` and `patience_draw` at `tick`, so two arms that reach the
    same contact index at the same tick get the same reply (STR-3).
    """
    if contact_index >= truth.patience_budget:
        return Reply(kind=ReplyKind.SILENCE, patience_spent=1)

    if behaviour is DebtorBehaviour.GO_SILENT and contact_index >= DISENGAGE_AFTER_CONTACTS:
        return Reply(kind=ReplyKind.SILENCE, patience_spent=1)

    if behaviour is DebtorBehaviour.OPT_OUT_MIDWAY and contact_index >= DISENGAGE_AFTER_CONTACTS:
        return Reply(kind=ReplyKind.OPT_OUT, patience_spent=1)

    u = streams.value(case_id, "reply_draw", tick)
    kind = ReplyKind(pick_from_mix(f"reply_mix.{behaviour.value}", u))

    promise_in_days = None
    if kind is ReplyKind.PROMISE:
        # A promise lands somewhere in the next fortnight. Whether it is kept is
        # `truth`, not this draw.
        promise_in_days = 1 + int(streams.value(case_id, "patience_draw", tick) * 14)

    spent = COMPLAINT_PATIENCE_COST if kind is ReplyKind.COMPLAINT else 1
    return Reply(kind=kind, promise_in_days=promise_in_days, patience_spent=spent)


# The words a debtor actually sends. Deliberately messy Hinglish: the classifier
# has to survive what a real inbox contains, not a canonical phrasing we chose to
# be easy. A promise encodes its own horizon so the round trip is testable — the
# debtor says "N din mein", the parser must recover N.
_REPLY_TEXT: Final[dict[ReplyKind, tuple[str, ...]]] = {
    ReplyKind.SILENCE: ("",),
    ReplyKind.HEDGED: (
        "Haan theek hai, dekhta hoon, baad mein baat karte hain",
        "abhi thoda tight hai, try karunga",
        "dekhte hain",
        "ok will see",
    ),
    ReplyKind.PROMISE: (
        "{n} din mein kar dunga",
        "haan {n} din mein bhej dunga bhai",
        "{n} din me ho jayega",
    ),
    ReplyKind.OPT_OUT: (
        "Mujhe baar baar mat call kariye, main khud dekh lunga",
        "STOP",
        "please band karo ye messages",
    ),
    ReplyKind.DISPUTE: (
        "ye galat charge hai maine nahi kiya",
        "I want to dispute this",
        "chargeback kar raha hoon",
    ),
    ReplyKind.COMPLAINT: (
        "maine kar diya payment, ab mat pareshan karo",
        "already paid, stop messaging",
    ),
}


def reply_text(reply: Reply, case_id: str, tick: int, streams: Streams) -> str:
    """Render a reply as the text the agent actually receives.

    The debtor writes; §11's classifier reads. Keeping the two apart is what
    makes the classifier testable against something it did not author.
    """
    bank = _REPLY_TEXT[reply.kind]
    index = int(streams.value(case_id, "reply_draw", tick) * len(bank)) % len(bank)
    template = bank[index]
    if reply.kind is ReplyKind.PROMISE and reply.promise_in_days is not None:
        return template.format(n=reply.promise_in_days)
    return template
