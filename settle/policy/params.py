"""Policy constants. SPEC §12, §13, INV-10.

Our configuration, not assumptions about the world. `settle.sim.generator.PARAMS`
holds claims about reality that could in principle be checked against published
data; these are choices we made and must defend. The distinction matters when a
judge asks where a number came from, so PRIORS.md keeps them in their own table.

They are still numbers that move reported metrics, which is what INV-10 actually
covers (SPEC §15). `attempt_budget` and `contact_budget` bound B3's violation
count; `frequency_cap_per_window` bounds contacts-per-case directly. PAR-1
enforces the correspondence with PRIORS.md in both directions.
"""

from typing import Final

from settle.schema.enums import ActionType, Channel, DeclineClass

POLICY_PARAMS: Final[dict[str, float]] = {
    # G4 — card network rules cap retries on a declined credential.
    "card_network_retry_cap": 4,
    # S3 — the budgets that bound an unguarded arm.
    "attempt_budget": 6,
    "contact_budget": 5,
    # G2 — SPEC §12.
    "frequency_cap_per_window": 3,
    "frequency_window_hours": 168,
    "min_contact_gap_hours": 20,
    # G9 — SPEC §12, the notified debit window.
    "notice_window_days": 3,
    # The runner's cadence when it has chosen to do nothing and no timer is
    # pending. It sets how many decisions an arm gets across the horizon, and
    # therefore contacts per case, which is a §14.4 headline.
    "decision_cadence_hours": 24,
    # G10 — per-class retry budget, distinct from G4's card-network cap.
    # `ambiguous` is 1 because §9 permits one retry at a different hour and then
    # a message; without the cap those retries run until G4 or S3 fires, which
    # is a different rule than the one §9 states.
    "class_retry_cap.time_shiftable": 4,
    "class_retry_cap.transient": 3,
    # A86. Zero until CP9, which was correct while `dead_instrument` could never
    # offer a retry at all — and which then silently blocked the one path the
    # class has. A re-authorised mandate is a fresh instrument, so it gets a
    # budget; two rather than `transient`'s three, because the customer has just
    # done something for us and burning their new card on repeated declines is
    # the wrong way to thank them. `legal_actions` and G3 both already require
    # the mandate to be ACTIVE, so this cap can only ever bite after a revival.
    "class_retry_cap.dead_instrument": 2,
    "class_retry_cap.auth_abandoned": 0,
    "class_retry_cap.ambiguous": 1,
    "class_retry_cap.terminal": 0,
    # --- A71, the action grid -------------------------------------------
    # Actions carry parameters, so the space has to be finite and declared.
    # Eight offsets, chosen to span two dimensions the policy actually needs:
    #
    #   a different hour of the same day   0, 6
    #   a different hour of another day    18, 30   (30 = 24 + 6)
    #   the same hour some days later      48, 72, 120, 168
    #
    # §9 asks `ambiguous` for "one retry at a different hour", which needs an
    # offset that is not a multiple of 24. Reaching a liquidity window needs
    # multi-day offsets. Eight is the largest grid that still leaves EXPLORE
    # able to cover every cell at 30k cases — see EXP-6.
    "action_grid.offset_now": 0,
    "action_grid.offset_later_today": 6,
    "action_grid.offset_next_morning": 18,
    "action_grid.offset_next_evening": 30,
    "action_grid.offset_two_days": 48,
    "action_grid.offset_three_days": 72,
    "action_grid.offset_five_days": 120,
    "action_grid.offset_one_week": 168,
    # Nothing may be scheduled past the horizon the agent stops acting at.
    "action_grid.max_horizon_h": 720,
    # --- §20 cost model, keyed on (ActionType, Channel|null) as A36 requires ---
    # These were a table in SPEC and nowhere in code until the policy needed
    # them. All ASSERTED pending D4.
    "action_cost.do_nothing": 0,
    "action_cost.retry": 5,
    "action_cost.switch_rail": 5,
    "action_cost.send_message.sms": 15,
    "action_cost.send_message.whatsapp": 35,
    "action_cost.request_mandate_update.sms": 15,
    "action_cost.request_mandate_update.whatsapp": 35,
    "action_cost.serve_notice.sms": 15,
    "action_cost.serve_notice.whatsapp": 35,
    "action_cost.voice_call": 400,
    "action_cost.escalate_human": 5000,
    # P(opt_out | action). A26: opt-out cost is DERIVED per action from this and
    # LTV, never from nuisance units multiplied by a tuned constant.
    "p_opt_out.do_nothing": 0.0,
    "p_opt_out.retry": 0.0,
    "p_opt_out.switch_rail": 0.0,
    "p_opt_out.send_message.sms": 0.004,
    "p_opt_out.send_message.whatsapp": 0.006,
    "p_opt_out.request_mandate_update.sms": 0.004,
    "p_opt_out.request_mandate_update.whatsapp": 0.006,
    "p_opt_out.serve_notice.sms": 0.003,
    "p_opt_out.serve_notice.whatsapp": 0.004,
    "p_opt_out.voice_call": 0.031,
    "p_opt_out.escalate_human": 0.018,
    # LTV = plan_value_paise x ltv_months (A26).
    "ltv_months": 8,
    # S7 — §13: expected recovery below this multiple of cost is not worth taking.
    "economic_stop_multiple": 3,
    # The agent's BELIEF about how close to a salary credit still counts as
    # liquid. Deliberately not `world.liquidity_window_days` (A85): a policy
    # handed the simulator's own parameter would demonstrate that we can read
    # our own generator, not that a merchant could learn the effect.
    "liquidity_window_days_belief": 1,
}

