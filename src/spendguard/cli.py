"""Keep offline ledger exports separate from the stdio MCP transport."""

from __future__ import annotations

import argparse
import csv
import json
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


def format_estimate(payload: dict) -> str:
    """Render an estimate_query_cost payload as plain terminal lines."""
    usd = payload["estimated_cost_usd"]
    nbytes = payload["estimated_bytes"]
    lines = [
        f"engine: {payload['engine']}",
        f"tier: {payload['accuracy_tier']}",
        f"bytes: {'n/a' if nbytes is None else nbytes}",
        f"usd: {'n/a' if usd is None else f'{usd:.6f}'} {payload['currency']}",
        f"calibration_applied: {str(payload['calibration_applied']).lower()}",
        f"estimate_id: {payload['estimate_id']}",
    ]
    if payload["caveats"]:
        lines.append("caveats:")
        lines.extend(f"  - {caveat}" for caveat in payload["caveats"])
    else:
        lines.append("caveats: none")
    return "\n".join(lines)


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
    estimate = subparsers.add_parser(
        "estimate", help="Pre-flight cost estimate (never executes the query)"
    )
    estimate.add_argument("engine", help="bigquery | snowflake | databricks")
    estimate.add_argument("sql", help="SQL statement to estimate")
    estimate.add_argument("--warehouse", help="Warehouse / billing model override")
    estimate.add_argument("--json", action="store_true", help="Print the raw JSON")
    subparsers.add_parser("serve", help="Run the stdio MCP server (also the default)")
    options = parser.parse_args(args)
    if options.command == "estimate":
        from spendguard.engines import MissingCredentialsError
        from spendguard.tools.estimate import estimate_query_cost

        try:
            payload = estimate_query_cost(options.engine, options.sql, options.warehouse)
        except (MissingCredentialsError, ValueError) as exc:
            parser.error(str(exc))
        if options.json:
            print(json.dumps(payload, indent=2))
        else:
            print(format_estimate(payload))
        return 0
    try:
        export_csv(options.csv)
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.error(str(exc))
    return 0
