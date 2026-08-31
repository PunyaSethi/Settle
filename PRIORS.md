# settle — PRIORS

Every numeric parameter in the generator and world model gets a row. Sources
must be public and citable. Anything without a source is marked `ASSERTED`, in
this table, in the README, and in the run output (INV-10).

INV-10 covers every number that can move a reported metric, including the ones
that describe the shape of the action space or of a behaviour (SPEC §15).
`action_lift.*` decides whether a retry outperforms a message; `reply_mix.*` and
`debtor.disengage_after_contacts` drive the promise-kept rate and the
opt-outs-induced count; `p_authorise.*` decides whether a retry beats a message
at 03:00. "Structural, not fitted" is exactly the reasoning that lets an
unsourced number reach a headline.

Sections are load-bearing, not decoration. `scripts/gate.sh` runs GEN-4, which
checks the **Sampled parameters** table against `settle.sim.generator.PARAMS` in
both directions: a sampled parameter with no row fails the build, and so does a
row with no parameter.

## Provenance — the CP11 sourcing pass

Three tiers, and the boundary is strict:

- **SOURCED** — a public figure supports this value directly.
- **DERIVED** — computed from a sourced figure, with the derivation stated in the row.
- **ASSERTED** — no public source found. The row says so.

Counted 2026-08-31 over all 188 rows in this file, after CP11.1:

| tier | rows |
|---|---|
| SOURCED | 1 |
| DERIVED | 3 |
| ASSERTED | 184 |

The CP11 pass itself scored 187 rows at 0 / 2 / 185. CP11.1 added
`notice_lead_hours` as the one SOURCED row (A97, and it is now enforced by G9
rather than supplied by coincidence) and moved `settlement_lag_h.mean` into
DERIVED by correcting it to a value the cited cycle actually admits (A96). Both
changes are consequences of what the pass found, not of further searching.

That is the honest outcome, and the README says so in those words. It is not
for want of looking. The sources below are real, primary and citable — RBI
circulars, Razorpay's own product documentation, TRAI's regulations — and
almost none of them is *a number about this population*. Indian payments data
is published as system-wide aggregate: UPI volumes, business-versus-technical
decline splits, NACH return counts, mandate creation totals. This model is
about the conditional behaviour of a customer whose recurring debit has already
failed — how often asking them to re-authorise works, how often a message turns
into a payment, what a contact costs in opt-out risk. Nobody publishes that,
and the regulators publish the opposite kind of number.

Where a source described the right regime but not the right value, the row was
left ASSERTED and the source recorded as context in the row's own cell. A
citation stretched to cover a number it does not support invites a reader to
check it, and being caught overselling one source discredits every other row in
this file. Three rows survive that test as DERIVED. Exactly one survives it as
SOURCED, and it is a regulator's minimum rather than a measurement.

### What was consulted

