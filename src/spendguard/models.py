"""Core data models shared by engines, ledger, budgets, and tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class AccuracyTier(str, Enum):
    """How much you should trust a cost estimate.

    - PRECISE: the warehouse tells us the exact bytes (BigQuery dry run).
    - UPPER_BOUND: derived from the optimizer plan; real cost will not exceed
      this in the common case, but it is not exact (Snowflake).
    - HEURISTIC: no pre-flight signal exists on the warehouse, so we estimate
      from warehouse size + plan shape and calibrate against actuals over time
      (Databricks). Treat as directional, not exact.
    """

    PRECISE = "PRECISE"
    UPPER_BOUND = "UPPER_BOUND"
    HEURISTIC = "HEURISTIC"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Estimate:
    """A pre-flight cost estimate for one SQL statement."""

    engine: str
    accuracy_tier: AccuracyTier
    estimated_bytes: int | None = None
    estimated_cost_usd: float | None = None
    currency: str = "USD"
    caveats: list[str] = field(default_factory=list)
    calibration_applied: bool = False
    sql_hash: str = ""

    def to_dict(self) -> dict:
        return {
            "engine": self.engine,
            "accuracy_tier": self.accuracy_tier.value,
            "estimated_bytes": self.estimated_bytes,
            "estimated_cost_usd": self.estimated_cost_usd,
            "currency": self.currency,
            "caveats": self.caveats,
            "calibration_applied": self.calibration_applied,
        }


@dataclass
class ActualCost:
    """The real billed cost of an executed query, fetched post-hoc."""

    engine: str
    query_id: str
    billed_bytes: int | None = None
    billed_cost_usd: float | None = None
    currency: str = "USD"
    recorded_at: datetime = field(default_factory=_utcnow)


@dataclass
class Budget:
    """A spend cap over a scope (one session, or one calendar day)."""

    name: str
    scope: str  # "session" | "day"
    max_cost_usd: float
    engine: str | None = None  # None = all engines


@dataclass
class SpendRecord:
    """One ledger entry: an estimate or a reconciled actual."""

    engine: str
    amount_usd: float
    kind: str  # "estimate" | "actual"
    sql_hash: str = ""
    recorded_at: datetime = field(default_factory=_utcnow)
