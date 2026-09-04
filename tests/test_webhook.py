"""CP12 — the webhook receiver. SPEC §16, INV-4, SF-3.

WBH-3 and WBH-6 are the two that matter.

WBH-3 is an ordering test, and ordering is the only thing that makes signature
verification worth having. A handler that parses first has already run a decoder
on unauthenticated bytes, and it tells an attacker holding no secret whether
their JSON was well-formed. The test proves the ordering by sending a malformed
body with a bad signature and asserting the *signature* is what rejected it —
and, so that it cannot pass vacuously, sends the same malformed body with a
*valid* signature and asserts the parse error is reachable after all.

WBH-6 asserts the ledger entry is on disk at the instant the response starts, by
reading the file from inside the ASGI `send` callable. Writing the record after
the 200 means a crash between the two loses the only evidence the event arrived,
and Razorpay will not send it again once it has a 2XX.

No TestClient — chosen, not forced
----------------------------------
`httpx` is pinned (CP12.1 F9), so `fastapi.testclient.TestClient` is available
and is deliberately not used. WBH-5 has to measure the moment the response
*starts*, because that is where Razorpay's five-second budget stops and where
the background work begins. `TestClient` returns after the whole ASGI call
completes, background tasks included, so it cannot separate the two. The direct
caller times `http.response.start` itself.
"""

import asyncio
import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any, Callable, NamedTuple

import pytest

from settle.api import webhook
from settle.api.app import app
from settle.audit.chain import read_entries, verify_entries
from settle.schema.enums import Actor, LedgerKind

SECRET = "cp12_webhook_secret_not_a_real_one"
REPO_ROOT = Path(__file__).resolve().parent.parent

PAID_EVENT: dict[str, Any] = {
    "entity": "event",
    "account_id": "acc_TESTACCOUNT",
    "event": "payment_link.paid",
    "contains": ["payment_link", "payment"],
    "payload": {
        "payment_link": {
            "entity": {
                "id": "plink_TESTLINK0001",
                "entity": "payment_link",
                "status": "paid",
                "amount": 249900,
                "reference_id": "case_000123",
                "notes": {"case_id": "case_000123", "arm": "EDGE"},
            }
        },
        "payment": {
            "entity": {
                "id": "pay_TESTPAYMENT01",
                "entity": "payment",
                "status": "captured",
                "amount": 249900,
                "method": "card",
                "notes": {"case_id": "case_000123"},
            }
        },
    },
    "created_at": 1788200000,
}


# --------------------------------------------------------------------------
# A minimal ASGI caller. See the module docstring for why it is here.
# --------------------------------------------------------------------------

class Response(NamedTuple):
    status: int
    body: bytes
    seconds_to_response_start: float
    seconds_to_completion: float

    @property
    def json(self) -> dict[str, Any]:
        return json.loads(self.body)


def call(
    method: str,
    path: str,
    *,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
    on_response_start: Callable[[], None] | None = None,
) -> Response:
    """Drive `app` through one HTTP request and report both timings.

    `seconds_to_response_start` is what Razorpay's 5-second budget measures;
    `seconds_to_completion` includes the background tasks, which run after the
    response has been sent but before the ASGI call returns.
    """
    sent: list[dict[str, Any]] = []
    started: list[float] = []

    all_headers = {"content-type": "application/json", **(headers or {})}
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in all_headers.items()],
        "client": ("127.0.0.1", 51234),
        "server": ("testserver", 80),
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            started.append(time.perf_counter())
            if on_response_start is not None:
                on_response_start()
        sent.append(message)

    begin = time.perf_counter()
    asyncio.run(app(scope, receive, send))
    finished = time.perf_counter()

    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return Response(
        status=status,
        body=payload,
        seconds_to_response_start=(started[0] if started else finished) - begin,
        seconds_to_completion=finished - begin,
    )


def sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def post_event(
    event: dict[str, Any] | bytes,
    *,
    event_id: str = "evt_TESTEVENT0001",
    signature: str | None = "valid",
    on_response_start: Callable[[], None] | None = None,
) -> Response:
    """POST one webhook. `signature="valid"` signs the body actually sent."""
    body = event if isinstance(event, bytes) else json.dumps(event).encode()
    headers = {"x-razorpay-event-id": event_id}
    if signature == "valid":
        headers["x-razorpay-signature"] = sign(body)
    elif signature is not None:
        headers["x-razorpay-signature"] = signature
    return call(
        "POST",
        "/webhooks/razorpay",
        body=body,
        headers=headers,
        on_response_start=on_response_start,
    )


