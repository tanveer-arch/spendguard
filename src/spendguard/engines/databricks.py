"""Databricks engine — HEURISTIC estimates.

Databricks exposes no dry-run API, so nobody can give you an exact pre-flight
cost — including us. What we do instead, honestly labeled HEURISTIC:

1. Take the SQL warehouse size (X-Small … 4X-Large) and tier → DBU burn rate.
2. Read the Spark plan (EXPLAIN) and derive a rough runtime from its shape:
   each Exchange (shuffle) / Sort / Join node adds expected seconds. These
   weights are deliberately crude — the ledger's calibration factor is what
   makes the numbers converge on reality over time.
3. Cost = DBU rate x max(runtime, 1-minute minimum billing) x $/DBU.

Every Estimate from this engine carries accuracy_tier=HEURISTIC and a caveat
saying exactly this. Actuals come from system.billing.usage after execution,
which is what trains the calibration.
"""

from __future__ import annotations

import hashlib
import os
import re

from spendguard.engines.base import Engine, MissingCredentialsError
from spendguard.models import AccuracyTier, ActualCost, Estimate
from spendguard.pricing import databricks as dbx_pricing

#: Crude per-node expected seconds in the Spark plan. These are order-of-
#: magnitude weights, not measurements — calibration corrects them per account.
PLAN_NODE_SECONDS: dict[str, float] = {
    "exchange": 20.0,  # shuffle
    "sort": 10.0,
    "join": 15.0,
    "aggregate": 8.0,
    "scan": 5.0,
}


def _sql_hash(sql: str) -> str:
    return hashlib.sha256(sql.encode()).hexdigest()[:16]


def complexity_seconds(plan_text: str) -> float:
    """Rough expected runtime in seconds from a Spark EXPLAIN plan.

    Pure text heuristic — counts plan node types and applies the weights in
    PLAN_NODE_SECONDS. Deterministic and offline-testable by design.
    """
    total = 0.0
    lowered = plan_text.lower()
    for node, weight in PLAN_NODE_SECONDS.items():
        total += len(re.findall(rf"\b{re.escape(node)}\b", lowered)) * weight
    return max(total, 5.0)  # never predict less than 5s of warehouse time


class DatabricksEngine(Engine):
    name = "databricks"

    def _connect(self):
        """Open a Databricks SQL connection, or raise an actionable error."""
        try:
            from databricks import sql as dbsql
        except ImportError as exc:
            raise MissingCredentialsError(
                "databricks-sql-connector is not installed. "
                "Run: pip install spendguard (or pip install databricks-sql-connector)."
            ) from exc
        host = os.environ.get("DATABRICKS_SERVER_HOSTNAME")
        path = os.environ.get("DATABRICKS_HTTP_PATH")
        token = os.environ.get("DATABRICKS_TOKEN")
        if not (host and path and token):
            raise MissingCredentialsError(
                "Databricks credentials not found. Set DATABRICKS_SERVER_HOSTNAME "
                "(your-workspace.cloud.databricks.com), DATABRICKS_HTTP_PATH "
                "(/sql/1.0/warehouses/<id>), and DATABRICKS_TOKEN."
            )
        return dbsql.connect(
            server_hostname=host, http_path=path, access_token=token
        )

    def _warehouse_size(self) -> tuple[str, str]:
        size = os.environ.get("DATABRICKS_WAREHOUSE_SIZE", "X-SMALL")
        tier = os.environ.get("DATABRICKS_TIER", "standard")
        return size, tier

    def estimate(self, sql: str, warehouse: str | None = None) -> Estimate:
        """EXPLAIN the query, weigh the plan, convert to dollars heuristically."""
        conn = self._connect()
        size, tier = self._warehouse_size()
        if warehouse:
            size = warehouse
        try:
            cur = conn.cursor()
            try:
                cur.execute("EXPLAIN " + sql)
                plan_text = "\n".join(str(r[0]) for r in cur.fetchall())
            finally:
                cur.close()
        finally:
            conn.close()

        seconds = complexity_seconds(plan_text)
        usd = dbx_pricing.runtime_cost_usd(size, seconds / 60.0, tier=tier)
        return Estimate(
            engine=self.name,
            accuracy_tier=AccuracyTier.HEURISTIC,
            estimated_bytes=None,  # Databricks exposes no pre-flight byte figure
            estimated_cost_usd=usd,
            caveats=[
                "HEURISTIC estimate: Databricks has no dry-run API. Derived from "
                f"warehouse size ({size}, {tier} tier) and Spark plan shape; "
                "directional, not exact. Improves as the ledger calibrates.",
                "Includes Databricks' 1-minute minimum billing per warehouse start.",
            ],
            sql_hash=_sql_hash(sql),
        )

    def execute(self, sql: str, max_rows: int = 1000, **kwargs) -> tuple[list[dict], str]:
        """Run the query and return (rows capped at max_rows, statement_id)."""
        conn = self._connect()
        try:
            cur = conn.cursor()
            try:
                cur.execute(sql)
                cols = [d[0] for d in cur.description or []]
                rows = [dict(zip(cols, r)) for r in cur.fetchmany(max_rows)]
                # TODO (live): the statement ID for reconciliation lives on the
                # Statement Execution API response, not the thrift cursor. When
                # wiring live, execute via databricks.sdk.service.sql and keep
                # response.statement_id for fetch_actual_cost.
                query_id = getattr(cur, "statement_id", None) or "unknown"
                return rows, query_id
            finally:
                cur.close()
        finally:
            conn.close()

    def fetch_actual_cost(self, query_id: str) -> ActualCost:
        """Reconcile via system.billing.usage for the statement.

        TODO (live): query system.billing.usage joined to system.query.history
        on statement_id for usage_quantity (DBUs) in the window after
        execution, then convert with pricing.databricks at the workspace's real
        $/DBU. Needs a live workspace to validate the join keys.
        """
        raise NotImplementedError(
            "fetch_actual_cost for Databricks needs a live workspace to validate "
            "the system.billing.usage join — see the TODO in "
            "engines/databricks.py."
        )
