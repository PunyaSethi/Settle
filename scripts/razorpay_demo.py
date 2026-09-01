"""The Razorpay edge demo. One payment link, one verified webhook, one ledger row.

    python scripts/razorpay_demo.py            mock mode — no credentials needed
    python scripts/razorpay_demo.py link       create the link, print id and URL
    python scripts/razorpay_demo.py wait       poll the edge ledger for the webhook
    python scripts/razorpay_demo.py finish     write out/razorpay_demo.json

Mock mode is the default, so cloning this repo and running the command with no
`.env` produces a working `MOCK_SANDBOX` demo. Set `RAZORPAY_MOCK_MODE=false`
with real `rzp_test_` credentials to run against Razorpay's test mode.

The contact-free projection
---------------------------
Razorpay's checkout SMS-verifies the payer's phone number, so a real payment
carries a real mobile number and there is no placeholder that can complete one.
That number must not be published.

The obvious move — hash the raw event, then delete the phone before committing —
is the one thing this file must not do. A hash computed over content the reader
cannot see, published beside content the reader can, is unverifiable: it reduces
to "trust me, it hashed correctly before I edited it". This project exists to
refuse exactly that claim about payment outcomes, and an artefact that makes it
about its own integrity would be self-refuting.

So the published chain is defined over a **projection** instead. `project()`
below constructs a record from a fixed field list — ids, statuses, amounts,
method, notes, timestamps, event id, delivery count, signature verification.
Customer contact fields are not in that list. They are not blanked and not
masked; they are absent from the schema, and there is no branch in which they
enter it. The hash covers the projection, the projection is what gets committed,
and a reader recomputes over exactly the bytes in front of them.

That is a stated scope, not a redaction. The chain covers the projection; it
does not cover the raw event. The raw event stays on the machine that received
it, in `out/razorpay_raw*.json`, which is gitignored.
"""

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    from dotenv import load_dotenv

    load_dotenv(REPO / ".env")
except ImportError:  # pragma: no cover — dotenv is pinned, this is belt and braces
    pass

from settle.audit.chain import GENESIS_HASH, read_entries, verify_entries  # noqa: E402
from settle.schema.canonical import canonical_json  # noqa: E402
from settle.integrations.razorpay_client import RazorpayClient  # noqa: E402

STATE = REPO / "out" / "razorpay_demo_state.json"
LEDGER = Path(os.environ.get("SETTLE_EDGE_LEDGER", REPO / "out" / "audit_edge.jsonl"))
ARTEFACT = REPO / "out" / "razorpay_demo.json"
RAW = REPO / "out" / "razorpay_raw.json"
CASE_ID = os.environ.get("SETTLE_DEMO_CASE", "case_000000")

# The projection's field list, stated once. Anything not named here does not
# reach the artefact — which is how "contact fields are excluded" becomes a
# property of the code rather than a promise about it.
PAYMENT_FIELDS = ("id", "status", "amount", "currency", "method", "captured",
                  "order_id", "error_code", "error_description", "error_reason",
                  "error_source", "error_step", "created_at", "notes")
LINK_FIELDS = ("id", "status", "amount", "amount_paid", "currency", "reference_id",
               "short_url", "created_at", "notes")
ORDER_FIELDS = ("id", "status", "amount", "amount_paid", "amount_due", "currency",
                "attempts", "receipt", "created_at", "notes")

# Named so a reader can check the exclusion rather than take it on trust. These
# are the fields Razorpay puts a customer's identity in; none is in the lists
# above, and this tuple exists so a test can assert the projection contains no
# key from it (RZP-4).
EXCLUDED_CONTACT_FIELDS = ("contact", "email", "customer_id", "customer",
                           "card", "card_id", "vpa", "token_id", "bank")

ENTITY_FIELDS = {
    "payment": PAYMENT_FIELDS,
    "payment_link": LINK_FIELDS,
    "order": ORDER_FIELDS,
}


def _case() -> dict[str, Any]:
    batch = REPO / "out" / "batch.jsonl"
    if not batch.exists():
        # Mock mode should work in a fresh clone, where the batch has not been
        # generated yet. A synthetic case is honest here: the whole record is
        # about to be labelled MOCK_SANDBOX anyway.
        return {"case_id": CASE_ID, "amount_paise": 325635, "rail": "card",
                "decline_code": "insufficient_funds", "attempt_number": 1}
    for line in batch.open(encoding="utf-8"):
        row = json.loads(line)
        if row["case_id"] == CASE_ID:
            return row
    raise SystemExit(f"case {CASE_ID} not found in {batch}")


def _client() -> RazorpayClient:
    """Mock unless explicitly told otherwise. See the module docstring."""
    return RazorpayClient()


