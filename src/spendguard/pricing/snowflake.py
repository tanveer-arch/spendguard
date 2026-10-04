"""Snowflake credit pricing.

Snowflake bills compute in *credits* per second a warehouse runs, with the
credit rate doubling per warehouse size (t-shirt sizing). The dollar value of
a credit depends on the Snowflake edition and region (roughly $2–$4); it is a
configurable constant here, not a universal truth.

USD_PER_CREDIT default: 2.0 (Standard edition, US region — approximate).
Override it for your contract; the engine exposes it as a setting.
"""

USD_PER_CREDIT = 2.0

#: Credits consumed per *hour* a warehouse runs, by t-shirt size.
#: Keys are normalized the same way credits_per_second() normalizes input,
#: so "X-Small", "xsmall" and "XSMALL" all match.
CREDITS_PER_HOUR: dict[str, float] = {
    "XSMALL": 1,
    "SMALL": 2,
    "MEDIUM": 4,
    "LARGE": 8,
    "XLARGE": 16,
    "2XLARGE": 32,
    "3XLARGE": 64,
    "4XLARGE": 128,
    "5XLARGE": 256,
    "6XLARGE": 512,
}


def credits_per_second(warehouse_size: str) -> float:
    """Credits burned per second of warehouse runtime for a t-shirt size."""
    size = warehouse_size.strip().upper().replace("-", "").replace(" ", "")
    try:
        return CREDITS_PER_HOUR[size] / 3600.0
    except KeyError:
        raise ValueError(
            f"Unknown Snowflake warehouse size {warehouse_size!r}; "
            f"expected one of {sorted(CREDITS_PER_HOUR)}"
        ) from None


def credits_to_usd(credits: float, usd_per_credit: float = USD_PER_CREDIT) -> float:
    """Convert Snowflake credits to USD. Pure math, no I/O."""
    if credits < 0:
        raise ValueError("credits must be >= 0")
    return credits * usd_per_credit


def runtime_cost_usd(
    warehouse_size: str,
    seconds: float,
    usd_per_credit: float = USD_PER_CREDIT,
) -> float:
    """Upper-bound-ish dollar cost of running a warehouse for `seconds`.

    Snowflake bills a 60-second minimum per warehouse start; this helper does
    not apply that minimum — the engine adds the caveat instead.
    """
    if seconds < 0:
        raise ValueError("seconds must be >= 0")
    return credits_per_second(warehouse_size) * seconds * usd_per_credit