| source | accessed | what it establishes | what it does not support |
|---|---|---|---|
| RBI, *Digital Payments — E-mandate Framework, 2026*, RBI/DPSS/2026-27/396, 21 Apr 2026 — https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=13374 | 2026-08-31 | **The one source that supports a value directly** (`notice_lead_hours = 24`, A97). The pre-debit rule G9 exists to model: "An issuer shall send a pre-transaction notification to the customer, at least 24 hours prior to the actual charge / debit." AFA-exempt limits of ₹15,000 per transaction, ₹1,00,000 for insurance, mutual-fund and credit-card-bill categories. A mandate may be withdrawn at any time, which is what makes `mandate_state=revoked` a real state rather than a modelling convenience. | `notice_window_days`. It fixes how long *before* a debit the customer must be told, not how long a notification stays good for debits afterwards, so the window length stays ASSERTED while the lead is SOURCED. It sets AFA thresholds, not an amount distribution, so `amount.*` and `escalation.min_amount_paise` stay ASSERTED. |
| RBI, *Processing of e-mandate on cards for recurring transactions*, RBI/2019-20/47, 21 Aug 2019 — https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=11668&Mode=0 | 2026-08-31 | The original of the same rule, at "at least 24 hours prior to the actual charge / debit to the card", with the AFA cap then at ₹2,000. Establishes that the 24-hour notification has been in force for the whole period any Indian recurring-payment dataset would cover. | Same as above. |
| Razorpay Docs, *Payment Retries* (Subscriptions) — https://razorpay.com/docs/payments/subscriptions/payment-retries/ | 2026-08-31 | The reference dunning cadence B2 is modelled on: a failed charge is reattempted on T+1, T+2 and T+3, and "if the charge still fails, the Subscription moves to the `halted` state". On emandate, retries occur "only when we get the confirmation or rejection of the last payment, as it may take more than 24 hours". The failure email "contains a link that the customer can use to change the card details" — the real-world `request_mandate_update`. | The retry cadence is class-blind, so no per-class row inherits it: `class_retry_cap.transient = 3` matching the vendor's three reattempts is a coincidence of one class, not a derivation, and stays ASSERTED. It gives no success rate for any of those reattempts, so `action_lift.*`, `mandate_update.success_rate.*` and `contact_response.*` stay ASSERTED. Its offsets are 24/48/72h and `action_grid` has 48 and 72 but not 24, so the grid is not derived from it. |
| Razorpay Docs, *Settlements* — https://razorpay.com/docs/payments/settlements/ | 2026-08-31 | The settlement cycle INV-1 and §13.1 are built around: "T+2 working days (where T is the date of transaction capture)", "working days do not include the bank holidays", and settlement moving to "the next working day after the bank holiday". Resolves OQ-6's cheap INV-10 win, in one direction only. | It supports a *maximum*, not a mean — see the finding recorded against `settlement_lag_h.mean` below. It says nothing about how often an authorisation fails to settle, so `auth_no_settle_rate` and `will_settle_rate` stay ASSERTED. |
| TRAI, *Telecom Commercial Communications Customer Preference Regulations, 2018* — https://trai.gov.in/node/3199 | 2026-08-31 | That the DND/preference regime G11 models exists, is a regulation rather than a courtesy, and distinguishes promotional from transactional and service messages — which is the distinction G11's exemption rests on. | The consolidated regulation is published only as a PDF this pass could not extract text from, so no clause is quoted here and nothing is elevated above ASSERTED on its authority. In particular the permitted-hours band was **not** verified against the primary text; see the G1 finding below. |
| Nets (Nexi Group) support, *Visa fines on attempts to complete already declined transactions*, published 28 Nov 2023, updated 12 May 2026 — https://support.nets.eu/article/visa-fines-on-attempts-to-complete-already-declined-transactions | 2026-08-31 | That a card-network reattempt cap of the kind G4 models is real and enforced with fees: "it is not allowed to reattempt, a transaction that has previously been declined, 15 or more times in a 30-day period." | This is a PSP's restatement with no Visa rule reference or effective date, and 15 is a ceiling rather than a value. `card_network_retry_cap = 4` is bounded by it, not derived from it, and stays ASSERTED. |

### Findings from the sourcing pass

Found at CP11 with the world model frozen, carried into CP11.1 and resolved
there. What each correction actually moved is measured in SPEC §15.3 rather than
assumed.

1. **RESOLVED (A96). `settlement_lag_h.mean = 38` contradicted our own cited
   settlement cycle.** T+2 *working* days from capture is 48 hours at its
   shortest, so 38h was faster than the vendor documentation we would cite for
   it — not merely unsourced but inconsistent with its nearest source. Raised to
   56, between the 48h floor and the 96h maximum, and the row reclassified
   DERIVED. The realised mean moves 38.05 -> 56.07 and the share landing at or
   beyond the documented floor moves 8.0% -> 86.6%. Downstream it moved a single
   SF-7 case on B2 and nothing else, because SF-1 is a fact about *whether* money
   settles rather than *when*, and the 60-day observation horizon absorbs an
   18-hour shift.

2. **RESOLVED (A97). G9 enforced the notice *window* but not the 24-hour
   *lead*.** `after_serve_notice` started the notified window at the moment of
   service, so a debit was permitted immediately afterwards, while the RBI rule
   requires at least 24 hours. The runner's 24-hour decision cadence supplied the
   lead by accident, which is not the same as a gate enforcing it. The window now
   opens `notice_lead_hours` after service and runs `notice_window_days` from
   there. It moved no arm's numbers, and `G9_NOTICE_LEAD_NOT_ELAPSED` fires zero
   times in a real run — both expected, because the cadence and the lead are both
   24. The rule now holds because it is enforced rather than because two
   unrelated constants happen to be equal.

3. **OPEN, deliberately (A98). G1's window opens an hour before the TRAI band.**
   Contact is permitted from 08:00 IST. Reporting on the 2025 TCCCPR amendments
   describes a prohibition on commercial communication between 21:00 and 09:00.
   This pass could not extract the clause from TRAI's consolidated PDF, so it is
   flagged rather than asserted — but if it holds, INV-2's window is one hour too
   wide at the start and two hours narrower than required at the end. **The
   window is deliberately not changed.** Guessing at a regulation is worse than
   documenting that we could not verify it. Recorded in Known Limitations.

## Generator and world parameters

Source of truth: `settle/sim/generator.py`, the `PARAMS` dict. Read by the
generator, by `settle/sim/world.py` (`action_lift.*`, `p_authorise.*`,
`auth_no_settle_rate`) and by `settle/sim/debtors.py` (`reply_mix.*`,
`debtor.*`, `patience.complaint_cost`).