_GRID_PREFIX = "action_grid.offset_"


def hour_offsets() -> tuple[int, ...]:
    """The declared offsets, sorted. A71.

    Derived from POLICY_PARAMS rather than written out a second time, so the
    grid EXPLORE samples and the grid OURS searches cannot drift apart. That
    drift is the failure A71 exists to prevent: an estimator trained on one grid
    and queried on another has zero coverage exactly where it is asked to
    predict.
    """
    return tuple(
        sorted(int(value) for key, value in POLICY_PARAMS.items() if key.startswith(_GRID_PREFIX))
    )


def max_horizon_h() -> int:
    """Nothing may be scheduled past the decision horizon."""
    return int(POLICY_PARAMS["action_grid.max_horizon_h"])


def class_retry_cap(decline_class: DeclineClass) -> int:
    """G10's cap for a class."""
    return int(POLICY_PARAMS[f"class_retry_cap.{decline_class.value}"])


def action_cost_paise(action_type: ActionType, channel: Channel | None = None) -> int:
    """§20's cost table, keyed on (ActionType, Channel|null)."""
    if channel is not None:
        keyed = f"action_cost.{action_type.value}.{channel.value}"
        if keyed in POLICY_PARAMS:
            return int(POLICY_PARAMS[keyed])
    bare = f"action_cost.{action_type.value}"
    if bare in POLICY_PARAMS:
        return int(POLICY_PARAMS[bare])
    # Channel-keyed verb asked about without a channel. Fall back to the
    # cheapest variant rather than raising mid-run: a missing price must not
    # take down a batch, and understating it cannot flatter the policy.
    prefix = f"{bare}."
    return min(int(v) for k, v in POLICY_PARAMS.items() if k.startswith(prefix))


def p_opt_out(action_type: ActionType, channel: Channel | None = None) -> float:
    """P(opt_out | action). A26 — stated per action, never derived from a
    nuisance-unit multiplier, which would smuggle in an empirical claim as a
    unit conversion."""
    if channel is not None:
        keyed = f"p_opt_out.{action_type.value}.{channel.value}"
        if keyed in POLICY_PARAMS:
            return float(POLICY_PARAMS[keyed])
    bare = f"p_opt_out.{action_type.value}"
    if bare in POLICY_PARAMS:
        return float(POLICY_PARAMS[bare])
    # The *highest* variant when the channel is unknown: understating opt-out
    # risk is the error that makes a policy over-contact.
    prefix = f"{bare}."
    return max(float(v) for k, v in POLICY_PARAMS.items() if k.startswith(prefix))


def opt_out_cost_paise(
    action_type: ActionType, plan_value_paise: int, channel: Channel | None = None
) -> float:
    """A26: `P(opt_out | action) x LTV`, and LTV is `plan_value x ltv_months`."""
    return p_opt_out(action_type, channel) * plan_value_paise * POLICY_PARAMS["ltv_months"]
