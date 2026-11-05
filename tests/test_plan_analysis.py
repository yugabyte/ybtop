from __future__ import annotations

import json
from typing import Any

import pytest

from ybtop import diagnose, live
from ybtop.plan_analysis import (
    analyze_indexes,
    extract_params,
    filter_columns,
    normalize_indexdef,
    plan_findings,
    plan_instability_finding,
    scan_ratio,
    selectivity,
    statement_findings,
    statement_flags,
    suggest_index,
    summarize_plan,
    wait_findings,
)


def _plan(*nodes: dict[str, Any]) -> str:
    """yb_pg_stat_plans-style JSON for a chain of nodes (first is the root)."""
    root: dict[str, Any] = {}
    cur = root
    for i, n in enumerate(nodes):
        cur.update(n)
        if i < len(nodes) - 1:
            cur["Plans"] = [{}]
            cur = cur["Plans"][0]
    return json.dumps([{"Plan": root}])


SEQ = _plan({"Node Type": "Seq Scan", "Relation Name": "orders", "Storage Filter": "(note = $1)"})


# Filter parsing
@pytest.mark.parametrize(
    "expr,expected",
    [
        ("(note = '?'::text)", {"equality": ["note"]}),
        ("((customer_id = 5) AND (created_at > now()))", {"equality": ["customer_id"], "range": ["created_at"]}),
        ("(lower((email)::text) = 'x'::text)", {"wrapped": ["email"], "wrapped_exprs": ["lower(email)"]}),
        ("((id)::text = $1)", {"cast": ["id"]}),
        ("(x IS NULL)", {"equality": ["x"]}),
        ("(a = ANY ('{1,2}'::integer[]))", {"equality": ["a"]}),
    ],
)
def test_filter_columns(expr: str, expected: dict[str, list[str]]) -> None:
    got = {k: v for k, v in filter_columns(expr).items() if v}
    assert got == expected


def test_suggest_index_shapes() -> None:
    assert suggest_index("public.t", "(a = 1)")["ddl"] == "CREATE INDEX t_a_idx ON public.t (a);"
    assert suggest_index("t", "((a = 1) AND (b > 2))")["ddl"] == "CREATE INDEX t_a_b_idx ON t (a, b ASC);"
    assert suggest_index("t", "(b > 2)")["ddl"] == "CREATE INDEX t_b_idx ON t (b ASC);"  # range needs ASC
    assert suggest_index("t", "(lower(s) = $1)")["ddl"] == "CREATE INDEX t_lower_s_idx ON t (lower(s));"
    assert suggest_index("t", "((id)::text = $1)") is None  # a cast cannot be fixed by an index
    assert suggest_index("t", None) is None


# Plans
def test_summarize_plan_finds_full_and_index_scans() -> None:
    s = summarize_plan(SEQ)
    assert s and [x["relation"] for x in s["full_scans"]] == ["orders"]
    idx = summarize_plan(_plan({"Node Type": "Index Scan", "Relation Name": "o", "Index Name": "i", "Index Cond": "(a = $1)"}))
    assert idx and not idx["full_scans"] and idx["scans"][0]["index"] == "i"
    assert summarize_plan("not json") is None and summarize_plan(None) is None


def test_full_scan_finding_is_high_with_candidate_index() -> None:
    f = plan_findings(summarize_plan(SEQ), mean_ms=200)
    assert f[0]["category"] == "full_scan" and f[0]["severity"] == "high"
    assert f[0]["candidate_index"]["ddl"].startswith("CREATE INDEX orders_note_idx")


def test_unfiltered_full_scan_is_only_informational() -> None:
    p = summarize_plan(_plan({"Node Type": "Seq Scan", "Relation Name": "lookup"}))
    assert plan_findings(p, mean_ms=200)[0]["severity"] == "info"


