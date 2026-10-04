"""Offline tests: rewrite heuristics produce concrete SQL."""

from spendguard.tools.rewrite import suggest_cheaper_query


def _issues(suggestions):
    return {s["issue"] for s in suggestions}


def test_unbounded_select_gets_limit():
    out = suggest_cheaper_query("bigquery", "SELECT a FROM t")
    by_issue = {s["issue"]: s for s in out}
    assert "unbounded_select" in by_issue
    assert by_issue["unbounded_select"]["rewritten_sql"] == "SELECT a FROM t LIMIT 1000"
    assert by_issue["unbounded_select"]["template"] is False


def test_trailing_semicolon_stripped_before_limit():
    out = suggest_cheaper_query("snowflake", "SELECT a FROM t;")
    by_issue = {s["issue"]: s for s in out}
    assert by_issue["unbounded_select"]["rewritten_sql"] == "SELECT a FROM t LIMIT 1000"


def test_order_by_without_limit_flagged():
    out = suggest_cheaper_query("bigquery", "SELECT a FROM t ORDER BY a")
    assert "unbounded_sort" in _issues(out)
    sort = next(s for s in out if s["issue"] == "unbounded_sort")
    assert sort["rewritten_sql"] == "SELECT a FROM t ORDER BY a LIMIT 1000"


def test_existing_limit_is_left_alone():
    out = suggest_cheaper_query("bigquery", "SELECT a FROM t LIMIT 5")
    assert "unbounded_select" not in _issues(out)
    assert "unbounded_sort" not in _issues(out)


def test_select_star_is_template_with_guidance():
    out = suggest_cheaper_query("snowflake", "SELECT * FROM t LIMIT 10")
    by_issue = {s["issue"]: s for s in out}
    assert "select_star" in by_issue
    assert by_issue["select_star"]["template"] is True
    assert "TODO" in by_issue["select_star"]["rewritten_sql"]


def test_bigquery_partition_filter_template():
    out = suggest_cheaper_query("bigquery", "SELECT a FROM proj.ds.events")
    by_issue = {s["issue"]: s for s in out}
    assert "missing_partition_filter" in by_issue
    s = by_issue["missing_partition_filter"]
    assert s["template"] is True
    assert "_PARTITIONTIME" in s["rewritten_sql"]
    assert "WHERE" in s["rewritten_sql"]


def test_partition_rule_is_bigquery_only():
    out = suggest_cheaper_query("snowflake", "SELECT a FROM events")
    assert "missing_partition_filter" not in _issues(out)


def test_partition_rule_skipped_when_filter_present():
    sql = "SELECT a FROM proj.ds.events WHERE _PARTITIONTIME > '2026-01-01'"
    out = suggest_cheaper_query("bigquery", sql)
    assert "missing_partition_filter" not in _issues(out)


def test_cross_join_flagged():
    out = suggest_cheaper_query("databricks", "SELECT * FROM a CROSS JOIN b LIMIT 5")
    assert "cross_join" in _issues(out)