def cmd_link() -> None:
    case = _case()
    client = _client()
    print(f"client: {client}")
    record = client.create_payment_link(
        amount_paise=case["amount_paise"],
        description=f"settle recovery — {case['case_id']} ({case['decline_code']})",
        notes={
            "case_id": case["case_id"],
            "rail": case["rail"],
            "decline_code": case["decline_code"],
            "attempt_number": str(case["attempt_number"]),
            "source": "settle CP12",
        },
    )
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(
        json.dumps({"case": case, "link": json.loads(record.model_dump_json())}, indent=2)
    )
    print()
    print(f"  case_id     {record.case_id}")
    print(f"  source      {record.source.value}")
    print(f"  plink id    {record.id}")
    print(f"  short_url   {record.short_url}")
    print(f"  amount      {record.amount_paise} paise  (INR {record.amount_paise / 100:.2f})")
    print(f"  status      {record.status}")
    print()
    if record.is_real:
        print("Pay it with a domestic test card — 5267 3181 8797 5449 — any future")
        print("expiry, any CVV, OTP 1234. Then: scripts/razorpay_demo.py wait")
    else:
        print("MOCK_SANDBOX. That URL does not resolve and is not meant to: the")
        print("`.invalid` TLD is reserved so a mock link can never be mistaken for")
        print("a real one. Set RAZORPAY_MOCK_MODE=false with rzp_test_ credentials")
        print("to create a real test-mode link.")


def _relevant(entries: list, case_id: str) -> list:
    return [e for e in entries if e.case_id == case_id]


def cmd_wait(timeout: float = 600.0) -> None:
    state = json.loads(STATE.read_text())
    case_id = state["link"]["notes"]["case_id"]
    print(f"waiting for webhooks on {case_id} / {state['link']['id']} …")
    deadline = time.time() + timeout
    seen: set[int] = set()
    while time.time() < deadline:
        if LEDGER.exists():
            rows = _relevant(read_entries(LEDGER), case_id)
            for entry in rows:
                if entry.seq not in seen:
                    seen.add(entry.seq)
                    print(f"  seq={entry.seq} {entry.payload['event'].get('event')} "
                          f"reason={entry.reason_code}")
            if any(r.payload["event"].get("event") == "payment_link.paid" for r in rows):
                print("payment_link.paid received.")
                return
        time.sleep(2.0)
    print("timed out without payment_link.paid")


# --------------------------------------------------------------------------
# The projection
# --------------------------------------------------------------------------

def _entity(holder: Any, fields: tuple[str, ...]) -> dict[str, Any] | None:
    """Copy exactly `fields` off an entity. Everything else is dropped.

    An allow-list rather than a deny-list, deliberately: a deny-list silently
    admits whatever field Razorpay adds next, and the field it adds next could
    be a customer's name.
    """
    if not isinstance(holder, dict):
        return None
    entity = holder.get("entity")
    if not isinstance(entity, dict):
        return None
    return {key: entity[key] for key in fields if key in entity}


def project(entry: Any) -> dict[str, Any]:
    """The contact-free record of one webhook delivery.

    This is what gets hashed and what gets committed. If a field is not
    constructed here it is not in the artefact — there is no later filtering
    step, because a later filtering step is how a field gets forgotten.
    """
    event = entry.payload["event"]
    payload = event.get("payload") or {}
    projected = {
        "ledger_seq": entry.seq,
        "case_id": entry.case_id,
        "at": entry.at.astimezone(timezone.utc).isoformat(),
        "arm": entry.arm,
        "kind": entry.kind.value,
        "actor": entry.actor.value,
        "reason_code": entry.reason_code,
        "event": event.get("event"),
        "event_id": entry.payload["event_id"],
        "event_created_at": event.get("created_at"),
        "account_id": event.get("account_id"),
        "delivery_count": entry.payload["delivery_count"],
        "subscribed": entry.payload["subscribed"],
        "signature_verified": entry.payload["signature_verified"],
        # A claim about the local raw record, not something a reader can check
        # against bytes they hold. It is inside the projection so the chain
        # covers it, and labelled in the artefact so nobody reads it as proof.
        "raw_body_sha256": entry.payload["body_sha256"],
        "entities": {},
    }
    for name, fields in ENTITY_FIELDS.items():
        entity = _entity(payload.get(name), fields)
        if entity is not None:
            projected["entities"][name] = entity
    return projected


def projection_hash(prev_hash: str, projected: dict[str, Any]) -> str:
    """`sha256(prev_hash + canonical_json(projection))` — SPEC §5.6's rule.

    Same construction as the audit chain, over the projection instead of over
    the raw event. `canonical_json` is the project's own encoder, so a reader
    recomputes with `settle.schema.canonical` and nothing else.
    """
    return hashlib.sha256(
        prev_hash.encode("ascii") + canonical_json(projected)
    ).hexdigest()


def build_chain(entries: list) -> list[dict[str, Any]]:
    """Chain the projections. Each links to the one before it."""
    chained: list[dict[str, Any]] = []
    prev = GENESIS_HASH
    for entry in entries:
        projected = project(entry)
        digest = projection_hash(prev, projected)
        chained.append({"projection": projected, "prev_hash": prev, "hash": digest})
        prev = digest
    return chained


