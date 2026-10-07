"""SQLite ledger at ~/.spendguard/ledger.db (or $SPENDGUARD_HOME/ledger.db).

Three tables:
- estimates: every pre-flight estimate we handed an agent.
- actuals: post-execution billed cost, joined back to its estimate.
- calibration: per-engine EWMA of actual/estimate ratios. Applied to future
  estimates so heuristic tiers converge on reality over time.

EWMA details: factor starts at 1.0, alpha = 0.3. Updates are skipped when the
estimate is 0 (divide-by-zero) or the actual is 0 while the estimate was
non-zero — a $0 actual usually means the reconciliation lookup failed, not
that the query was free, and folding it in would corrupt the factor.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from spendguard.models import Estimate

ALPHA = 0.3  # EWMA weight for the newest actual/estimate ratio


def home_dir() -> Path:
    """State directory. $SPENDGUARD_HOME overrides ~/.spendguard (used by tests)."""
    return Path(os.environ.get("SPENDGUARD_HOME", Path.home() / ".spendguard"))


def db_path() -> Path:
    return home_dir() / "ledger.db"


@contextmanager
def _connect():
    """Open the ledger DB, ensuring the schema exists.

    A context manager (not just a connection factory): the connection is
    committed and *closed* on exit. ``with sqlite3.connect(...) as conn:``
    only commits -- it never closes -- which leaks the connection and leaves
    WAL checkpoint timing to the garbage collector.
    """
    home_dir().mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path())
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS estimates (
                id INTEGER PRIMARY KEY,
                engine TEXT NOT NULL,
                accuracy_tier TEXT NOT NULL,
                estimated_bytes INTEGER,
                estimated_cost_usd REAL,
                sql_hash TEXT NOT NULL,
                ts TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS actuals (
                id INTEGER PRIMARY KEY,
                estimate_id INTEGER NOT NULL REFERENCES estimates(id),
                query_id TEXT NOT NULL,
                billed_bytes INTEGER,
                billed_cost_usd REAL,
                ts TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS calibration (
                engine TEXT PRIMARY KEY,
                factor REAL NOT NULL,
                n INTEGER NOT NULL
            );
            """
        )
        yield conn
        conn.commit()
    finally:
        conn.close()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_estimate(estimate: Estimate) -> int:
    """Persist an estimate; returns its row id for later reconciliation."""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO estimates (engine, accuracy_tier, estimated_bytes,"
            " estimated_cost_usd, sql_hash, ts) VALUES (?, ?, ?, ?, ?, ?)",
            (
                estimate.engine,
                estimate.accuracy_tier.value,
                estimate.estimated_bytes,
                estimate.estimated_cost_usd,
                estimate.sql_hash,
                _utcnow_iso(),
            ),
        )
        return cur.lastrowid


def record_actual(
    estimate_id: int,
    query_id: str,
    billed_bytes: int | None,
    billed_cost_usd: float | None,
) -> None:
    """Persist an actual and fold the actual/estimate ratio into calibration."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO actuals (estimate_id, query_id, billed_bytes,"
            " billed_cost_usd, ts) VALUES (?, ?, ?, ?, ?)",
            (estimate_id, query_id, billed_bytes, billed_cost_usd, _utcnow_iso()),
        )
        row = conn.execute(
            "SELECT engine, estimated_cost_usd FROM estimates WHERE id = ?",
            (estimate_id,),
        ).fetchone()
        if row is None:
            return
        engine, estimated = row
        if estimated is None or estimated <= 0:
            return  # no meaningful ratio; skip (divide-by-zero guard)
        if billed_cost_usd is None or billed_cost_usd <= 0:
            return  # $0 actual = reconciliation miss, not a free query
        ratio = billed_cost_usd / estimated
        cur = conn.execute(
            "SELECT factor, n FROM calibration WHERE engine = ?", (engine,)
        ).fetchone()
        if cur is None:
            factor, n = 1.0, 0
        else:
            factor, n = cur
        new_factor = (1 - ALPHA) * factor + ALPHA * ratio
        conn.execute(
            "INSERT INTO calibration (engine, factor, n) VALUES (?, ?, ?)"
            " ON CONFLICT(engine) DO UPDATE SET factor=excluded.factor, n=excluded.n",
            (engine, new_factor, n + 1),
        )


def calibration_factor(engine: str) -> float:
    """Current EWMA calibration factor for an engine (1.0 = uncalibrated)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT factor FROM calibration WHERE engine = ?", (engine,)
        ).fetchone()
        return row[0] if row else 1.0


def apply_calibration(estimate: Estimate) -> Estimate:
    """Scale a dollar estimate by the engine's calibration factor, in place.

    Only touches estimated_cost_usd; bytes are left alone. Marks the estimate
    so agents can see the number was adjusted from experience, not theory.
    """
    factor = calibration_factor(estimate.engine)
    if factor != 1.0 and estimate.estimated_cost_usd is not None:
        estimate.estimated_cost_usd = estimate.estimated_cost_usd * factor
        estimate.calibration_applied = True
        estimate.caveats.append(
            f"Adjusted by ledger calibration x{factor:.2f} "
            f"({estimate.engine} historical actual/estimate ratio)."
        )
    return estimate


def spend_summary(period_days: int = 1) -> dict:
    """Per-engine totals of reconciled actuals over the last `period_days`.

    Returns {"bigquery": {"actual_usd": ..., "queries": N, ...}, ...} plus a
    "_totals" key. Estimates without a reconciled actual are counted
    separately so unreconciled spend is visible, not hidden.
    """
    with _connect() as conn:
        actuals = conn.execute(
            """
            SELECT e.engine, SUM(a.billed_cost_usd), COUNT(*)
            FROM actuals a JOIN estimates e ON a.estimate_id = e.id
            WHERE a.ts >= datetime('now', ?)
            GROUP BY e.engine
            """,
            (f"-{period_days} days",),
        ).fetchall()
        pending = conn.execute(
            """
            SELECT engine, COUNT(*), SUM(estimated_cost_usd) FROM estimates
            WHERE id NOT IN (SELECT estimate_id FROM actuals)
              AND ts >= datetime('now', ?)
            GROUP BY engine
            """,
            (f"-{period_days} days",),
        ).fetchall()
    out: dict = {}
    total_usd = 0.0
    for engine, usd, n in actuals:
        out[engine] = {"actual_usd": round(usd or 0.0, 4), "queries": n}
        total_usd += usd or 0.0
    for engine, n, est_usd in pending:
        out.setdefault(engine, {"actual_usd": 0.0, "queries": 0})
        out[engine]["pending_estimates"] = n
        out[engine]["pending_estimated_usd"] = round(est_usd or 0.0, 4)
    out["_totals"] = {"actual_usd": round(total_usd, 4), "period_days": period_days}
    return out


def recent_estimate_costs(engine: str, limit: int = 50) -> list[float]:
    """Last `limit` estimated dollar costs for an engine (for anomaly detection)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT estimated_cost_usd FROM estimates WHERE engine = ?"
            " AND estimated_cost_usd IS NOT NULL ORDER BY id DESC LIMIT ?",
            (engine, limit),
        ).fetchall()
    return [r[0] for r in rows]
