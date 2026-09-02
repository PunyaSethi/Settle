# settle — known limitations

SPEC §19 fixes this as the last section of the README. It is a file of its own
because it outgrew a section, and because a limitations list that has to compete
for space with a results table loses.

Every entry states the limitation **and what it costs**. A limitation that only
restates a design decision is a feature description wearing a hair shirt.

Nothing here is a surprise found late. Each entry is either a boundary drawn on
purpose, a measurement that came back against us, or a bug we shipped and fixed.
The three categories are labelled, because they are not the same kind of thing
and a reader deciding how much to trust this work should be able to tell them
apart.

---

## 1. Scope, drawn on purpose

### Partial and reduced debit amounts are out of scope

`Action` carries no amount field. Every retry is for the full outstanding
amount, and `mandate_cap_paise` is read by the gates but never used to size a
smaller debit.

**What it costs.** Reduced-amount retries are common in real recovery and are
often the difference between a `willing_broke` customer paying something and
paying nothing. Excluding them means the policy cannot express the single most
natural response to insufficient funds, and our `willing_broke` numbers are
therefore a floor rather than an estimate. A continuous amount dimension
multiplies the action space and the estimator's training burden, and at 9 days
that was the trade. It is the first thing to add. (SPEC §5.3, A17)

### Email is not a channel

`Channel` is exactly `sms | whatsapp | voice`.

**What it costs.** G7's "no contact after opt-out, on every channel" is complete
only against that list. A production system with email would need G7 re-proved
against it, and the opt-out guarantee we report does not automatically extend.
The limitation is in the guarantee, not just in the coverage. (SPEC §5.3, A33)

### A pre-debit notice is charged as a full contact

Under G9 a served e-mandate notice costs G2 budget and `patience_budget` like
any other contact.

**What it costs.** The alternative reading — that a regulator-mandated
notification is overhead and should be exempt from a nuisance budget — is
defensible and we did not take it. Under our treatment a compliant `enach` retry
consumes two of three weekly contacts, which makes `enach` look more expensive
to work than it may actually be, and biases the policy away from that rail. We
chose the conservative direction deliberately, but it is a choice and the number
moves if you make it the other way. (SPEC §12 G9, A25)

### The TRAI DND exemption is asserted, not sourced

G11 blocks `voice_call` on a DND flag but permits SMS and WhatsApp, on the
reading that DND covers unsolicited commercial contact and a transactional
message to an existing customer about a failed payment is exempt.

**What it costs.** We could not extract that exemption from the primary
regulation. If the reading is wrong, every SMS and WhatsApp message OURS sends
to a DND-flagged customer is a compliance breach, and our "zero compliance
violations" claim would not survive. It is asserted, and it is load-bearing.
(SPEC §12 G11)

### G1's contact window may open an hour early

G1 permits contact from 08:00 IST. The TRAI time band may begin at 09:00.

**What it costs.** We could not resolve the clause from the primary text, so the
window is deliberately unchanged rather than guessed in either direction. If
09:00 is correct, a share of OURS's dispatches sit outside the permitted band
and are violations we are not counting. We report this rather than quietly
moving the constant to whichever value makes our compliance column look better.
(SPEC §12, A98)

### The Razorpay edge is one link, not a load test

Three webhook deliveries, one case, one payment.

**What it costs.** It shows the pipeline terminates in something real. It says
nothing about throughput, concurrent delivery, or behaviour under Razorpay's
retry storm. The duplicate-delivery path is covered by WBH-4 in tests and has
never been exercised by an actual Razorpay retry.

### One payment link per case, ever

Payment links use `reference_id = case_id`, which Razorpay enforces as unique.

**What it costs.** Free vendor-side idempotency: a case cannot accidentally
receive two links. But a case that legitimately needs a second link — the first
expired, the customer asked again — cannot get one. Accepted deliberately;
production would use `case_id` plus an attempt counter.

### The committed artefact's chain covers a projection, not the raw event

Razorpay's checkout SMS-verifies the payer's phone number, so a real payment
carries a real mobile number and no placeholder completes one. Contact fields
are absent from the projection schema that `out/razorpay_demo.json` publishes
and hashes.

**What it costs.** A reader can verify that the published records hash as
claimed and are internally consistent. They cannot verify that the projection
faithfully reflects a raw event they have never seen. That gap is real. The
alternative — hashing the raw event and stripping the number afterwards — moves
the gap somewhere strictly worse, because then *nothing* recomputes.

---

## 2. Measurements that came back against us

These are the entries a reader should weigh most heavily. They are results we
would have preferred not to get, reported at the same volume as the ones we
would.

### Retry timing was a differentiator, and it is not

