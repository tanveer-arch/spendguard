"""Offline tests: pricing math. No I/O, no credentials."""

import pytest

from spendguard.pricing import bigquery as bq
from spendguard.pricing import databricks as dbx
from spendguard.pricing import snowflake as sf


def test_bigquery_one_tib_is_6_25():
    assert bq.bytes_to_usd(2**40) == pytest.approx(6.25)


def test_bigquery_one_gib():
    assert bq.bytes_to_usd(2**30) == pytest.approx(6.25 / 1024)


def test_bigquery_zero_bytes_is_free():
    assert bq.bytes_to_usd(0) == 0.0


def test_bigquery_negative_raises():
    with pytest.raises(ValueError):
        bq.bytes_to_usd(-1)


def test_snowflake_credits_per_second_spot_checks():
    assert sf.credits_per_second("XSMALL") == pytest.approx(1 / 3600)
    assert sf.credits_per_second("X-Small") == pytest.approx(1 / 3600)
    assert sf.credits_per_second("xlarge") == pytest.approx(16 / 3600)
    assert sf.credits_per_second("2X-Large") == pytest.approx(32 / 3600)
    assert sf.credits_per_second("6X-Large") == pytest.approx(512 / 3600)


def test_snowflake_unknown_size_raises():
    with pytest.raises(ValueError, match="Unknown Snowflake warehouse size"):
        sf.credits_per_second("MEGA")


def test_snowflake_credits_to_usd_default_rate():
    assert sf.credits_to_usd(10) == pytest.approx(20.0)
    assert sf.credits_to_usd(10, usd_per_credit=3.0) == pytest.approx(30.0)


def test_snowflake_runtime_cost_one_hour_xsmall():
    # 1 credit/hour * $2/credit
    assert sf.runtime_cost_usd("XSMALL", 3600) == pytest.approx(2.0)


def test_databricks_dbu_per_hour_spot_checks():
    assert dbx.dbu_per_hour("X-Small") == pytest.approx(0.5)
    assert dbx.dbu_per_hour("SMALL") == pytest.approx(1.0)
    assert dbx.dbu_per_hour("4X-Large", tier="premium") == pytest.approx(64 * 1.5)
    assert dbx.dbu_per_hour("MEDIUM", tier="enterprise") == pytest.approx(2.0 * 2.0)


def test_databricks_unknown_size_or_tier_raise():
    with pytest.raises(ValueError, match="warehouse size"):
        dbx.dbu_per_hour("COLOSSAL")
    with pytest.raises(ValueError, match="tier"):
        dbx.dbu_per_hour("SMALL", tier="ultra")


def test_databricks_minimum_one_minute_billing():
    # 30s on a Small (1 DBU/hr) still bills the 1-minute minimum
    thirty_s = dbx.runtime_cost_usd("SMALL", 0.5)
    one_min = dbx.runtime_cost_usd("SMALL", 1.0)
    assert thirty_s == pytest.approx(one_min) == pytest.approx(1.0 / 60 * 0.40)