@pytest.fixture
def edge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A clean ledger file and idempotency database per test."""
    ledger_path = tmp_path / "audit_edge.jsonl"
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("SETTLE_EDGE_LEDGER", str(ledger_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'settle.db'}")
    webhook.reset_edge_state()
    yield ledger_path
    webhook.reset_edge_state()


# --------------------------------------------------------------------------
# WBH-1 / WBH-2 — signature verification
# --------------------------------------------------------------------------

def test_WBH_1_valid_signature_accepted_invalid_rejected(edge: Path) -> None:
    """The gate itself. Mandatory HMAC SHA256, SPEC §16."""
    accepted = post_event(PAID_EVENT, event_id="evt_valid_0001")
    assert accepted.status == 200
    assert accepted.json["status"] == "ok"
    assert accepted.json["replay"] is False

    wrong = post_event(PAID_EVENT, event_id="evt_wrong_0001", signature="0" * 64)
    assert wrong.status == 400
    assert wrong.json["reason_code"] == webhook.REASON_SIGNATURE_INVALID

    # A signature computed with the wrong secret is the realistic attack, and it
    # is well-formed hex, so it rules out "we only reject malformed hex".
    forged = json.dumps(PAID_EVENT).encode()
    other = call(
        "POST",
        "/webhooks/razorpay",
        body=forged,
        headers={
            "x-razorpay-event-id": "evt_forged_0001",
            "x-razorpay-signature": sign(forged, "a-different-secret"),
        },
    )
    assert other.status == 400
    assert other.json["reason_code"] == webhook.REASON_SIGNATURE_INVALID

    # A body altered after signing must fail: the signature covers the bytes.
    body = json.dumps(PAID_EVENT).encode()
    tampered = body.replace(b"249900", b"999900")
    assert tampered != body
    replaced = call(
        "POST",
        "/webhooks/razorpay",
        body=tampered,
        headers={
            "x-razorpay-event-id": "evt_tampered_001",
            "x-razorpay-signature": sign(body),
        },
    )
    assert replaced.status == 400
    assert replaced.json["reason_code"] == webhook.REASON_SIGNATURE_INVALID

    # Nothing rejected reached the ledger.
    entries = read_entries(edge)
    assert len(entries) == 1


def test_WBH_2_unsigned_request_rejected(edge: Path) -> None:
    """No header, no entry. An endpoint that accepts unsigned traffic is open."""
    unsigned = post_event(PAID_EVENT, event_id="evt_unsigned_01", signature=None)
    assert unsigned.status == 400
    assert unsigned.json["reason_code"] == webhook.REASON_SIGNATURE_MISSING

    empty = post_event(PAID_EVENT, event_id="evt_empty_sig_1", signature="")
    assert empty.status == 400
    assert empty.json["reason_code"] in {
        webhook.REASON_SIGNATURE_MISSING,
        webhook.REASON_SIGNATURE_INVALID,
    }

    assert not edge.exists() or read_entries(edge) == []


def test_WBH_2_missing_secret_refuses_rather_than_skips(
    edge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no secret configured, refuse. Skipping verification when it cannot
    be done is how an endpoint becomes unauthenticated by accident."""
    monkeypatch.delenv("RAZORPAY_WEBHOOK_SECRET", raising=False)
    response = post_event(PAID_EVENT, event_id="evt_nosecret_01", signature="0" * 64)
    assert response.status == 400
    assert response.json["reason_code"] == webhook.REASON_SECRET_MISSING


# --------------------------------------------------------------------------
# WBH-3 — verification happens before parsing
# --------------------------------------------------------------------------

def test_WBH_3_signature_is_verified_before_the_body_is_parsed(edge: Path) -> None:
    """Malformed body, bad signature: it must fail on the signature."""
    malformed = b'{"event": "payment_link.paid", "payload": {oh no'

    rejected = post_event(malformed, event_id="evt_malformed_1", signature="0" * 64)
    assert rejected.status == 400
    assert rejected.json["reason_code"] == webhook.REASON_SIGNATURE_INVALID, (
        "a malformed body with a bad signature failed on the parse, which means "
        "a decoder ran on unauthenticated input"
    )

    unsigned = post_event(malformed, event_id="evt_malformed_2", signature=None)
    assert unsigned.status == 400
    assert unsigned.json["reason_code"] == webhook.REASON_SIGNATURE_MISSING

    # Not vacuous: the same body, correctly signed, does reach the parser and
    # fails there. Without this the test would pass on a handler that rejected
    # everything for the same reason.
    signed = post_event(malformed, event_id="evt_malformed_3", signature="valid")
    assert signed.status == 400
    assert signed.json["reason_code"] == webhook.REASON_BODY_UNPARSEABLE

    # Bytes that are not UTF-8 at all take the same route.
    invalid_utf8 = post_event(b"\xff\xfe\x00garbage", event_id="evt_bin_0001")
    assert invalid_utf8.status == 400
    assert invalid_utf8.json["reason_code"] == webhook.REASON_BODY_UNPARSEABLE

    # A well-formed JSON scalar is not an event either.
    not_object = post_event(b'"just a string"', event_id="evt_scalar_001")
    assert not_object.status == 400
    assert not_object.json["reason_code"] == webhook.REASON_BODY_UNPARSEABLE

    assert not edge.exists() or read_entries(edge) == []


