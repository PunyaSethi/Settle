"""POST /webhooks/razorpay — the signature-verified receiver. SPEC §16.

The handler does four things and then stops:

    1. verify HMAC SHA256 over the raw bytes, before parsing them
    2. check the idempotency store on the event id
    3. write the raw event to the ledger
    4. return 200

Why "and then stops" is the design
----------------------------------
Razorpay requires a 2XX within 5 seconds. It retries with exponential backoff for
24 hours and disables the webhook after 24 hours of failure. A handler that does
its work inline is a handler that eventually takes six seconds, at which point
Razorpay retries — and the slow work runs twice, concurrently, on the same event.
That is how an endpoint turns one payment into two recoveries. So processing runs
after the response is sent, through `register_processor`, and the handler itself
has no path that can grow.

Why verification comes before parsing
-------------------------------------
The signature is computed over the exact bytes on the wire. Parsing first means
running a JSON decoder on unauthenticated input and, worse, means a malformed
body from an attacker who does not hold the secret returns a *parse* error —
which tells them the signature check is not where they thought it was, and gives
them a decoder to probe. `verify_signature` therefore takes `bytes`, never a
parsed object, and nothing in this module reads the body before it returns True.
WBH-3 asserts exactly that: a malformed body with a bad signature fails on the
signature.
"""

import hashlib
import hmac
import json
import os
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, NamedTuple

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse

from settle.audit.chain import Ledger
from settle.integrations.idempotency import Delivery, IdempotencyStore
from settle.schema.enums import Actor, LedgerKind

__all__ = [
    "EVENT_ID_HEADER",
    "SIGNATURE_HEADER",
    "SUBSCRIBED_EVENTS",
    "WebhookDelivery",
    "clear_processors",
    "edge_ledger",
    "idempotency_store",
    "register_processor",
    "reset_edge_state",
    "router",
    "verify_signature",
]

SIGNATURE_HEADER: Final[str] = "x-razorpay-signature"
EVENT_ID_HEADER: Final[str] = "x-razorpay-event-id"

# SPEC §16. Anything else is recorded and ignored — an unsubscribed event
# arriving means the dashboard and the code disagree, and the ledger is where
# that shows up.
SUBSCRIBED_EVENTS: Final[frozenset[str]] = frozenset(
    {"payment.captured", "payment.failed", "payment_link.paid"}
)

# The real edge is not a simulation arm. Ledger entries need an `arm`, and
# labelling live traffic "B0" or "OURS" would put it in the same bucket the
# evaluation reads. `EDGE` is a bucket the evaluation does not read.
EDGE_ARM: Final[str] = "EDGE"

# A webhook whose notes carry no case_id still gets written; the ledger is the
# record of what arrived, not of what we managed to understand.
UNJOINED_CASE_ID: Final[str] = "_unjoined"

REASON_RECEIVED: Final[str] = "WEBHOOK_RECEIVED"
REASON_REPLAY: Final[str] = "WEBHOOK_REPLAY"
REASON_SIGNATURE_INVALID: Final[str] = "WEBHOOK_SIGNATURE_INVALID"
REASON_SIGNATURE_MISSING: Final[str] = "WEBHOOK_SIGNATURE_MISSING"
REASON_SECRET_MISSING: Final[str] = "WEBHOOK_SECRET_MISSING"
REASON_BODY_UNPARSEABLE: Final[str] = "WEBHOOK_BODY_UNPARSEABLE"

DEFAULT_EDGE_LEDGER: Final[str] = "out/audit_edge.jsonl"

router = APIRouter()


class WebhookDelivery(NamedTuple):
    """What a processor is handed, after the response has already gone out."""

    event_id: str
    event_type: str
    case_id: str
    event: dict[str, Any]
    delivery: Delivery
    ledger_seq: int


Processor = Callable[[WebhookDelivery], None]

_processors: list[Processor] = []
_ledger: Ledger | None = None
_store: IdempotencyStore | None = None


def register_processor(processor: Processor) -> None:
    """Register work to run *after* the response is sent.

    Deliberately the only way to attach behaviour to an inbound event. The seam
    exists so that the next person with something to do on `payment_link.paid`
    has somewhere to put it that is not inside the 5-second budget.
    """
    _processors.append(processor)


def clear_processors() -> None:
    _processors.clear()


def edge_ledger() -> Ledger:
    """The ledger the edge writes to. Separate file, same hash chain rules.

    Not `out/audit.jsonl`: the evaluation's ledgers are per-arm and replayable
    from a seed, and interleaving live traffic into one of them would make it
    neither.
    """
    global _ledger
    if _ledger is None:
        _ledger = Ledger(Path(os.environ.get("SETTLE_EDGE_LEDGER", DEFAULT_EDGE_LEDGER)))
    return _ledger


def idempotency_store() -> IdempotencyStore:
    global _store
    if _store is None:
        _store = IdempotencyStore()
    return _store


