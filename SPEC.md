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
  mandate_update_due_tick int | null     # A86, a pending re-authorisation
  mandate_revived    bool                # A86, it came back
  contact_response_due_tick int | null   # A89, a pending customer response
  contact_response_verb  ActionType|null # A89, which verb is pending
  last_attempt_tick  int | null          # A90, the tick the last debit fired
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

### 6.1 Contact response — A89

Before A89, no contact verb could produce a settlement. `world.attempt()` ran
for debits only, so a message, a voice call and a human escalation were
dispatched, priced, gated and logged while being structurally incapable of
recovering money. Every comparison of contact-heavy against contact-light arms
made before this point was measuring the absence of a mechanism, not a policy
difference. A86 fixed one instance of this; A89 fixes the class.

"Same recovery, far fewer contacts" is trivially true when contacts cannot
recover anything. The claim only means something once they can.

The mechanism mirrors §9.1's, and for the same reason it is not a coin flip at
dispatch:

- a dispatched contact sets a pending customer response, at a delay drawn from
  `contact_response.delay_h_max`. The arm has to decide what to do while it
  waits;
- when it lands, the customer either pays of their own accord or does not:

      p = contact_response.rate[intent]
          x action_lift[verb]
          x contact_response.behaviour_multiplier[behaviour]
          x p_authorise.dnd_contact_penalty, where it applies

  drawn from the indexed stream `contact_response_draw` at the tick the response
  is *due*, and therefore shared across arms;
- conditioned on `intent_type`, because a message to someone who has left is not
  a message that gets paid, and modulated by §8's debtor behaviour, because
  `go_silent` is near zero by definition and `pay_then_complain` is the one that
  reliably pays;
- `action_lift` is reused rather than duplicated, so a voice call outranks an
  SMS here for the same declared reason it does for a debit, and `serve_notice`
  sits at zero because a regulatory notice is not a persuasion.

**A customer-initiated payment is still a payment.** It runs through the same
`settle()` the debit path uses, so `auth_no_settle_rate`, `settlement_lag_h` and
`will_reverse` all apply, and it is reported through the same §6 layer — it can
be dropped into an SF-2 or duplicated into an SF-3. Routing it around either
would make messaging the one channel where money is certain and reporting is
perfect, which is the opposite of what this project models. WLD-7 asserts it.

Nothing is submitted to a rail, so G3, G4 and G9 have nothing to say about it.
That is the only respect in which it differs from a debit.

**Money already in flight survives the decision horizon.** A response pending
when a stop fires is resolved before stopping: the world runs to 60 days
(§13.1), and a customer who was going to pay on day 31 still pays. Dropping it
would understate every contact-bearing arm by exactly the contacts it made near
the end. The draw is addressed at the due tick, so resolving late is
bit-identical to resolving on time (WLD-8).

**A pending response is not a wake-up reason.** There is nothing for an arm to
decide at the moment a customer pays unprompted, and waking for it inserts
decision points at whatever hour the response lands — mostly hours when G1 shuts
the contact window. Measured at CP9.1: it cut EXPLORE's coverage of every
contact verb by about 60% and inflated `do_nothing`. It would also hand
contact-heavy arms more decisions than contact-light ones for no reason
connected to policy. A pending *re-authorisation* does wake the runner, because
a revived mandate opens debit paths an arm should be given the chance to use.

Every parameter here is ASSERTED, carries a PRIORS row, and is a REQUIRED member
of the D4 sensitivity sweep. `contact_response.rate.*` decides whether
contacting anyone is viable at all, and therefore whether the contact-restraint
result is a finding or an artefact.

**WLD-9 is the general guard.** Every verb must route to `attempt()`, to
`contact_payment()`, or to a declared zero lift with a stated reason. A verb
that falls through all three is a priced no-op, which is the bug CP9 and CP9.1
were both spent on.

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
| `dead_instrument` | card_expired, mandate_revoked, card_stolen | do_nothing, request_mandate_update, send_message, and — once the mandate is ACTIVE again — retry and serve_notice | any retry while the mandate is dead |
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

### 9.1 Mandate re-authorisation — A86

Before A86, `request_mandate_update` was legal, selected, and structurally
incapable of succeeding: it is contact-bearing, so `world.attempt()` produced no
outcome for it, and nothing revived a dead mandate. §9 named it as the recovery
path for `dead_instrument` while the simulator gave that path a hard zero. 17% of
the batch was unwinnable by construction, which inflated every arm's apparent
restraint — a policy that does nothing where nothing can work looks wise rather
than idle.

The mechanism, and it is deliberately not a coin flip at dispatch:

- a dispatched `request_mandate_update` sets a pending re-authorisation, at a
  delay drawn from `mandate_update.response_delay_h_max`. The mandate is dead
  for the whole of that wait, so an arm that asks has to decide what to do in
  the meantime;
- when it lands, `mandate_update.success_rate.<intent>` decides whether the
  mandate becomes ACTIVE, drawn from the indexed stream `mandate_revival_draw`
  at that tick and therefore shared across arms;
- the probability is conditioned on `intent_type`. A churned customer does not
  re-authorise, and a single global rate would make `intent_type` decorative in
  the place it decides most;
- once ACTIVE, G3 stops blocking and the debit paths open.

