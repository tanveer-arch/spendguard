# Contributing to spendguard

Thanks for considering a contribution — this project grows by people who've
watched an agent burn warehouse budget and decided to do something about it.

## Setup

```bash
git clone https://github.com/tanveer-arch/spendguard.git
cd spendguard
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Linting

```bash
ruff check
```
We use `ruff` for linting. Please ensure `ruff check` passes on the whole codebase before submitting a PR. You can also use `pre-commit` to run it automatically.

## Tests

```bash
python -m pytest
```

All pricing math, ledger calibration (EWMA), budget/anomaly logic, and rewrite
heuristics are covered by **offline** unit tests. If your change touches one of
those areas, it needs a test. Engine modules (BigQuery / Snowflake / Databricks)
require live cloud credentials, so they are exercised via live-integration
scripts, not pytest — keep them importable without credentials and raise
`MissingCredentialsError` with a helpful message when creds are absent.

## PR conventions

- One focused change per PR. Keep diffs small and reviewable.
- Add/extend a test for any behavioral change.
- Update the README's accuracy-tier table or roadmap if you change engine behavior.
- Run `python -m pytest` and paste the result in the PR description.

## Labels we use

Watch for **`good first issue`** and **`hacktoberfest`** — we keep a standing
queue of small, well-scoped tasks (new rewrite heuristics, pricing-table
updates, docs) specifically for first-time contributors. Maintainers aim to
respond to every issue and PR within 24 hours.

## What we're not

spendguard is a drop-in stdio MCP server, not a gateway or a hosted service.
PRs that add deployment infrastructure, auth servers, or billing backends are
out of scope — the whole point is that it runs wherever your agent runs.
