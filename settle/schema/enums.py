"""Closed value sets, per SPEC §5, §7, §8, §13.2.

Every enum here is closed. Adding a member to one of these is a change to a
frozen contract and requires an amendment note in SPEC.md.
"""

from enum import Enum


class Rail(str, Enum):
    """SPEC §5.1 — the debit rail."""

    CARD = "card"
    UPI_AUTOPAY = "upi_autopay"
    ENACH = "enach"


class MandateState(str, Enum):
    """SPEC §5.1."""

    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"
    NONE = "none"


class Language(str, Enum):
    """SPEC §5.1."""

    EN = "en"
    HI = "hi"
    HINGLISH = "hinglish"


class DeclineClass(str, Enum):
    """SPEC §9 — deterministic decline-code classification."""

    TIME_SHIFTABLE = "time_shiftable"
    TRANSIENT = "transient"
    DEAD_INSTRUMENT = "dead_instrument"
    AUTH_ABANDONED = "auth_abandoned"
    AMBIGUOUS = "ambiguous"
    TERMINAL = "terminal"


class ActionType(str, Enum):
    """SPEC §5.3 — the closed verb set. `do_nothing` is first-class."""

    DO_NOTHING = "do_nothing"
    RETRY = "retry"
    SWITCH_RAIL = "switch_rail"
    SEND_MESSAGE = "send_message"
    REQUEST_MANDATE_UPDATE = "request_mandate_update"
    SERVE_NOTICE = "serve_notice"
    ESCALATE_HUMAN = "escalate_human"
    VOICE_CALL = "voice_call"


class Channel(str, Enum):
    """Contact channel. SPEC §5.3.

    Exactly this list. Email is out of scope (Known Limitations), and G7's
    opt-out-on-every-channel is bounded by exactly these members — an opt-out
    test cannot be complete against an open set.
    """

    SMS = "sms"
    WHATSAPP = "whatsapp"
    VOICE = "voice"


class ReportedStatus(str, Enum):
    """SPEC §5.5 — `status captured|failed|none`."""

    CAPTURED = "captured"
    FAILED = "failed"
    NONE = "none"


class LedgerKind(str, Enum):
    """SPEC §5.6."""

    EVENT = "event"
    DIAGNOSIS = "diagnosis"
    DECISION = "decision"
    GATE_CHECK = "gate_check"
    DISPATCH = "dispatch"
    REPORTED_OUTCOME = "reported_outcome"
    RECONCILIATION = "reconciliation"
    STOP = "stop"


class Actor(str, Enum):
    """SPEC §5.6."""

    SYSTEM = "system"
    POLICY = "policy"
    MODEL = "model"
    LLM = "llm"
    HUMAN = "human"


class ChosenBy(str, Enum):
    """SPEC §5.4."""

    HEURISTIC = "heuristic"
    MODEL = "model"
    LLM = "llm"


class ArmMode(str, Enum):
    """SPEC §4 — whether a gate verdict is binding.

    ENFORCE blocks the dispatch. OBSERVE logs the violation and permits it.
    Only B3 runs in OBSERVE (INV-11).
    """

    ENFORCE = "ENFORCE"
    OBSERVE = "OBSERVE"


class StopClass(str, Enum):
    """SPEC §13.2.

    COMPLIANCE stops (S4, S5) are relaxed in OBSERVE. TERMINAL_STATE stops
    (S1, S2, S3, S6) are binding in every arm without exception.
    """

    COMPLIANCE = "COMPLIANCE"
    TERMINAL_STATE = "TERMINAL_STATE"


class IntentType(str, Enum):
    """SPEC §5.2 — hidden. The enum is public; the value never is."""

    WILLING_ABLE = "willing_able"
    WILLING_BROKE = "willing_broke"
    DISPUTING = "disputing"
    CHURNED = "churned"
    ADVERSARIAL = "adversarial"


class DebtorBehaviour(str, Enum):
    """SPEC §8 — layered on top of `IntentType`."""

    PROMISE_AND_BREAK = "promise_and_break"
    DISPUTE_STALL = "dispute_stall"
    GO_SILENT = "go_silent"
    OPT_OUT_MIDWAY = "opt_out_midway"
    HEDGED_REPLY = "hedged_reply"
    PAY_THEN_COMPLAIN = "pay_then_complain"


class SilentFailureClass(str, Enum):
    """SPEC §7 — the taxonomy the reconciliation pass reports against."""

    SF1 = "SF-1"
    SF2 = "SF-2"
    SF3 = "SF-3"
    SF4 = "SF-4"
    SF5 = "SF-5"
    SF6 = "SF-6"
    SF7 = "SF-7"
