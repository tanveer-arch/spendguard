# spendguard 💰

![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)
![Hacktoberfest](https://img.shields.io/badge/Hacktoberfest-opted--in-orange.svg)

**Your AI agent has a company credit card and no spending limit. spendguard is the bouncer.**

Give an agent a "run SQL" tool on BigQuery or Snowflake and it will happily `SELECT *` a billion-row table and burn $4 before you've finished your coffee. Nobody watches the meter. spendguard is a drop-in MCP server that sits between your agent and the warehouse: it previews the dollar cost *before* every query, enforces budgets, learns how accurate its estimates are, and suggests cheaper rewrites when a query blows the budget.

Not a previewer — a **spend governor**.

## 30-second start

```bash
uvx spendguard-mcp
# or
pipx install spendguard-mcp
`
*(Note: PyPI release (uvx spendguard-mcp) coming with v0.1.0)*
```

Add to your MCP client (Claude Code, Cursor, Codex, Copilot — see `examples/.mcp.json.example`):

```json
{ "mcpServers": { "spendguard": { "command": "uvx", "args": ["spendguard-mcp"],
  "env": { "BIGQUERY_PROJECT": "my-project",
            "GOOGLE_APPLICATION_CREDENTIALS": "/path/to/sa.json" } } } }
```

Then ask your agent:

> *"Using spendguard, how much will this query cost before you run it?"*

```jsonc
// estimate_query_cost("bigquery", "SELECT * FROM proj.ds.events_2025")
{
  "accuracy_tier": "PRECISE",
  "estimated_bytes": 1409286144,
  "estimated_cost_usd": 0.0081,
  "caveats": []
}

// run_query_bounded("bigquery", "SELECT * FROM ...", "max_estimated_cost_usd": 5.0)
{ "status": "refused", "reason": "over_call_cap",
  "detail": "Estimated $12.40 exceeds your per-call cap $5.00.",
  "suggestion": "Call suggest_cheaper_query with this SQL..." }

// spend_report()
{ "bigquery": { "actual_usd": 3.21, "queries": 41 }, ... }
```

## The tools

| Tool | What it does |
|---|---|
| `describe_engine_capabilities` | What each engine can/can't tell you — the honesty contract, first |
| `estimate_query_cost` | Free pre-flight estimate, calibrated from your ledger history |
| `run_query_bounded` | Estimate → budget gate → execute → reconcile actual billed cost |
| `spend_report` | Reconciled spend per engine + calibration state |
| `set_budget` | Persist a daily/session cap or confirm-above threshold |
| `suggest_cheaper_query` | Concrete rewrites: LIMIT injection, partition filters, SELECT \* guidance |

## Accuracy tiers — we tell you how much to trust the number

| Engine | Tier | How |
|---|---|---|
| BigQuery | **PRECISE** | Free dry run → exact bytes scanned × $6.25/TiB |
| Snowflake | **UPPER_BOUND** | `EXPLAIN USING JSON` plan → largest byte figure as bound; dollars assume one 60s minimum billing window |
| Databricks | **HEURISTIC** | No dry-run API exists — warehouse size × plan-shape runtime × $/DBU, then **calibrated against `system.billing.usage` actuals** over time |

Every estimate carries its tier and caveats. BigQuery RLS-masked tables report 0 bytes *by design* — we flag it instead of calling it free. Remote-function / `ML.GENERATE_TEXT` billing is excluded and flagged. Capacity-billed projects get bytes only, no fake dollars.

## What makes it different

- **A ledger with a memory.** Every estimate is stored; actuals are reconciled post-execution (`INFORMATION_SCHEMA.JOBS`, Snowflake query history, `system.billing.usage`). The per-engine calibration factor (EWMA, α=0.3) makes heuristic estimates converge on *your* reality.
- **Budgets with teeth.** Daily/session caps, anomaly detection (flags queries >40× your rolling median), and a human-confirm flow: over-threshold queries return a single-use 5-minute token the agent must hand back.
- **It fixes, not just refuses.** Over-budget queries get concrete rewrites, not error messages.
- **No gateway, no SaaS, no new infrastructure.** One stdio process, SQLite ledger at `~/.spendguard/`. It runs wherever your agent runs.

## GitHub Action: cost-delta on every dbt PR

`action/` is a composite action for dbt/SQL repos: it dry-runs every changed `*.sql` file at head and base SHAs and posts a sticky PR comment with per-file bytes, estimated USD, and the total delta — optionally failing the check over a budget. v1 is BigQuery-only, and the comment says so.

```yaml
- uses: tanveer-arch/spendguard/action@v1
  with:
    gcp_project: my-project
    gcp_credentials: ${{ secrets.GCP_SA_KEY }}
    fail_on_over_cap: true
    max_delta_usd: 10
```

## Roadmap

- [ ] Databricks `fetch_actual_cost` wiring against a live workspace (`system.billing.usage` join)
- [ ] Snowflake reconciliation via `ACCOUNT_USAGE.QUERY_HISTORY`
- [ ] Snowflake key-pair auth path (JWT)
- [ ] PR-comment action for Snowflake/Databricks (query-plan based)
- [ ] Per-developer attribution for team spend reports

## Contributing

PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). We keep a standing queue of `good first issue` / `hacktoberfest` tasks and aim to respond within 24 hours.

## License

MIT — see [LICENSE](LICENSE).