Both parameters are ASSERTED, both carry PRIORS rows, and both are REQUIRED
members of the D4 sensitivity sweep. `mandate_update.success_rate.*` decides
whether 17% of the batch is winnable at all, which makes it the highest-leverage
unsourced number in the model.

**The retry ban is about the credential, not the class.** §9 forbids
`dead_instrument` any retry because retrying an expired card gets the same
decline. That is not a statement about the card the customer supplies when they
act on the request. Stated as a derivation, for the reason A66 derived
`serve_notice` — a listed exception drifts, a rule does not:

    RETRY is viable for dead_instrument  <=>  mandate_state is ACTIVE

`serve_notice` follows from A66's own rule. The condition reads `ObservedCase`
only, so LEG-3 still holds exactly: no field of `CaseState` changes what
`legal_actions` returns, and EXPLORE's propensity denominator does not move with
a case's contact history. A mandate coming back is a change in the world that
the merchant observes, not a change in gate state — `mandate_state` is a §5.1
field and the registry really does flip.

G10's `class_retry_cap.dead_instrument` moves from 0 to 2 to match. At zero it
was correct while the class could never offer a retry, and would have silently
blocked the one path the class has.

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

**The calibrator is part of the model, and it was erasing the answer** (A92).
Post-hoc isotonic calibration is monotone, so it can never invert an ordering —
it can only *tie* candidates, and ties are the damage. It is a step function
with a few dozen levels, so it cannot express a difference smaller than one
step. A retry costs 5 paise against a median debit near ₹500 and carries no
opt-out risk, so S7 clears one at roughly **0.03%** uplift; isotonic's steps are
around **3 points**, two orders of magnitude coarser than the decision.

Measured at CP10, on real candidate grids from held-out cases:

| | median uplift spread | decisions scored flat | uplift ECE | overall ECE |
|---|---|---|---|---|
| GBM + isotonic | 0.0298 | 11.5% | 0.0193 | 0.0160 |
| GBM, uncalibrated | 0.0708 | 0.0% | 0.0176 | 0.0392 |

Calibration cost 11.5% of decisions their entire resolution and bought **nothing**
on the criterion the model is selected by. Uplift ECE did not notice, because it
bins by predicted uplift before comparing against a matched control rate — a
model returning one number for every candidate is still binned and still scores
well. **Uplift ECE is blind to the failure that matters most to the policy.**

Selection is therefore `min(uplift ECE)` **subject to a resolution floor**: a
scorer flat on more than `MAX_FLAT_DECISION_RATE` of multi-option decisions is
not selectable at any calibration. It is a constant with a confidence interval.
Resolution is measured on real candidate grids built from held-out test cases —
never from the generator, which `settle/agent/` may not import (INV-8).

**The cost is stated, not hidden.** The shipped model's probability *level* is
calibrated to 0.0392 ECE where the rejected candidate reached 0.0160. §14.4
reports the shipped model's number. We give up calibration of the level to keep
calibration and resolution of the difference, because §10.2 uses only the
difference — and the reliability diagram is reported for what actually ships.

A84 said this from CP8 and `train.py` did not do it: it selected on overall ECE,
and `uplift_calibration` was called by nothing. At CP9 the two criteria
disagreed — LR won overall, GBM won the difference — so the shipped model was
chosen by the criterion this section rejects. Wired at CP9.1 (A91): selection is
`min(ece_uplift)`, both criteria are printed for both models, and a disagreement
is named in the training output rather than resolved silently by a `min()`.

**Features must vary across the candidates they are asked to separate** (A93).
Within one decision only the action and its dispatch moment change, so a feature
computed at the *decision* tick is constant across every candidate and cannot
contribute to the choice. `days_since_last_attempt` was computed at the decision
tick and was therefore identical across all eight offsets of a retry — while
ranking 2nd of 45 by permutation importance. It is now computed at the dispatch
moment, and `hours_to_contact_window` is added for the same reason: `18h` and
`30h` are both "tomorrow", and only one of them lands inside G1's window.

Eight of forty-six features now vary across the retry candidates of a single
decision. The remaining thirty-eight describe the case, which is the same case
whichever option is taken.

**Feature parity between training and serving.** The row the estimator is asked
to score must be the row it was trained on. Until CP9.1 `policy.py` omitted
`last_attempt_tick`, so `days_since_last_attempt` and `has_prior_attempt` were
reconstructed in training and constant at serve time. `CaseState` carries the
tick the last debit *fired* — the offset included, because a scheduled retry
reaches the bank when it is submitted — and EST-12 asserts the two rows are
byte-identical for the same inputs.

**Artifacts are content-addressed and never overwritten.** `out/model_<sha>.pkl`,
with `out/model.latest` naming the current one. The CP8-to-CP9 comparison is
unrecoverable because retraining replaced a bare `model.pkl` in place: the world
had changed and the model had changed, and with the old artifact gone the two
could not be told apart. A run that cannot be re-measured against its
predecessor is a run whose numbers cannot be attributed.

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