def test_index_scan_with_leftover_filter_reports_coverage() -> None:
    p = summarize_plan(_plan({"Node Type": "Index Scan", "Relation Name": "o", "Index Name": "i",
                              "Index Cond": "(a = $1)", "Storage Filter": "(b = $2)"}))
    assert [f["category"] for f in plan_findings(p, mean_ms=50)] == ["index_coverage"]


def test_never_analyzed_table_is_flagged() -> None:
    f = plan_findings(summarize_plan(SEQ), mean_ms=1, table_info={"orders": {"never_analyzed": True, "indexes": []}})
    assert "statistics" in {x["category"] for x in f}


def test_plan_instability_needs_a_big_gap() -> None:
    fast = {"planid": "1", "avg_exec_ms": 1.0, "calls": 10}
    assert plan_instability_finding([fast, {"planid": "2", "avg_exec_ms": 2.0, "calls": 10}]) is None
    assert plan_instability_finding([fast, {"planid": "2", "avg_exec_ms": 30.0, "calls": 10}])["category"] == "plan_instability"


# Statements
def test_scan_ratio_uses_client_rows_so_pushdown_failures_show() -> None:
    # A cast blocks pushdown, so DocDB returns every row it scans
    r = {"docdb_rows_scanned_per_call": 345_920.0, "docdb_rows_returned_per_call": 345_920.0, "rows_per_call": 1.0}
    assert scan_ratio(r) == 345_920.0
    assert "scans_far_more_than_returned" in statement_flags(r)
    assert scan_ratio({"docdb_rows_scanned_per_call": 10.0, "rows_per_call": 1.0}) is None  # too small to matter
    assert statement_flags({"docdb_rows_scanned_per_call": 5000.0, "rows_per_call": 4000.0}) == []  # large legitimate read


def test_statement_findings_cover_retries_catalog_tail() -> None:
    stats = {"calls": 100, "mean_ms": 40.0, "conflict_retries_per_call": 0.5, "read_restart_retries_per_call": 0.2,
             "catalog_wait_time_per_call": 20.0, "rows_per_call": 50_000.0}
    cats = {f["category"] for f in statement_findings(stats, {"p50": 5.0, "p99": 500.0})}
    assert cats == {"contention", "catalog", "result_size", "latency_variance"}
    assert statement_findings({"calls": 1, "mean_ms": 1.0}, None) == []


def test_wait_findings() -> None:
    prof = {"samples": 100, "by_wait_event": [
        {"wait_event": "ConflictResolution_WaitOnConflictingTxns", "pct": 40.0},
        {"wait_event": "CatalogRead", "pct": 12.0}]}
    assert {f["category"] for f in wait_findings(prof)} == {"lock_contention", "catalog"}
    assert wait_findings({"samples": 0}) == [] and wait_findings(None) == []


# Index hygiene
def _ix(name: str, cols: list[str], scans: int, **kw: Any) -> dict[str, Any]:
    r = dict(table="public.t", index=name, valid=True, ready=True, live=True, unique=False, primary=False,
             cols=cols, opclasses=["c"] * len(cols), options=["4"] + ["0"] * (len(cols) - 1), predicate="",
             has_expr=False, definition=f"CREATE INDEX {name} ON public.t ({','.join(cols)})",
             definition_norm=f"CREATE INDEX ON public.t ({','.join(cols)})", bytes=1000, scans=scans)
    r.update(kw)
    return r


def _names(res: dict[str, list[dict[str, Any]]]) -> dict[str, list[str]]:
    return {k: [x["index"] for x in v] for k, v in res.items()}


def test_index_recommendations_never_drop_every_path() -> None:
    """Regression: dup pair + a wider index must not all be reported droppable."""
    res = analyze_indexes([_ix("cust", ["2"], 394), _ix("cust2", ["2"], 16645),
                           _ix("cust_stat", ["2", "3"], 0), _ix("status", ["3"], 0)])
    names = _names(res)
    assert names == {"broken": [], "duplicate": ["cust2"], "redundant": ["cust"], "unused": ["status"]}
    dropped = set(names["duplicate"] + names["redundant"] + names["unused"])
    assert "cust_stat" not in dropped  # the keeper inherits scans


