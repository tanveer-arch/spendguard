"""CSV exports use a disposable ledger, never real warehouse accounts."""

import csv

import pytest

from spendguard.ledger import store
from spendguard.models import AccuracyTier, Estimate


def test_export_preserves_pending_estimates_and_multiple_actuals(tmp_path, monkeypatch):
    from spendguard.cli import main

    monkeypatch.setenv("SPENDGUARD_HOME", str(tmp_path / "state"))
    first = store.record_estimate(
        Estimate(
            engine="bigquery",
            accuracy_tier=AccuracyTier.PRECISE,
            estimated_bytes=1024,
            estimated_cost_usd=1.25,
            sql_hash="synthetic",
        )
    )
    pending = store.record_estimate(
        Estimate(
            engine="snowflake",
            accuracy_tier=AccuracyTier.UPPER_BOUND,
            estimated_bytes=None,
            estimated_cost_usd=None,
            sql_hash="synthetic",
        )
    )
    store.record_actual(first, "one", None, 0.0)
    store.record_actual(first, "two", None, 2.5)
    target = tmp_path / "spend.csv"
    assert main(["export", "--csv", str(target)]) == 0
    with target.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        assert reader.fieldnames == [
            "estimate_id",
            "engine",
            "estimated_usd",
            "actual_usd",
            "timestamp",
        ]
        rows = list(reader)
    assert [row["estimate_id"] for row in rows] == [
        str(first),
        str(first),
        str(pending),
    ]
    assert [row["actual_usd"] for row in rows] == ["0.0", "2.5", ""]
    assert rows[-1]["estimated_usd"] == ""
    assert all(row["timestamp"] for row in rows)


def test_export_empty_ledger_does_not_create_state(tmp_path, monkeypatch):
    from spendguard.cli import main

    home = tmp_path / "absent"
    monkeypatch.setenv("SPENDGUARD_HOME", str(home))
    target = tmp_path / "empty.csv"
    assert main(["export", "--csv", str(target)]) == 0
    assert len(target.read_text().splitlines()) == 1
    assert not home.exists()


def test_export_cannot_overwrite_ledger(tmp_path, monkeypatch):
    from spendguard.cli import main

    monkeypatch.setenv("SPENDGUARD_HOME", str(tmp_path))
    store.calibration_factor("bigquery")
    before = store.db_path().read_bytes()
    with pytest.raises(SystemExit) as error:
        main(["export", "--csv", str(store.db_path())])
    assert error.value.code == 2
    assert store.db_path().read_bytes() == before


def test_invalid_ledger_leaves_existing_export_untouched(tmp_path, monkeypatch):
    from spendguard.cli import main

    monkeypatch.setenv("SPENDGUARD_HOME", str(tmp_path))
    store.db_path().write_bytes(b"not SQLite")
    target = tmp_path / "spend.csv"
    target.write_text("preserve me")
    with pytest.raises(SystemExit) as error:
        main(["export", "--csv", str(target)])
    assert error.value.code == 2
    assert target.read_text() == "preserve me"
    assert not list(tmp_path.glob(".spendguard-export-*"))


def test_default_transport_is_preserved(monkeypatch):
    import sys
    from types import ModuleType
    from unittest.mock import Mock

    from spendguard.cli import main

    server = ModuleType("spendguard.server")
    server.main = Mock()
    monkeypatch.setitem(sys.modules, "spendguard.server", server)
    assert main([]) == 0
    assert main(["serve"]) == 0
    assert server.main.call_count == 2
