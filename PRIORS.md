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
| attempt_number.max | 4 | ASSERTED | 2026-08-27 | pending D4 |
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
| settlement_lag_h.mean | 38 | ASSERTED | 2026-08-27 | pending D4 |
| settlement_lag_h_max | 96 | ASSERTED | 2026-08-27 | pending D4 |
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
| p_authorise.dnd_contact_penalty | 0.6 | ASSERTED | 2026-08-27 | pending D4 |
| p_authorise.day_window_start_hour | 9 | ASSERTED | 2026-08-27 | pending D4 |
| p_authorise.day_window_end_hour | 20 | ASSERTED | 2026-08-27 | pending D4 |

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

SPEC §20. Not generator parameters — these price actions and derive opt-out
cost, and they are consumed by the policy rather than by the world.

| parameter | value | source | date | sensitivity |
|---|---|---|---|---|
| ltv_months |  | ASSERTED | pending D4 |  |
| p_opt_out_send_message_sms |  | ASSERTED | pending D4 |  |
| p_opt_out_send_message_whatsapp |  | ASSERTED | pending D4 |  |
| p_opt_out_request_mandate_update_sms |  | ASSERTED | pending D4 |  |
| p_opt_out_request_mandate_update_whatsapp |  | ASSERTED | pending D4 |  |
| p_opt_out_serve_notice_sms |  | ASSERTED | pending D4 |  |
| p_opt_out_voice_call |  | ASSERTED | pending D4 |  |
| p_opt_out_escalate_human |  | ASSERTED | pending D4 |  |