We hypothesised that learning *when* to retry — reaching a customer's liquidity
window — would be a substantial part of the win. We tested it and withdrew the
claim.

The three liquidity features rank 22th, 34th and 40th of 46 by
permutation importance. The model's probability does move across the eight
offsets — median spread 6.0 percentage points over 1,770 retry rows — but it is
not moving because it has found paydays.

`days_since_last_attempt` is a separate matter and is worth stating plainly
rather than folding into the same sentence. It ranks 2nd of 46, which is
near the top. That is a claim about **recency**, not about liquidity: how long
it has been since the last attempt is a different hypothesis from whether today
is a payday, and it is the one that survived. `train.py` groups all four as
"timing features", which understates one and overstates the other.

**What it costs.** A coarse signal exists; a liquidity curve does not. The
retry-timing story is not available to us, and a chunk of what makes this
project interesting has to come from the observability layer instead. We record
this as a measured negative result rather than deleting the hypothesis and
writing the README as though we had never held it. (SPEC §10.1, A83)

**A note on these two numbers.** Until CP13.1 they existed only in `train.py`'s
stdout, and the figures the README carried — 3.7 points, ranks 26–37 of 45 —
reproduced neither. They predated A93, which recomputed
`days_since_last_attempt` at the dispatch moment and added a 46th feature. They
were carried forward from an old training log without the training being re-run.
They are computed into `out/metrics.json` now and checked by CHT-3, which is the
whole argument for that test in one incident.

### 184 of 188 priors are asserted, not sourced

The sourcing pass attached a public citation to every PARAMS and POLICY_PARAMS
row that could carry one. The result: **1 SOURCED, 3 DERIVED, 184 ASSERTED**.

The reason is structural, not laziness. Indian payments data is published as
system-wide aggregate — NPCI's monthly UPI volumes, RBI's e-mandate circulars,
issuer-level decline summaries. This model concerns the *conditional* behaviour
of a customer whose recurring debit has already failed: how likely they are to
pay after a second retry at a different hour, how much patience they have before
a message becomes harassment, whether a dead mandate can be revived. Nobody
publishes that. It sits inside individual merchants' data and does not come out.

**What it costs.** Every headline number rests on a parameter set that is mostly
our judgement. This is why §15.2's sensitivity sweep exists and why it is
reported in the README body rather than an appendix — it is the only honest
answer available. Near-miss rows were left ASSERTED deliberately rather than
citing a source that nearly fits, each carrying its near-miss in its own cell.
(SPEC §15.1, A94, A100)

### The shipped model has worse overall calibration than the one we rejected

Two numbers, both reported:

| | overall ECE | uplift ECE | decisions it cannot separate |
|---|---|---|---|
| **GBM** (shipped) | **0.0392** | **0.0176** | 0.0% |
| GBM + isotonic (rejected) | **0.0160** | 0.0193 | 11.5% |

The rejected model is more than twice as well calibrated on the level of the
probability. We shipped the worse one on purpose. §10.2 computes expected value
as uplift over `do_nothing`, so the policy consumes only the *difference*
between two probabilities and never either one alone. Isotonic regression
flattened that difference: it returned an identical probability for every option
on 11.5% of real multi-option decisions, which makes the argmax a coin toss on
those cases whatever the calibration says.

**What it costs.** If you read a probability off this model and act on its
level — sizing a provision, quoting a likelihood to a customer — you are using a
number that is measurably 2.5x less accurate than one we could have shipped. The
model is fit for the decision it was selected for and worse for any other. We
state both numbers rather than the flattering one. (SPEC §10.1, A84, A92)

### The headline flips at 4x on an asserted number

Three of fourteen swept members lose the headline conclusion at the top of the
range: `mandate_update.success_rate.*`, `contact_response.rate.*`, and
`contact_response.behaviour_multiplier.*`.

`contact_response.rate.*` is the one that should worry a reader. It governs how
often a dispatched contact causes a customer to go and pay. It is ASSERTED. Push
it to 4x — a world where messaging people works four times better than we assume
— and B2's ladder climbs past OURS.

**What it costs.** The flip is in exactly the direction a sceptical reader would
guess: our restraint result depends on contacting not being very effective, and
we set the number that decides that. Every flip is B2 climbing rather than OURS
falling, which is a mild consolation and not a rebuttal. Eleven of fourteen
members hold across the full 16x range. (SPEC §15.2, A95, A99)

### Seven of fourteen swept parameters leave OURS completely unmoved

Not because the policy is robust. Because it makes 36 contacts in 10,000 cases,
and a parameter describing what happens when you contact someone cannot move an
arm that does not contact anyone.

