"""Deterministic reply classification. SPEC §11.

No LLM call. Not "we don't call one yet" — this module cannot: it imports no
client and makes no network call, and RPL-6 asserts it.

The contract, carried over from prior production work:

- something **locates** a span; deterministic code **parses and validates** the
  value from it. Even here, where the locator is a regex rather than a model,
  the two stay separate — `find_date_spans` finds, `parse_date_span` evaluates,
  and the classifier never constructs a date itself.
- **A hedged reply is not a promise.** "dekhta hoon", "baad mein baat karte
  hain", "try karunga" set nothing. Wrongly logging a brush-off as a promise
  suppresses contact for weeks under G6, which is a worse failure than missing a
  real promise: the customer who would have paid never hears from us again.
- Anything unresolved returns `unclear` and is counted. That count is the LLM
  escalation rate.

Devanagari numerals are accepted alongside Latin: `gpt-transcribe` returns
Devanagari for Hindi speech regardless of the `language` parameter, so any date
parser downstream of it that assumes ASCII digits will silently find nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from typing import Final

DEVANAGARI_DIGITS: Final[dict[str, str]] = {
    "०": "0", "१": "1", "२": "2", "३": "3", "४": "4",
    "५": "5", "६": "6", "७": "7", "८": "8", "९": "9",
}

# How far ahead a promise may be and still be a promise. Beyond this it is not a
# commitment, it is a brush-off with a number attached.
PROMISE_HORIZON_DAYS: Final[int] = 30


class ReplyKind(str, Enum):
    OPT_OUT = "opt_out"
    PROMISE = "promise"
    DISPUTE = "dispute"
    PAYMENT_CLAIM = "payment_claim"
    HEDGED = "hedged"
    UNCLEAR = "unclear"


class Confidence(str, Enum):
    HIGH = "high"
    LOW = "low"


@dataclass(frozen=True)
class ReplyVerdict:
    kind: ReplyKind
    promise_date: date | None = None
    confidence: Confidence = Confidence.HIGH
    matched_span: str | None = None


# Unambiguous. These resolve here and never escalate.
_OPT_OUT = re.compile(
    r"\b(stop|unsubscribe|opt.?out|do not contact|dont contact|remove me|"
    r"mat karo|mat kariye|mat call|band karo|band kar|pareshan mat)\b",
    re.IGNORECASE,
)
_DISPUTE = re.compile(
    r"\b(dispute|chargeback|charge back|fraud|unauthorised|unauthorized|"
    r"galat charge|galat kata|maine nahi|nahi liya|wrong charge)\b",
    re.IGNORECASE,
)
_PAYMENT_CLAIM = re.compile(
    r"\b(already paid|payment done|paid it|kar diya|ho gaya|bhej diya|"
    r"transfer kar diya|paise bhej)\b",
    re.IGNORECASE,
)

# A brush-off. Present without an explicit commitment, the reply is hedged and
# sets nothing at all.
_HEDGE = re.compile(
    r"\b(dekhta hoon|dekhte hain|dekhenge|baad mein|baad me|try karunga|"
    r"koshish karunga|shayad|maybe|might|will see|let me see|theek hai|thik hai)\b",
    re.IGNORECASE,
)

# An explicit commitment verb. Necessary but not sufficient — a promise also
# needs a date that parses, is future, and is inside the horizon.
_COMMITMENT = re.compile(
    r"(?:\b(?:kar dunga|kar dungi|bhej dunga|bhej dungi|ho jayega|de dunga|"
    r"pay kar dunga|will pay|i will pay|pay karunga)\b"
    # Devanagari, for the same reason the numerals are here: gpt-transcribe
    # returns it for Hindi speech and a Latin-only pattern finds nothing.
    r"|कर दूंगा|कर दूँगा|भेज दूंगा|भेज दूँगा|हो जाएगा|हो जायेगा|दे दूंगा)",
    re.IGNORECASE,
)

# Spoken dates arrive as words, not digits. A transcript of "pandrah tareekh"
# contains no numeral at all, so a parser that only reads digits finds nothing
# and the flagship promise clip classifies as unclear.
WORD_NUMBERS: Final[dict[str, int]] = {
    "ek": 1, "do": 2, "teen": 3, "char": 4, "chaar": 4, "paanch": 5, "panch": 5,
    "chhe": 6, "chah": 6, "che": 6, "saat": 7, "aath": 8, "nau": 9, "das": 10,
    "gyarah": 11, "barah": 12, "terah": 13, "chaudah": 14, "pandrah": 15,
    "pandhra": 15, "solah": 16, "satrah": 17, "atharah": 18, "unnees": 19,
    "bees": 20, "ikkis": 21, "baees": 22, "teis": 23, "chaubis": 24,
    "pachchis": 25, "chhabbis": 26, "sattais": 27, "atthais": 28,
    # Devanagari, because gpt-transcribe returns it for Hindi speech whatever
    # the `language` parameter says.
    "एक": 1, "दो": 2, "तीन": 3, "चार": 4, "पाँच": 5, "पांच": 5, "सात": 7,
    "आठ": 8, "दस": 10, "पंद्रह": 15, "बीस": 20, "पच्चीस": 25,
}

_WORD_ALTERNATION = "|".join(sorted(WORD_NUMBERS, key=len, reverse=True))
_DAY_OF_MONTH = re.compile(
    rf"(\d{{1,2}}|{_WORD_ALTERNATION})\s*(?:tareekh|tarikh|tarik|तारीख)", re.IGNORECASE
)
_IN_DAYS = re.compile(
    rf"(\d{{1,2}}|{_WORD_ALTERNATION})\s*(?:din|day|days|दिन)\s*(?:mein|me|in)?",
    re.IGNORECASE,
)
_IN_WEEKS = re.compile(
    rf"(\d{{1,2}}|{_WORD_ALTERNATION})\s*(?:hafte|hafta|week|weeks|हफ्ते)", re.IGNORECASE
)
_TOMORROW = re.compile(r"\b(kal|tomorrow)\b", re.IGNORECASE)
_DAY_AFTER = re.compile(r"\b(parso|parson|day after tomorrow)\b", re.IGNORECASE)


def normalise(text: str) -> str:
    """Devanagari numerals to Latin, so one parser handles both scripts."""
    return "".join(DEVANAGARI_DIGITS.get(ch, ch) for ch in text)


def find_date_spans(text: str) -> list[tuple[str, str]]:
    """Locate candidate date spans. Locates only — evaluates nothing.

    Returned in source order, so a self-correction ("agle mahine... nahi nahi,
    pandrah tareekh ko") resolves to the last span rather than the first.
    """
    spans: list[tuple[int, str, str]] = []
    for kind, pattern in (
        ("day_of_month", _DAY_OF_MONTH),
        ("in_days", _IN_DAYS),
        ("in_weeks", _IN_WEEKS),
        ("tomorrow", _TOMORROW),
        ("day_after", _DAY_AFTER),
    ):
        for match in pattern.finditer(text):
            spans.append((match.start(), kind, match.group(0)))
    return [(kind, span) for _, kind, span in sorted(spans)]


def parse_date_span(kind: str, span: str, anchor: date) -> date | None:
    """Evaluate one located span against `anchor`. SPEC §11.

    `anchor` is the case's `created_at`, never a clock: a promise parsed against
    wall time would resolve differently on a replay and the ledger would stop
    reproducing.
    """
    digits = re.search(r"\d{1,2}", span)

    def value() -> int | None:
        """The number in this span, whether written as a digit or a word."""
        if digits:
            return int(digits.group(0))
        for word, number in WORD_NUMBERS.items():
            if re.search(rf"(?<![\w]){re.escape(word)}(?![\w])", span, re.IGNORECASE):
                return number
        return None

    if kind == "tomorrow":
        return anchor + timedelta(days=1)
    if kind == "day_after":
        return anchor + timedelta(days=2)
    if kind == "in_days":
        days = value()
        return anchor + timedelta(days=days) if days else None
    if kind == "in_weeks":
        return anchor + timedelta(weeks=value() or 1)
    if kind == "day_of_month":
        day = value()
        if day is None:
            return None
        if not 1 <= day <= 28:
            return None
        candidate = anchor.replace(day=day)
        if candidate <= anchor:
            # Next month. The commitment is forward-looking by definition.
            month = anchor.month % 12 + 1
            year = anchor.year + (1 if anchor.month == 12 else 0)
            candidate = date(year, month, day)
        return candidate
    return None


def validate(promise: date | None, anchor: date) -> date | None:
    """Future, and inside the horizon. Anything else is not a commitment."""
    if promise is None:
        return None
    if promise <= anchor:
        return None
    if (promise - anchor).days > PROMISE_HORIZON_DAYS:
        return None
    return promise


def classify_reply(text: str | None, anchor: date) -> ReplyVerdict:
    """Classify one reply. Deterministic, pure, and total."""
    if not text or not text.strip():
        return ReplyVerdict(ReplyKind.UNCLEAR, confidence=Confidence.LOW)

    cleaned = normalise(text)

    # Unambiguous first, and these never escalate.
    #
    # Opt-out outranks everything, including a payment claim in the same
    # sentence. "already paid, stop messaging" is both, and honouring the
    # opt-out is the only reading where being wrong is not a compliance breach.
    if match := _OPT_OUT.search(cleaned):
        return ReplyVerdict(ReplyKind.OPT_OUT, matched_span=match.group(0))
    if match := _DISPUTE.search(cleaned):
        return ReplyVerdict(ReplyKind.DISPUTE, matched_span=match.group(0))
    if match := _PAYMENT_CLAIM.search(cleaned):
        return ReplyVerdict(ReplyKind.PAYMENT_CLAIM, matched_span=match.group(0))

    committed = _COMMITMENT.search(cleaned)
    hedged = _HEDGE.search(cleaned)

    # A hedge without an explicit commitment is a brush-off, whatever else the
    # sentence contains. It sets nothing — no promise date, no suppression.
    if hedged and not committed:
        return ReplyVerdict(ReplyKind.HEDGED, matched_span=hedged.group(0))

    if committed:
        spans = find_date_spans(cleaned)
        for kind, span in reversed(spans):
            promise = validate(parse_date_span(kind, span, anchor), anchor)
            if promise is not None:
                return ReplyVerdict(ReplyKind.PROMISE, promise_date=promise, matched_span=span)
        # Committed to something, but to no date this parser can stand behind.
        # §11: disagreement becomes a confirmation turn, not a silent guess.
        return ReplyVerdict(ReplyKind.UNCLEAR, confidence=Confidence.LOW, matched_span=committed.group(0))

    return ReplyVerdict(ReplyKind.UNCLEAR, confidence=Confidence.LOW)


def escalation_rate(verdicts: list[ReplyVerdict]) -> float:
    """Fraction the deterministic classifier could not resolve. SPEC §11."""
    if not verdicts:
        return 0.0
    return sum(1 for v in verdicts if v.kind is ReplyKind.UNCLEAR) / len(verdicts)
