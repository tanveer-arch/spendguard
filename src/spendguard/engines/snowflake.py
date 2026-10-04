"""Snowflake engine — UPPER_BOUND estimates via EXPLAIN USING JSON.

Snowflake exposes no pre-flight byte count, so we take the optimizer plan
(EXPLAIN USING JSON) and extract the largest byte figure the planner admits
to — a genuine upper bound, labeled as such. Dollar conversion uses the
warehouse's credit burn rate; Snowflake bills a 60-second minimum per
warehouse start, which we surface as a caveat rather than silently baking in.

Auth: key-pair (JWT) is preferred — SNOWFLAKE_PRIVATE_KEY_PATH pointing at an
unencrypted RSA .p8. Password auth via SNOWFLAKE_PASSWORD is accepted as a
fallback. The role is never defaulted to ACCOUNTADMIN.
"""

from __future__ import annotations

import hashlib
import json
import os

from spendguard.engines.base import Engine, MissingCredentialsError
from spendguard.models import AccuracyTier, ActualCost, Estimate
from spendguard.pricing import snowflake as sf_pricing


def _sql_hash(sql: str) -> str:
    return hashlib.sha256(sql.encode()).hexdigest()[:16]


def _max_bytes_in_plan(node) -> int | None:
    """Recursively find the largest 'bytes'-like figure in an EXPLAIN plan.

    The JSON plan's node statistics vary by Snowflake version; we scan for
    common keys ('bytes', 'bytesAssigned', 'outputBytes') and take the max as
    an upper bound. Returns None when the plan carries no byte figures.
    """
    best: int | None = None
    if isinstance(node, dict):
        for key, value in node.items():
            kl = key.lower()
            if kl in {"bytes", "bytesassigned", "outputbytes"} and isinstance(
                value, (int, float)
            ):
                best = value if best is None else max(best, int(value))
            child = _max_bytes_in_plan(value)
            if child is not None:
                best = child if best is None else max(best, child)
    elif isinstance(node, list):
        for item in node:
            child = _max_bytes_in_plan(item)
            if child is not None:
                best = child if best is None else max(best, child)
    return best


