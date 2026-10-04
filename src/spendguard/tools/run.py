"""run_query_bounded tool: estimate -> budget gate -> execute -> reconcile.

The full spend-governor loop in one tool call:
1. Pre-flight estimate (calibrated from the ledger).
2. Budget policy check (caps, anomaly, confirm threshold).
3. Execute with row cap; never fetches more than max_rows to enforce it.
4. Reconcile: fetch the actual billed cost, record it, fold it into
   calibration, and add it to the session spend.
"""

from __future__ import annotations

from spendguard.budgets import Decision, Policy, consume_confirmation, request_confirmation
from spendguard.engines import get_engine
from spendguard.ledger import apply_calibration, record_actual, record_estimate

# One policy per server process = one agent session.
_policy = Policy()


def run_query_bounded(
    engine: str,
    sql: str,
    max_rows: int = 1000,
    max_estimated_cost_usd: float | None = None,
    warehouse: str | None = None,
    confirmation_token: str | None = None,
) -> dict:
    """Run SQL only if it passes every guardrail; reconcile actuals after."""
    eng = get_engine(engine)
    estimate = eng.estimate(sql, warehouse=warehouse)
    apply_calibration(estimate)
    estimate_id = record_estimate(estimate)

    # Caller-supplied hard cap beats everything.
    if (
        max_estimated_cost_usd is not None
        and estimate.estimated_cost_usd is not None
        and estimate.estimated_cost_usd > max_estimated_cost_usd
    ):
        return {
            "status": "refused",
            "reason": "over_call_cap",
            "detail": (
                f"Estimated ${estimate.estimated_cost_usd:.4f} exceeds your "
                f"per-call cap ${max_estimated_cost_usd:.4f}."
            ),
            "estimate": estimate.to_dict(),
        }

    check = _policy.check(estimate)

    if check.decision == Decision.NEEDS_CONFIRMATION:
        if not confirmation_token or not consume_confirmation(
            confirmation_token, estimate
        ):
            token = request_confirmation(estimate)
            return {
                "status": "needs_confirmation",
                "reason": "confirm_above_threshold",
                "detail": " ".join(check.reasons),
                "confirmation_token": token,
                "confirmation_token_ttl_seconds": 300,
                "instruction": (
                    "Show the estimate and reasons to the human. If they approve, "
                    "call run_query_bounded again with confirmation_token set."
                ),
                "estimate": estimate.to_dict(),
            }

    if check.decision in (Decision.OVER_CAP, Decision.ANOMALY):
        return {
            "status": "refused",
            "reason": check.decision.value,
            "detail": " ".join(check.reasons),
            "estimate": estimate.to_dict(),
            "suggestion": (
                "Call suggest_cheaper_query with this SQL for a concrete "
                "cheaper rewrite, or raise the relevant cap in "
                "~/.spendguard/config.toml."
            ),
        }

    rows, query_id = eng.execute(sql, max_rows=max_rows)

    # Reconcile: fetch actual billed cost and train calibration.
    actual_usd: float | None = None
    try:
        actual = eng.fetch_actual_cost(query_id)
        record_actual(
            estimate_id, query_id, actual.billed_bytes, actual.billed_cost_usd
        )
        actual_usd = actual.billed_cost_usd
        if actual_usd:
            _policy.record_spend(actual_usd)
    except NotImplementedError as exc:
        # Engine hasn't wired live reconciliation yet: honest, not silent.
        reconcile_note = str(exc)
    else:
        reconcile_note = "reconciled"

    return {
        "status": "ok",
        "rows": rows,
        "row_count": len(rows),
        "truncated": len(rows) == max_rows,
        "query_id": query_id,
        "estimate": estimate.to_dict(),
        "actual_cost_usd": actual_usd,
        "reconciliation": reconcile_note,
    }
