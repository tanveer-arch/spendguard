"""Budgets with teeth: caps, anomaly detection, human confirmation.

Config lives in ~/.spendguard/config.toml (or $SPENDGUARD_HOME/config.toml):

    [budgets]
    daily_max_usd = 50.0        # hard stop for the calendar day, all engines
    session_max_usd = 10.0      # hard stop for this agent session
    confirm_above_usd = 5.0     # single query above this needs a human token

The preview -> confirm -> execute flow: when a query needs confirmation,
`request_confirmation()` returns a single-use token (5-minute expiry) that the
agent must hand back as `run_query_bounded(confirmation_token=...)`. Tokens
are consumed on use and expire silently — a stale token is a refusal, not an
error to retry around.

Anomaly detection: a query estimated at more than ANOMALY_MULTIPLE (40x) the
rolling median of recent estimates for its engine is flagged, even if it is
under every cap. Medians come from the ledger, so the baseline is *your*
workload, not a global guess.
"""

from __future__ import annotations

import secrets
import statistics
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from spendguard.ledger import store as ledger_store
from spendguard.models import Estimate

ANOMALY_MULTIPLE = 40.0
CONFIRM_TOKEN_TTL_SECONDS = 300  # 5 minutes

# token -> (sql_hash, expires_at_monotonic). In-memory on purpose: a confirm
# token must not survive a process restart.
_confirmation_tokens: dict[str, tuple[str, float]] = {}


class Decision(str, Enum):
    OK = "ok"
    OVER_CAP = "over_cap"
    NEEDS_CONFIRMATION = "needs_confirmation"
    ANOMALY = "anomaly"


@dataclass
class CheckResult:
    decision: Decision
    reasons: list[str] = field(default_factory=list)
    estimate_usd: float | None = None


def _config_path() -> Path:
    return ledger_store.home_dir() / "config.toml"


def load_limits() -> dict[str, float]:
    """Read budget limits from config.toml; missing file = no caps configured."""
    path = _config_path()
    if not path.exists():
        return {}
    limits: dict[str, float] = {}
    try:
        import tomllib
    except ImportError:  # Python 3.10 fallback
        import tomli as tomllib  # type: ignore[no-redef]
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    for key in ("daily_max_usd", "session_max_usd", "confirm_above_usd"):
        value = data.get("budgets", {}).get(key)
        if value is not None:
            limits[key] = float(value)
    return limits


@dataclass
class Policy:
    """Evaluates one estimate against caps, session spend, and anomaly baseline."""

    session_spent_usd: float = 0.0

    def check(self, estimate: Estimate) -> CheckResult:
        """Return the decision for running this estimate's query."""
        usd = estimate.estimated_cost_usd
        result = CheckResult(decision=Decision.OK, estimate_usd=usd)
        if usd is None:
            result.reasons.append(
                "No dollar figure available (capacity billing or unknown); "
                "passing through — set an explicit byte cap if you want this blocked."
            )
            return result

        limits = load_limits()

        daily_max = limits.get("daily_max_usd")
        if daily_max is not None:
            spent_today = ledger_store.spend_summary(1)["_totals"]["actual_usd"]
            if spent_today + usd > daily_max:
                result.decision = Decision.OVER_CAP
                result.reasons.append(
                    f"Daily cap ${daily_max:.2f} would be exceeded "
                    f"(spent ${spent_today:.2f} + this query ${usd:.2f})."
                )

        session_max = limits.get("session_max_usd")
        if session_max is not None and self.session_spent_usd + usd > session_max:
            result.decision = Decision.OVER_CAP
            result.reasons.append(
                f"Session cap ${session_max:.2f} would be exceeded "
                f"(spent ${self.session_spent_usd:.2f} + this query ${usd:.2f})."
            )

        # Anomaly: 40x the rolling median for this engine.
        history = ledger_store.recent_estimate_costs(estimate.engine)
        if len(history) >= 5:
            median = statistics.median(history)
            if median > 0 and usd > ANOMALY_MULTIPLE * median:
                if result.decision == Decision.OK:
                    result.decision = Decision.ANOMALY
                result.reasons.append(
                    f"Anomaly: ${usd:.2f} is >{ANOMALY_MULTIPLE:.0f}x your median "
                    f"${median:.2f} for {estimate.engine} "
                    f"({len(history)} recent estimates)."
                )

        confirm_above = limits.get("confirm_above_usd")
        if (
            confirm_above is not None
            and usd > confirm_above
            and result.decision == Decision.OK
        ):
            result.decision = Decision.NEEDS_CONFIRMATION
            result.reasons.append(
                f"Single-query cost ${usd:.2f} exceeds the confirm threshold "
                f"${confirm_above:.2f}; a human confirmation token is required."
            )
        return result

    def record_spend(self, usd: float) -> None:
        """Add reconciled spend to the running session total."""
        self.session_spent_usd += usd


def request_confirmation(estimate: Estimate) -> str:
    """Issue a single-use confirmation token for this estimate (5-min TTL)."""
    token = secrets.token_urlsafe(24)
    _confirmation_tokens[token] = (
        estimate.sql_hash,
        time.monotonic() + CONFIRM_TOKEN_TTL_SECONDS,
    )
    return token


def consume_confirmation(token: str, estimate: Estimate) -> bool:
    """Validate and burn a confirmation token. False = missing/expired/mismatch."""
    entry = _confirmation_tokens.pop(token, None)
    if entry is None:
        return False
    sql_hash, expires_at = entry
    if time.monotonic() > expires_at:
        return False
    return sql_hash == estimate.sql_hash


def save_budget(name: str, scope: str, max_cost_usd: float) -> dict:
    """Persist a named budget cap into config.toml.

    scope: "session" or "day". `name` maps to the config key
    `<name>_max_usd` under [budgets]; the Policy reads the well-known keys
    daily_max_usd / session_max_usd / confirm_above_usd.
    """
    if scope not in {"session", "day"}:
        raise ValueError(f"scope must be 'session' or 'day', got {scope!r}")
    if max_cost_usd <= 0:
        raise ValueError("max_cost_usd must be > 0")
    # Map friendly names onto the keys Policy.load_limits() reads.
    key_map = {"daily": "daily_max_usd", "session": "session_max_usd"}
    key = key_map.get(name, f"{name}_max_usd")
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = path.read_text().splitlines() if path.exists() else []
    entry = f"{key} = {max_cost_usd}"
    if not any(line.strip() == "[budgets]" for line in lines):
        lines.append("[budgets]")
    for i, line in enumerate(lines):
        if line.strip().startswith(key):
            lines[i] = entry
            break
    else:
        lines.append(entry)
    path.write_text("\n".join(lines) + "\n")
    return {"name": name, "scope": scope, "max_cost_usd": max_cost_usd}
