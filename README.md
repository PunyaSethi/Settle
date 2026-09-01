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
as a real `payment.failed` webhook (`pay_TWrJELO1R1PGeA`). To be clear about
whose fault that is: the card used was Razorpay's *international* test card and
this test account accepts domestic Indian cards only. It is a property of the
account, not a defect in `settle` — and a real decline and a real capture in one
verifiable chain is worth more than a clean single capture with the failures
edited out.

Each webhook was verified by HMAC SHA256 **before its body was parsed**, checked
against an event-id idempotency store, and written to a hash-chained ledger — the
same `LedgerEntry` chain (SPEC §5.6) the simulated runs write to.

### The artefact verifies itself, over a stated scope

`out/razorpay_demo.json` publishes a hash chain you can recompute. For each row:

```
sha256(prev_hash.encode("ascii") + canonical_json(projection)).hexdigest() == hash
```

with `canonical_json` from `settle.schema.canonical` and the first `prev_hash`
being 64 zeros. Test `RZP-4` is exactly that recomputation, and it is the
artefact's whole value.

**What the chain covers is a projection of each webhook, not the raw event.**
Razorpay's checkout SMS-verifies the payer's phone number, so a real payment
carries a real mobile number and no placeholder can complete one. That number is
not published.

The obvious alternative was to hash the raw event and strip the number before
committing. That is the one thing this artefact must not do. A hash computed over
content the reader cannot see, published beside content they can, is not
evidence — it collapses to *"trust me, it verified before I edited it"*, which is
precisely the claim about payment outcomes this project exists to refuse. Making
it about our own integrity would be self-refuting.

So the projection is built from a fixed allow-list of fields — ids, statuses,
amounts, method, order id, payment link id, notes, timestamps, event id, delivery
count, signature verification. Customer contact fields are **absent from that
schema**: not blanked, not masked, with no branch that admits them, and an
allow-list rather than a deny-list so that the next field Razorpay adds cannot
arrive by default. This is a stated scope, not a redaction. The raw events stay
on the machine that received them, in `out/razorpay_raw*.json`, gitignored.

Every Razorpay id is published verbatim: they are test-mode objects and safe.

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

```
python scripts/razorpay_demo.py
```

`RAZORPAY_MOCK_MODE` defaults to **true**, so that command works in a fresh
clone with no `.env` and mints a `MOCK_SANDBOX` record. The opposite default is
the dangerous one: a missing key that quietly produces a plausible-looking id.

To run against real test mode, put `RAZORPAY_KEY_ID` (must start `rzp_test_`),
`RAZORPAY_KEY_SECRET` and `RAZORPAY_WEBHOOK_SECRET` in `.env` and set
`RAZORPAY_MOCK_MODE=false`. Live keys are refused outright — there is no live
path, and this project moves no money.

```
uvicorn settle.api.app:app --port 8002        # the receiver
python scripts/razorpay_demo.py link          # create a link
python scripts/razorpay_demo.py wait          # poll for the webhook
python scripts/razorpay_demo.py finish        # write out/razorpay_demo.json
```

Three routes, and exactly three (SPEC §16). `POST /webhooks/razorpay` works;
`POST /voice/extract` and `GET /` are declared and return 501 until D5.

Use a **domestic** test card (`5267 3181 8797 5449`) — see the decline above for
what happens otherwise.

## Known limitations

SPEC §19 fixes this as the last section of the finished README. It is opened
here at CP12.2 with the Razorpay edge's limitations, because they were being
decided one at a time and needed somewhere to live.

**It is not yet complete.** Six places in SPEC.md and PRIORS.md already say
"recorded in Known Limitations" against entries that are not below yet: partial
and reduced debit amounts being out of scope (§5.3), email as a channel (§5.3),
treating a G9 pre-debit notice as a full contact rather than as regulatory
overhead exempt from G2 (§12), the TRAI DND transactional-message exemption
(§12 G11), the G1 / TRAI time-band ambiguity (A98), and one PRIORS row that
could not be verified. Those are named in their own sections and land here in
D5. Listing them now is the point: a Known Limitations section that reads as
complete while six entries are outstanding is worse than none.

### The Razorpay edge

**One payment link per case, ever.** Payment links use
`reference_id = case_id`, which Razorpay enforces as unique. This gives free
vendor-side idempotency: a case cannot accidentally receive two links. It also
means a case that legitimately needs a second link — the first expired, the
customer asked again — cannot get one. Accepted deliberately; production would
use `case_id` plus an attempt counter.

**The committed chain covers a projection, not the raw event.** Razorpay's
checkout SMS-verifies the payer's phone number, so a real payment carries a real
mobile number and there is no placeholder that completes one. Customer contact
fields are therefore absent from the projection schema that
`out/razorpay_demo.json` publishes and hashes. What a reader can verify is that
the published records hash as claimed and are internally consistent; what they
cannot verify is that the projection faithfully reflects a raw event they have
never seen. That gap is real and is the price of not publishing the number. The
alternative — hashing the raw event and stripping the number afterwards — moves
the gap somewhere worse, because then *nothing* recomputes. The raw events stay
on the machine that received them.

**The demo's first payment attempt was declined for an environmental reason.**
`international_transaction_not_allowed`: the card used was Razorpay's
international test card and the test account accepts domestic Indian cards only.
It is kept in the artefact because a real decline alongside a real capture in
one verifiable chain is worth more than a clean single capture. It is a property
of the test account and demonstrates nothing about `settle`'s own behaviour in
either direction — it should not be read as a bug, and equally should not be
read as evidence that decline handling works.

**The edge is one link, not a load test.** Three webhook deliveries, one case.
It shows the pipeline terminates in something real. It says nothing about
throughput, concurrent delivery, or behaviour under Razorpay's retry storm — the
duplicate-delivery path is covered by WBH-4 in tests and has never been exercised
by a real retry.
