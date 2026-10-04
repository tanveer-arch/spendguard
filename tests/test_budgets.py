"""Offline tests: budget policy, anomaly detection, confirmation tokens."""

import time

import pytest

from spendguard.budgets import policy
from spendguard.budgets.policy import Decision, Policy
from spendguard.ledger import store
from spendguard.models import AccuracyTier, Estimate


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SPENDGUARD_HOME", str(tmp_path))


@pytest.fixture(autouse=True)
def clean_tokens():
    policy._confirmation_tokens.clear()
    yield
    policy._confirmation_tokens.clear()


def _estimate(cost=1.0, engine="bigquery") -> Estimate:
    return Estimate(
        engine=engine,
        accuracy_tier=AccuracyTier.PRECISE,
        estimated_bytes=2**30,
        estimated_cost_usd=cost,
        sql_hash="hash1",
    )


def _write_config(text: str, tmp_path=None):
    import os

    home = os.environ["SPENDGUARD_HOME"]
    with open(os.path.join(home, "config.toml"), "w") as fh:
        fh.write(text)


def test_no_config_means_no_caps():
    assert policy.load_limits() == {}
    result = Policy().check(_estimate(cost=999.0))
    assert result.decision == Decision.OK


def test_daily_cap_blocks():
    _write_config("[budgets]\ndaily_max_usd = 5.0\n")
    result = Policy().check(_estimate(cost=6.0))
    assert result.decision == Decision.OVER_CAP
    assert any("Daily cap" in r for r in result.reasons)


def test_daily_cap_allows_under():
    _write_config("[budgets]\ndaily_max_usd = 50.0\n")
    result = Policy().check(_estimate(cost=6.0))
    assert result.decision == Decision.OK


def test_session_cap_uses_running_total():
    _write_config("[budgets]\nsession_max_usd = 10.0\n")
    p = Policy(session_spent_usd=9.0)
    result = p.check(_estimate(cost=2.0))
    assert result.decision == Decision.OVER_CAP
    assert any("Session cap" in r for r in result.reasons)


def test_confirm_threshold_requires_token_flow():
    _write_config("[budgets]\nconfirm_above_usd = 5.0\n")
    result = Policy().check(_estimate(cost=6.0))
    assert result.decision == Decision.NEEDS_CONFIRMATION
    assert any("confirmation token" in r for r in result.reasons)


def test_anomaly_flags_40x_median():
    for _ in range(6):
        store.record_estimate(_estimate(cost=0.10))
    result = Policy().check(_estimate(cost=5.0))  # 50x the $0.10 median
    assert result.decision == Decision.ANOMALY
    assert any("40x" in r for r in result.reasons)


def test_anomaly_needs_minimum_history():
    for _ in range(3):  # fewer than the 5 needed
        store.record_estimate(_estimate(cost=0.10))
    result = Policy().check(_estimate(cost=5.0))
    assert result.decision == Decision.OK


def test_confirmation_token_round_trip():
    est = _estimate(cost=6.0)
    token = policy.request_confirmation(est)
    assert policy.consume_confirmation(token, est) is True
    # single-use: second consume fails
    assert policy.consume_confirmation(token, est) is False


def test_confirmation_token_wrong_or_unknown():
    est = _estimate(cost=6.0)
    token = policy.request_confirmation(est)
    other = _estimate(cost=6.0)
    other.sql_hash = "different"
    assert policy.consume_confirmation(token, other) is False
    assert policy.consume_confirmation("nope-not-a-token", est) is False


def test_confirmation_token_expiry():
    est = _estimate(cost=6.0)
    token = policy.request_confirmation(est)
    sql_hash, _ = policy._confirmation_tokens[token]
    policy._confirmation_tokens[token] = (sql_hash, time.monotonic() - 1)
    assert policy.consume_confirmation(token, est) is False


def test_save_budget_persists_and_policy_reads_it():
    policy.save_budget("daily", "day", 25.0)
    policy.save_budget("session", "session", 8.0)
    limits = policy.load_limits()
    assert limits["daily_max_usd"] == 25.0
    assert limits["session_max_usd"] == 8.0
    # idempotent overwrite
    policy.save_budget("daily", "day", 30.0)
    assert policy.load_limits()["daily_max_usd"] == 30.0


def test_save_budget_validates():
    with pytest.raises(ValueError):
        policy.save_budget("daily", "week", 10.0)
    with pytest.raises(ValueError):
        policy.save_budget("daily", "day", 0)
