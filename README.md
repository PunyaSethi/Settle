# settle

A recovery agent that is correct when it cannot trust what it is told about
outcomes.

> The README proper — headline metrics, architecture diagram, reliability curve,
> one-command reproduction, how the thresholds were chosen, priors and
> provenance, known limitations, in that order — is fixed by SPEC §19 and lands
> in D5. This section is here early because it is the one claim that has to be
> made before any number is read, not after.

## Simulated at scale, real at the edges

**The batch is synthetic.** All 10,000 cases in `out/batch.jsonl` come from a
seeded generator in `settle/sim/`. Nobody's card was declined, no money moved,
and every recovery number in this repo is measured against a world we built. We
say so first because it is the reason measurement is possible at all: the
simulator holds a `HiddenTruth` the agent never sees, which is what lets us
report how often the system was *wrong* about an outcome rather than only what
it did. A live pilot could not produce that number.

**One object in this repo is real.** `out/razorpay_demo.json` records a single
Razorpay **test-mode** payment link, created over the network against the real
API, paid in a browser with a test card, and reported back by a real webhook
delivered through a real tunnel. Nothing about it is replayed from a fixture.

| | |
|---|---|
| Payment link | `plink_TWr7e2EFJ8ITvn` |
| Payment (captured) | `pay_TWrM4OohnxgMOu` |
| Order | `order_TWr8cmMDYa9lph` |
| Joined to case | `case_000000` — ₹3,256.35, card, `insufficient_funds` |
| Webhooks received | `payment.failed`, `payment.captured`, `payment_link.paid` |

It took two attempts, and the first one failing is the more useful half. Razorpay
declined it with `international_transaction_not_allowed` and reported that back
as a real `payment.failed` webhook (`pay_TWrJELO1R1PGeA`) — a genuine failed
payment, verified and written to the ledger like any other event, rather than a
happy path with the failures edited out.

The webhook that reported it was verified by HMAC SHA256 before its body was
parsed, checked against an event-id idempotency store, and written to a
hash-chained ledger — the same `LedgerEntry` chain (SPEC §5.6) the simulated
runs write to, and it verifies with the same `python -m settle.audit.verify`.
The full trace, including the raw event and the ledger row with its hashes, is
in `out/razorpay_demo.json`.

One field in that file is redacted, and it is worth being explicit about the
cost. Razorpay's checkout autofilled the account holder's real mobile number, so
`contact` is removed from the committed events. The `hash` and `prev_hash`
values were computed over the event as received, *before* that removal — so
recomputing a hash from the payload as printed will not match, and the artefact
says so in its own `redaction` block. The unredacted chain verifies on the
machine that received it. Every Razorpay id is left verbatim: they are test-mode
objects and safe to publish.

The point is narrow and worth stating plainly: it demonstrates that the pipeline
terminates in something real, and nothing more. It is not evidence that the
policy recovers money. A payment link created is not revenue recovered.

### Which is which is never left to the reader

Every payment link record carries an explicit `source`:

| `source` | What it is |
|---|---|
| `RAZORPAY_TEST_MODE` | A real object in Razorpay's test mode. `plink_…`, resolvable URL, visible in the dashboard. |
| `MOCK_SANDBOX` | Constructed locally. `MOCK_plink_…`, and a URL on the reserved `.invalid` TLD that cannot resolve for anyone. |

The pairing is validated by the model, not left to discipline: a record carrying
a locally-minted id cannot be labelled `RAZORPAY_TEST_MODE`, and the records are
frozen, so neither can be relabelled after the fact. Paste a mock id into the
Razorpay dashboard and it finds nothing; click a mock URL and it fails to
resolve. Both failures are loud, which is the point — the quiet version of this
mistake is a demo that shows a synthetic id and calls it a payment.

### Running it without credentials

`RAZORPAY_MOCK_MODE` defaults to **true**, so cloning this repo and running it
with no `.env` gives you a working demo that mints `MOCK_SANDBOX` records. The
opposite default is the dangerous one: a missing key that quietly produces a
plausible-looking id.

To run against real test mode, put `RAZORPAY_KEY_ID` (must start `rzp_test_`),
`RAZORPAY_KEY_SECRET` and `RAZORPAY_WEBHOOK_SECRET` in `.env` and set
`RAZORPAY_MOCK_MODE=false`. Live keys are refused outright — there is no live
path, and this project moves no money.

```
uvicorn settle.api.app:app --port 8002
```

Three routes, and exactly three (SPEC §16). `POST /webhooks/razorpay` works;
`POST /voice/extract` and `GET /` are declared and return 501 until D5.