**What it costs.** The margin over B2 is not a claim that our contacts
outperform B2's contacts. It is a claim that contacting is mostly not worth
doing. Those are different claims and only the second is supported. A reader who
takes the sensitivity sweep as evidence of a robust *contact policy* has read it
wrong, and this sentence exists so they do not. (SPEC §15.2, A99)

### Incremental scoring discards timing value, understating our own result

A case OURS recovers on day 3 and B0 recovers on day 28 scores as **zero**.

**What it costs.** 25 days of avoided churn risk and a cycle of float, counted
as nothing. This one runs against us, and we keep it because the alternative —
attribution windows — is where recovery products go to flatter themselves.
`median_days_to_recovery` is reported as a secondary metric. (SPEC §14.3, A15)

---

## 3. The auditor, and the limit of what we can prove

### The auditor was built for the wrong direction of error

It was designed to catch overstatement — money claimed and never settled. The
measured error runs the other way for every arm: reported-minus-reconciled is
negative throughout, because the observability layer drops settlement webhooks.
OURS believed 4,146 cases recovered when 5,122 had settled.

**What it costs.** SF-1 (marked recovered, never settled) is the class the design
was oriented around, and it is real but small: 138 cases for OURS. The larger
error is 976 settlements the agent never learned about, and the harm from those
is SF-2 — chasing someone who has already paid. Our SF-2 is 1 against the fixed
ladder's 86, but that is a consequence of contacting 36 times rather than 14,027,
not of the auditor being good at finding them. A system with our observability
and B2's contact volume would produce B2's SF-2 count.

### The silent-failure auditor is validated only in simulation

The reconciliation *mechanism* transfers to production. Against live Razorpay it
joins on `payment_id` against the Settlement Recon endpoint, which returns
transactions settled on a given day or month. There is no per-payment settled
flag to query — the Settlements API returns aggregate entities with no
per-payment breakdown — so production reconciliation is necessarily a lagged
batch join against a date-scoped report. That is exactly the architecture built
here. The design is forced by the real API, not an artefact of simulation.

**What does not transfer is our ability to measure the auditor's own accuracy.**
In simulation we hold `HiddenTruth` and therefore know what the auditor missed.
In production we would not. Every `silent_failure_rate` in this repo is a
measurement of a detector against a world we constructed.

**What it costs.** The number we are proudest of is the one we cannot prove
outside our own simulator. A judge should read `silent_failure_rate` as
"this detector catches N% of failures *in a world where we know the answer*",
and nothing stronger. The auditor has never been pointed at live money. (SPEC §7)

---

## 4. Bugs we shipped, found, and fixed

Three defects in the world model survived into working code and each one
invalidated a headline before it was corrected. They are listed because a build
log that contains no mistakes is a build log that is not being kept honestly,
and because each was caught by a test we had to write after the fact.

| Bug | What was wrong | What it invalidated |
|---|---|---|
| **Scheduling fired immediately** | `retry(at_hour_offset=n)` was dispatched at once and the offset kept only as a wake-up hint. The offset dimension of the action grid carried no behaviour. | Every retry-timing result before CP5.1. The estimator was being asked to learn a dimension that did not exist. (OQ-30, A73) |
| **Dead instruments were unrecoverable** | `class_retry_cap.dead_instrument` was 0 and nothing could revive a dead mandate, so 17% of the batch was structurally unwinnable by any arm. | Every arm's ceiling. Recovery rates before CP9.1 were measured against a world where a sixth of the cases could not be recovered at all. (OQ-51, A86) |
| **Contacts could not produce settlements** | `world.attempt()` ran for debits only. `action_lift.send_message`, `.voice_call` and `.escalate_human` carried priors while sitting in a branch only debits reached. | The project's primary claim. "Same recovery, far fewer contacts" was trivially true, because contacts were structurally incapable of recovering anything. (OQ-51, A89) |

The third is the one worth dwelling on. It made our headline result true by
construction for four checkpoints, and it looked exactly like a win. It was
found by asking "which verbs can reach `attempt()`?" rather than by any test
failing. WLD-9 now asserts that every verb routes to `attempt()`, to
`contact_payment()`, or to a declared zero lift with a stated reason — a general
guard, because the specific bug would have been fixed by a specific test and the
next one would have looked different.

---

## 5. What we would do next, in order

1. **Reduced-amount retries.** The largest single gap in the action space.
2. **Fit `contact_response.rate.*` to real data.** It is the parameter that
   decides the headline and it is currently ours.
3. **Point the auditor at live settlement data.** Not to improve it — to find
   out how much of its simulated accuracy is real.
4. **Re-prove G7 against email**, if email is added.
5. **Resolve the G1 / TRAI band** from the primary regulation rather than
   documenting the ambiguity.