def test_broken_index_reported_and_excluded_from_other_checks() -> None:
    res = analyze_indexes([_ix("ok", ["2"], 5), _ix("bad", ["2"], 0, valid=False)])
    assert _names(res) == {"broken": ["bad"], "duplicate": [], "redundant": [], "unused": []}


def test_unique_primary_and_expression_indexes_are_left_alone() -> None:
    res = analyze_indexes([_ix("pk", ["1"], 0, primary=True, unique=True), _ix("u", ["2"], 0, unique=True),
                           _ix("e", ["0"], 0, has_expr=True, definition_norm="CREATE INDEX ON t (lower(x))")])
    assert _names(res)["unused"] == ["e"]  # only the expression index may be unused


def test_different_sharding_is_not_a_prefix() -> None:
    hash_idx = _ix("h", ["2"], 5)
    asc_wide = _ix("w", ["2", "3"], 5, options=["0", "0"])  # first column ASC vs HASH
    assert _names(analyze_indexes([hash_idx, asc_wide]))["redundant"] == []


def test_helpers() -> None:
    assert normalize_indexdef("CREATE UNIQUE INDEX foo ON public.t USING lsm (a HASH)") == "CREATE UNIQUE INDEX  ON public.t USING lsm (a HASH)"
    assert extract_params("select $1, $3, $2") == 3 and extract_params("select 1") == 0


# Diagnose helpers
def test_single_statement_validation() -> None:
    assert diagnose._check_single_statement(" select 1; ") == "select 1"
    assert diagnose._check_single_statement("(select 1)") == "(select 1)"
    for bad in ("", "select 1; drop table t", "drop table t", "create index i on t(a)", "vacuum"):
        with pytest.raises(live.LiveError):
            diagnose._check_single_statement(bad)
    assert diagnose._is_select("WITH x AS (select 1) select * from x")
    assert not diagnose._is_select("update t set a = 1")


def test_merge_plans_combines_nodes_weighted() -> None:
    row = lambda calls, avg, mx, params: {"query_id": "1", "planid": "9", "calls": calls, "avg_exec_ms": avg,  # noqa: E731
                                          "max_exec_ms": mx, "max_exec_params": params, "query": None}
    merged = diagnose._merge_plans({"a": [row(10, 10.0, 50.0, "$1 = 'a'")], "b": [row(30, 20.0, 90.0, "$1 = 'b'")]})
    assert len(merged) == 1
    m = merged[0]
    assert m["calls"] == 40 and m["avg_exec_ms"] == pytest.approx(17.5)
    assert m["max_exec_ms"] == 90.0 and m["max_exec_params"] == "$1 = 'b'"


def test_broad_scan_is_not_reported_as_a_missing_index() -> None:
    """Regression: a filter returning a third of the table is not helped by an index."""
    p = summarize_plan(_plan({"Node Type": "Seq Scan", "Relation Name": "orders", "Storage Filter": "(lower(status) = $1)"}))
    broad = plan_findings(p, mean_ms=270, scanned_per_call=200_000, rows_per_call=66_667)
    assert [f["category"] for f in broad] == ["broad_scan"] and broad[0]["severity"] == "low"
    assert "candidate_index" not in broad[0]
    selective = plan_findings(p, mean_ms=270, scanned_per_call=200_000, rows_per_call=200)
    assert selective[0]["category"] == "full_scan" and selective[0]["candidate_index"]
    assert plan_findings(p, mean_ms=270)[0]["category"] == "full_scan"  # unknown selectivity keeps the advice


def test_selectivity() -> None:
    assert selectivity(50.0, 200.0) == 0.25 and selectivity(500.0, 200.0) == 1.0
    assert selectivity(None, 10.0) is None and selectivity(5.0, 0) is None
