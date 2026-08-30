# settle — SPEC v1

Razorpay AI Buildathon 2026, Track 03 (AI Revenue Recovery).
Frozen document. Changes require an explicit amendment note at the bottom.

## 1. Thesis

Most recovery agents are graded on what they *did*. `settle` is graded on what
the money did, and on how often the system was wrong about it.

Every comparable system assumes perfect observability: the executor acts, the
outcome is reported, the outcome is believed. In production that is false.
Webhooks drop and replay. Authorisation is not settlement. Captured payments
reverse. A customer who has already paid keeps getting chased because the
confirmation never arrived.

`settle` models unreliable outcome reporting explicitly, runs an independent
reconciliation pass that does not trust the executor's own account of events,
and reports a `silent_failure_rate` next to every recovery number.

**One-line pitch:** A recovery agent that is correct when it cannot trust what
it is told about outcomes.

## 2. Loss type

Primary: **failed subscription mandates / recurring debits** (card, UPI Autopay,
e-NACH).

Secondary slice: 15% of cases are escalation-eligible (high value, retries
exhausted). After gates and stops, ~2-3% actually reach voice contact. This
slice is where promise-to-pay lives.

Out of scope as a lane: checkout abandonment, B2B invoice chasing, disputes,
refunds.

### 2.1 Escalation eligibility

`is_escalation_eligible(case)` reads only `ObservedCase` fields
(`amount_paise >= escalation.min_amount_paise` AND
`attempt_number >= escalation.min_attempt_number`).

It lives in `settle/policy/escalation.py` and `settle/sim/generator.py` imports
it from there. The dependency runs sim -> policy and never policy -> sim: the
latter is an INV-8 breach. A46 required the policy to recompute from
observables; it could not, because the rule lived inside the package the policy
may not import.

## 3. Scope

| In | Out |
|---|---|
| One loss type, deep | Multiple loss types, shallow |
| Batch of 10,000+ synthetic cases, fixed seed | Real customer data |
| Unreliable observability layer | Perfect outcome reporting |
| Independent reconciliation pass | Trusting the executor |
| Adversarial debtor behaviours | Cooperative-payer-only simulation |
| Razorpay Test Mode at the edges | Live money movement |
| Hinglish promise extraction, one segment | Voice across the whole batch |
| Three-screen viewer: batch, case trace, voice lab | Dashboards, Streamlit, React |
| Calibrated tabular model | Fine-tuned or custom-trained LLM |
| Three FastAPI routes (webhook, voice extract, viewer) | Multi-endpoint service, auth, tenancy |

## 4. Non-negotiable invariants

Gates always evaluate and always log. What varies is whether the verdict is
binding. ENFORCE blocks the dispatch; OBSERVE logs the violation and permits it.
Gates are never bypassed, skipped, or reimplemented per arm — there is one gate
implementation and one code path. Arms OURS, EXPLORE, B0, B1, B2 and LLM-STRAT
run in ENFORCE. Only B3 runs in OBSERVE, and its violations are counted and
reported. The invariants below hold unconditionally for any arm in ENFORCE.
Each has a named test.

| ID | Invariant |
|---|---|
| INV-1 | No case is marked RECOVERED without a settlement record, not an authorisation |
| INV-2 | No contact is dispatched outside 08:00–19:00 IST |
| INV-3 | No contact after opt-out, ever, on any channel |
| INV-4 | No two dispatches share an idempotency key |
| INV-5 | Audit entry is written BEFORE dispatch, never after |
| INV-6 | Audit chain verifies: every entry references the previous entry's hash |
| INV-7 | No contact between a logged promise and its promise date |
| INV-8 | Hidden truth is readable by exactly three packages: `settle/sim/` constructs it, `settle/execute/` is the world boundary that produces it, and `settle/recon/` compares belief against it (§7). Every other package — `agent`, `policy`, `schema`, `runner`, `audit`, `diagnose` — is banned from importing `settle.sim.truth`. |
| INV-9 | In arm OURS, no LLM output is ever an action. The text reader emits spans over text only. The LLM-STRAT ablation arm proposes actions by design; those proposals are clamped to the closed verb set of §5.3 by a deterministic adapter and pass the same gates in ENFORCE mode. |
| INV-10 | Every numeric prior in the model traces to a cited source or is marked ASSERTED |
| INV-11 | Arm OURS can never run in OBSERVE mode. Asserted by test. |

INV-8 is enforced structurally: hidden fields live on a separate object that is
not passed into the agent package. A test asserts the agent package never
imports from `settle.sim.truth`.

## 5. Domain model — FROZEN

Changing any of these after CP1 requires an amendment note.

### 5.1 ObservedCase — what the agent can see

```
case_id            str
created_at         datetime      # as_of anchor; never wall clock, never mtime
customer_id        str
amount_paise       int
rail               enum          card | upi_autopay | enach
decline_code       str           # raw gateway string
decline_reason     str           # raw gateway text
attempt_number     int >= 1
mandate_state      enum          active | expired | revoked | none
mandate_cap_paise  int | null
tenure_months      int >= 0
prior_failures     int >= 0
prior_recoveries   int >= 0
plan_value_paise   int
observed_credit_day int | null   # 1..28; inferred salary day, often null
consent_whatsapp   bool
dnd_flag           bool
language           enum          en | hi | hinglish
```

### 5.2 HiddenTruth — simulator only, NEVER visible to the agent

```
case_id             str
true_recoverability float 0..1
intent_type         enum   willing_able | willing_broke | disputing | churned | adversarial
patience_budget     int          # contacts before disengagement
payday_day          int 1..28
response_fn_params  object       # drives P(pay | action, hour)
will_settle         bool         # if authorised, does it actually settle
settlement_lag_h    int
will_reverse        bool
```

