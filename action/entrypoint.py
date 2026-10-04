#!/usr/bin/env python3
"""spendguard GitHub Action entrypoint (v1: BigQuery only).

On a pull_request event: finds changed *.sql files, dry-runs each at the PR
head SHA and at the base SHA, and posts/updates a sticky PR comment with a
per-file bytes + estimated-USD table and the total cost delta.

Only stdlib + google-cloud-bigquery are used. Dry runs are free.
Honest scope note: dollar figures use the on-demand $6.25/TiB rate and do not
account for the 1 TiB/month free tier, RLS-masked tables, or remote-function
billing -- the comment says so.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.request

USD_PER_TIB = 6.25
MARKER = "<!-- spendguard-cost -->"


def gh_api(path: str, token: str, method: str = "GET", body: dict | None = None):
    url = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def get_text(url: str, token: str) -> str:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as resp:
        return resp.read().decode("utf-8", errors="replace")


def dry_run_bytes(client, sql: str, project: str) -> int | None:
    from google.cloud import bigquery

    try:
        job = client.query(
            sql,
            job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False),
        )
        return job.total_bytes_processed or 0
    except Exception as exc:
        print(f"::warning::dry-run failed, skipping cost for one file: {exc}")
        return None


def fmt_bytes(n: int | None) -> str:
    if n is None:
        return "n/a"
    for unit, div in (("TiB", 2**40), ("GiB", 2**30), ("MiB", 2**20), ("KiB", 2**10)):
        if n >= div:
            return f"{n / div:.2f} {unit}"
    return f"{n} B"


def fmt_usd(n: int | None) -> str:
    if n is None:
        return "n/a"
    return f"${n / 2**40 * USD_PER_TIB:.4f}"


def main() -> int:
    project = os.environ["INPUT_GCP_PROJECT"]
    token = os.environ["INPUT_GITHUB_TOKEN"]
    repo = os.environ["GITHUB_REPOSITORY"]
    event = json.load(open(os.environ["GITHUB_EVENT_PATH"]))
    pr_number = event["pull_request"]["number"]
    base_sha = event["pull_request"]["base"]["sha"]

    creds = os.environ["INPUT_GCP_CREDENTIALS"]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        fh.write(creds)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = fh.name

    from google.cloud import bigquery

    client = bigquery.Client(project=project)

    files = gh_api(f"/repos/{repo}/pulls/{pr_number}/files?per_page=100", token)
    sql_files = [
        f for f in files if f["filename"].endswith(".sql") and f["status"] != "removed"
    ]

    rows: list[
        tuple[str, int | None, int | None]
    ] = []  # (file, head_bytes, base_bytes)
    for f in sql_files:
        head_sql = get_text(f["raw_url"], token)
        head_bytes = dry_run_bytes(client, head_sql, project)
        base_bytes = None
        if f["status"] != "added":
            base_url = (
                f"https://raw.githubusercontent.com/{repo}/{base_sha}/{f['filename']}"
            )
            try:
                base_sql = get_text(base_url, token)
                base_bytes = dry_run_bytes(client, base_sql, project)
            except Exception as exc:
                print(f"::warning::could not dry-run base version: {exc}")
        rows.append((f["filename"], head_bytes, base_bytes))

    total_head = sum(b or 0 for _, b, _ in rows)
    total_base = sum(b or 0 for _, _, b in rows)
    delta = total_head - total_base

    lines = [
        MARKER,
        "## Ã°Å¸â€™Â° spendguard Ã¢â‚¬â€ query cost delta",
        "",
        "| file | head bytes | head est. | base est. | delta |",
        "| --- | --- | --- | --- | --- |",
    ]
    for filename, head_b, base_b in rows:
        d = None if head_b is None or base_b is None else head_b - base_b
        d_usd = "n/a" if d is None else f"${d / 2**40 * USD_PER_TIB:+.4f}"
        lines.append(
            f"| `{filename}` | {fmt_bytes(head_b)} | {fmt_usd(head_b)} | "
            f"{fmt_usd(base_b)} | {d_usd} |"
        )
    lines += [
        "",
        f"**Total delta: {fmt_usd(delta)}** ({fmt_bytes(abs(delta))} "
        f"{'more' if delta >= 0 else 'less'} scanned per run)",
        "",
        "> Estimates use BigQuery on-demand ($6.25/TiB), dry-run bytes. They do "
        "not subtract the 1 TiB/month free tier, and RLS-masked tables report "
        "0 bytes by design Ã¢â‚¬â€ $0.00 is not proof a query is free.",
    ]
    body = "\n".join(lines)

    comments = gh_api(f"/repos/{repo}/issues/{pr_number}/comments?per_page=100", token)
    existing = next((c for c in comments if MARKER in c.get("body", "")), None)
    if existing:
        gh_api(
            f"/repos/{repo}/issues/comments/{existing['id']}",
            token,
            method="PATCH",
            body={"body": body},
        )
    else:
        gh_api(
            f"/repos/{repo}/issues/{pr_number}/comments",
            token,
            method="POST",
            body={"body": body},
        )

    fail = os.environ.get("INPUT_FAIL_ON_OVER_CAP", "false").lower() == "true"
    cap = float(os.environ.get("INPUT_MAX_DELTA_USD", "10"))
    delta_usd = delta / 2**40 * USD_PER_TIB
    if fail and delta_usd > cap:
        print(f"::error::PR cost delta ${delta_usd:.4f} exceeds cap ${cap:.2f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