### Sampled parameters

Read by the generator or world to draw a value.

| parameter | value | source | date | sensitivity |
|---|---|---|---|---|
| rail_mix.card | 0.42 | ASSERTED | 2026-08-27 | pending D4 |
| rail_mix.upi_autopay | 0.45 | ASSERTED | 2026-08-27 | pending D4 |
| rail_mix.enach | 0.13 | ASSERTED | 2026-08-27 | pending D4 |
| decline_class_mix.time_shiftable | 0.46 | ASSERTED | 2026-08-27 | pending D4 |
| decline_class_mix.transient | 0.13 | ASSERTED | 2026-08-27 | pending D4 |
| decline_class_mix.dead_instrument | 0.17 | ASSERTED | 2026-08-27 | pending D4 |
| decline_class_mix.auth_abandoned | 0.11 | ASSERTED | 2026-08-27 | pending D4 |
| decline_class_mix.ambiguous | 0.1 | ASSERTED | 2026-08-27 | pending D4 |
| decline_class_mix.terminal | 0.03 | ASSERTED | 2026-08-27 | pending D4 |
| unmapped_code_rate | 0.02 | ASSERTED | 2026-08-27 | pending D4 |
| amount.median_paise | 49900 | ASSERTED | 2026-08-27 | pending D4 |
| amount.log_sigma | 0.8 | ASSERTED | 2026-08-27 | pending D4 |
| amount.min_paise | 4900 | ASSERTED | 2026-08-27 | pending D4 |
| amount.max_paise | 999900 | ASSERTED | 2026-08-27 | pending D4 |
| plan_value.prorated_rate | 0.08 | ASSERTED | 2026-08-27 | pending D4 |
| plan_value.prorated_fraction | 0.5 | ASSERTED | 2026-08-27 | pending D4 |
| tenure.mean_months | 9 | ASSERTED | 2026-08-27 | pending D4 |
| tenure.max_months | 60 | ASSERTED | 2026-08-27 | pending D4 |
| attempt_number.decay | 0.45 | ASSERTED | 2026-08-27 | pending D4 |
| attempt_number.max | 4 | DERIVED — Razorpay Subscriptions reattempts a failed charge on T+1, T+2 and T+3 and then halts the subscription; the original charge plus three reattempts is four submissions, which is the largest `attempt_number` a case can present with. https://razorpay.com/docs/payments/subscriptions/payment-retries/ | 2026-08-31 | pending D4 |
| prior_failures.mean | 0.9 | ASSERTED | 2026-08-27 | pending D4 |
| prior_failures.max | 8 | ASSERTED | 2026-08-27 | pending D4 |
| prior_recoveries.mean | 0.6 | ASSERTED | 2026-08-27 | pending D4 |
| prior_recoveries.max | 8 | ASSERTED | 2026-08-27 | pending D4 |
| consent_whatsapp_rate | 0.71 | ASSERTED | 2026-08-27 | pending D4 |
| dnd_flag_rate | 0.09 | ASSERTED | 2026-08-27 | pending D4 |
| language_mix.en | 0.27 | ASSERTED | 2026-08-27 | pending D4 |
| language_mix.hi | 0.21 | ASSERTED | 2026-08-27 | pending D4 |
| language_mix.hinglish | 0.52 | ASSERTED | 2026-08-27 | pending D4 |
| mandate_state_base.active | 0.9 | ASSERTED | 2026-08-27 | pending D4 |
| mandate_state_base.expired | 0.05 | ASSERTED | 2026-08-27 | pending D4 |
| mandate_state_base.revoked | 0.03 | ASSERTED | 2026-08-27 | pending D4 |
| mandate_state_base.none | 0.02 | ASSERTED | 2026-08-27 | pending D4 |
| mandate_state_dead.active | 0 | ASSERTED | 2026-08-27 | pending D4 |
| mandate_state_dead.expired | 0.34 | ASSERTED | 2026-08-27 | pending D4 |
| mandate_state_dead.revoked | 0.58 | ASSERTED | 2026-08-27 | pending D4 |
| mandate_state_dead.none | 0.08 | ASSERTED | 2026-08-27 | pending D4 |
| mandate_cap.known_rate | 0.64 | ASSERTED | 2026-08-27 | pending D4 |
| mandate_cap.multiple | 3 | ASSERTED | 2026-08-27 | pending D4 |
| observed_credit_day.known_rate | 0.31 | ASSERTED | 2026-08-27 | pending D4 |
| observed_credit_day.exact_rate | 0.72 | ASSERTED | 2026-08-27 | pending D4 |
| observed_credit_day.max_error_days | 3 | ASSERTED | 2026-08-27 | pending D4 |
| escalation.min_amount_paise | 74900 | ASSERTED | 2026-08-27 | pending D4 |
| escalation.min_attempt_number | 2 | ASSERTED | 2026-08-27 | pending D4 |
| intent_mix.willing_able | 0.33 | ASSERTED | 2026-08-27 | pending D4 |
| intent_mix.willing_broke | 0.38 | ASSERTED | 2026-08-27 | pending D4 |
| intent_mix.disputing | 0.07 | ASSERTED | 2026-08-27 | pending D4 |
| intent_mix.churned | 0.16 | ASSERTED | 2026-08-27 | pending D4 |
| intent_mix.adversarial | 0.06 | ASSERTED | 2026-08-27 | pending D4 |
| behaviour_mix.promise_and_break | 0.21 | ASSERTED | 2026-08-27 | pending D4 |
| behaviour_mix.dispute_stall | 0.08 | ASSERTED | 2026-08-27 | pending D4 |
| behaviour_mix.go_silent | 0.33 | ASSERTED | 2026-08-27 | pending D4 |
| behaviour_mix.opt_out_midway | 0.12 | ASSERTED | 2026-08-27 | pending D4 |
| behaviour_mix.hedged_reply | 0.2 | ASSERTED | 2026-08-27 | pending D4 |
| behaviour_mix.pay_then_complain | 0.06 | ASSERTED | 2026-08-27 | pending D4 |
| recoverability_mean.willing_able | 0.78 | ASSERTED | 2026-08-27 | pending D4 |
| recoverability_mean.willing_broke | 0.42 | ASSERTED | 2026-08-27 | pending D4 |
| recoverability_mean.disputing | 0.15 | ASSERTED | 2026-08-27 | pending D4 |
| recoverability_mean.churned | 0.06 | ASSERTED | 2026-08-27 | pending D4 |
| recoverability_mean.adversarial | 0.11 | ASSERTED | 2026-08-27 | pending D4 |
| recoverability.spread | 0.12 | ASSERTED | 2026-08-27 | pending D4 |
| patience.mean | 4 | ASSERTED | 2026-08-27 | pending D4 |
| patience.min | 1 | ASSERTED | 2026-08-27 | pending D4 |
| patience.max | 9 | ASSERTED | 2026-08-27 | pending D4 |
| payday.first_of_month_rate | 0.46 | ASSERTED | 2026-08-27 | pending D4 |
| payday.seventh_rate | 0.18 | ASSERTED | 2026-08-27 | pending D4 |
| will_settle_rate | 0.962 | ASSERTED | 2026-08-27 | pending D4 |
| settlement_lag_h.mean | 56 | DERIVED — Razorpay settles T+2 working days from capture, so 48h is the floor of the documented cycle and 96h (`settlement_lag_h_max`) its weekend-spanning maximum; 56 sits between them. Was 38 until CP11.1 (A96), which is below the floor and therefore contradicted the very source it would have cited. https://razorpay.com/docs/payments/settlements/ | 2026-08-31 | pending D4 |
| settlement_lag_h_max | 96 | DERIVED — Razorpay settles T+2 working days from capture, excluding bank holidays. A Thursday or Friday capture spans the weekend, so in a week with no public holiday the cycle's maximum is four calendar days = 96h. Public holidays can exceed it; the cap truncates that tail and the truncation is a stated limitation. https://razorpay.com/docs/payments/settlements/ | 2026-08-31 | pending D4 |
| will_reverse_rate | 0.011 | ASSERTED | 2026-08-27 | pending D4 |
| reversal_delay_days_max | 21 | ASSERTED | 2026-08-27 | pending D4 |
| response.base_mean | 0.22 | ASSERTED | 2026-08-27 | pending D4 |
| response.payday_lift | 0.38 | ASSERTED | 2026-08-27 | pending D4 |
| response.hour_lift | 0.15 | ASSERTED | 2026-08-27 | pending D4 |
| auth_no_settle_rate | 0.018 | ASSERTED | 2026-08-27 | pending D4 |
| action_lift.do_nothing | 0 | ASSERTED | 2026-08-27 | pending D4 |
| action_lift.retry | 1 | ASSERTED | 2026-08-27 | pending D4 |
| action_lift.switch_rail | 1.05 | ASSERTED | 2026-08-27 | pending D4 |
| action_lift.send_message | 0.35 | ASSERTED | 2026-08-27 | pending D4 |
| action_lift.request_mandate_update | 0.45 | ASSERTED | 2026-08-27 | pending D4 |
| action_lift.serve_notice | 0 | ASSERTED | 2026-08-27 | pending D4 |
| action_lift.escalate_human | 0.9 | ASSERTED | 2026-08-27 | pending D4 |
| action_lift.voice_call | 0.7 | ASSERTED | 2026-08-27 | pending D4 |
| reply_mix.promise_and_break.promise | 0.7 | ASSERTED | 2026-08-27 | pending D4 |
| reply_mix.promise_and_break.hedged | 0.2 | ASSERTED | 2026-08-27 | pending D4 |
| reply_mix.promise_and_break.silence | 0.1 | ASSERTED | 2026-08-27 | pending D4 |
| reply_mix.dispute_stall.dispute | 0.65 | ASSERTED | 2026-08-27 | pending D4 |
| reply_mix.dispute_stall.hedged | 0.25 | ASSERTED | 2026-08-27 | pending D4 |
| reply_mix.dispute_stall.silence | 0.1 | ASSERTED | 2026-08-27 | pending D4 |
| reply_mix.go_silent.hedged | 0.15 | ASSERTED | 2026-08-27 | pending D4 |
| reply_mix.go_silent.silence | 0.85 | ASSERTED | 2026-08-27 | pending D4 |
| reply_mix.opt_out_midway.hedged | 0.45 | ASSERTED | 2026-08-27 | pending D4 |
| reply_mix.opt_out_midway.opt_out | 0.4 | ASSERTED | 2026-08-27 | pending D4 |
| reply_mix.opt_out_midway.silence | 0.15 | ASSERTED | 2026-08-27 | pending D4 |
| reply_mix.hedged_reply.hedged | 0.8 | ASSERTED | 2026-08-27 | pending D4 |
| reply_mix.hedged_reply.silence | 0.2 | ASSERTED | 2026-08-27 | pending D4 |
| reply_mix.pay_then_complain.complaint | 0.55 | ASSERTED | 2026-08-27 | pending D4 |
| reply_mix.pay_then_complain.hedged | 0.25 | ASSERTED | 2026-08-27 | pending D4 |
| reply_mix.pay_then_complain.silence | 0.2 | ASSERTED | 2026-08-27 | pending D4 |
| debtor.disengage_after_contacts | 2 | ASSERTED | 2026-08-27 | pending D4 |
| patience.complaint_cost | 2 | ASSERTED | 2026-08-27 | pending D4 |
| p_authorise.base_floor | 0.5 | ASSERTED | 2026-08-27 | pending D4 |
| p_authorise.switch_rail_same_rail_penalty | 0.5 | ASSERTED | 2026-08-27 | pending D4 |
| p_authorise.retry_cross_rail_penalty | 0.9 | ASSERTED | 2026-08-27 | pending D4 |
| p_authorise.dnd_contact_penalty | 0.6 | ASSERTED | 2026-08-27 | pending D4. Read by `contact_response_probability` since A89; the branch in `p_authorise` that named it tested for `SendMessage`/`VoiceCall` inside a function only debits ever reached, so until CP9.1 it was a prior nothing could apply. |
| p_authorise.day_window_start_hour | 9 | ASSERTED | 2026-08-27 | pending D4 |
| p_authorise.day_window_end_hour | 20 | ASSERTED | 2026-08-27 | pending D4 |
| natural_recovery.willing_able | 0.45 | ASSERTED | 2026-08-30 | REQUIRED in the D4 sweep — B0's recovery is subtracted from every arm (§14.3) |
| natural_recovery.willing_broke | 0.18 | ASSERTED | 2026-08-30 | REQUIRED in the D4 sweep — B0's recovery is subtracted from every arm (§14.3) |
| natural_recovery.disputing | 0.05 | ASSERTED | 2026-08-30 | REQUIRED in the D4 sweep — B0's recovery is subtracted from every arm (§14.3) |
| natural_recovery.churned | 0.01 | ASSERTED | 2026-08-30 | REQUIRED in the D4 sweep — B0's recovery is subtracted from every arm (§14.3) |
| natural_recovery.adversarial | 0.03 | ASSERTED | 2026-08-30 | REQUIRED in the D4 sweep — B0's recovery is subtracted from every arm (§14.3) |
| natural_recovery.max_day | 45 | ASSERTED | 2026-08-30 | REQUIRED in the D4 sweep — B0's recovery is subtracted from every arm (§14.3) |
| mandate_update.success_rate.willing_able | 0.35 | ASSERTED | 2026-08-30 | REQUIRED in the D4 sweep — with A86 this decides how much of the 17% `dead_instrument` slice is winnable at all, and it is the highest-leverage unsourced number in the world model. Sweep it and report the range over which the conclusion survives. |
| mandate_update.success_rate.willing_broke | 0.15 | ASSERTED | 2026-08-30 | REQUIRED in the D4 sweep — with A86 this decides how much of the 17% `dead_instrument` slice is winnable at all, and it is the highest-leverage unsourced number in the world model. Sweep it and report the range over which the conclusion survives. |
| mandate_update.success_rate.disputing | 0.03 | ASSERTED | 2026-08-30 | REQUIRED in the D4 sweep — with A86 this decides how much of the 17% `dead_instrument` slice is winnable at all, and it is the highest-leverage unsourced number in the world model. Sweep it and report the range over which the conclusion survives. |
| mandate_update.success_rate.churned | 0.01 | ASSERTED | 2026-08-30 | REQUIRED in the D4 sweep — with A86 this decides how much of the 17% `dead_instrument` slice is winnable at all, and it is the highest-leverage unsourced number in the world model. Sweep it and report the range over which the conclusion survives. |
| mandate_update.success_rate.adversarial | 0.02 | ASSERTED | 2026-08-30 | REQUIRED in the D4 sweep — with A86 this decides how much of the 17% `dead_instrument` slice is winnable at all, and it is the highest-leverage unsourced number in the world model. Sweep it and report the range over which the conclusion survives. |
| mandate_update.response_delay_h_max | 72 | ASSERTED | 2026-08-30 | REQUIRED in the D4 sweep — the wait is what makes a mandate update a decision rather than a coin flip, and it competes with the 30-day decision horizon. |
| contact_response.rate.willing_able | 0.2 | ASSERTED | 2026-08-30 | REQUIRED in the D4 sweep — A89 makes this the number that decides whether contacting anyone is viable at all, and therefore whether the contact-restraint result is a finding or an artefact. Sweep it and report the range over which the conclusion survives. |
| contact_response.rate.willing_broke | 0.08 | ASSERTED | 2026-08-30 | REQUIRED in the D4 sweep — A89 makes this the number that decides whether contacting anyone is viable at all, and therefore whether the contact-restraint result is a finding or an artefact. Sweep it and report the range over which the conclusion survives. |
| contact_response.rate.disputing | 0.02 | ASSERTED | 2026-08-30 | REQUIRED in the D4 sweep — A89 makes this the number that decides whether contacting anyone is viable at all, and therefore whether the contact-restraint result is a finding or an artefact. Sweep it and report the range over which the conclusion survives. |
| contact_response.rate.churned | 0.005 | ASSERTED | 2026-08-30 | REQUIRED in the D4 sweep — A89 makes this the number that decides whether contacting anyone is viable at all, and therefore whether the contact-restraint result is a finding or an artefact. Sweep it and report the range over which the conclusion survives. |
| contact_response.rate.adversarial | 0.02 | ASSERTED | 2026-08-30 | REQUIRED in the D4 sweep — A89 makes this the number that decides whether contacting anyone is viable at all, and therefore whether the contact-restraint result is a finding or an artefact. Sweep it and report the range over which the conclusion survives. |
| contact_response.behaviour_multiplier.promise_and_break | 0.5 | ASSERTED | 2026-08-30 | REQUIRED in the D4 sweep — §8's debtors modulate A89's response rate, so this sets how much of the batch is reachable by a message. |
| contact_response.behaviour_multiplier.dispute_stall | 0.2 | ASSERTED | 2026-08-30 | REQUIRED in the D4 sweep — §8's debtors modulate A89's response rate, so this sets how much of the batch is reachable by a message. |
| contact_response.behaviour_multiplier.go_silent | 0.05 | ASSERTED | 2026-08-30 | REQUIRED in the D4 sweep — §8's debtors modulate A89's response rate, so this sets how much of the batch is reachable by a message. |
| contact_response.behaviour_multiplier.opt_out_midway | 0.4 | ASSERTED | 2026-08-30 | REQUIRED in the D4 sweep — §8's debtors modulate A89's response rate, so this sets how much of the batch is reachable by a message. |
| contact_response.behaviour_multiplier.hedged_reply | 0.6 | ASSERTED | 2026-08-30 | REQUIRED in the D4 sweep — §8's debtors modulate A89's response rate, so this sets how much of the batch is reachable by a message. |
| contact_response.behaviour_multiplier.pay_then_complain | 1.3 | ASSERTED | 2026-08-30 | REQUIRED in the D4 sweep — §8's debtors modulate A89's response rate, so this sets how much of the batch is reachable by a message. |
| contact_response.delay_h_max | 96 | ASSERTED | 2026-08-30 | REQUIRED in the D4 sweep — the wait is what makes a contact a decision rather than a coin flip, and a response due past the 30-day decision horizon still lands. |
| world.liquidity_window_days | 1 | ASSERTED | 2026-08-27 | REQUIRED in the D4 sweep — highest-leverage parameter in the world model. It determines how often a time_shiftable retry lands inside the liquidity window, which is the mechanism the retry-timing result depends on. Must be swept in D4 sensitivity, and the range over which the conclusion survives reported explicitly. |

