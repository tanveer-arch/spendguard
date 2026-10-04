"""spend_report tool: how much has this agent actually spent?"""

from __future__ import annotations

from spendguard.ledger import calibration_factor, spend_summary
from spendguard.engines import ENGINES


def spend_report(period_days: int = 1) -> dict:
    """Per-engine reconciled spend plus calibration state."""
    summary = spend_summary(period_days)
    summary["calibration"] = {
        name: round(calibration_factor(name), 4) for name in ENGINES
    }
    return summary