`escalation_eligible` is not hidden truth and is not a field of either model. It
is derivable from `ObservedCase` alone by the `escalation.*` rule in PARAMS
(§2's high-value slice), and the generator records it on the sim-side container
purely so the realised mix can be reported. Any policy that needs it MUST
recompute it from observables. Test GEN-6 asserts the recomputation matches the
recorded flag for every case in the batch — if it ever stops matching, the flag
has become a channel.

### 5.3 Action — closed verb set

```
do_nothing
retry(at_hour_offset: int, rail: enum)
switch_rail(to: enum)
send_message(channel: Channel, template_id: str)
request_mandate_update(channel: Channel)
serve_notice(channel: Channel)
escalate_human
voice_call                      # high-value slice only

Channel = sms | whatsapp | voice
```

`do_nothing` is a first-class action and must be selectable. Any arm that cannot
emit `do_nothing` is a baseline, not a policy.

The agent's belief about liquidity (`liquidity_window_days_belief`) is a
`POLICY_PARAMS` entry, deliberately distinct from the world's
`world.liquidity_window_days`. The agent is not permitted to be right by
construction: a policy handed the simulator's own parameter would demonstrate
that we can read our own generator, not that a merchant could learn the effect.

Actions carry no amount field. Partial or reduced debits against
`mandate_cap_paise` are common in real recovery and are deliberately out of
scope: a continuous amount dimension multiplies the action space and the
estimator's training burden. Recorded in Known Limitations.

Email is out of scope; recorded in Known Limitations. G7's
opt-out-on-every-channel is bounded by exactly this list.

**The action grid.** Actions carry parameters, so the space must be finite,
declared, and identical everywhere. `POLICY_PARAMS` declares it:

```
action_grid.hour_offsets   (0, 6, 18, 30, 48, 72, 120, 168)   hours
action_grid.max_horizon_h  720                                <= decision_horizon_days
```

Eight offsets, spanning the two dimensions the policy actually needs. `0` and
`6` are a different hour of the same day; `18` and `30` a different hour of
another day; `48`, `72`, `120` and `168` the same hour some days later. §9 asks
`ambiguous` for "one retry at a different hour", which requires an offset that
is not a multiple of 24. Reaching a liquidity window requires multi-day offsets.

**Binding constraint.** This is the same grid the OURS policy searches. EXPLORE
and OURS enumerate candidates through one function, and neither may widen or
narrow it locally. An estimator trained on one grid and queried on another has
zero coverage exactly where it is asked to predict, and its held-out
calibration would look fine because the held-out set shares the same blind spot.

Only `retry` carries a schedulable offset in the frozen verb set above, so the
grid widens retries and evaluates every other verb at the runner's current hour.
Widening the offset dimension to contacts needs a §5.3 amendment.

`serve_notice` is an explicit verb, not an executor side effect. Under G9 a
served notice is a full contact costing G2 budget and patience_budget (§20). If
the executor served notices implicitly the policy would never price them, and
the notice-then-debit sequencing G9 exists to force would be invisible to the
decision. On `enach` the agent must choose to spend a contact on notice before
it can legally debit outside an active window.

### 5.4 Decision

```
decision_id       str
case_id           str
at                datetime
action            Action
p_success         float          # calibrated, from estimator
expected_value    int            # paise
alternatives      list[Alternative]
chosen_by         enum   heuristic | model | llm
reason_code       str
arm               str
propensity        float | null   # (0, 1]; logged at draw time by EXPLORE, null elsewhere
arm_mode          enum   ENFORCE | OBSERVE
```

```
Alternative
  action      Action
  p_success   float
  ev_paise    int
  legal       bool          # false if a gate would have blocked it
  block_gate  str | null    # which gate, when legal is false
```

A bare triple is unreadable in audit JSONL, which is a reviewed deliverable.
Recording why a rejected alternative was rejected — and whether it was rejected
on economics or blocked by a gate — is the difference between a decision log and
a number dump.

### 5.5 ReportedOutcome vs ActualOutcome

The split that carries the project.

```
ReportedOutcome                  ActualOutcome (hidden)
  case_id                          case_id
  at                               at
  status  captured|failed|none     settled  bool
  payment_id  str | null           settled_at  datetime | null
  amount_paise int | null          reversed  bool
  arrival_count int >= 1           amount_paise int | null
  reply_text  str | null
```

`reply_text` is what the customer said back, if anything. The agent reads text,
never intent: §11's classifier turns it into a verdict, and a hedged reply turns
into nothing at all.

The agent reads `ReportedOutcome` only. Reconciliation compares the two.

### 5.6 LedgerEntry — append-only, hash-chained

```
seq          int
case_id      str
at           datetime
kind         enum  event | diagnosis | decision | gate_check | dispatch |
                   reported_outcome | reconciliation | stop
actor        enum  system | policy | model | llm | human
payload      json
reason_code  str
prev_hash    str
hash         str   sha256(prev_hash + canonical_json(entry_without_hash))
```

Written to `out/audit.jsonl`. Verifiable by `python -m settle.audit.verify`.

### 5.7 CaseState — what the gates read

Implied throughout §12 and §13 but never specified. Gates cannot be pure without
it.

```
CaseState
  case_id            str
  arm                str
  arm_mode           ArmMode
  status             enum  open | stopped
  stop_reason        str | null
  stop_class         StopClass | null
  attempts_used      int                 # retries only; read by G4 and G10
  rail_switches_used int                 # a switch is a change of instrument
  card_submissions_used int              # G4; a retry on card or a switch to card
  contacts_used      int
  contact_history    list[datetime]      # for G2's rolling window
  last_contact_at    datetime | null
  opted_out          bool
  disputed           bool
  promise_date       date | null
  promise_logged_at  datetime | null
  notice_window_until datetime | null    # G9
  dispatched_keys    frozenset[str]      # G5 idempotency
  settled            bool                # S1; a settlement, never an authorisation
  settled_at         datetime | null
  scheduled          Scheduled | null    # one pending commitment, at most
  tick               int                 # hours since case created_at
```

```
Scheduled
  action           Action
  due_tick         int
  scheduled_at     int                 # tick when it was chosen
```

`retry(at_hour_offset=n)` is a commitment to debit in n hours, not a debit now
with a note attached. A schedulable choice sets `scheduled` and the runner
sleeps to `due_tick`.

**Gates are re-evaluated when it fires.** Circumstances change between choosing
and firing: the customer may have opted out, promised, or raised a dispute. An
action that fires on a verdict taken three days earlier is a compliance hole,
and it is the shape of bug a replayed webhook would exploit. A blocked schedule
is logged and cleared, never silently dropped, and control returns to the arm.

At most one commitment is pending. A second choice replaces it and the
replacement is logged: a queue of scheduled actions is a queue of decisions
taken under circumstances that no longer hold. A commitment due past
`decision_horizon_days` never fires — S6 stops the case first.

Collections are frozen types, not `list` and `set`. A frozen model holding a
mutable list is only half frozen, and a `set`'s iteration order varies with
`PYTHONHASHSEED` — two processes would serialise the same state differently and
GEN-1 would stop holding. `contact_history` is a `tuple`; `dispatched_keys` is a
`frozenset` serialised sorted.

A rail switch is a change of instrument, not a retry. Counting it against a
class retry budget makes `switch_rail` unusable for `auth_abandoned`, the one
class whose recovery path it is. `attempts_used` and `rail_switches_used` are
therefore separate, and G10 reads only the first.

S1 reads `settled`. It is a recorded field rather than an argument to
`check_stops` because a caller-supplied bool is inference by another name, and
§5.7's rule is that state transitions are recorded, never inferred.

State transitions are recorded, never inferred. Any quantity a gate needs is a
field here, not a derived scan of the ledger.

The evaluation instant is `case.created_at + tick hours`. It is derived from the
case's own anchor and never from a clock, which is what lets every gate be a
pure function of its arguments.

## 6. The observability layer — DIFFERENTIATOR

`settle/sim/observability.py` sits between the world and the agent. Every
outcome the agent learns about passes through it.

| Parameter | Effect |
|---|---|
| `webhook_drop_rate` | Outcome never reported. Agent keeps chasing a paid customer. |
| `webhook_duplicate_rate` | Outcome reported 2+ times. Tests INV-4. |
| `out_of_order_rate` | Events arrive in the wrong sequence |
| `settlement_lag_reporting` | Delay between money settling and the settlement being reported |
| `reversal_reporting_delay` | Delay between a reversal happening and it being reported |

All five default to non-zero. A run with all five at zero is available as
`--perfect-observability` and exists only to quantify what the layer costs.

`auth_no_settle_rate` is deliberately NOT an observability parameter. Whether an
authorised payment actually settles is a fact about the bank, not about our
reporting layer. Placing it here would mean `--perfect-observability` zeroes it,
making authorisation equivalent to settlement and silently abolishing SF-1 — a
real-world failure class, not a reporting artefact. It lives in world PARAMS.

`--perfect-observability` zeroes only the five reporting-layer parameters:
`webhook_drop_rate`, `webhook_duplicate_rate`, `out_of_order_rate`,
`settlement_lag_reporting`, `reversal_reporting_delay`. The world still fails to
settle, still reverses, still lags. The flag measures what unreliable reporting
costs, not what a perfect world would pay.

## 7. Silent failure taxonomy — DIFFERENTIATOR

`settle/recon/silent_failures.py`. Each class is detected by comparing the
ledger against `ActualOutcome`, independently of the executor.

| ID | Failure | Harm |
|---|---|---|
| SF-1 | Marked recovered, never settled | Overstated revenue |
| SF-2 | Settled, never reported — chasing a paid customer | Direct customer harm |
| SF-3 | Duplicate outcome caused duplicate contact | Harassment, INV-4 breach |
| SF-4 | Promise logged, date passed, no follow-up | Lost recovery |
| SF-5 | Dispatch after opt-out | Compliance breach |
| SF-6 | Dispatch outside contact window | Compliance breach |
| SF-7 | Recovered then reversed, case never reopened | Overstated revenue |

**Reconciliation runs at `observation_horizon_days` (60), not at the decision
horizon (30).** Settlements land late and reversals land later; a reconciler
that stopped when the agent stopped acting could not see the tail it exists to
audit. An outcome landing past day 60 is marked `censored` and reported as such,
never guessed.

`settle/recon/` is permitted to import `settle.sim.truth` — a narrow, named
extension of INV-8's permitted set, which is exactly `sim`, `execute` and
`recon`. `execute` has held that access since CP4 as the world boundary; INV-8's
wording had not named it, which is how an exception becomes a habit. It exists to compare
what was believed against what happened, which is impossible without both. The
exception is exactly this package and no other, and REC-6 asserts no third
module has quietly joined it. An unstated exception is how INV-8 dies.

SF-5 and SF-6 are different in kind from the rest. They are compliance breaches,
and for any arm in ENFORCE they must be zero. A non-zero SF-5 or SF-6 for OURS
is a gate failure, not an audit finding, and the run says so loudly.

`silent_failure_rate` is reported in the headline metrics table, not in an
appendix. The demo batch contains deliberately seeded instances of each class so
the auditor is visibly catching something. A detector that always reports zero is
indistinguishable from a broken detector.

**What transfers to production, precisely.** Against live Razorpay,
reconciliation joins on `payment_id` against the Settlement Recon endpoint,
which returns transactions settled on a given day or month. There is no
per-payment settled flag to query: the Settlements API returns aggregate
settlement entities (id, amount, status, fees, tax, utr) with no per-payment
breakdown. Production reconciliation is therefore necessarily a lagged batch
join against a date-scoped report — which is the architecture built here. The
design is forced by the real API, not an artefact of simulation.

What does not transfer is our ability to measure the auditor's own accuracy: in
simulation we know what it missed, in production we would not.

README section 1 must state this distinction and must not imply the auditor has
been validated against live money.

## 8. Adversarial debtors — DIFFERENTIATOR

`settle/sim/debtors.py`. Behaviours layered on top of `intent_type`:

| Behaviour | Description |
|---|---|
| `promise_and_break` | Commits to a date, does not pay, may re-promise |
| `dispute_stall` | Raises a dispute to freeze collection, later withdraws |
| `go_silent` | Responds once, then never again |
| `opt_out_midway` | Opts out after N contacts; all later contact is a violation |
| `hedged_reply` | Ambiguous language that is not a commitment (see §11) |
| `pay_then_complain` | Pays, then reports harassment if contacted again — pairs with SF-2 |

Every arm faces the same debtors under common random numbers.

## 9. Diagnosis — deterministic first

`settle/diagnose/taxonomy.py`. Decline code to class, pure lookup. The LLM is
never invoked on a decline code.

| Class | Codes | Viable actions | Forbidden |
|---|---|---|---|
| `time_shiftable` | insufficient_funds | do_nothing, retry, serve_notice | contact, same-hour retry |
| `transient` | gateway_timeout, issuer_down | do_nothing, retry, serve_notice | contact |
| `dead_instrument` | card_expired, mandate_revoked, card_stolen | do_nothing, request_mandate_update, send_message | any retry |
| `auth_abandoned` | authentication_failed | do_nothing, send_message, switch_rail | retry same rail |
| `ambiguous` | do_not_honour | do_nothing, retry, send_message | repeated retry beyond G10's cap |
| `terminal` | fraud_flagged | do_nothing, escalate_human | everything else |

The Viable column is authoritative and exhaustive. The Forbidden column is
commentary explaining why an omission is deliberate; it is not a subtractive
blacklist. Any verb absent from Viable is not available to that class.

`serve_notice` is viable for **every class with a viable retry**, derived rather
than listed. On `enach` a debit outside an active notice window is blocked by G9,
and `serve_notice` is the only action that opens one. A class that may retry but
may not serve notice is unreachable on `enach` by construction — which is what
happened to `ambiguous` when A57 named two classes and missed the third. Stated
as a derivation so it cannot drift again:

    SERVE_NOTICE is viable for class C  <=>  RETRY is viable for class C

**Escalation-eligible cases** (§2.1) additionally gain `voice_call` and
`escalate_human` for `dead_instrument`, `auth_abandoned` and `ambiguous`.
Without this the 15% slice §2 defines is unreachable and `voice_call` is viable
for no class at all. Eligibility is a property of `ObservedCase`, so
`legal_actions` may read it without becoming state-dependent.

Codes not in the table map to `ambiguous` and are counted. An unmapped-code rate
above 5% is a gate failure.

**Rail interaction:** on `enach`, a `time_shiftable` liquidity-window retry is
only legal inside an active notice window (G9). Outside it, the agent must
schedule notice-then-debit, costing 24h. This is a real planning constraint and
the policy must handle it, not route around it.

## 10. Policy

### 10.1 Estimator

`settle/agent/estimator.py`. `HistGradientBoostingClassifier` wrapped in
`CalibratedClassifierCV` (isotonic), predicting `P(settle | case, action, hour)`.

Logistic regression ships alongside as the interpretable baseline. If LR wins on
calibration, LR is used and that is reported.

Training data comes from the EXPLORE arm only. EXPLORE selects uniformly at
random over the legal action and hour grid, runs through gates in ENFORCE mode,
and executes on a seed range disjoint from the evaluation batch.
Evaluation is on a held-out seed the estimator never saw AND on one perturbed
generator variant with shifted parameters. The disjoint seed alone is
insufficient: it draws differently from the same world model and therefore tests
nothing about whether the estimator has memorised the generator's functional
form. Both are reported.

Fixed baseline policies cannot serve as training data: B2 always sends the same
ladder at the same offsets, so the log has near-zero coverage of the
action x hour space the estimator must score. A model trained on it would
extrapolate everywhere the policy actually needs it, while ECE and Brier looked
excellent on an equally degenerate held-out set.

EXPLORE logs its own propensity at draw time. The sampler writes
`propensity = 1/len(legal_pairs)` into the decision record, where `legal_pairs`
is the exact set it drew from. Propensity is never computed analytically after
the fact: legality is joint over action and hour — G1 constrains hours, and G9
constrains them further on `enach` — so it does not factor, and any formula
would be free to drift from the sampler.

Splits are by **case**, never by row. Rows from one case share hidden truth —
the same `true_recoverability`, the same `payday_day`, the same debtor — so
splitting by row puts sibling rows either side of the boundary, the model scores
well by having memorised the case, and every metric comes out optimistic with
nothing warning you.

**The natural-recovery confound.** A case that self-cures (§14.3) settles
whatever the arm did, so every row from it labels `settled=True` and teaches the
model that the action worked. Measured at CP7: self-cured cases carry a 0.99 base
rate against 0.52 for the rest, and are 21.8% of training rows. The estimator is
therefore predicting *whether this case settles*, not *whether this action causes
settlement*. Those are different questions and only the second is a policy. The
label must become action-attributed before the estimator drives OURS.

Rows where `do_nothing` was the only legal gate-passing option are not
decisions; they are the absence of one. 78.6% of EXPLORE's ticks are such rows,
and training on them teaches the model to predict inaction rather than to
predict settlement. The estimator trains on the 21.4% where an arm had a real
choice. Filtered row count is reported alongside the training set size.

Coverage is reported per (action, hour_bucket) cell. Cells below a minimum
observation threshold are flagged as EXTRAPOLATED in the reliability report and
excluded from the headline calibration figures.

**Retry timing — a measured negative result.** Retry timing was hypothesised as
a primary differentiator and tested. Median predicted probability moves 3.7
points across the eight declared offsets, p90 9.4 points. Timing features rank
26–37 of 45 by permutation importance, an order of magnitude below decline
class. The observed variation is a contact-window artefact — daytime beats 3am —
not a liquidity curve. The cyclical encoding (`days_to_month_start`)
outperformed the linear one and was still negligible.

A coarse timing signal exists. A smooth liquidity curve does not. The claim that
the policy learns payday timing is withdrawn. Reported in the README as a
measured negative result.

**Model selection is decided on uplift calibration, not overall.** §10.2
subtracts `p_settle(do_nothing)` from every action, so the quantity the policy is
sensitive to is the difference, not either term alone. LR wins overall
(ECE 0.0189 vs 0.0225) and loses on `do_nothing` rows (0.0359 vs 0.0307).
Selection is therefore made on the calibration of the uplift term itself. If the
two models split, the hybrid ships and is stated explicitly rather than hidden.

Reported: reliability diagram, ECE, Brier score.

### 10.2 Selection

```
EV(a) = [p_settle(a) − p_settle(do_nothing)] × amount_recoverable
        − action_cost(a)
        − opt_out_cost(a)
```

Uplift, not raw probability. 21.8% of cases self-cure regardless of any action
(§14.3), so a raw probability tells the policy that acting on a `willing_able`
case succeeds 99% of the time — true, and useless, because it would have settled
anyway. Subtracting the `do_nothing` term cancels the self-cure component and
leaves the action's causal contribution.

This is §14.3's incremental subtraction applied at decision level rather than
batch level. It is also what gives `do_nothing` non-zero expected value, without
which the contact-restraint result is unreachable by construction.

The estimator must therefore predict `p_settle(do_nothing)` accurately. Its
calibration on `do_nothing` rows is reported separately.

Attribution windows were considered and rejected: the window would need tuning,
a self-cure landing inside one would be falsely credited, and uplift achieves
the same correction with no new parameter. Labels stay as they are.

`argmax` over the legal action set, including `do_nothing`. Ties break toward
the cheaper action.

## 11. Free text and promise extraction

`settle/text/`. The only place an LLM runs.

Contract, carried over from prior production work:

- The model **locates spans**. It never produces a value.
- Deterministic code **parses and validates** the value from the span.
- Disagreement between model and parser becomes a confirmation turn, not a
  silent guess.

**Deterministic-first routing.** Plain code classifies every reply before any LLM
call. Unambiguous replies (STOP, clear opt-out, clear payment confirmation,
empty) never reach the model. Only replies the deterministic classifier cannot
resolve confidently escalate to the LLM. Escalation rate is reported per run.

Applied to promises: model marks the span containing a date commitment; code
parses it against `created_at` as anchor, validates it is future and within
horizon.

**A hedged reply is not a promise.** "Dekhta hoon", "baad mein baat karte hain",
"try karunga" must NOT produce a suppression window. Test PROM-3 asserts this.
Wrongly logging a brush-off as a promise suppresses contact for weeks and is a
worse failure than missing a real promise.

The escalation rate is measured against an adversarial corpus written
independently of the classifier. A rate measured against text the classifier's
own author produced is not a measurement.

The deterministic classifier is `settle/text/classify.py`. It imports no client
and makes no network call, and RPL-6 asserts it. Its `unclear` count is the LLM
escalation rate, reported per run. An opt-out outranks every other reading in
the same message — "already paid, stop messaging" is both a payment claim and an
opt-out, and honouring the opt-out is the only reading where being wrong is not
a compliance breach.

STT: `gpt-transcribe`. Note: it returns Devanagari for Hindi speech regardless of
the `language` parameter — that parameter steers recognition, not output script.
All date and numeral parsing must accept both Latin and Devanagari.

All LLM and STT responses cached to `out/llm_cache.json`, keyed on input hash.
`--max-llm-calls` defaults to 2000; exceeding it aborts the run rather than
degrading silently. Every run prints calls, cache hits, tokens, estimated cost.

## 12. Gates — can block a dispatch

`settle/policy/gates.py`. Evaluated in order. Pure Python. Cannot be bypassed.

| ID | Gate |
|---|---|
| G1 | Contact window 08:00–19:00 IST |
| G2 | Frequency cap: 3 per week, 20h minimum gap |
| G3 | Mandate validity |
| G4 | Card-network submission cap. Counts submissions to the card network, whichever verb produced them: a `retry` on card and a `switch_rail` **to** card both count, a switch away does not. Distinct from G10, which caps retries per decline class. |
| G5 | Idempotency key uniqueness. The key is built from `due_tick` — the tick the action fires at — not from the tick at which it was chosen. Otherwise scheduling and then rescheduling the same action produces the same key twice and G5 blocks a legitimate action. Satisfied structurally: the runner dispatches only when `state.tick == due_tick`. |
| G6 | Promise suppression window |
| G7 | Opt-out honoured on every channel |
| G8 | Dispute freeze |
| S7 | Economic stop. Lives in `settle/agent/policy.py`, not in `stops.py`: it compares expected recovery against cost, which needs an EV, which needs the estimator. A pure stop cannot ask that question. |

| G9 | e-mandate pre-debit notice. A served notice covers a notified debit window of 3 days from the notified date. Retries inside the window inherit the notice. Retries outside require fresh notice, which costs 24h lead time. G9 sequences the plan, it is not a checkbox. Window length ASSERTED, source in D4. A served notice is a full contact: subject to G1's window, counted against G2's frequency cap, and consuming patience_budget. ASSERTED. Consequence: outside an active window a compliant enach retry costs two of three weekly contacts. The alternative treatment (notice as regulatory overhead, exempt from G2) is recorded in Known Limitations. |

| G10 | Class retry budget. Per-class cap on retries, distinct from G4's card-network cap. `ambiguous` = 1: §9 permits one retry at a different hour, then a message. Without this, `ambiguous` retries are unbounded until G4 or S3 fires. |
| G11 | TRAI DND registry. Blocks `voice_call` when `dnd_flag` is set. Does not block SMS or WhatsApp: DND covers unsolicited commercial contact, and a transactional message to an existing customer about a failed payment is exempt. That exemption is ASSERTED and recorded in Known Limitations. |

Every gate has exactly one named test. Blocks are logged with a reason code and
counted; `gate_blocks > 0` is a required condition for a valid run.

`evaluate_gates` derives the IST evaluation hour from `case.created_at +
state.tick`. It does not accept an hour argument. An inconsistent `(tick, hour)`
pair must be unrepresentable, not merely undocumented.

## 13. Stops — terminal

`settle/policy/stops.py`.

| ID | Stop |
|---|---|
| S1 | Recovered — settlement confirmed, not authorisation |
| S2 | Dead instrument with no customer path |
| S3 | Attempt or message budget exhausted |
| S4 | Opt-out |
| S5 | Dispute raised |
| S6 | Decision horizon reached — 30 simulated days from case creation. |
| S7 | Economic stop: expected recovery < 3 × (action cost + nuisance) |

Stops are terminal. Post-stop events are recorded and do nothing. Test ADV-1
fires events at stopped cases and asserts zero dispatches.

### 13.1 Decision horizon vs observation horizon

Two distinct constants:

| Constant | Value | Meaning |
|---|---|---|
| decision_horizon_days | 30 | Agent stops acting. One billing cycle. |
| observation_horizon_days | 60 | World keeps running. Settlements land, reversals fire, scoring happens here. |

`settlement_lag_h` and reversal delay both require a stated maximum in
PRIORS.md. Without one, 60 days is a second arbitrary cliff and SF-7 remains
undetectable often enough to look plausible while being wrong. Any outcome
landing beyond `observation_horizon_days` is right-censored; the censored
fraction is reported per arm. B0's counterfactual is scored at
`observation_horizon_days`, not at `decision_horizon_days`.

A single horizon would break INV-1 and SF-7: a case authorised on day 29 settles
on day 32, and reversals land mostly outside a 30-day window. Both would score
as losses or go undetected, quietly flattering the headline number.

### 13.2 Stop classes and OBSERVE mode

| Class | Stops | Behaviour in OBSERVE (B3 only) |
|---|---|---|
| Compliance | S4 opt-out, S5 dispute | Relaxed. Case continues, violation logged and counted. |
| Terminal state | S1 recovered, S2 dead instrument, S3 budget exhausted, S6 decision horizon | Binding in every arm without exception. |

S7 (economic) is a policy choice, not a stop class; B3 does not apply it by
definition of max pressure.

Without this split B3 cannot violate INV-3: S4 would fire before G7 is ever
consulted, G7 and G8 would be shadowed, and the unguarded baseline would
generate structurally zero opt-out violations while appearing to test them.

## 14. Evaluation

### 14.1 Arms

| Arm | Description |
|---|---|
| B0 | Do nothing — natural recovery baseline |
| B1 | Single immediate retry |
| B2 | Fixed dunning ladder: 3 retries plus generic messages |
| B3 | Max pressure. Gates run in OBSERVE — violations logged and counted, not blocked. |
| EXPLORE | Uniform random over legal actions and hours. Gated in ENFORCE. Generates estimator training data. Runs on a disjoint seed range. |
| OURS | `settle` |
| LLM-STRAT | Optional ablation arm, 300 cases, flag-gated. Proposes actions; clamped to the closed verb set and gated in ENFORCE. |

EXPLORE evaluates gates itself and samples uniformly from `(action, hour)` pairs
that pass both `legal_actions` and `evaluate_gates` in ENFORCE. Its logged
propensity is `1/len(passing_pairs)`: the probability the executed action was
chosen, not the probability it was proposed.

Sampling from the legal set and letting gates block afterwards would make the
executed distribution non-uniform in a way the logged propensity does not
describe, and every IPS estimate built on it would be wrong.

The Arm protocol therefore permits an arm to consult gates before choosing.
OURS requires the same visibility to populate `Alternative.legal` and
`Alternative.block_gate`. Consulting gates is not bypassing them: the runner
evaluates them again, in the one implementation §4 requires, and its verdict
binds.

Baselines are given full capability and denied only intelligence: same channels,
same templates, same e-mandate notice. A baseline crippled by omission produces a
fake win. That includes channel choice — a baseline that only ever sends SMS
where the customer has consented to WhatsApp has been crippled, not simplified.

### 14.2 Common random numbers

All randomness is pre-drawn per case before any arm runs, as INDEXED STREAMS
addressed by (case_id, stream_name, tick). Every arm reading tick N of a given
stream receives the identical value regardless of how many actions it has taken.

Sequential consumption would break CRN even with identical seeds: an arm taking
seven actions desyncs from one taking three, and every draw after the divergence
point differs. Indexing removes the coupling between an arm's action count and
the randomness it observes.

Streams span `observation_horizon_days` (60), not `decision_horizon_days` (30).
Draws sized to the decision horizon would diverge in the settlement and reversal
tail, and incremental scoring would be silently wrong in our favour — the exact
failure §14.3 exists to prevent.

Named streams, each with a stated tick unit:

```
action_outcome    per action attempt
settle_roll       per authorisation
reversal_roll     per settlement
webhook_drop      per reported outcome
webhook_dup       per reported outcome
reply_draw        per contact
patience_draw     per contact
out_of_order      per reported outcome
natural_recovery_draw  per case
natural_recovery_day   per case
```

`out_of_order` and the natural-recovery pair are shared for the same reason as
the rest. A reporting distortion drawn from a private address would differ
between arms facing the same case, and a self-cure that differed between arms
would make §14.3's subtraction compare two different events.

### 14.3 Incremental scoring

A case that also recovers under B0 is **not counted**. Roughly a fifth of
at-risk value returns on its own and counting it is the easiest way for a
recovery product to flatter itself.

Natural recovery is the mechanism that makes incremental scoring meaningful.
Without it B0 recovers zero, incremental equals gross, and the definition
protects nothing. It is also what gives `do_nothing` positive expected value:
if inaction never recovers anything, every action dominates it and the
contact-restraint result is unreachable by construction.

This definition discards timing value. A case OURS recovers on day 3 and B0
recovers on day 28 scores as zero, despite 25 days of avoided churn risk and a
cycle of float. This is a deliberate conservative choice that understates our
result. `median_days_to_recovery` is reported as a secondary metric and the
README states this understatement explicitly rather than leaving a judge to find
it.

### 14.4 Metrics — headline table

| Metric |
|---|
| Incremental recovery (₹) |
| Incremental rate (%) |
| Contacts per case |
| Contacts per recovery |
| Cost per ₹100 recovered |
| Cases deliberately not contacted |
| Opt-outs induced |
| Compliance violations (must be 0 for OURS) |
| **Silent failure rate, by class** |
| **Reported minus reconciled recovery (₹)** |
| Promise-kept rate |
| Calibration: ECE, Brier |
| median_days_to_recovery |

The two bold rows are the ones no comparable submission can print.

## 15. Priors and provenance

`PRIORS.md`. Every numeric parameter in the generator and world model gets a row:

```
| parameter | value | source | date | sensitivity |
```

Sources must be public and citable: NPCI UPI statistics, RBI e-mandate
circulars, published issuer decline data, Razorpay's own published figures.
Anything without a source is marked `ASSERTED` in the table, in the README, and
in the run output.

A sensitivity sweep varies each parameter across a plausible range and reports
whether the headline conclusion survives. Parameters where it does not are named
explicitly. `world.liquidity_window_days` is named as a required member of the
sensitivity sweep, not left to discretion.

INV-10 covers every number that can move a reported metric, including those
that describe the shape of the action space or of a behaviour. `ACTION_LIFT`
determines whether a retry outperforms a message and is therefore upstream of
every rupee in §14.4. "Structural, not fitted" is precisely the reasoning that
lets an unsourced number reach a headline, and it is the criticism this project
levels at comparable work. If a number can move a metric, it gets a row.

Applied consistently, this rule caught `DISENGAGE_AFTER_CONTACTS`, the complaint
probability, and five inline multipliers in `p_authorise` after the initial
pass. The test for whether a number belongs in PARAMS is not whether it feels
like a parameter — it is whether changing it would move a number in §14.4.

**Nothing from any employer.** No code, no data, no figures learned on the job,
not paraphrased. Every prior is public or asserted.

## 16. Razorpay integration

Real at the edges, simulated at scale.

- Three FastAPI routes:

  ```
  POST /webhooks/razorpay   signature-verified webhook receiver
  POST /voice/extract       upload audio, returns full extraction trace
  GET  /                    serves the static viewer
  ```

- HMAC SHA256 signature verification, mandatory
- Handler verifies, writes the raw event, returns 200. Nothing else.
  Razorpay requires 2XX within 5 seconds; it retries with exponential backoff
  for 24 hours and disables the webhook after 24h of failure.
- Idempotency store keyed on event id — real webhooks genuinely do arrive twice
- Subscribed events: `payment.captured`, `payment.failed`, `payment_link.paid`
- Demo shows one real `pay_...` / `plink_...` id next to its ledger row

Credentials in `.env` only. `.env` in `.gitignore` from commit one.

## 17. Repo layout

```
settle/
  schema/          frozen contracts
  sim/
    streams.py     indexed random streams, addressed by (case_id, name, tick)
    generator.py   world construction; also the batch CLI
    truth.py       HiddenTruth — agent package must never import this
    world.py       response model
    observability.py
    debtors.py
  diagnose/
    taxonomy.py
  agent/
    estimator.py
    policy.py
    llm.py         optional arm
    llm_clamp.py   deterministic adapter: clamps LLM-STRAT output to the closed
                   verb set of §5.3 before gates
  policy/
    gates.py
    stops.py
  execute/
    executor.py
  audit/
    chain.py
    verify.py
  recon/
    reconcile.py
    silent_failures.py
  text/
    reader.py
    promise.py
  eval/
    run_batch.py
    baselines.py
    metrics.py
    charts.py
  api/
    webhook.py
viewer/
  index.html     single file, vanilla JS, no build step
scripts/
  gate.sh
checkpoints/
  *.allowlist    one per checkpoint, consumed by scripts/gate.sh
tests/
fixtures/
out/
PRIORS.md
SPEC.md
PLAN.md
README.md
```

The batch CLI is `python -m settle.sim.generator --cases 10000 --seed 42 --out
out/batch.jsonl`. It lives in `generator.py`; there is no `settle/sim/generate.py`
shim. Observed cases and hidden truth are written to separate files — one file
holding both would be an INV-8 breach waiting for the first person who greps it.

Stack: Python, FastAPI (one route), SQLite via SQLAlchemy with `DATABASE_URL`
override, matplotlib, sklearn, pytest. Audit ledger is JSONL on disk, not in the
database.

## 18. Checkpoint discipline

- Every checkpoint has a stated goal, a file allowlist, named test IDs, a gate
- `scripts/gate.sh` enforces the allowlist, frozen files, and the named tests
- CC touches nothing outside the allowlist. If a frozen file must change, CC
  stops and asks
- No commits until explicitly approved
- CC writes a CHECKPOINT_REPORT at the end of each checkpoint
- PLAN.md is edited by CC only, via targeted str_replace

## 19. README order — fixed

1. Headline metrics table, including silent failure rate
2. Architecture diagram
3. Reliability curve
4. One-command reproduction
5. How thresholds were chosen
6. Priors and provenance
7. Known limitations

## 20. Cost constants — ASSERTED, pending sourcing

All values placeholder until PRIORS.md is filled in D4. Every one is marked
ASSERTED in run output.

| Action | Channel | Cost (paise) | Nuisance units | P(opt_out) |
|---|---|---|---|---|
| do_nothing | — | 0 | 0 | 0 |
| retry | — | 5 | 0 | 0 |
| switch_rail | — | 5 | 0 | 0 |
| send_message | sms | 15 | 1 | ASSERTED |
| send_message | whatsapp | 35 | 1 | ASSERTED |
| request_mandate_update | sms | 15 | 1 | ASSERTED |
| request_mandate_update | whatsapp | 35 | 1 | ASSERTED |
| serve_notice | sms | 15 | 1 | ASSERTED |
| voice_call | voice | 400 | 3 | ASSERTED |
| escalate_human | — | 5000 | 4 | ASSERTED |

Cost is keyed on `(ActionType, Channel|null)` and nothing else. The previous
table mixed action names with channel names, which would have forced a lookup
shim at D2 that quietly disagreed with the spec.

Opt-out cost is DERIVED per action, never tuned:

```
opt_out_cost(action) = P(opt_out | action) × LTV
LTV                  = plan_value_paise × ltv_months
```

`ltv_months` is an ASSERTED prior, swept in §15 sensitivity.

Nuisance units are NOT a cost multiplier. They are only a counter against the
hidden patience_budget (§5.2). Deriving cost as
`nuisance_units × cost_per_unit` would smuggle an empirical claim — that a voice
call is exactly 3x as likely to induce opt-out as a WhatsApp — in as a unit
conversion. `P(opt_out | action)` is stated per action instead.

Tuning either quantity against incremental recovery would make the
contact-restraint result circular; tuning against contact count would make the
recovery result circular.

## 21. Open questions

Resolved inside the checkpoint that reaches them, not by further spec amendment.

| ID | Question | Checkpoint |
|---|---|---|
| OQ-3 | EXPLORE draws uniformly over legal pairs, but after G9 most `enach` retry hours are illegal without an active notice. Uniform-over-legal therefore under-samples `enach` retries — exactly the cell where §9's rail-interaction constraint is decided. May need stratification or a longer EXPLORE run on that rail. | D2 |
| OQ-4 | With S4/S5 relaxed (§13.2) and S7 not applied, B3 terminates only on S3 or S6. Its violation count is then a function of the message budget constant rather than of debtor behaviour. Fix the budget a priori and report it as an input, or the headline "B3 generated N violations" is a tuning artefact. | D4 |
| OQ-5 | A22's perturbed generator variant needs a named perturbation set — which parameters, shifted how far — fixed before the estimator is trained. Otherwise it is a check that can be quietly weakened until it passes. | D2 |
| OQ-6 | Razorpay's default settlement cycle is publicly documented. `settlement_lag_h` should be a cited prior, not ASSERTED, and the recon-report availability lag should be folded into it. Cheap INV-10 win. | D4 |
| OQ-20 | `settlement_lag_reporting` and `reversal_reporting_delay` are declared but unconsumed until the D3 reporting layer exists. GEN-4 cannot detect a dead parameter. Add a liveness check to the D3 checkpoint asserting every observability parameter is read by at least one code path. | D3 |
| OQ-34 | §14.2's named stream list has `webhook_drop` and `webhook_dup` but not `out_of_order`, and `streams.py` closes that list. The reporting layer draws it from its own address instead, so it is not shared across arms the way the other reporting draws are. Add it to the stream set. | D3 |
| OQ-31 | Only `retry` carries a schedulable offset in §5.3's frozen verb set, so contacts are never scheduled to a chosen hour — the grid's offset dimension covers debits only. Widening it is a §5.3 amendment, and OURS may not need it. | CP8 |

Resolved:

- OQ-34 — `out_of_order` drew from its own address, so two arms could face
  different reporting distortion on the same case. Resolved at CP6.1: it is a
  named stream and the reporting layer reads only shared streams. OBS-3.
- OQ-35 — SF-4 and SF-5 were unreachable without reply handling. Resolved at
  CP6.1: replies are classified and applied, and both classes now occur from
  real behaviour.
- OQ-20 — `settlement_lag_reporting` and `reversal_reporting_delay` were
  declared and read by nothing. Resolved at CP6: all five reporting parameters
  are applied in `observability.report()`, and OBS-1 asserts each is read.
- OQ-28 — nothing could set `settled`, so S1 could never fire. Resolved at CP6:
  reconciliation is the only thing entitled to say a case recovered.
- OQ-30 — `at_hour_offset` was a label: the runner dispatched immediately and
  used it only as a wake-up hint, so the offset dimension of the action grid
  carried no behaviour for an estimator to learn. Resolved by A73: a schedulable
  choice becomes a commitment that fires at `due_tick`, re-gated on arrival.
- OQ-32 — the gate's EXPLORE runs took minutes. Resolved: the fixture is
  parameterised and the gate runs 3,000 cases; the 30,000-case run is a manual
  D4 exercise.
- OQ-33 — 78.6% of EXPLORE's decisions were `do_nothing` with no alternative.
  Resolved by A75: the estimator trains only on rows where the choice set had
  more than one member, and the filtered count is reported.
- OQ-26 — the runner's daily cadence was a bare literal. Resolved by A68:
  `decision_cadence_hours` in POLICY_PARAMS with a PRIORS row, covered by PAR-1.
- OQ-27 — G4 read `attempts_used`, so a switch to card escaped the card-network
  cap. Resolved by A70: G4 counts `card_submissions_used`, which a retry on card
  and a switch to card both increment.
- OQ-29 — the suite was slow enough to stop being run. Resolved by A69: the
  10,000-case runs are marked `slow` and opt-in.
- OQ-22 — `ambiguous` had a viable retry and no way to open a notice window, so
  it was unreachable on `enach`. Resolved by A66: `serve_notice` is derived from
  the presence of a viable retry rather than listed per class.
- OQ-23 — G10 counted rail switches against a class retry budget. Resolved by
  A67: `rail_switches_used` is a separate counter and G10 reads `attempts_used`.
- OQ-2 — common random numbers must span the 60-day observation horizon, not the
  30-day decision horizon. Resolved by X6: §14.2 now specifies indexed streams
  addressed by `(case_id, stream_name, tick)`, sized to `observation_horizon_days`.
- OQ-7 — `Channel` was typed as an unspecified enum. Resolved by A33: exactly
  `sms | whatsapp | voice`, with email out of scope and G7 bounded by that list.
- OQ-8 — the §20 cost table mixed action names with channel names. Resolved by
  A36: one key space, `(ActionType, Channel|null)`.
- OQ-9 — `Decision.alternatives` was a bare triple. Resolved by A35: a named
  `Alternative` model carrying `legal` and `block_gate`.
- OQ-10 — checkpoint allowlists had no home in the repo. Resolved by
  `checkpoints/`, one allowlist per checkpoint, consumed by `scripts/gate.sh`.
- OQ-12 — bounds on `attempt_number`, `payday_day`, `arrival_count`,
  `tenure_months`, `prior_failures`, `prior_recoveries` and `propensity` were
  interpretation. Resolved by A37: recorded in §5.1, §5.2, §5.4 and §5.5.
- OQ-1 — `P(opt_out | action)` was stated nowhere. Resolved by A36, which
  rebuilt the §20 table around it on the `(ActionType, Channel|null)` key space,
  with matching `p_opt_out_*` rows in PRIORS.md.
- OQ-11 — the frozen-file check could not run without a baseline. Resolved at
  commit `938b54b`: `scripts/gate.sh` now reports SPEC.md, DECISIONS.md and
  PRIORS.md as verified rather than unverifiable.

## Amendments

- 2026-08-27 — A1: §2 secondary slice restated as 15% escalation-eligible, ~2-3% reaching voice.
- 2026-08-27 — A2: §3 scope row "Static HTML trace viewer" → "Three-screen viewer: batch, case trace, voice lab".
- 2026-08-27 — A3: §11 deterministic-first routing added to the LLM contract; unambiguous replies never reach the model.
- 2026-08-27 — A4: §13 S6 given an explicit 30-day horizon. Superseded by A13.
- 2026-08-27 — A5: §16 one FastAPI route → three (webhook, voice extract, viewer).
- 2026-08-27 — A6: §17 `viewer/index.html` added to the repo layout. Position corrected by A18.
- 2026-08-27 — A7: §20 added — cost constants, ASSERTED pending sourcing.
- 2026-08-27 — A8: §4 preamble replaced with the ENFORCE/OBSERVE gate-mode rule; INV-11 added (OURS never runs in OBSERVE).
- 2026-08-27 — A9: §4 INV-9 rewritten to scope the no-LLM-actions rule to OURS and admit LLM-STRAT via a clamping adapter.
- 2026-08-27 — A10: §3 scope row updated to three FastAPI routes, for consistency with A5.
- 2026-08-27 — A11: §12 G9 given a 3-day notified debit window with inheritance; §9 gains the enach rail-interaction constraint.
- 2026-08-27 — A12: §10.1 estimator training data moved from baseline logs to the EXPLORE arm, with propensities and per-cell coverage reporting.
- 2026-08-27 — A13: §13 S6 renamed to decision horizon; §13.1 added splitting decision (30d) from observation (60d) horizon.
- 2026-08-27 — A14: §20 nuisance cost per unit changed from tuned to DERIVED from P(opt_out | contact) × customer_LTV.
- 2026-08-27 — A15: §14.3 states that incremental scoring discards timing value, with `median_days_to_recovery` as a secondary metric.
- 2026-08-27 — A16: §7 states precisely what reconciliation transfers to production and that the auditor is not validated against live money.
- 2026-08-27 — A17: §5.3 records that actions carry no amount field and that partial debits are out of scope.
- 2026-08-27 — A18: §17 `viewer/` dedented to repo root, sibling of `scripts/` and `tests/`.
- 2026-08-27 — A19: `.gitattributes` — `*.jsonl -text`, to stop LF normalisation breaking hash-chain fixtures.
- 2026-08-27 — A20: `requirements.txt` — pandas repinned from 3.0.5 to the latest 2.x.
- 2026-08-27 — A21: §14.1 arms table — EXPLORE added, B3 redefined as OBSERVE-mode, LLM renamed LLM-STRAT.
- 2026-08-27 — A22: §10.1 evaluation reinstated on a held-out seed AND a perturbed generator variant, with the reason the seed alone is insufficient.
- 2026-08-27 — A23: §10.1 propensity logged by the sampler as `1/len(legal_pairs)`, never computed analytically; legality does not factor over action and hour.
- 2026-08-27 — A24: §13.2 added — stop classes split into compliance (relaxed in OBSERVE) and terminal state (binding in every arm).
- 2026-08-27 — A25: §12 G9 — a served pre-debit notice is a full contact, subject to G1 and counted against G2.
- 2026-08-27 — A26: §20 opt-out cost derived per action from `P(opt_out | action) × LTV`; nuisance units demoted to a patience counter. §10.2 EV formula updated to match.
- 2026-08-27 — A27: §13.1 — maxima required for `settlement_lag_h` and reversal delay; right-censoring reported; B0 scored at the observation horizon.
- 2026-08-27 — A28: PRIORS.md — `settlement_lag_h_max`, `reversal_delay_days_max`, `ltv_months` added as ASSERTED, values pending D4.
- 2026-08-27 — A29: §14.4 — `median_days_to_recovery` added to the headline metrics table.
- 2026-08-27 — A30: §17 — `settle/agent/llm_clamp.py` added, the deterministic adapter required by INV-9.
- 2026-08-27 — A31: §7 — production reconciliation restated as a lagged batch join against the Settlement Recon endpoint, since no per-payment settled flag exists.
- 2026-08-27 — A32: §21 Open questions added.
- 2026-08-27 — X4: §20 redundant standalone nuisance-units sentence removed.
- 2026-08-27 — X5: §20 cost table gains a `P(opt_out | action)` column, ASSERTED pending D4; per-action rows added to PRIORS.md.
- 2026-08-27 — X6: §14.2 common random numbers respecified as indexed streams over `(case_id, stream_name, tick)`, spanning the observation horizon. Resolves OQ-2.
- 2026-08-27 — A33: §5.3 `Channel` enumerated as `sms | whatsapp | voice`; email out of scope. Resolves OQ-7.
- 2026-08-27 — A34: §5.3 `serve_notice(channel)` added to the closed verb set, so the policy prices the G9 notice instead of the executor hiding it.
- 2026-08-27 — A35: §5.4 `alternatives` retyped to `list[Alternative]`, carrying `legal` and `block_gate`. Resolves OQ-9.
- 2026-08-27 — A36: §20 cost table rekeyed on `(ActionType, Channel|null)`; PRIORS `p_opt_out_*` rows follow the same key space. Resolves OQ-8.
- 2026-08-27 — A37: §5.1, §5.2, §5.4, §5.5 record the field bounds explicitly rather than leaving them to the implementation. Resolves OQ-12.
- 2026-08-27 — A38: §21 — OQ-11 opened, OQ-7/8/9/10/12 resolved.
- 2026-08-27 — A39: §5.1 `observed_credit_day` bounded to 1..28, the same domain as `payday_day` it estimates.
- 2026-08-27 — A40: §21 — OQ-1 and OQ-11 resolved.
- 2026-08-27 — A41: `Alternative` now enforces the `legal` / `block_gate` pairing stated in §5.4. Test SCH-9.
- 2026-08-27 — A42: CP0-CP1 commit message corrected from 33 to 41 tests.
- 2026-08-27 — A43: §6 — `auth_no_settle_rate` removed from the observability layer and moved to world PARAMS; `settlement_lag_h` and `reversal_rate` restated as the reporting-side `settlement_lag_reporting` and `reversal_reporting_delay`. `--perfect-observability` now zeroes five parameters, not six.
- 2026-08-27 — A44: §17 — `settle/sim/streams.py` and `checkpoints/` recorded in the repo layout.
- 2026-08-27 — A45: §17 — the batch CLI is `python -m settle.sim.generator`; no `generate.py` shim.
- 2026-08-27 — A46: §5.2 — `escalation_eligible` recorded as derivable from observables, never a channel. Test GEN-6.
- 2026-08-27 — A47: §15 — INV-10 extended to every number that can move a metric; `ACTION_LIFT` and `_REPLY_MIX` moved into PARAMS with PRIORS rows.
- 2026-08-27 — A48: `debtor.disengage_after_contacts` and `patience.complaint_cost` moved into PARAMS. Resolves OQ-18.
- 2026-08-27 — A49: `p_authorise.*` — six literals (base floor, same-rail switch, cross-rail retry, DND, day-window start and end) moved into PARAMS. Resolves OQ-19.
- 2026-08-27 — A50: PRIORS.md split into **Sampled parameters** and **Asserted targets**; `escalation.target_overall_rate` moved to the second. Resolves OQ-21.
- 2026-08-27 — A51: §21 — OQ-20 recorded, to be resolved by a D3 liveness check.
- 2026-08-27 — A52: §15 — records what the INV-10 rule caught on the second pass, and the test for whether a number belongs in PARAMS.
- 2026-08-27 — A53: `world.liquidity_window_days` moved into PARAMS — the last INV-10 literal, and the highest-leverage number in the world model.
- 2026-08-27 — A54: §15 — `world.liquidity_window_days` named as a required member of the D4 sensitivity sweep.
- 2026-08-29 — A55: §5.7 `CaseState` added — the contract the gates read, without which no gate can be pure.
- 2026-08-29 — A56: §9 Viable column restated as authoritative and exhaustive; Forbidden is commentary.
- 2026-08-29 — A57: §9 `serve_notice` made viable where a debit is. Superseded by A66.
- 2026-08-29 — A58: §12 G10 class retry budget and G11 TRAI DND added.
- 2026-08-29 — A59: §12 gates take no `hour` argument; the IST hour is derived from `created_at + tick`.
- 2026-08-29 — A60: §5.7 `settled` and `settled_at` recorded; S1 reads state rather than a caller's bool.
- 2026-08-29 — A61: §5.7 records the frozen collection types; `canonical_json` sorts frozensets.
- 2026-08-29 — A62: §2.1 escalation eligibility moved to `settle/policy/escalation.py`; the dependency runs sim -> policy.
- 2026-08-29 — A63: §9 escalation-eligible cases gain `voice_call` and `escalate_human`.
- 2026-08-29 — A64: `settle/policy/params.py` created; PRIORS gains a Policy constants table; GEN-4 renamed PAR-1.
- 2026-08-29 — A65: `scripts/gate.sh` derives its frozen list; intentionally local-only files report as INFO.
- 2026-08-29 — A66: §9 `serve_notice` derived from the presence of a viable retry, not listed. Resolves OQ-22.
- 2026-08-29 — A67: §5.7 `rail_switches_used` split from `attempts_used`; G10 reads retries only. Resolves OQ-23.
- 2026-08-29 — A68: `decision_cadence_hours` moved into POLICY_PARAMS with a PRIORS row. Resolves OQ-26.
- 2026-08-29 — A69: `pytest.ini` marks the 10,000-case runs `slow`; the default suite skips them. Resolves OQ-29.
- 2026-08-29 — A70: §12 G4 restated as a card-network *submission* cap; `card_submissions_used` added to §5.7. Resolves OQ-27.
- 2026-08-29 — A71: §5.3 declares the action grid — eight hour offsets, bounded by the decision horizon, shared by EXPLORE and OURS as a binding constraint.
- 2026-08-29 — A72: §14.1 — EXPLORE samples the gate-passing set, not the legal set; the Arm protocol permits consulting gates before choosing.
- 2026-08-30 — A73: §5.7 `Scheduled` added — a schedulable choice is a commitment that fires at `due_tick` and is re-gated on arrival. Resolves OQ-30.
- 2026-08-30 — A74: §12 G5's idempotency key is built from `due_tick`, so rescheduling the same action is not a duplicate.
- 2026-08-30 — A75: §10.1 — the estimator trains only on rows where the choice set had more than one member. Resolves OQ-33.
- 2026-08-30 — A76: §7 — reconciliation runs at the 60-day observation horizon; censoring is reported, never guessed.
- 2026-08-30 — A77: INV-8 names `settle/recon/` as its single exception, and §7 records why. Resolves OQ-20 and OQ-28.
- 2026-08-30 — A78: §14.3 records natural recovery as the mechanism that makes incremental scoring meaningful; `world.natural_recovery` added with per-intent priors.
- 2026-08-30 — A79: §5.5 `ReportedOutcome` carries `reply_text`; §11 records the deterministic classifier and the escalation rate it reports.
- 2026-08-30 — A80: §11 — the escalation rate is measured against a corpus written independently of the classifier. Measured at CP7.0: 44.4% agreement, 72.2% escalation.
- 2026-08-30 — A81: §10.1 — splits are by case; the natural-recovery confound is recorded with its measured base rates.
- 2026-08-30 — A82: §10.2 — EV is uplift over `do_nothing`, which cancels the self-cure component. Attribution windows considered and rejected.
- 2026-08-30 — A83: §10.1 — the retry-timing claim is withdrawn and recorded as a measured negative result.
- 2026-08-30 — A84: §10.1 — model selection is decided on uplift calibration; a split ships a stated hybrid.
- 2026-08-30 — A85: §5.3 and §13 — the agent's liquidity belief is its own POLICY_PARAMS entry; S7 lives in the policy because it needs an EV.
