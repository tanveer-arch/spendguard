"""`spendguard estimate` uses a stubbed dry-run client and a disposable ledger."""

import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from spendguard.engines.bigquery import BigQueryEngine
from spendguard.pricing import bigquery as bq_pricing


class FakeClient:
    def __init__(self, total_bytes):
        self.total_bytes = total_bytes
        self.calls = []

    def query(self, sql, job_config=None):
        self.calls.append((sql, job_config))
        return SimpleNamespace(total_bytes_processed=self.total_bytes)


@pytest.fixture
def bigquery_module(monkeypatch):
    """Use the real library when installed; otherwise stub the dry-run config."""
    try:
        from google.cloud import bigquery  # noqa: F401
    except ImportError:
        cloud = ModuleType("google.cloud")
        cloud.bigquery = ModuleType("google.cloud.bigquery")
        cloud.bigquery.QueryJobConfig = SimpleNamespace
        monkeypatch.setitem(sys.modules, "google", ModuleType("google"))
        monkeypatch.setitem(sys.modules, "google.cloud", cloud)
        monkeypatch.setitem(sys.modules, "google.cloud.bigquery", cloud.bigquery)


@pytest.fixture
def dry_run(tmp_path, monkeypatch, bigquery_module):
    """Isolate the ledger and replace the BigQuery client with a fake."""
    monkeypatch.setenv("SPENDGUARD_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("BIGQUERY_BILLING_MODEL", raising=False)

    def install(total_bytes):
        client = FakeClient(total_bytes)
        monkeypatch.setattr(BigQueryEngine, "_client", lambda self: client)
        return client

    return install


def test_estimate_prints_tier_bytes_usd_and_caveats(dry_run, capsys):
    from spendguard.cli import main

    client = dry_run(10 * 1024**4)
    assert main(["estimate", "bigquery", "SELECT * FROM t"]) == 0
    out = capsys.readouterr().out
    assert client.calls[0][0] == "SELECT * FROM t"
    assert client.calls[0][1].dry_run is True
    usd = bq_pricing.bytes_to_usd(10 * 1024**4)
    assert "engine: bigquery" in out
    assert "tier: PRECISE" in out
    assert f"bytes: {10 * 1024**4}" in out
    assert f"usd: {usd:.6f}" in out
    assert "calibration_applied: false" in out
    assert "estimate_id: 1" in out
    assert "caveats: none" in out


def test_zero_bytes_surfaces_rls_caveat(dry_run, capsys):
    from spendguard.cli import main

    dry_run(0)
    assert main(["estimate", "BigQuery", "SELECT 1"]) == 0
    out = capsys.readouterr().out
    assert "bytes: 0" in out
    assert "caveats:\n  - Dry run reports 0 bytes" in out


def test_capacity_billing_prints_no_dollar_figure(dry_run, capsys):
    from spendguard.cli import main

    dry_run(2048)
    assert main(["estimate", "bigquery", "SELECT 1", "--warehouse", "capacity"]) == 0
    out = capsys.readouterr().out
    assert "usd: n/a" in out
    assert "capacity (Editions) billing" in out


def test_json_output_matches_estimate_query_cost(dry_run, capsys):
    from spendguard.cli import main
    from spendguard.tools.estimate import estimate_query_cost

    dry_run(0)
    assert main(["estimate", "bigquery", "SELECT 1", "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    direct = estimate_query_cost("bigquery", "SELECT 1")
    assert printed.keys() == direct.keys()
    assert printed.pop("estimate_id") + 1 == direct.pop("estimate_id")
    assert printed == direct


def test_missing_credentials_exits_cleanly(
    tmp_path, monkeypatch, capsys, bigquery_module
):
    from spendguard.cli import main

    monkeypatch.setenv("SPENDGUARD_HOME", str(tmp_path))
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    with pytest.raises(SystemExit) as error:
        main(["estimate", "bigquery", "SELECT 1"])
    assert error.value.code == 2
    assert "BigQuery credentials not found" in capsys.readouterr().err


def test_unknown_engine_exits_cleanly(tmp_path, monkeypatch, capsys):
    from spendguard.cli import main

    monkeypatch.setenv("SPENDGUARD_HOME", str(tmp_path))
    with pytest.raises(SystemExit) as error:
        main(["estimate", "redshift", "SELECT 1"])
    assert error.value.code == 2
    assert "Unknown engine 'redshift'" in capsys.readouterr().err