def test_WBH_3_verify_signature_takes_raw_bytes(edge: Path) -> None:
    """The unit underneath, checked directly.

    Re-serialising a parsed body and signing that verifies a different string
    from the one Razorpay signed, so the function must never see a parsed
    object. Equal JSON with different whitespace has a different signature, and
    that is correct behaviour, not a bug to paper over.
    """
    body = json.dumps(PAID_EVENT).encode()
    assert webhook.verify_signature(body, sign(body), SECRET) is True
    assert webhook.verify_signature(body, sign(body).upper(), SECRET) is False
    assert webhook.verify_signature(body, None, SECRET) is False
    assert webhook.verify_signature(body, sign(body), "") is False

    respaced = json.dumps(PAID_EVENT, indent=2).encode()
    assert json.loads(respaced) == json.loads(body)
    assert webhook.verify_signature(respaced, sign(body), SECRET) is False


# --------------------------------------------------------------------------
# WBH-4 — replay
# --------------------------------------------------------------------------

def test_WBH_4_replayed_event_id_recorded_once_and_dispatches_nothing(edge: Path) -> None:
    """SF-3, in production. Razorpay retries for 24 hours; a second delivery of
    the same event must not become a second recovery."""
    dispatched: list[webhook.WebhookDelivery] = []
    webhook.register_processor(dispatched.append)

    first = post_event(PAID_EVENT, event_id="evt_replay_0001")
    assert first.status == 200
    assert first.json["replay"] is False
    assert first.json["delivery_count"] == 1

    second = post_event(PAID_EVENT, event_id="evt_replay_0001")
    third = post_event(PAID_EVENT, event_id="evt_replay_0001")

    # 200 both times: a retry that gets a 4xx keeps being retried for a day.
    assert (second.status, third.status) == (200, 200)
    assert second.json["replay"] is True
    assert (second.json["delivery_count"], third.json["delivery_count"]) == (2, 3)

    # Recorded once. One row for the event id, whatever the delivery count.
    store = webhook.idempotency_store()
    assert store.count() == 1
    record = store.get("evt_replay_0001")
    assert record is not None
    assert record.delivery_count == 3

    # Dispatches nothing. Exactly one delivery caused work.
    assert len(dispatched) == 1
    assert dispatched[0].case_id == "case_000123"
    assert dispatched[0].event_id == "evt_replay_0001"

    # The replays are still *recorded* — the ledger is the account of what
    # arrived, and a duplicate arriving is a fact about the world, not noise.
    # This mirrors ReportedOutcome.arrival_count in SPEC §5.5.
    entries = read_entries(edge)
    assert len(entries) == 3
    assert [e.reason_code for e in entries] == [
        webhook.REASON_RECEIVED,
        webhook.REASON_REPLAY,
        webhook.REASON_REPLAY,
    ]
    verify_entries(entries)

    # A genuinely different event is not suppressed by the first one.
    other = post_event(PAID_EVENT, event_id="evt_replay_0002")
    assert other.json["replay"] is False
    assert len(dispatched) == 2
    assert store.count() == 2


# --------------------------------------------------------------------------
# WBH-5 — the 5-second budget
# --------------------------------------------------------------------------

SLOW_PROCESSING_SECONDS = 1.0
RAZORPAY_BUDGET_SECONDS = 5.0


def test_WBH_5_handler_returns_within_the_budget_under_slow_processing(edge: Path) -> None:
    """Razorpay requires 2XX within 5 seconds, retries with exponential backoff
    for 24 hours and disables the webhook after 24 hours of failure. Processing
    inline is therefore not a performance question, it is an availability one."""
    ran: list[float] = []

    def slow(parcel: webhook.WebhookDelivery) -> None:
        time.sleep(SLOW_PROCESSING_SECONDS)
        ran.append(time.perf_counter())

    webhook.register_processor(slow)

    response = post_event(PAID_EVENT, event_id="evt_slow_00001")
    assert response.status == 200

    # The response beat the slow path, with room to spare.
    assert response.seconds_to_response_start < RAZORPAY_BUDGET_SECONDS
    assert response.seconds_to_response_start < SLOW_PROCESSING_SECONDS, (
        "the response waited on the processor, so the processor is inside the "
        "handler's budget after all"
    )

    # And the slow work really did run — otherwise this passes by doing nothing.
    assert len(ran) == 1
    assert response.seconds_to_completion >= SLOW_PROCESSING_SECONDS