| G9 | e-mandate pre-debit notice. A served notice opens the debit window `notice_lead_hours` (24) after service and the window then runs `notice_window_days` (3) from that point (A97). Three ways to fail, not two: no notice, too early — inside the lead — or expired. Retries inside the window inherit the notice; retries outside require fresh notice. G9 sequences the plan, it is not a checkbox. **The 24-hour lead is SOURCED**: RBI/DPSS/2026-27/396 (21 Apr 2026) requires the pre-transaction notification "at least 24 hours prior to the actual charge / debit", as did RBI/2019-20/47 before it. It is the only rule in this table with a regulator behind it rather than an assertion. The 3-day window length remains ASSERTED — the framework fixes how long *before* a debit the customer must be told, not how long a notification stays good afterwards. Until CP11.1 the gate enforced only the window and the runner's 24-hour decision cadence supplied the lead by coincidence; a compliance gate that holds because of an unrelated constant is not enforced, it is lucky. A served notice is a full contact: subject to G1's window, counted against G2's frequency cap, and consuming patience_budget. ASSERTED. Consequence: outside an active window a compliant enach retry costs two of three weekly contacts. The alternative treatment (notice as regulatory overhead, exempt from G2) is recorded in Known Limitations. |

| G10 | Class retry budget. Per-class cap on retries, distinct from G4's card-network cap. `ambiguous` = 1: §9 permits one retry at a different hour, then a message. Without this, `ambiguous` retries are unbounded until G4 or S3 fires. |
| G11 | TRAI DND registry. Blocks `voice_call` when `dnd_flag` is set. Does not block SMS or WhatsApp: DND covers unsolicited commercial contact, and a transactional message to an existing customer about a failed payment is exempt. That exemption is ASSERTED and recorded in Known Limitations. |

**G1 and the TRAI time band — unverified, recorded rather than guessed (A98).**
G1 opens contact at 08:00 IST. Reporting on the 2025 TCCCPR amendments describes
a prohibited window of 21:00-09:00, which would make our start an hour too
early. The clause could not be extracted from TRAI's consolidated PDF, so this
is flagged rather than corrected. If it holds, INV-2's window is one hour too
wide at the start and two hours narrower than required at the end. Recorded in
Known Limitations.

