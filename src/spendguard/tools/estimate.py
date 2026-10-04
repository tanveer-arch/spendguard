"""estimate_query_cost tool: pre-flight estimate + ledger record + calibration."""

from __future__ import annotations

from spendguard.engines import get_engine
from spendguard.ledger import apply_calibration, record_estimate


def estimate_query_cost(engine: str, sql: str, warehouse: str | None = None) -> dict:
    """Estimate cost, apply ledger calibration, record, return a JSON dict."""
    eng = get_engine(engine)
    estimate = eng.estimate(sql, warehouse=warehouse)
    apply_calibration(estimate)
    estimate_id = record_estimate(estimate)
    payload = estimate.to_dict()
    payload["estimate_id"] = estimate_id
    return payload
