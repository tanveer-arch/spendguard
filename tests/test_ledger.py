"""Offline tests: ledger calibration (EWMA), guards, and spend summaries."""

import os

import pytest

from spendguard.ledger import store
from spendguard.models import AccuracyTier, Estimate


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Point $SPENDGUARD_HOME at a temp dir so tests never touch ~/.spendguard."""
    monkeypatch.setenv("SPENDGUARD_HOME", str(tmp_path))
    assert "SPENDGUARD_HOME" in os.environ


def _estimate(engine="bigquery", cost=10.0, tier=AccuracyTier.PRECISE) -> Estimate:
    return Estimate(
        engine=engine,
        accuracy_tier=tier,
        estimated_bytes=2**30,
        estimated_cost_usd=cost,
        sql_hash="abc123",
    )


def test_uncalibrated_factor_is_one():
    assert store.calibration_factor("bigquery") == 1.0


def test_ewma_moves_toward_actual_ratio():
    eid = store.record_estimate(_estimate(cost=10.0))
    store.record_actual(eid, "job1", 2**30, 20.0)  # ratio 2.0
    # factor = 0.7*1.0 + 0.3*2.0 = 1.3
    assert store.calibration_factor("bigquery") == pytest.approx(1.3)

    store.record_actual(eid, "job2", 2**30, 10.0)  # ratio 1.0
    # factor = 0.7*1.3 + 0.3*1.0 = 1.21
    assert store.calibration_factor("bigquery") == pytest.approx(1.21)


def test_zero_estimate_skips_update_no_divide_by_zero():
    eid = store.record_estimate(_estimate(cost=0.0))
    store.record_actual(eid, "job1", 2**30, 5.0)  # actual > 0, estimate 0
    assert store.calibration_factor("bigquery") == 1.0


def test_zero_actual_skips_update():
    # A $0 actual is a reconciliation miss, not a free query.
    eid = store.record_estimate(_estimate(cost=10.0))
    store.record_actual(eid, "job1", 0, 0.0)
    assert store.calibration_factor("bigquery") == 1.0


def test_calibration_is_per_engine():
    eid = store.record_estimate(_estimate(engine="snowflake", cost=10.0))
    store.record_actual(eid, "q1", None, 20.0)
    assert store.calibration_factor("snowflake") == pytest.approx(1.3)
    assert store.calibration_factor("bigquery") == 1.0


def test_apply_calibration_scales_and_marks():
    eid = store.record_estimate(_estimate(cost=10.0))
    store.record_actual(eid, "job1", 2**30, 20.0)
    est = _estimate(cost=10.0)
    store.apply_calibration(est)
    assert est.estimated_cost_usd == pytest.approx(13.0)
    assert est.calibration_applied is True
    assert any("calibration" in c for c in est.caveats)


def test_apply_calibration_noop_when_uncalibrated():
    est = _estimate(cost=10.0)
    store.apply_calibration(est)
    assert est.estimated_cost_usd == pytest.approx(10.0)
    assert est.calibration_applied is False


def test_spend_summary_totals_and_pending():
    eid = store.record_estimate(_estimate(cost=10.0))
    store.record_actual(eid, "job1", 2**30, 7.5)
    store.record_estimate(_estimate(cost=3.0))  # never reconciled -> pending

    summary = store.spend_summary(1)
    assert summary["bigquery"]["actual_usd"] == pytest.approx(7.5)
    assert summary["bigquery"]["queries"] == 1
    assert summary["bigquery"]["pending_estimates"] == 1
    assert summary["bigquery"]["pending_estimated_usd"] == pytest.approx(3.0)
    assert summary["_totals"]["actual_usd"] == pytest.approx(7.5)