class SnowflakeEngine(Engine):
    name = "snowflake"

    def _connect(self):
        """Open a Snowflake connection, or raise an actionable error."""
        try:
            import snowflake.connector
        except ImportError as exc:
            raise MissingCredentialsError(
                "snowflake-connector-python is not installed. "
                "Run: pip install spendguard (or pip install snowflake-connector-python)."
            ) from exc
        account = os.environ.get("SNOWFLAKE_ACCOUNT")
        user = os.environ.get("SNOWFLAKE_USER")
        if not account or not user:
            raise MissingCredentialsError(
                "Snowflake credentials not found. Set SNOWFLAKE_ACCOUNT and "
                "SNOWFLAKE_USER, plus SNOWFLAKE_PRIVATE_KEY_PATH (key-pair/JWT, "
                "preferred) or SNOWFLAKE_PASSWORD."
            )
        kwargs: dict = {
            "account": account,
            "user": user,
            "role": os.environ.get("SNOWFLAKE_ROLE"),  # never defaulted
            "warehouse": os.environ.get("SNOWFLAKE_WAREHOUSE"),
            "database": os.environ.get("SNOWFLAKE_DATABASE"),
            "schema": os.environ.get("SNOWFLAKE_SCHEMA"),
        }
        key_path = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH")
        if key_path:
            # TODO (live): load the DER/PEM key with cryptography, strip the
            # header, and pass private_key=... to connect(). Kept out of the
            # default path so the module stays importable without `cryptography`.
            raise MissingCredentialsError(
                "Key-pair auth is preferred but the key-loading path is not "
                "wired yet: set SNOWFLAKE_PASSWORD as a fallback for now."
            )
        password = os.environ.get("SNOWFLAKE_PASSWORD")
        if not password:
            raise MissingCredentialsError(
                "Set SNOWFLAKE_PRIVATE_KEY_PATH (preferred) or SNOWFLAKE_PASSWORD."
            )
        kwargs["password"] = password
        return snowflake.connector.connect(**{k: v for k, v in kwargs.items() if v})

    def _warehouse_size(self, conn) -> str:
        """Resolve the current warehouse's t-shirt size via SHOW WAREHOUSES."""
        cur = conn.cursor()
        try:
            cur.execute("SHOW WAREHOUSES")
            target = (os.environ.get("SNOWFLAKE_WAREHOUSE") or "").upper()
            for row in cur.fetchall():
                # SHOW WAREHOUSES returns (name, state, type, size, ...)
                if len(row) > 3 and row[0].upper() == target:
                    return str(row[3]).upper()
        finally:
            cur.close()
        # Fallback: caller-supplied size, else XSMALL (cheapest assumption,
        # disclosed in caveats).
        return os.environ.get("SNOWFLAKE_WAREHOUSE_SIZE", "XSMALL").upper()

    def estimate(self, sql: str, warehouse: str | None = None) -> Estimate:
        """EXPLAIN the query and upper-bound bytes from the optimizer plan."""
        conn = self._connect()
        caveats = [
            ("Snowflake exposes no pre-flight byte count: this is an UPPER BOUND "
            "derived from the optimizer plan, not an exact figure."),
            ("Snowflake bills a 60-second minimum per warehouse start; short "
            "queries cost at least that."),
        ]
        try:
            cur = conn.cursor()
            try:
                cur.execute("EXPLAIN USING JSON " + sql)
                plan_text = cur.fetchone()[0]
            finally:
                cur.close()
            plan = json.loads(plan_text) if isinstance(plan_text, str) else plan_text
            upper_bytes = _max_bytes_in_plan(plan)
            if upper_bytes is None:
                caveats.append(
                    "The optimizer plan carried no byte figures, so no byte "
                    "upper bound could be derived — dollar figure is "
                    "warehouse-rate based only."
                )
            # We cannot know runtime pre-flight; the honest upper bound for a
            # bounded agent query is one 60s minimum at the warehouse rate.
            # Anything longer is genuinely unknown — say so.
            size = self._warehouse_size(conn)
            usd = sf_pricing.runtime_cost_usd(size, 60.0)
            caveats.append(
                f"Dollar figure assumes a single 60s minimum billing window on "
                f"a {size} warehouse; longer-running queries cost more."
            )
            return Estimate(
                engine=self.name,
                accuracy_tier=AccuracyTier.UPPER_BOUND,
                estimated_bytes=upper_bytes,
                estimated_cost_usd=usd,
                caveats=caveats,
                sql_hash=_sql_hash(sql),
            )
        finally:
            conn.close()

    def execute(self, sql: str, max_rows: int = 1000, **kwargs) -> tuple[list[dict], str]:
        """Run the query and return (rows capped at max_rows, query_id)."""
        conn = self._connect()
        try:
            cur = conn.cursor()
            try:
                cur.execute(sql)
                cols = [d[0] for d in cur.description or []]
                rows = [dict(zip(cols, r)) for r in cur.fetchmany(max_rows)]
                return rows, cur.sfqid
            finally:
                cur.close()
        finally:
            conn.close()

    def fetch_actual_cost(self, query_id: str) -> ActualCost:
        """Reconcile via QUERY_HISTORY: bytes scanned and credits used.

        TODO (live): query SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY for
        BYTES_SCANNED and CREDITS_USED_COMPUTE for the query_id, then convert
        credits via pricing.snowflake.credits_to_usd with the account's real
        $/credit. The stub below returns the shape; wire the SQL when a live
        account is available for testing.
        """
        raise NotImplementedError(
            "fetch_actual_cost for Snowflake needs a live account to validate "
            "the ACCOUNT_USAGE.QUERY_HISTORY shape — see the TODO in "
            "engines/snowflake.py. Implement against BYTES_SCANNED and "
            "CREDITS_USED_COMPUTE, then convert with pricing.snowflake."
        )
