"""Databricks SQL warehouse pricing (heuristic layer).

Databricks bills SQL warehouses in DBUs (Databricks Units) per hour, with the
rate set by warehouse size and multiplied by the tier (Standard / Premium /
Enterprise). The *dollar* price of a DBU varies by cloud, region, and contract
($0.22–$0.70+), so USD_PER_DBU is a configurable approximate default, not a
quote.

Because Databricks exposes no dry-run API, all dollar figures derived here
are HEURISTIC accuracy tier and must be presented that way.
"""

#: DBUs consumed per hour by SQL warehouse size (Databricks-published rates).
DBU_PER_HOUR: dict[str, float] = {
    "2X-SMALL": 0.25,
    "X-SMALL": 0.5,
    "SMALL": 1.0,
    "MEDIUM": 2.0,
    "LARGE": 4.0,
    "X-LARGE": 8.0,
    "2X-LARGE": 16.0,
    "3X-LARGE": 32.0,
    "4X-LARGE": 64.0,
}

#: Approximate tier multipliers on the DBU rate.
TIER_MULTIPLIER: dict[str, float] = {
    "standard": 1.0,
    "premium": 1.5,
    "enterprise": 2.0,
}

#: Approximate default $/DBU (Databricks-published list, varies by region).
USD_PER_DBU = 0.40

#: Databricks bills a 1-minute minimum per warehouse start.
MIN_BILLABLE_MINUTES = 1.0


def dbu_per_hour(warehouse_size: str, tier: str = "standard") -> float:
    """DBUs burned per hour for a warehouse size and tier."""
    size = warehouse_size.strip().upper()
    mult = TIER_MULTIPLIER.get(tier.strip().lower())
    if mult is None:
        raise ValueError(
            f"Unknown Databricks tier {tier!r}; expected one of {sorted(TIER_MULTIPLIER)}"
        )
    try:
        return DBU_PER_HOUR[size] * mult
    except KeyError:
        raise ValueError(
            f"Unknown Databricks warehouse size {warehouse_size!r}; "
            f"expected one of {sorted(DBU_PER_HOUR)}"
        ) from None


def runtime_cost_usd(
    warehouse_size: str,
    minutes: float,
    tier: str = "standard",
    usd_per_dbu: float = USD_PER_DBU,
) -> float:
    """Heuristic dollar cost of running a warehouse for `minutes`.

    Applies the 1-minute minimum billing that Databricks enforces.
    """
    if minutes < 0:
        raise ValueError("minutes must be >= 0")
    billable = max(minutes, MIN_BILLABLE_MINUTES)
    return dbu_per_hour(warehouse_size, tier) * (billable / 60.0) * usd_per_dbu