### Asserted targets

Not sampled. Checked against a realised distribution by a test. Recorded here
because they are numbers that appear in reported output, but nothing reads them
to make a decision.

| parameter | value | source | date | sensitivity |
|---|---|---|---|---|
| escalation.target_overall_rate | 0.15 | ASSERTED | 2026-08-27 | pending D4 |

## Observability parameters

SPEC §6. Source of truth: `settle/sim/observability.py`, `ObservabilityConfig`
field defaults. All five default to non-zero on purpose — a run with them at
zero is `--perfect-observability`, which measures what unreliable *reporting*
costs. It does not make the world perfect: `auth_no_settle_rate`,
`settlement_lag_h.mean` and `will_reverse_rate` are world parameters and sit in
the section above, out of the flag's reach, so SF-1 and SF-7 remain producible.

| parameter | value | source | date | sensitivity |
|---|---|---|---|---|
| observability.webhook_drop_rate | 0.021 | ASSERTED | 2026-08-27 | pending D4 |
| observability.webhook_duplicate_rate | 0.037 | ASSERTED | 2026-08-27 | pending D4 |
| observability.out_of_order_rate | 0.014 | ASSERTED | 2026-08-27 | pending D4 |
| observability.settlement_lag_reporting | 6 | ASSERTED | 2026-08-27 | pending D4 |
| observability.reversal_reporting_delay | 18 | ASSERTED | 2026-08-27 | pending D4 |

