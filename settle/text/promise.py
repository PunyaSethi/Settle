"""Promise extraction, as a trace. SPEC §11, CP7.0's contract unchanged.

    extract(transcript, anchor) -> Extraction

This module adds no new judgement. `settle.text.classify` already implements the
contract and this is the layer that shows its working: every span the locator
found, what each parsed to, which ones validation rejected and why, the verdict,
and the action — or the explicit absence of one.

The contract, unchanged
-----------------------
Something **locates** a span; deterministic code **parses and validates** it.
The locator never produces a date and the parser never decides what is a date
worth reading. Disagreement becomes a confirmation turn, never a silent guess.

That split is why this file is thin. `classify.find_date_spans` locates,
`classify.parse_date_span` evaluates, `classify.validate` bounds, and
`classify.classify_reply` decides. Re-implementing any of them here would give
the batch path and the voice path two different notions of what a promise is,
and the first divergence would be invisible until a demo.

What this module *does* add is one more span kind. "agle mahine" — next month —
is a date reference the batch classifier has no pattern for, because a reply
saying only "next month" carries no day and resolves to nothing it could act on.
For voice it matters anyway: clip 1 is a self-correction, "agle mahine" followed
by "pandrah tareekh", and a locator blind to the first half cannot demonstrate
that the second half wins. Locating it and then rejecting it at validation is the
honest shape — the span was seen, priced, and found wanting.

Anchored, never clocked
-----------------------
`anchor` is the case's `created_at`. A relative date parsed against wall time
resolves differently on every replay, and the ledger stops reproducing. There is
no `date.today()` in this file and VOI-4 asserts it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final

from settle.text.classify import (
    Confidence,
    ReplyKind,
    ReplyVerdict,
    classify_reply,
    find_date_spans,
    normalise,
    parse_date_span,
    validate,
)

__all__ = ["Extraction", "LocatedSpan", "ParsedSpan", "extract"]

# "agle mahine", "next month". Located for voice and always rejected: a month
# with no day is not a commitment this system can act on. See the docstring.
_NEXT_MONTH: Final[re.Pattern[str]] = re.compile(
    r"\b(agle\s+mah?[ie]+ne|agle\s+month|next\s+month|अगले\s+महीने)\b", re.IGNORECASE
)
NEXT_MONTH_KIND: Final[str] = "next_month"

# What each verdict does to the case. Stated here rather than inferred at the
# call site, so the trace can show the consequence next to the reason.
ACTIONS: Final[dict[ReplyKind, dict[str, str]]] = {
    ReplyKind.PROMISE: {
        "action": "log_promise",
        "effect": "promise_date set; G6 suppresses contact until that date",
    },
    ReplyKind.OPT_OUT: {
        "action": "set_opted_out",
        "effect": "opted_out set; S4 stops the case, G7 blocks every channel",
    },
    ReplyKind.DISPUTE: {
        "action": "set_disputed",
        "effect": "disputed set; S5 stops the case",
    },
    ReplyKind.PAYMENT_CLAIM: {
        "action": "reconcile",
        "effect": "claim recorded; reconciliation decides, not the claim",
    },
    ReplyKind.HEDGED: {
        "action": "none",
        "effect": (
            "nothing is set. No promise_date, no suppression window, no stop. "
            "The case proceeds exactly as if the customer had not replied."
        ),
    },
    ReplyKind.UNCLEAR: {
        "action": "confirmation_turn",
        "effect": "escalated for confirmation; nothing is set on a guess",
    },
}


@dataclass(frozen=True)
class LocatedSpan:
    """A candidate the locator found. It carries no value — that is the point."""

    kind: str
    text: str
    start: int

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "text": self.text, "start": self.start}


@dataclass(frozen=True)
class ParsedSpan:
    """One located span, evaluated and validated. Rejections keep their reason."""

    span: LocatedSpan
    parsed: date | None
    validated: date | None
    rejected_because: str | None

    @property
    def accepted(self) -> bool:
        return self.validated is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.span.as_dict(),
            "parsed_date": self.parsed.isoformat() if self.parsed else None,
            "validated_date": self.validated.isoformat() if self.validated else None,
            "accepted": self.accepted,
            "rejected_because": self.rejected_because,
        }


@dataclass(frozen=True)
class Extraction:
    """The whole chain, in the order it happened."""

    transcript: str
    anchor: date
    spans: list[ParsedSpan] = field(default_factory=list)
    verdict: ReplyVerdict = field(default_factory=lambda: ReplyVerdict(ReplyKind.UNCLEAR))

    @property
    def promise_date(self) -> date | None:
        return self.verdict.promise_date

    @property
    def action(self) -> str:
        return ACTIONS[self.verdict.kind]["action"]

    @property
    def sets_nothing(self) -> bool:
        return self.action == "none"

    def as_dict(self) -> dict[str, Any]:
        chosen = next((p for p in reversed(self.spans) if p.accepted), None)
        return {
            "transcript": self.transcript,
            "anchor": self.anchor.isoformat(),
            "spans_located": [p.span.as_dict() for p in self.spans],
            "spans_evaluated": [p.as_dict() for p in self.spans],
            "n_spans": len(self.spans),
            "chosen_span": chosen.span.as_dict() if chosen else None,
            "verdict": {
                "kind": self.verdict.kind.value,
                "confidence": self.verdict.confidence.value,
                "matched_span": self.verdict.matched_span,
                "promise_date": (
                    self.verdict.promise_date.isoformat()
                    if self.verdict.promise_date else None
                ),
            },
            "action": self.action,
            "effect": ACTIONS[self.verdict.kind]["effect"],
            "sets_nothing": self.sets_nothing,
        }


def locate(text: str) -> list[LocatedSpan]:
    """Every date-ish span, in source order. Locates only; evaluates nothing.

    Source order is load-bearing. A self-correction resolves to the LAST span
    that validates, so the order the locator returns is what decides which of
    "agle mahine" and "pandrah tareekh" the system acts on.
    """
    cleaned = normalise(text)
    spans = [
        LocatedSpan(kind=kind, text=span, start=cleaned.index(span))
        for kind, span in find_date_spans(cleaned)
    ]
    spans.extend(
        LocatedSpan(kind=NEXT_MONTH_KIND, text=match.group(0), start=match.start())
        for match in _NEXT_MONTH.finditer(cleaned)
    )
    return sorted(spans, key=lambda s: s.start)


def evaluate(span: LocatedSpan, anchor: date) -> ParsedSpan:
    """Parse and validate one span against the anchor."""
    if span.kind == NEXT_MONTH_KIND:
        # Located, then refused: a month with no day is not a date. Saying so
        # explicitly is better than never looking, because the trace can then
        # show the customer's first answer being considered and set aside.
        return ParsedSpan(span, None, None, "a month with no day is not a commitment")

    parsed = parse_date_span(span.kind, span.text, anchor)
    if parsed is None:
        return ParsedSpan(span, None, None, "no date could be parsed from this span")
    validated = validate(parsed, anchor)
    if validated is None:
        reason = (
            "not in the future" if parsed <= anchor
            else "beyond the 30-day promise horizon"
        )
        return ParsedSpan(span, parsed, None, reason)
    return ParsedSpan(span, parsed, validated, None)


def extract(transcript: str | None, anchor: date) -> Extraction:
    """The full trace for one transcript. Pure, total, and anchored.

    The verdict comes from `classify_reply` — the same function the batch path
    uses — so the voice lab cannot drift from the simulation's notion of a
    promise. Everything else here exists to show how it got there.
    """
    text = transcript or ""
    verdict = classify_reply(text, anchor)
    spans = [evaluate(span, anchor) for span in locate(text)]
    return Extraction(transcript=text, anchor=anchor, spans=spans, verdict=verdict)