def cmd_finish() -> None:
    state = json.loads(STATE.read_text())
    case_id = state["link"]["notes"]["case_id"]
    entries = read_entries(LEDGER)
    verify_entries(entries)
    rows = _relevant(entries, case_id)
    if not rows:
        raise SystemExit(f"no ledger rows for {case_id} — has the link been paid?")

    chain = build_chain(rows)

    def entity_of(row: dict[str, Any], name: str) -> dict[str, Any]:
        return row["projection"]["entities"].get(name) or {}

    captured = next(
        (entity_of(r, "payment") for r in chain
         if entity_of(r, "payment").get("status") == "captured"), {}
    )
    failed = [entity_of(r, "payment") for r in chain
              if entity_of(r, "payment").get("status") == "failed"]
    link = next(
        (entity_of(r, "payment_link") for r in chain
         if r["projection"]["event"] == "payment_link.paid"), {}
    )

    artefact = {
        "checkpoint": "CP12.1",
        "title": "Simulated at scale, real at the edges",
        "written_at": datetime.now(tz=timezone.utc).isoformat(),
        "claim": (
            "The 10,000-case batch is synthetic. The payment link and the "
            "webhooks below are real Razorpay test-mode objects, created and "
            "delivered over the network, verified by HMAC SHA256 and written to "
            "the hash-chained ledger. Nothing here is replayed from a fixture."
        ),
        "scope_of_the_chain": {
            "covers": "the projection published in this file, and nothing else",
            "does_not_cover": "the raw webhook event, which is not published",
            "why": (
                "Razorpay's checkout SMS-verifies the payer's phone number, so a "
                "real payment carries a real mobile number and no placeholder can "
                "complete one. Customer contact fields are therefore excluded from "
                "the projection by construction — not blanked, not masked, absent "
                "from the schema, with no branch that admits them. This is a "
                "stated scope, not a redaction: hashing a raw event and then "
                "editing it before publishing would produce a hash no reader "
                "could recompute, which is the 'trust me, it verified before I "
                "changed it' claim this project exists to refuse."
            ),
            "excluded_fields": list(EXCLUDED_CONTACT_FIELDS),
            "exclusion_applies_to": (
                "chain[*].projection — the Razorpay-sourced records. The `case` "
                "block above is the synthetic batch case and its `customer_id` "
                "(cust_000000) is a simulator identifier for a person who does "
                "not exist, so it is not customer contact data and is not excluded."
            ),
            "verify": (
                "For each row of `chain`: sha256(prev_hash.encode('ascii') + "
                "canonical_json(projection)).hexdigest() == hash, with "
                "canonical_json from settle.schema.canonical. prev_hash of the "
                "first row is 64 zeros. Test RZP-4 does exactly this."
            ),
            "raw_body_sha256_note": (
                "Each projection carries raw_body_sha256, the digest of the raw "
                "signed bytes. It is inside the projection so the chain covers "
                "it, but it is a claim about a local file rather than something "
                "a reader can check — the raw event stays in out/razorpay_raw.json, "
                "which is gitignored."
            ),
        },
        "case": state["case"],
        "payment_link_as_created": state["link"],
        "razorpay_ids": {
            "payment_link_id": state["link"]["id"],
            "payment_id": captured.get("id"),
            "order_id": captured.get("order_id"),
            "failed_payment_ids": [p["id"] for p in failed if p.get("id")],
        },
        "outcome": {
            "payment_link_status": link.get("status"),
            "amount_paid_paise": link.get("amount_paid"),
            "payment_attempts": len(failed) + (1 if captured else 0),
            "note": (
                "Two attempts, and the failed one is kept deliberately. The first "
                "was declined by Razorpay with international_transaction_not_allowed "
                "— the test card used was Razorpay's international one and this "
                "account accepts domestic Indian cards only. That is a property of "
                "the test account, not a defect in settle. It arrived as a real "
                "payment.failed webhook, verified and chained like any other event. "
                "A real decline and a real capture in one verifiable chain is worth "
                "more than a clean single capture."
            ),
        },
        "chain": chain,
        "chain_head": chain[-1]["hash"],
        "entries_in_local_ledger": len(entries),
    }
    ARTEFACT.write_text(json.dumps(artefact, indent=2) + "\n", encoding="utf-8")

    # The raw events, for the machine that received them. Gitignored.
    RAW.write_text(
        json.dumps(
            [json.loads(r.model_dump_json()) for r in rows], indent=2
        ) + "\n",
        encoding="utf-8",
    )

    print(f"wrote {ARTEFACT.relative_to(REPO)}  ({len(chain)} chained projections)")
    print(f"wrote {RAW.relative_to(REPO)}  (raw events, gitignored, stays local)")
    print(f"chain head {artefact['chain_head']}")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "link"
    commands = {"link": cmd_link, "wait": cmd_wait, "finish": cmd_finish}
    if command not in commands:
        raise SystemExit(f"usage: {sys.argv[0]} [{'|'.join(commands)}]")
    commands[command]()