The window is deliberately **not** changed. Guessing at a regulation is worse
than documenting that we could not verify it: a window moved to 09:00 on the
strength of a press summary would be a compliance claim resting on the same
quality of evidence this project criticises elsewhere, and it would silently
change every arm's contact opportunities in the process.

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
mandate_revival_draw   per pending re-authorisation
mandate_response_delay per mandate update dispatched
contact_response_draw  per pending customer response
contact_response_delay per contact dispatched
```

`out_of_order` and the natural-recovery pair are shared for the same reason as
the rest. A reporting distortion drawn from a private address would differ
between arms facing the same case, and a self-cure that differed between arms
would make §14.3's subtraction compare two different events.

The mandate pair (A86) and the contact pair (A89) are shared for the same
reason: whether a customer re-authorises, or goes and pays after being
messaged, is a fact about the customer, so two arms that act at the same tick
must get the same answer. Without that, "the arm that contacts more recovers
more" would be partly a statement about which arm drew the luckier numbers. They are two addresses rather than one because
the success is drawn at the tick the re-authorisation lands and the delay at the
tick it was requested — one stream would make a second request's delay the same
number that decided the first request's success whenever they coincide, and a
coupling nobody declared is exactly what this section exists to remove.

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

### 15.1 The sourcing pass — CP11, carried forward at CP11.1

**188 rows: 1 SOURCED, 3 DERIVED, 184 ASSERTED.** The CP11 pass itself scored
187 rows at 0 SOURCED, 2 DERIVED, 185 ASSERTED; CP11.1 added the one SOURCED row
and moved one more into DERIVED, both as direct consequences of what the pass
found. The full citation list, with what each source establishes and what it
deliberately does not support, is in PRIORS.md under "Provenance".

That result is the honest one and the README says so in those words. It is not
for want of looking. The searches found real primary material — the RBI
e-mandate framework, Razorpay's own settlement and retry documentation, TRAI's
TCCCPR — and almost none of it is a number about this population. **Indian
payments data is published as system-wide aggregate**: UPI volumes,
business-versus-technical decline splits, NACH return counts, mandate creation
totals. **This model concerns the conditional behaviour of a customer whose
recurring debit has already failed** — how often asking them to re-authorise
works, how often a message becomes a payment, what a contact costs in opt-out
risk. **Nobody publishes that.** The regulators publish the opposite kind of
number, and no amount of further searching changes that.

**The boundary was applied strictly, and near-miss rows were left ASSERTED
deliberately.** Several were one plausible sentence from a citation and did not
get one: `class_retry_cap.transient = 3` matches Razorpay's three automatic
reattempts, but that cadence is class-blind, so the match is a coincidence of
one class rather than a derivation. `card_network_retry_cap = 4` sits inside the
published network reattempt caps but is not computed from them, so it is bounded
by a source rather than sourced. `decline_class_mix.time_shiftable = 0.46` has
the right *direction* — NPCI-attributed reporting says insufficient balance
dominates recurring-debit failure — and no public number behind the magnitude.
Each of those carries its near-miss in its own source cell. A citation stretched
to cover a number it does not support invites a reader to check it, and being
caught overselling one source discredits every other row in the file.

| tier | rows | which |
|---|---|---|
| SOURCED | 1 | `notice_lead_hours = 24` — RBI/DPSS/2026-27/396: pre-transaction notification "at least 24 hours prior to the actual charge / debit" |
| DERIVED | 3 | `attempt_number.max = 4` (original charge + Razorpay's three reattempts); `settlement_lag_h_max = 96` (T+2 working days spanning a weekend); `settlement_lag_h.mean = 56` (between the 48h floor and the 96h maximum of the same documented cycle) |
| ASSERTED | 184 | everything else, each saying so in its own row |

**What the pass found, and what CP11.1 did about it.**

1. **`settlement_lag_h.mean = 38` contradicted our own cited settlement cycle.**
   T+2 working days is 48h at its shortest, so 92% of our settlements were
   landing faster than the vendor documentation says is possible. **Fixed at
   CP11.1 (A96): 38 -> 56**, and the row reclassified DERIVED. The realised mean
   moves 38.05 -> 56.07 and the share landing at or beyond the documented 48h
   floor moves 8.0% -> 86.6%. It changed almost nothing downstream, which is
   itself the finding: see §15.3.
2. **G9 enforced the notice *window* but not the RBI's 24-hour *lead*.** A debit
   was permitted the instant a notice was served. **Fixed at CP11.1 (A97)**, and
   the lead is now the project's only SOURCED constant.
3. **G1 opens contact at 08:00 IST, and the TRAI band may start at 09:00.**
   **Deliberately not fixed** — see §12. The clause could not be extracted from
   the primary text, and guessing at a regulation is worse than documenting that
   we could not verify it.

### 15.2 The sensitivity sweep — CP11

`settle/eval/sensitivity.py`, 2,000 cases, seed 42, results in
`out/sensitivity.json`. Fourteen members — every row PRIORS marks REQUIRED, plus
`p_opt_out.*`, `ltv_months` and `action_lift.*` — each at 0.25x, 0.5x, 1x, 2x and
4x of its shipped value, clamped to the range the consumer can legally take. Two
conclusions are checked at every point:

    headline   OURS incremental rate  >  B2 incremental rate
    restraint  OURS contacts per case <  B2 contacts per case

At 1x: OURS 27.90% against B2's 25.65%, 0.0045 contacts per case against 1.426,
₹0.09 per ₹100 recovered against ₹0.27.

**The estimator is not retrained at any point.** The shipped model was fitted on
EXPLORE logs drawn from the world at 1x, so every off-1x row is a policy running
a model that is now wrong about the world. That is the question a merchant
actually faces — a prior is an estimate and the policy has to survive being
wrong about it — but it is not "what OURS would score if refitted here", and the
two must not be read as the same number.

**Survival ranges.** Both conclusions hold across the full 0.25x–4x span, a
16-fold swing, for eleven of the fourteen members. Three lose the headline at
4x and only at 4x:

| member | headline | restraint |
|---|---|---|
| `mandate_update.success_rate.*` | 0.25x–2x | 0.25x–4x |
| `contact_response.rate.*` | 0.25x–2x | 0.25x–4x |
| `contact_response.behaviour_multiplier.*` | 0.25x–2x | 0.25x–4x |
| every other member | 0.25x–4x | 0.25x–4x |

The restraint conclusion never flips anywhere in the sweep.

**How the three flips happen, which matters more than that they happen.** OURS
does not get worse. It is flat at 27.90% across all five multiples of all three.
B2 climbs into it: at 4x `contact_response.rate.*` B2 reaches 28.00% and passes
us; at 4x `mandate_update.success_rate.*` it reaches 27.90% and ties.
`contact_response.rate.*` and `contact_response.behaviour_multiplier.*` produce
identical numbers because §6.1 multiplies them, so they are one exposure
reported twice, not two.

**Why OURS is flat, and what it costs us — the exposure, recorded verbatim
(A99).** These are the words the finding is recorded in, and the README carries
them:

> The 2.25-point margin is not a claim that our contacts outperform B2's. It is
> a claim that contacting is not worth doing. Seven of fourteen swept parameters
> leave OURS's incremental rate completely unmoved, because a policy that does
> not contact cannot be affected by any prior describing what happens when you
> do. Every contact-side parameter in the sweep moves B2 and only B2.
>
> The headline flips at 4x on `contact_response.rate.*`, an ASSERTED number set
> conservatively by our own admission, and it flips in the direction a sceptical
> reader would guess: if contacting customers works better than we assumed, the
> fixed dunning ladder beats us.
>
> Restraint here is a claim about CONTACTS, not activity. OURS dispatches 6,993
> actions against B2's 6,576. It is more active and less intrusive. The README
> must say "far fewer contacts" and must not permit the reading "far less work".

**The zero-contact behaviour is a priced decision, not an incapacity.** Halving
`p_opt_out.*` or `ltv_months` — equivalent, since §20 multiplies them — takes
OURS from 0.0045 to 0.011 contacts per case, and quartering either takes it to
0.028. Doubling either takes it to zero. S7 behaves the same way: at 0.25x
`economic_stop_multiple` OURS contacts 0.021 per case and recovers 28.00%, and
at 4x it contacts nothing and recovers 27.55%. The policy is trading 0.45 points
of recovery for the last of its contacts, and that trade is visible and
reversible rather than hard-wired.

**The parameter named REQUIRED since CP2.3 turns out not to matter.**
`world.liquidity_window_days` moves OURS from 27.65% to 28.25% across a 16-fold
swing — 0.6 points. That is the same conclusion A83 reached from the estimator
side when it withdrew the retry-timing claim, reached independently from the
world side. Both conclusions survive the whole range.

**`MAX_FLAT_DECISION_RATE` is inert, and the reason is worth stating.** Nothing
moves at any multiple, because the uncalibrated GBM wins uplift ECE outright
(0.0176 against GBM+isotonic's 0.0193) and is therefore selected at every floor
from 0.0125 to 0.20. A92's resolution floor is not currently load-bearing: it
would decide the outcome only if the flat candidate also won on calibration of
the uplift. At CP10 it did not. The floor is a guard against a failure that has
not recurred, not a tuning knob, and the sweep says so.

### 15.3 What the CP11.1 corrections moved — measured, not assumed

Both fixes came out of §15.1's findings, and both were measured before and after
at 2,000 cases on seed 42, one at a time.

**A96, the settlement lag, moved almost nothing.** SF-1 is unchanged for every
arm (23 for OURS, 23 for B2). SF-7 moves by one case, on B2 only (8 -> 9). B0's
recovery, both arms' incremental rates, contacts and cost per ₹100 are all
bit-identical. The hypothesis was that a longer settlement tail leaves more
authorisations unsettled at any moment and widens the window those classes are
detected in. It does not, for two reasons worth recording: **SF-1 is a fact
about whether money settles at all, not about when** — it is driven by
`will_settle` and `auth_no_settle_rate`, neither of which moved — and the
reconciler runs at a 60-day observation horizon that absorbs an 18-hour shift
without noticing. The parameter is live, not dead: the realised mean moves
38.05 -> 56.07 and the share landing beyond the documented 48h floor moves
8.0% -> 86.6%. It was simply wrong in a direction nothing downstream was
sensitive to. A wrong number that changes no result is still worth fixing, and
worth reporting as having changed no result.

**A97, the 24-hour lead, moved no arm's numbers at all.** Recovery, incremental
rate, contacts, dispatches, spend and every silent-failure class are identical
for OURS, B2 and B0. The only movement is in gate accounting: B2's G9 blocks
fall 4,052 -> 4,002, because the window now sits 24 hours later and the 72-96h
band that used to be expired is now inside it.

**The new branch never fires.** `G9_NOTICE_LEAD_NOT_ELAPSED` is returned zero
times across a 2,000-case run of B1 and of B2; every G9 block is still an
expiry. That is structural rather than lucky twice over:
`decision_cadence_hours` and `notice_lead_hours` are both 24, so no arm on the
runner's cadence can offer a debit inside the lead, and OURS consults gates
before choosing so it never proposes one. §7 says a detector that always reports
zero is indistinguishable from a broken one — GAT-9 is what makes this one
distinguishable, and it asserts the block at 0h, 1h and 23h and the permission
at exactly 24h. **The point of A97 is not that it changed a number. It is that
the rule now holds because it is enforced rather than because two unrelated
constants happen to be equal**, and it will keep holding if either of them moves.

## 16. Razorpay integration

Real at the edges, simulated at scale.

- Three FastAPI routes, and exactly three:

  ```
  POST /webhooks/razorpay   signature-verified webhook receiver
  POST /voice/extract       upload audio, returns full extraction trace
  GET  /                    serves the static viewer
  ```

  A route that is declared and not yet built returns 501, never 404. The route
  table is a contract; one that grows to fit whatever got implemented is not.

- HMAC SHA256 signature verification, mandatory
- Verification happens BEFORE the body is parsed. The signature covers the raw
  bytes, so re-serialising a parsed body checks a different string from the one
  that was signed; and parsing first runs a decoder on unauthenticated input,
  then reports a *parse* error to a sender holding no secret. A malformed body
  with a bad signature must fail on the signature.
- Handler verifies, writes the raw event, returns 200. Nothing else.
  Razorpay requires 2XX within 5 seconds; it retries with exponential backoff
  for 24 hours and disables the webhook after 24h of failure. All processing
  runs after the response is sent, never inside the handler.
- The raw event is on disk before the response starts, which is INV-5's
  write-ahead ordering applied to the edge: a record written after the 200 does
  not exist if the process dies in between, and Razorpay never re-sends an
  event it has already had a 2XX for.
- Idempotency store keyed on event id — real webhooks genuinely do arrive twice.
  This is SF-3's production instance. The store holds exactly one row per event
  id, and that row carries a `delivery_count`: a replay is counted, not
  discarded, mirroring `ReportedOutcome.arrival_count` in §5.5. Exactly one
  delivery is allowed to cause work.

  **The ledger records every delivery; the store records the event once.** These
  are deliberately different, and "recorded once" means the second. A replay
  appends its own entry under `WEBHOOK_REPLAY` while the store's single row has
  its count incremented and nothing is dispatched. Suppressing the second ledger
  entry would make a duplicate arrival invisible, and a duplicate arriving is a
  fact about the world — it is the exact condition §7's auditor claims to
  detect, so the system cannot be blind to it in its own inbox.
- Edge traffic writes to its own ledger under arm `EDGE`, not into a per-arm
  evaluation ledger. The evaluation's ledgers replay from a seed; interleaving
  live traffic into one would make it neither replayable nor live.
- Subscribed events: `payment.captured`, `payment.failed`, `payment_link.paid`.
  An unsubscribed event arriving is recorded and flagged — it means the
  dashboard and the code disagree, and the ledger is where that becomes visible.
- Demo shows one real `pay_...` / `plink_...` id next to its ledger row,
  committed to `out/razorpay_demo.json` so the artefact survives ngrok going
  away. `scripts/razorpay_demo.py` is the entry point, mock by default.

**The committed artefact is self-verifying, and its chain covers a projection.**
Razorpay's checkout SMS-verifies the payer's phone number, so a real payment
carries a real mobile number and there is no placeholder that can complete one.
That number is not published.

The rejected fix was to hash the raw event and strip the number before
committing. A hash computed over content the reader cannot see, published beside
content they can, is not evidence — it reduces to "trust me, it verified before I
edited it", which is the claim about payment outcomes this whole project exists
to refuse. An artefact making that claim about its own integrity would be
self-refuting.

So the published chain is defined over a **contact-free projection** built from a
fixed allow-list of fields: ids, statuses, amounts, method, order id, payment
link id, notes, timestamps, event id, delivery count, signature verification.
Customer contact fields are absent from that schema — not blanked, not masked,
with no branch that admits them, and an allow-list rather than a deny-list so the
next field Razorpay adds cannot arrive by default. The hash covers the
projection, the projection is what is committed, and a reader recomputes over
exactly the bytes in front of them with `settle.schema.canonical`.

This is a stated scope, not a redaction. The chain covers the projection; it does
not cover the raw event, which stays on the machine that received it in
`out/razorpay_raw*.json`, gitignored. Test RZP-4.

**Real vs synthetic, enforced by the type.** Every payment link record carries an
explicit `source`: `RAZORPAY_TEST_MODE` for an object that exists in Razorpay's
test mode, `MOCK_SANDBOX` for one constructed locally. The pairing is validated,
not trusted — a mock id wears a `MOCK_plink_` prefix and a mock URL sits on the
reserved `.invalid` TLD (RFC 2606), so the difference survives a screenshot and
a record cannot be relabelled after it is built. This is the one idea worth
taking from the competing repos in the track, and its rule is theirs: a payment
link created is not revenue recovered.

`RAZORPAY_MOCK_MODE` defaults to true, so a judge cloning the repo without
credentials gets a working demo rather than a stack trace. The opposite default
is the dangerous one — a missing key that yields a plausible-looking id. Live
keys are refused outright; there is no live path.

Credentials in `.env` only, read from the environment and never hardcoded, and
scrubbed out of any exception, log line or ledger entry that could carry them.
`.env` in `.gitignore` from commit one.

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
    grid.py        the action grid, shared by OURS and EXPLORE (A87)
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
- PLAN.md is edited by CC only, via targeted str_replace. **PLAN.md belongs on
  every allowlist by default.** It was absent from every one of them from CP4 to
  CP12, so the file CC owns was the file CC was never permitted to touch, and it
  sat at `(pending)` for nine checkpoints. Added at CP12.1.

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

- OQ-54 — the estimator returned identical probabilities for a large share of
  multi-option decisions, so OURS declined retries costing 5 paise at zero
  opt-out risk and lost to B2 on incremental recovery. Diagnosed at CP10 in four
  parts: the probabilities were bit-identical rather than rounded together; the
  isotonic calibrator was the cause; the uplift was *not* swamped by noise
  (signal-to-noise 2.95 on the paired difference); and eight features do vary
  across retry candidates, so the model was not structurally unable to
  discriminate. Resolved by A92, with A93 fixing a feature that should have
  varied and did not. A direct two-model uplift learner was tested and was worse
  on every measure — recorded below rather than dropped.

- OQ-51 — no contact verb could produce a settlement. `world.attempt()` ran for
  debits only, so `action_lift.send_message`, `.voice_call` and
  `.escalate_human` carried PRIORS rows while sitting in a branch only debits
  reached, and `p_authorise.dnd_contact_penalty` with them. The project's
  primary claim — same recovery, far fewer contacts — was trivially true because
  contacts were structurally incapable of recovering anything. Resolved at CP9.1
  by A89, and guarded generally by WLD-9: every verb routes to `attempt()`, to
  `contact_payment()`, or to a declared zero lift with a stated reason.
- OQ-52 — `policy.py` never passed `last_attempt_tick`, so two features were
  reconstructed in training and constant in use. Resolved by A90. EST-12.
- OQ-53 — A84's selection rule had no code path and the shipped model was chosen
  by the criterion A84 rejects. Resolved by A91. Model artifacts are
  content-addressed in the same amendment, so the next world change can be
  separated from the next model change.
- OQ-50 — `settle/agent/policy.py` imported the action grid from
  `settle/runner/arms/explore.py`. The agent is the thing being evaluated and
  the runner is the harness that evaluates it, so the dependency ran backwards
  and the policy could not be used without the experiment that measured it —
  the same error class as CP3.1's escalation rule. Resolved by A87: the grid
  lives in `settle/policy/grid.py`, both consumers import it from there, and
  POL-8 walks the AST of every module under `settle/agent/` to assert none of
  them imports `settle.runner`.
- OQ-49 — a 10,000-case OURS run took 25 minutes, which is unusable for the D4
  sweep and impossible in a demo. Resolved by A88 down to 3.2 minutes, and the
  residue is reported rather than hidden: `predict_proba` costs ~3.5ms whether
  it is handed one row or fifteen, because the time goes on walking 200 boosting
  iterations, so the fix was fewer calls rather than fewer rows. The memo and
  horizon warming take a 100-case run from 2,889 predict calls to 134. POL-9
  asserts the probabilities are bit-identical to an unwarmed estimator.
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
- 2026-08-30 — A86: §9.1 added — a dispatched `request_mandate_update` can revive a dead mandate, at a delay, with the success drawn from the shared `mandate_revival_draw` stream and conditioned on `intent_type`. §9's retry ban is restated as a rule about the credential rather than the class; §5.7 gains `mandate_update_due_tick` and `mandate_revived`; §14.2 gains two streams. `class_retry_cap.dead_instrument` moves 0 → 2. Tests WLD-3, WLD-4, WLD-5.
- 2026-08-30 — A87: §17 — the action grid moves to `settle/policy/grid.py`; `settle/agent/` may not import `settle/runner/`. Resolves OQ-50. Test POL-8.
- 2026-08-30 — A88: §10.1 — the estimator memoises on `(action, tick, last_attempt_tick)` within a case and warms the remaining decision horizon on a miss. An optimisation only: POL-9 asserts bit-identical probabilities. Resolves OQ-49.
- 2026-08-30 — A89: §6.1 added — a dispatched contact can lead to a customer-initiated payment, at a delay, drawn from the shared `contact_response_draw` stream and conditioned on `intent_type` and on §8's debtor behaviour. It settles through the same `settle()` a debit does and reports through the same §6 layer. §5.7 gains `contact_response_due_tick` and `contact_response_verb`; §14.2 gains two streams. Tests WLD-6, WLD-7, WLD-8, WLD-9. Resolves OQ-51.
- 2026-08-30 — A90: §5.7 and §10.1 — `last_attempt_tick` recorded on `CaseState` and passed by the policy, closing the train/serve skew on `days_since_last_attempt` and `has_prior_attempt`. Test EST-12. Resolves OQ-52.
- 2026-08-30 — A91: §10.1 — A84's uplift-calibration rule is implemented in `train.py` and both criteria are printed; model artifacts become `out/model_<sha>.pkl` with an `out/model.latest` pointer. Resolves OQ-53.
- 2026-08-30 — A92: §10.1 — the calibrator is part of the model and enters selection as its own candidate; selection is `min(uplift ECE)` subject to a resolution floor, because uplift ECE is blind to a scorer that has stopped discriminating. Isotonic is rejected on resolution and the cost to the reported ECE is stated. Test EST-13. Resolves OQ-54.
- 2026-08-30 — A93: §10.1 — `days_since_last_attempt` is computed at the dispatch moment rather than the decision tick, so it varies across the offsets it is asked to separate; `hours_to_contact_window` added for the same reason. 46 features.
- 2026-08-31 — A94: §15.1 added — the sourcing pass, its tier counts (0 SOURCED, 2 DERIVED, 185 ASSERTED), the citation list in PRIORS.md, and three findings recorded rather than fixed: `settlement_lag_h.mean` contradicts the settlement cycle we cite, G9 enforces the notice window but not the RBI 24-hour lead, and G1 opens an hour before the TRAI band appears to permit.
- 2026-08-31 — A95: §15.2 added — the sensitivity sweep. `settle/eval/sensitivity.py`, fourteen members at 0.25x–4x, results in `out/sensitivity.json`. Both conclusions survive the full range for eleven members; three lose the headline at 4x, all by B2 climbing rather than OURS falling. Seven members leave OURS unmoved because it makes 9 contacts in 2,000 cases. Tests SEN-1, SEN-2, REB-1.
- 2026-08-31 — F6: `tests/test_ledger.py` — EXE-1's world-reader list gains `settle/eval/sensitivity.py` as a third named exception, with the reason recorded in the list itself. It rebinds `world.ACTION_LIFT` after patching PARAMS and never dispatches; reaching the module through `sys.modules` would have passed the test by evading it.
- 2026-08-31 — A96: `settlement_lag_h.mean` 38 -> 56, and the row reclassified DERIVED against Razorpay's documented T+2 working-day cycle (48h floor, 96h weekend-spanning maximum). Found by the CP11 sourcing pass: the old value contradicted the very source it would have cited. §15.3 records what it moved, which is one SF-7 case.
- 2026-08-31 — A97: §12 G9 — a served notice opens the debit window `notice_lead_hours` (24) AFTER service, not at the moment of service, and the window then runs `notice_window_days` from that point. `notice_lead_hours` enters POLICY_PARAMS as the project's first SOURCED prior, citing RBI/DPSS/2026-27/396. New reason code `G9_NOTICE_LEAD_NOT_ELAPSED`. Tests GAT-9, GAT-13. §15.3 records that it moved no arm's numbers, which is the intended result.
- 2026-08-31 — A98: §12 — the G1 / TRAI time-band ambiguity recorded rather than guessed. The window is deliberately unchanged. Known Limitations.
- 2026-08-31 — A99: §15.2 — the exposure finding recorded verbatim: the margin is a claim that contacting is not worth doing, and restraint is a claim about contacts rather than about activity.
- 2026-08-31 — A100: §15.1 — the sourcing outcome recorded plainly, with the structural reason Indian payments data cannot supply these numbers and the statement that near-miss rows were left ASSERTED deliberately. Counts move to 188 rows: 1 SOURCED, 3 DERIVED, 184 ASSERTED.
- 2026-09-01 — X7: §16's heading was duplicated on one line (`## 16. Razorpay integration## 16. Razorpay integration`). Corrected.
- 2026-09-01 — A101: §16 — signature verification is specified to happen BEFORE the body is parsed, with the reason. Test WBH-3.
- 2026-09-01 — A102: §16 — the idempotency store holds one row per event id carrying a `delivery_count`; a replay is counted rather than discarded, mirroring `ReportedOutcome.arrival_count` in §5.5, and exactly one delivery causes work. SF-3's production instance. Test WBH-4.
- 2026-09-01 — A103: §16 — the raw event is on disk before the response starts, INV-5's write-ahead ordering applied to the edge. Edge traffic writes to its own ledger under arm `EDGE` rather than into a per-arm evaluation ledger. Test WBH-6.
- 2026-09-01 — A104: §16 — real-vs-synthetic labelling made a validated property of the record rather than a convention: `source` is `RAZORPAY_TEST_MODE` or `MOCK_SANDBOX`, mock ids carry a `MOCK_plink_` prefix, mock URLs sit on the reserved `.invalid` TLD, and the pairing is enforced by the model. `RAZORPAY_MOCK_MODE` defaults to true so the repo runs without credentials; live keys are refused. Tests RZP-1, RZP-2.
- 2026-09-01 — A105: §16 — the route table is fixed at exactly three, and a declared-but-unbuilt route returns 501 rather than 404.
- 2026-09-01 — A106: §16 — the committed artefact's hash chain is defined over a contact-free projection rather than over the raw webhook event, because Razorpay's checkout SMS-verifies the payer's number and a hash over unpublished content cannot be recomputed by a reader. Stated scope, not redaction: contact fields are absent from the projection schema, built from an allow-list. Raw events stay local and gitignored. Test RZP-4.
- 2026-09-01 — A107: §16 — the ledger records every webhook delivery while the idempotency store records the event once. Recorded explicitly so WBH-4's "recorded once" reads as the deliberate interpretation it is: suppressing the replay entry would blind the system to the duplicate-arrival condition §7 claims to detect.
- 2026-09-01 — A108: §16 — `scripts/razorpay_demo.py` named as the demo entry point, mock by default so a clone with no credentials runs.
- 2026-09-01 — A109: §18 — PLAN.md belongs on every allowlist by default. It was on none from CP4 to CP12, so the one file CC owns was the one file CC could never edit, and it sat at `(pending)` for nine checkpoints.
- 2026-09-01 — F9: `requirements.txt` — `httpx==0.28.1` pinned. The webhook tests keep driving the ASGI app directly rather than switching to `TestClient`: only the direct caller can timestamp `http.response.start`, which is where Razorpay's 5-second budget actually stops.
- 2026-09-01 — A110: §16 — `reference_id = case_id` recorded in Known Limitations. Razorpay enforces it unique, which buys free vendor-side idempotency and costs the ability to issue a second link for a case that legitimately needs one. Production would key it on `case_id` plus an attempt counter.
- 2026-09-01 — A111: README gains the Known Limitations section SPEC §19 fixes as its last, opened with the Razorpay edge's four entries. It is explicitly incomplete: six existing "recorded in Known Limitations" references across SPEC.md and PRIORS.md are named there as outstanding and land in D5. A section that reads complete while entries are missing is worse than no section.
- 2026-09-01 — F13: `plink_TWrboR36RZ13fH` (unpaid, case_000001) cancelled via the API. No live payment link remains on the test account, so no stray webhook can reach the ngrok URL now that it points back at an unrelated app.
- 2026-09-01 — F14: `tests/test_webhook.py` — the "No TestClient" docstring corrected. It still claimed httpx was unpinned after F9 pinned it, which made a correct decision read as a forced one. The blocked-fix note in `checkpoints/cp12_1.allowlist` is now discharged.
- 2026-09-02 — A112: §17 — `settle/eval/report.py` added: the committed metrics artefact, `out/charts/metrics.json`, built from the run artefacts and `out/sensitivity.json`. Every number the README quotes is drawn from it and CHT-3 asserts the correspondence. A README figure with no artefact behind it is the failure this project exists to criticise, so it is a test rather than a habit.
- 2026-09-02 — A113: §17 — `settle/eval/charts.py` added. Four charts, committed to `out/charts/`, regenerated by one command and rendered only from the committed artefact. CHT-1 asserts byte-identical re-renders and that the committed PNGs match a fresh render, so a chart cannot drift from the numbers it draws.
- 2026-09-02 — A114: §19 — the fixed README order is implemented, and the sourcing outcome and the calibration trade are stated in the body rather than an appendix. Section order is asserted by test, because the order is the argument: metrics before architecture before limitations.
- 2026-09-02 — A115: `KNOWN_LIMITATIONS.md` added, discharging the six references that had pointed at nothing since CP2 and adding the measured negative results. Every entry states the limitation and its cost; a limitation that only restates a design decision is a feature description. Recorded here rather than in the README because it outgrew a section.
- 2026-09-02 — A116: §14.4 — incremental recovery is reported per decline class as well as in aggregate. The aggregate hides the shape: the entire OURS margin is `auth_abandoned` (+48.9 points) and it loses to B2 on three of six classes including the largest. Chart 3 draws the losses at the same weight as the wins.
- 2026-09-02 — A117: §14.4 — B3's headline recovery is reported next to its 889 compliance violations and 52.70% silent-failure rate wherever it appears. B3 recovers more than OURS and runs in OBSERVE; printing the first number without the second two would misrepresent an unguarded upper bound as a competitor.