# --------------------------------------------------------------------------
# WBH-6 — write-ahead
# --------------------------------------------------------------------------

def test_WBH_6_raw_event_is_on_disk_before_the_response_starts(edge: Path) -> None:
    """Read the ledger file from inside the ASGI `send`, at the instant the
    response begins. INV-5's ordering, applied to the edge: a record written
    after the 200 is a record that does not exist if the process dies in
    between, and Razorpay never re-sends an event it has already had a 2XX for.
    """
    snapshot: list[str] = []

    def peek() -> None:
        snapshot.append(edge.read_text(encoding="utf-8") if edge.exists() else "")

    response = post_event(PAID_EVENT, event_id="evt_writeahead_1", on_response_start=peek)
    assert response.status == 200

    assert snapshot and snapshot[0].strip(), "the ledger was empty when the response started"
    written = [json.loads(line) for line in snapshot[0].splitlines() if line.strip()]
    assert len(written) == 1

    entry = written[0]
    assert entry["kind"] == LedgerKind.EVENT.value
    assert entry["actor"] == Actor.SYSTEM.value
    assert entry["case_id"] == "case_000123"
    assert entry["arm"] == webhook.EDGE_ARM
    assert entry["reason_code"] == webhook.REASON_RECEIVED

    # The raw event, verbatim — not a summary of it. The point of storing the
    # event is that reconciliation can later disagree with our reading of it.
    assert entry["payload"]["event"] == PAID_EVENT
    assert entry["payload"]["event_id"] == "evt_writeahead_1"
    assert entry["payload"]["subscribed"] is True
    assert entry["payload"]["signature_verified"] is True
    assert (
        entry["payload"]["body_sha256"]
        == hashlib.sha256(json.dumps(PAID_EVENT).encode()).hexdigest()
    )

    # And it is a real link in the chain, not a line appended to a file.
    verify_entries(read_entries(edge))
    assert response.json["ledger_seq"] == 0


def test_WBH_6_an_event_with_no_case_id_is_still_recorded(edge: Path) -> None:
    """Unjoined is a state, not a reason to drop the record. A webhook we cannot
    explain is exactly the one worth having written down."""
    orphan = {"entity": "event", "event": "payment.failed", "payload": {}, "created_at": 1}
    response = post_event(orphan, event_id="evt_orphan_0001")
    assert response.status == 200

    entries = read_entries(edge)
    assert len(entries) == 1
    assert entries[0].case_id == webhook.UNJOINED_CASE_ID
    assert entries[0].payload["event"] == orphan
    assert entries[0].payload["subscribed"] is True


def test_WBH_6_an_unsubscribed_event_is_recorded_and_flagged(edge: Path) -> None:
    """SPEC §16 subscribes to three events. A fourth arriving means the dashboard
    and the code disagree, and the ledger is where that becomes visible."""
    stray = {"entity": "event", "event": "refund.processed", "payload": {}, "created_at": 1}
    assert post_event(stray, event_id="evt_stray_00001").status == 200
    entries = read_entries(edge)
    assert entries[0].payload["subscribed"] is False


# --------------------------------------------------------------------------
# The route table — SPEC §16 fixes it at three
# --------------------------------------------------------------------------

def test_WBH_1_the_app_declares_exactly_the_three_spec_routes(edge: Path) -> None:
    """All three do their job as of CP15. The table is the contract; what each
    route returns is checked underneath."""
    # Read from the OpenAPI schema rather than `app.routes`: FastAPI keeps an
    # included router as one nested object, so walking `app.routes` would report
    # the two decorated stubs and silently miss the route that matters.
    declared = {
        (path, method.upper())
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    }
    assert declared == {
        ("/webhooks/razorpay", "POST"),
        ("/voice/extract", "POST"),
        ("/", "GET"),
    }, "SPEC §16 fixes the route table at exactly three"

    # Both were 501 stubs until CP15. `/voice/extract` extracts now and `GET /`
    # serves viewer/index.html (A129, A133); the table is still the three §16
    # fixes, which is what this test is actually about.
    assert call("GET", "/").status == 200
    # No body: the route rejects rather than transcribing nothing.
    assert call("POST", "/voice/extract").status == 400