def reset_edge_state() -> None:
    """Drop the ledger handle and store so the next call re-reads the environment."""
    global _ledger, _store
    if _ledger is not None:
        _ledger.close()
    _ledger = None
    if _store is not None:
        _store.close()
    _store = None
    clear_processors()


def verify_signature(body: bytes, signature: str | None, secret: str) -> bool:
    """HMAC SHA256 over the raw bytes, compared in constant time.

    Takes `bytes` and not a parsed object on purpose: re-serialising a parsed
    body to check a signature verifies a different string from the one that was
    signed, and any whitespace or key-order difference silently fails.
    """
    if not signature or not secret:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _case_id_from(event: dict[str, Any]) -> str:
    """Join an inbound event back to a case through the notes we set on the link.

    Razorpay echoes `notes` on the payment link and on the payment created
    against it, so both entity shapes are checked. `reference_id` is the
    fallback because `create_payment_link` sets it to the case id too, and a
    join with one anchor is a join that breaks the first time an entity omits it.
    """
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return UNJOINED_CASE_ID
    for key in ("payment_link", "payment", "order"):
        holder = payload.get(key)
        if not isinstance(holder, dict):
            continue
        entity = holder.get("entity")
        if not isinstance(entity, dict):
            continue
        notes = entity.get("notes")
        if isinstance(notes, dict) and notes.get("case_id"):
            return str(notes["case_id"])
        if entity.get("reference_id"):
            return str(entity["reference_id"])
    return UNJOINED_CASE_ID


def _bad_request(reason_code: str, detail: str) -> JSONResponse:
    return JSONResponse({"reason_code": reason_code, "detail": detail}, status_code=400)


@router.post("/webhooks/razorpay")
async def receive_webhook(request: Request, background: BackgroundTasks) -> JSONResponse:
    """Verify, record, write, return. Nothing else. SPEC §16."""
    # The raw bytes, before any decoder has touched them. Everything below the
    # signature check may assume the sender holds the shared secret; nothing
    # above it may assume anything at all.
    raw: bytes = await request.body()

    secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "").strip()
    if not secret:
        # Refuse rather than accept unverified traffic. An endpoint that skips
        # verification when it cannot verify is an unauthenticated endpoint with
        # extra steps.
        return _bad_request(
            REASON_SECRET_MISSING,
            "RAZORPAY_WEBHOOK_SECRET is not configured; refusing to accept "
            "unverified traffic",
        )

    signature = request.headers.get(SIGNATURE_HEADER)
    if signature is None:
        return _bad_request(REASON_SIGNATURE_MISSING, f"missing {SIGNATURE_HEADER}")
    if not verify_signature(raw, signature, secret):
        return _bad_request(REASON_SIGNATURE_INVALID, "signature verification failed")

    # Authenticated. Only now is it safe to decode.
    try:
        event = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as error:
        return _bad_request(REASON_BODY_UNPARSEABLE, f"body is not JSON: {error}")
    if not isinstance(event, dict):
        return _bad_request(REASON_BODY_UNPARSEABLE, "body is not a JSON object")

    body_sha256 = hashlib.sha256(raw).hexdigest()
    event_type = str(event.get("event") or "unknown")
    # Razorpay stamps `x-razorpay-event-id` on every delivery and keeps it stable
    # across retries — that is the whole point of the header. The body-hash
    # fallback keeps the store keyed on something rather than on nothing if a
    # delivery arrives without it.
    event_id = request.headers.get(EVENT_ID_HEADER) or f"sha256:{body_sha256}"

    delivery = idempotency_store().record(
        event_id, event_type=event_type, body_sha256=body_sha256
    )

    case_id = _case_id_from(event)
    entry = edge_ledger().append(
        case_id=case_id or UNJOINED_CASE_ID,
        at=datetime.now(tz=timezone.utc),
        kind=LedgerKind.EVENT,
        actor=Actor.SYSTEM,
        payload={
            "event": event,
            "event_id": event_id,
            "body_sha256": body_sha256,
            "delivery_count": delivery.delivery_count,
            "subscribed": event_type in SUBSCRIBED_EVENTS,
            "signature_verified": True,
            "source": "RAZORPAY_TEST_MODE",
        },
        reason_code=REASON_REPLAY if delivery.is_replay else REASON_RECEIVED,
        arm=EDGE_ARM,
    )

    if delivery.should_dispatch and _processors:
        parcel = WebhookDelivery(
            event_id=event_id,
            event_type=event_type,
            case_id=case_id,
            event=event,
            delivery=delivery,
            ledger_seq=entry.seq,
        )
        for processor in list(_processors):
            background.add_task(processor, parcel)

    return JSONResponse(
        {
            "status": "ok",
            "event_id": event_id,
            "replay": delivery.is_replay,
            "delivery_count": delivery.delivery_count,
            "ledger_seq": entry.seq,
        },
        status_code=200,
    )
