"""Keep offline ledger exports separate from the stdio MCP transport."""

from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

from spendguard.ledger.store import db_path

FIELDS = ["estimate_id", "engine", "estimated_usd", "actual_usd", "timestamp"]


def export_csv(destination: Path) -> None:
    """Export each actual (or a pending estimate) without modifying the ledger."""
    ledger = db_path().resolve()
    if destination.resolve() in {
        ledger,
        Path(str(ledger) + "-wal"),
        Path(str(ledger) + "-shm"),
    }:
        raise ValueError("CSV destination must not overwrite the ledger")
    rows = []
    if ledger.exists():
        with closing(sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True)) as conn:
            rows = conn.execute(
                "SELECT e.id, e.engine, e.estimated_cost_usd, a.billed_cost_usd,"
                " COALESCE(a.ts, e.ts) FROM estimates e"
                " LEFT JOIN actuals a ON a.estimate_id=e.id ORDER BY e.id, a.id"
            ).fetchall()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=destination.parent,
            prefix=".spendguard-export-",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            writer = csv.writer(stream)
            writer.writerow(FIELDS)
            for row in rows:
                # Guard spreadsheet interpretation without emitting raw SQL.
                writer.writerow(
                    [
                        "'" + cell
                        if isinstance(cell, str)
                        and cell.startswith(("=", "+", "-", "@", "\t", "\r", "\n"))
                        else cell
                        for cell in row
                    ]
                )
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args == ["serve"]:
        from spendguard.server import main as serve

        serve()
        return 0
    parser = argparse.ArgumentParser(prog="spendguard")
    subparsers = parser.add_subparsers(dest="command", required=True)
    export = subparsers.add_parser("export", help="Export the local ledger")
    export.add_argument("--csv", type=Path, required=True, help="Destination CSV file")
    subparsers.add_parser("serve", help="Run the stdio MCP server (also the default)")
    options = parser.parse_args(args)
    try:
        export_csv(options.csv)
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.error(str(exc))
    return 0
