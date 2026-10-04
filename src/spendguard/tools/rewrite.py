"""suggest_cheaper_query tool: concrete cheaper rewrites, not just warnings.

Pure text heuristics — no warehouse access needed. Each suggestion carries an
optional `rewritten_sql`: real SQL the agent can run as-is (LIMIT injection)
or a clearly-marked template where a schema-specific choice is required
(column lists, partition columns), in which case `template` is True and the
detail names exactly what the agent must fill in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_LIMIT = 1000


@dataclass
class Suggestion:
    issue: str
    detail: str
    rewritten_sql: str | None = None
    template: bool = False

    def to_dict(self) -> dict:
        return {
            "issue": self.issue,
            "detail": self.detail,
            "rewritten_sql": self.rewritten_sql,
            "template": self.template,
        }


def _strip_trailing_semicolon(sql: str) -> str:
    return sql.rstrip().removesuffix(";").rstrip()


def _has_limit(sql: str) -> bool:
    return re.search(r"\bLIMIT\s+\d+", sql, re.IGNORECASE) is not None


def _is_select(sql: str) -> bool:
    cleaned = re.sub(r"(--.*?$|/\*.*?\*/)", "", sql, flags=re.M | re.S).strip()
    return cleaned[:6].upper() == "SELECT" or cleaned[:1] == "("


def _ensure_limit(sql: str, limit: int = DEFAULT_LIMIT) -> str:
    """Append LIMIT when missing. `(SELECT …) LIMIT n` is valid on all 3 engines."""
    return f"{_strip_trailing_semicolon(sql)} LIMIT {limit}"


def _rule_unbounded_select(sql: str) -> Suggestion | None:
    if _is_select(sql) and not _has_limit(sql):
        return Suggestion(
            issue="unbounded_select",
            detail=(
                "No LIMIT: the warehouse materializes and ships the full result "
                "set. Bounding it is the single cheapest optimization."
            ),
            rewritten_sql=_ensure_limit(sql),
        )
    return None


def _rule_unbounded_sort(sql: str) -> Suggestion | None:
    if (
        _is_select(sql)
        and re.search(r"\bORDER\s+BY\b", sql, re.IGNORECASE)
        and not _has_limit(sql)
    ):
        return Suggestion(
            issue="unbounded_sort",
            detail=(
                "ORDER BY without LIMIT forces a full sort of every result row. "
                "A LIMIT lets the engine use a top-N sort instead."
            ),
            rewritten_sql=_ensure_limit(sql),
        )
    return None


def _rule_select_star(sql: str) -> Suggestion | None:
    if re.search(r"^\s*SELECT\s+\*\s", sql, re.IGNORECASE | re.M):
        return Suggestion(
            issue="select_star",
            detail=(
                "SELECT * scans and ships every column; on columnar warehouses "
                "you pay for bytes of columns you never use. Replace * with the "
                "columns the task actually needs."
            ),
            rewritten_sql=re.sub(
                r"(^\s*SELECT\s+)\*\s",
                r"\1/* TODO: list only needed columns, e.g. id, created_at */ ",
                sql,
                count=1,
                flags=re.IGNORECASE | re.M,
            ),
            template=True,
        )
    return None


def _rule_bigquery_partition_filter(sql: str) -> Suggestion | None:
    """BigQuery: queries without a partition filter scan whole tables."""
    if re.search(
        r"(_PARTITIONTIME|_PARTITIONDATE|\b_PARTITION\b)", sql, re.IGNORECASE
    ):
        return None
    m = re.search(r"\bFROM\s+(`?[\w.\-]+`?)", sql, re.IGNORECASE)
    if not m:
        return None
    table = m.group(1)
    filt = "_PARTITIONTIME >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)"
    if re.search(r"\bWHERE\b", sql, re.IGNORECASE):
        rewritten = re.sub(
            r"\bWHERE\b", f"WHERE {filt} AND", sql, count=1, flags=re.IGNORECASE
        )
    else:
        rewritten = (
            sql[: m.end()] + f" WHERE {filt}" + sql[m.end():]
        )
    return Suggestion(
        issue="missing_partition_filter",
        detail=(
            f"No partition filter on {table}: BigQuery scans every partition. "
            "Adjust the column name (_PARTITIONTIME vs your own date column) "
            "and the window to the task."
        ),
        rewritten_sql=_strip_trailing_semicolon(rewritten),
        template=True,
    )


def _rule_cross_join(sql: str) -> Suggestion | None:
    if re.search(r"\bCROSS\s+JOIN\b", sql, re.IGNORECASE):
        return Suggestion(
            issue="cross_join",
            detail=(
                "CROSS JOIN explodes row counts multiplicatively — usually an "
                "accident or a missing join condition. Rewrite as an INNER JOIN "
                "with an ON clause, or confirm the cartesian product is intended."
            ),
        )
    return None


def suggest_cheaper_query(engine: str, sql: str) -> list[dict]:
    """Return concrete rewrite suggestions for an expensive-looking query."""
    engine = engine.strip().lower()
    rules = [
        _rule_unbounded_select,
        _rule_unbounded_sort,
        _rule_select_star,
        _rule_cross_join,
    ]
    if engine == "bigquery":
        rules.append(_rule_bigquery_partition_filter)
    suggestions = [s for rule in rules for s in [rule(sql)] if s is not None]
    return [s.to_dict() for s in suggestions]
