"""spendguard MCP server: a spend governor for agents running warehouse SQL.

Tools:
- describe_engine_capabilities: what each engine can/can't tell you, honestly.
- estimate_query_cost: pre-flight dollar estimate, calibrated from the ledger.
- run_query_bounded: estimate -> budget gate -> execute -> reconcile actuals.
- spend_report: reconciled spend per engine, plus calibration state.
- set_budget: persist a spend cap (day / session).
- suggest_cheaper_query: concrete cheaper rewrites for expensive-looking SQL.

Run with: spendguard   (entry point -> main -> stdio server)
Requires the `mcp` package; engines additionally need their cloud SDKs and
credentials (see examples/.mcp.json.example).
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from spendguard.budgets import save_budget
from spendguard.engines import ENGINES
from spendguard.models import AccuracyTier
from spendguard.tools import estimate as estimate_tool
from spendguard.tools import report as report_tool
from spendguard.tools import rewrite as rewrite_tool
from spendguard.tools import run as run_tool

mcp = FastMCP("spendguard")

_CAPABILITIES: dict[str, dict] = {
    "bigquery": {
        "accuracy_tier": AccuracyTier.PRECISE.value,
        "cost_signal": "Dry run returns the exact bytes the query would scan; "
        "converted at the on-demand rate ($6.25/TiB).",
        "supports_bytes": True,
        "supports_dollars": True,
        "caveats": [
            "Row-level-security masked tables report 0 bytes by design — "
            "$0.00 is never proof a query is free.",
            "Remote functions / BigQuery ML remote inference bill separately "
            "and are excluded (flagged by text heuristic).",
            "Capacity (Editions) billing: bytes only, no dollar figure.",
        ],
    },
    "snowflake": {
        "accuracy_tier": AccuracyTier.UPPER_BOUND.value,
        "cost_signal": "EXPLAIN USING JSON optimizer plan; the largest byte "
        "figure in the plan is taken as an upper bound. Dollars assume one "
        "60s minimum billing window at the warehouse credit rate.",
        "supports_bytes": True,
        "supports_dollars": True,
        "caveats": [
            "No pre-flight byte count exists on Snowflake — this is a bound, "
            "not an exact figure.",
            "60-second minimum billing per warehouse start is assumed; "
            "longer queries cost more.",
            "$/credit depends on edition/region (default $2.0, configurable).",
        ],
    },
    "databricks": {
        "accuracy_tier": AccuracyTier.HEURISTIC.value,
        "cost_signal": "No dry-run API exists. Estimated from SQL warehouse "
        "size (DBU/hr) x plan-shape runtime heuristic x $/DBU, then "
        "calibrated against system.billing.usage actuals over time.",
        "supports_bytes": False,
        "supports_dollars": True,
        "caveats": [
            "Directional, not exact — the first estimates on a new workspace "
            "are rough until the ledger calibrates.",
            "Includes Databricks' 1-minute minimum billing.",
        ],
    },
}


@mcp.tool()
def describe_engine_capabilities(engine: str) -> dict:
    """What cost signals an engine supports and how much to trust them.

    Call this first: it bakes the honesty contract in before any estimate.
    """
    name = engine.strip().lower()
    if name not in _CAPABILITIES:
        return {"error": f"unknown engine {engine!r}", "engines": sorted(ENGINES)}
    return {"engine": name, **_CAPABILITIES[name]}


@mcp.tool()
def estimate_query_cost(engine: str, sql: str, warehouse: str | None = None) -> dict:
    """Pre-flight dollar estimate for SQL. Free, never executes the query."""
    return estimate_tool.estimate_query_cost(engine, sql, warehouse)


@mcp.tool()
def run_query_bounded(
    engine: str,
    sql: str,
    max_rows: int = 1000,
    max_estimated_cost_usd: float | None = None,
    warehouse: str | None = None,
    confirmation_token: str | None = None,
) -> dict:
    """Run SQL only if it passes every guardrail; reconcile actuals after.

    Flow: estimate -> budget gate (caps / anomaly / human confirm) -> execute
    with row cap -> fetch actual billed cost -> train ledger calibration.
    If a human confirmation is required, the response includes a single-use
    token (5-min TTL): show the estimate to the human, then call again with
    confirmation_token set.
    """
    return run_tool.run_query_bounded(
        engine,
        sql,
        max_rows=max_rows,
        max_estimated_cost_usd=max_estimated_cost_usd,
        warehouse=warehouse,
        confirmation_token=confirmation_token,
    )


@mcp.tool()
def spend_report(period_days: int = 1) -> dict:
    """Reconciled spend per engine over the period, plus calibration state."""
    return report_tool.spend_report(period_days)


@mcp.tool()
def set_budget(name: str, scope: str, max_cost_usd: float) -> dict:
    """Persist a spend cap. name: daily | session (scope: day | session).

    Examples: set_budget("daily", "day", 50.0), set_budget("session", "session", 10.0).
    """
    return save_budget(name, scope, max_cost_usd)


@mcp.tool()
def suggest_cheaper_query(engine: str, sql: str) -> list[dict]:
    """Concrete cheaper rewrites for expensive-looking SQL (LIMIT injection,
    partition-filter templates, SELECT * guidance, cross-join flags)."""
    return rewrite_tool.suggest_cheaper_query(engine, sql)


def main() -> None:
    """Entry point: run the stdio MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