## Cost and policy parameters

Consumed by the policy rather than by the world.

### Policy constants

Our configuration, not assumptions about the world. These are choices we made
and must defend, distinct from sampled parameters which are claims about
reality. Source of truth: `settle/policy/params.py`, the `POLICY_PARAMS` dict.
PAR-1 checks this table against it in both directions.

| parameter | value | source | date | sensitivity |
|---|---|---|---|---|
| card_network_retry_cap | 4 | ASSERTED — bounded by, not derived from, the nearest public figure: a PSP's restatement of Visa's cap at 15 reattempts of an already-declined transaction per 30 days, with no Visa rule reference. 4 is strictly inside it. https://support.nets.eu/article/visa-fines-on-attempts-to-complete-already-declined-transactions | 2026-08-31 | pending D4 |
| attempt_budget | 6 | ASSERTED | 2026-08-29 | pending D4 |
| contact_budget | 5 | ASSERTED | 2026-08-29 | pending D4 |
| frequency_cap_per_window | 3 | ASSERTED | 2026-08-29 | pending D4 |
| frequency_window_hours | 168 | ASSERTED | 2026-08-29 | pending D4 |
| min_contact_gap_hours | 20 | ASSERTED | 2026-08-29 | pending D4 |
| notice_lead_hours | 24 | SOURCED — RBI, Digital Payments — E-mandate Framework, 2026, RBI/DPSS/2026-27/396, 21 Apr 2026: "An issuer shall send a pre-transaction notification to the customer, at least 24 hours prior to the actual charge / debit." Same requirement in RBI/2019-20/47 of 21 Aug 2019. https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=13374 | 2026-08-31 | G9 blocks a debit inside the lead (A97). Not swept: it is a regulatory minimum, and a sweep of it would be a sweep of how far we are willing to break the rule. |
| notice_window_days | 3 | ASSERTED — the RBI e-mandate framework fixes a 24-hour pre-debit *lead time*, not how long a served notice stays valid for later debits. The lead is sourced; this window is not. https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=13374 | 2026-08-31 | pending D4 |
| decision_cadence_hours | 24 | ASSERTED | 2026-08-29 | pending D4 |
| action_cost.do_nothing | 0 | ASSERTED | 2026-08-30 | pending D4 |
| action_cost.retry | 5 | ASSERTED | 2026-08-30 | pending D4 |
| action_cost.switch_rail | 5 | ASSERTED | 2026-08-30 | pending D4 |
| action_cost.send_message.sms | 15 | ASSERTED | 2026-08-30 | pending D4 |
| action_cost.send_message.whatsapp | 35 | ASSERTED | 2026-08-30 | pending D4 |
| action_cost.request_mandate_update.sms | 15 | ASSERTED | 2026-08-30 | pending D4 |
| action_cost.request_mandate_update.whatsapp | 35 | ASSERTED | 2026-08-30 | pending D4 |
| action_cost.serve_notice.sms | 15 | ASSERTED | 2026-08-30 | pending D4 |
| action_cost.serve_notice.whatsapp | 35 | ASSERTED | 2026-08-30 | pending D4 |
| action_cost.voice_call | 400 | ASSERTED | 2026-08-30 | pending D4 |
| action_cost.escalate_human | 5000 | ASSERTED | 2026-08-30 | pending D4 |
| p_opt_out.do_nothing | 0 | ASSERTED | 2026-08-30 | pending D4 |
| p_opt_out.retry | 0 | ASSERTED | 2026-08-30 | pending D4 |
| p_opt_out.switch_rail | 0 | ASSERTED | 2026-08-30 | pending D4 |
| p_opt_out.send_message.sms | 0.004 | ASSERTED | 2026-08-30 | pending D4 |
| p_opt_out.send_message.whatsapp | 0.006 | ASSERTED | 2026-08-30 | pending D4 |
| p_opt_out.request_mandate_update.sms | 0.004 | ASSERTED | 2026-08-30 | pending D4 |
| p_opt_out.request_mandate_update.whatsapp | 0.006 | ASSERTED | 2026-08-30 | pending D4 |
| p_opt_out.serve_notice.sms | 0.003 | ASSERTED | 2026-08-30 | pending D4 |
| p_opt_out.serve_notice.whatsapp | 0.004 | ASSERTED | 2026-08-30 | pending D4 |
| p_opt_out.voice_call | 0.031 | ASSERTED | 2026-08-30 | pending D4 |
| p_opt_out.escalate_human | 0.018 | ASSERTED | 2026-08-30 | pending D4 |
| ltv_months | 8 | ASSERTED | 2026-08-30 | pending D4 |
| economic_stop_multiple | 3 | ASSERTED | 2026-08-30 | REQUIRED in the D4 sweep — S7 refuses any action whose expected recovery is below this multiple of its cost. Measured at CP9: it declined a mandate-update campaign returning 1.69x on the project's own priced cost, so this constant, not the estimator, is what decides the restraint result at the margin. |
| liquidity_window_days_belief | 1 | ASSERTED | 2026-08-30 | pending D4 |
| action_grid.offset_now | 0 | ASSERTED | 2026-08-29 | pending D4 |
| action_grid.offset_later_today | 6 | ASSERTED | 2026-08-29 | pending D4 |
| action_grid.offset_next_morning | 18 | ASSERTED | 2026-08-29 | pending D4 |
| action_grid.offset_next_evening | 30 | ASSERTED | 2026-08-29 | pending D4 |
| action_grid.offset_two_days | 48 | ASSERTED | 2026-08-29 | pending D4 |
| action_grid.offset_three_days | 72 | ASSERTED | 2026-08-29 | pending D4 |
| action_grid.offset_five_days | 120 | ASSERTED | 2026-08-29 | pending D4 |
| action_grid.offset_one_week | 168 | ASSERTED | 2026-08-29 | pending D4 |
| action_grid.max_horizon_h | 720 | ASSERTED | 2026-08-29 | pending D4 |
| class_retry_cap.time_shiftable | 4 | ASSERTED | 2026-08-29 | pending D4 |
| class_retry_cap.transient | 3 | ASSERTED — Razorpay's three automatic reattempts are class-blind, so the match with this one class is a coincidence and not a derivation. | 2026-08-31 | pending D4 |
| class_retry_cap.dead_instrument | 2 | ASSERTED | 2026-08-30 | REQUIRED in the D4 sweep — A86 gives a re-authorised mandate a debit budget, and this caps it, so it bounds how much of the `dead_instrument` slice is recoverable. |
| class_retry_cap.auth_abandoned | 0 | ASSERTED | 2026-08-29 | pending D4 |
| class_retry_cap.ambiguous | 1 | ASSERTED | 2026-08-29 | pending D4 |
| class_retry_cap.terminal | 0 | ASSERTED | 2026-08-29 | pending D4 |

### Model selection constants

Not priors about the world and not policy configuration — thresholds on how a
model is chosen. They still move every number in §14.4, because they decide
which model ships, which is the test §15 states for whether a number belongs
here. Source of truth: `settle/agent/estimator.py`.

| parameter | value | source | date | sensitivity |
|---|---|---|---|---|
| MAX_FLAT_DECISION_RATE | 0.05 | ASSERTED | 2026-08-30 | REQUIRED in the D4 sweep — the share of multi-option decisions a scorer may return one number for and still be selectable (A92). At CP10 it rejected GBM+isotonic at 11.5% flat and admitted the uncalibrated GBM at 0.0%; anything between those two leaves the outcome unchanged, and the sweep should report where it stops doing so. |

### Cost and opt-out parameters

Superseded at CP8. §20's cost table and the `p_opt_out` priors now live in
`POLICY_PARAMS` above, where PAR-1 checks them against the code in both
directions. They sat here as blank rows for four checkpoints, which is how a
number stays unsourced without anyone noticing.
