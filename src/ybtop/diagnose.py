"""Slow-query and cluster-bottleneck diagnosis on top of :class:`ybtop.live.LiveContext`.

Answers "why is this query slow, what is it costing the cluster, and what should I change?":
ranking by impact, plans (recorded + EXPLAIN), full-scan and index-hygiene analysis, and
cluster-level wait / hotspot triage. Rules live in :mod:`ybtop.plan_analysis`; this module
gathers the evidence from the database and assembles the report.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any, Optional

import psycopg

from ybtop import queries as Q
from ybtop.db import SQL_SERVICE_TAG, fetch_all
from ybtop.live import LiveContext, LiveError, _clamp, _query_id_arg, _r, _trunc
from ybtop.merge import merge_pg_stat_statements
from ybtop.plan_analysis import (
    BROAD_SELECTIVITY,
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

_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}
_EXPLAINABLE = ("select", "with", "insert", "update", "delete", "merge", "values")
_SORTS = ("total_time", "mean_time", "calls", "rows_scanned", "scan_ratio")


def _sort_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(findings, key=lambda f: _SEVERITY_ORDER.get(f["severity"], 9))


def _check_single_statement(sql: str) -> str:
    text = (sql or "").strip().rstrip(";").strip()
    if not text:
        raise LiveError("sql is empty")
    if ";" in text:
        raise LiveError("only a single statement is accepted (found ';')")
    first = re.split(r"\W+", text.lstrip("( \n\t"), maxsplit=1)[0].lower()
    if first not in _EXPLAINABLE:
        raise LiveError(f"cannot EXPLAIN a statement starting with {first!r}")
    return text


def _first(row: Any) -> Any:
    """First column of a row (rows are dicts here: the connection uses ``dict_row``)."""
    if row is None:
        return None
    return next(iter(row.values())) if isinstance(row, dict) else row[0]


def _is_select(sql: str) -> bool:
    return re.split(r"\W+", sql.lstrip("( \n\t"), maxsplit=1)[0].lower() in ("select", "with", "values")


def _merge_plans(per_node: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """yb_pg_stat_plans is a per-node cache: merge rows with the same (query_id, planid)."""
    acc: dict[tuple[str, str], dict[str, Any]] = {}
    for rows in per_node.values():
        for r in rows:
            k = (r["query_id"], r["planid"])
            a = acc.get(k)
            if a is None:
                acc[k] = {**r, "_sum": r["avg_exec_ms"] * r["calls"]}
                continue
            a["calls"] += r["calls"]
            a["_sum"] += r["avg_exec_ms"] * r["calls"]
            if r["max_exec_ms"] > a["max_exec_ms"]:
                a["max_exec_ms"], a["max_exec_params"] = r["max_exec_ms"], r["max_exec_params"]
            if not a.get("query") and r.get("query"):
                a["query"] = r["query"]
    out = []
    for a in acc.values():
        a["avg_exec_ms"] = a.pop("_sum") / a["calls"] if a["calls"] else a["avg_exec_ms"]
        out.append(a)
    out.sort(key=lambda p: p["avg_exec_ms"] * p["calls"], reverse=True)
    return out


class Diagnostics:
    def __init__(self, live: LiveContext) -> None:
        self.live = live

    # Shared evidence
    def _statements(self, database: Optional[str] = None) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """Cluster-merged ``pg_stat_statements`` (excluding ybtop's own), optionally for one database."""
        base, _ = self.live.caps()
        results, errors = self.live._fan_out(lambda conn, _n: Q.pg_stat_statements_raw(conn, base))
        chunks = [
            [r for r in rows if not (r.get("query") or "").startswith(SQL_SERVICE_TAG)
             and (not database or r.get("dbname") == database)]
            for rows in results.values()
        ]
        if not any(chunks):
            return [], errors
        merged = merge_pg_stat_statements(chunks, include_docdb_per_call=True, include_rows_total=True)
        return merged, errors

    def _ash_by_query(self, window_seconds: int) -> dict[str, dict[str, float]]:
        """query_id → {pct, avg_active_sessions} over the window; {} when ASH is unavailable."""
        try:
            prof = self.live._wait_profile(
                window_seconds, query_id=None, pid=None, nodes=self.live.nodes(), describe=False, top=5000
            )
        except Exception:  # noqa: BLE001, ASH is optional
            return {}
        return {
            q["query_id"]: {"pct": q["pct"], "avg_active_sessions": round(q["samples"] / window_seconds, 3)}
            for q in prof.get("by_query_id", [])
        }

    def _recorded_plans(self, query_id: Optional[int] = None, *, seq_scans_only: bool = False, limit: int = 500) -> list[dict[str, Any]]:
        _, live = self.live.caps()
        if not live.stat_plans:
            return []
        results, _ = self.live._fan_out(
            lambda conn, _n: Q.stat_plans_rows(conn, query_id=query_id, seq_scans_only=seq_scans_only, limit=limit)
        )
        return _merge_plans(results)

    def _index_report(
        self, database: Optional[str], schema: Optional[str] = None, table: Optional[str] = None
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Catalog rows joined with scan counters summed over every node that answered."""
        nodes = self.live.nodes()
        with self.live._conn(database=database) as conn:
            catalog = Q.index_catalog_rows(conn, schema=schema, table=table)
        usage, errors = self.live._fan_out(lambda conn, _n: Q.index_usage_rows(conn), nodes, database=database)
        scans: Counter[int] = Counter()
        reads: Counter[int] = Counter()
        for rows in usage.values():
            for u in rows:
                scans[u["indexrelid"]] += u["scans"]
                reads[u["indexrelid"]] += u["tup_read"]
        rows = []
        for c in catalog:
            rows.append(
                {
                    **c,
                    "definition_norm": normalize_indexdef(c["definition"]),
                    "scans": scans.get(c["indexrelid"], 0),
                    "tup_read": reads.get(c["indexrelid"], 0),
                }
            )
        meta = {
            "nodes_reporting": len(usage),
            "nodes_total": len(nodes),
            "node_errors": errors,
        }
        return rows, meta

    # top_queries
    def top_queries(
        self,
        *,
        sort_by: str = "total_time",
        limit: int = 20,
        min_calls: int = 1,
        database: Optional[str] = None,
        ash_window_seconds: int = 300,
        query_chars: int = 300,
    ) -> dict[str, Any]:
        """Statements ranked by what they cost the cluster, with red flags."""
        if sort_by not in _SORTS:
            raise LiveError(f"sort_by must be one of {', '.join(_SORTS)}")
        limit = _clamp(limit, 1, 200, "limit")
        min_calls = _clamp(min_calls, 1, 10**9, "min_calls")
        ash_window_seconds = _clamp(ash_window_seconds, 5, 3600, "ash_window_seconds")
        database = self.live.check_database(database)
        merged, errors = self._statements(database)
        total_ms = sum(m["total_ms"] for m in merged) or 1.0
        ash = self._ash_by_query(ash_window_seconds)

        def scanned_total(m: dict[str, Any]) -> float:
            return (m.get("docdb_rows_scanned_per_call") or 0) * m["calls"]

        def ratio(m: dict[str, Any]) -> float:
            return scan_ratio(m) or 0.0

        keys = {
            "total_time": lambda m: m["total_ms"],
            "mean_time": lambda m: m["mean_ms"],
            "calls": lambda m: m["calls"],
            "rows_scanned": scanned_total,
            "scan_ratio": ratio,
        }
        rows = [m for m in merged if m["calls"] >= min_calls]
        rows.sort(key=keys[sort_by], reverse=True)
        out = []
        for m in rows[:limit]:
            a = ash.get(m["queryid"], {})
            out.append(
                {
                    "query_id": m["queryid"],
                    "database": m.get("dbname"),
                    "query": _trunc(m.get("query"), query_chars),
                    "calls": m["calls"],
                    "total_ms": m["total_ms"],
                    "mean_ms": m["mean_ms"],
                    "share_of_total_time_pct": round(100.0 * m["total_ms"] / total_ms, 2),
                    "ash_share_pct": a.get("pct"),
                    "avg_active_sessions": a.get("avg_active_sessions"),
                    "rows_per_call": m.get("rows_per_call"),
                    "docdb_rows_scanned_per_call": m.get("docdb_rows_scanned_per_call"),
                    "docdb_rows_returned_per_call": m.get("docdb_rows_returned_per_call"),
                    "scan_ratio": _r(ratio(m)) or None,
                    "conflict_retries_per_call": m.get("conflict_retries_per_call"),
                    "read_restart_retries_per_call": m.get("read_restart_retries_per_call"),
                    "catalog_wait_ms_per_call": m.get("catalog_wait_time_per_call"),
                    "flags": statement_flags(m),
                }
            )
        return {
            "sort_by": sort_by,
            "statements_considered": len(merged),
            "queries": out,
            "errors": errors,
            "notes": [
                "share_of_total_time_pct: this statement's share of ALL statement time since pg_stat_statements was last reset.",
                f"ash_share_pct / avg_active_sessions: share of active-session samples over the last {ash_window_seconds}s "
                "(what it costs the cluster right now; null when it was not active in the window).",
                "scan_ratio: DocDB rows scanned per row handed to the client. Values far above 1 mean rows are read and thrown away "
                "(missing/unsuitable index, or a filter that cannot be pushed into the index).",
                "Next: diagnose_query(query_id) for the why and a recommendation.",
            ],
        }

    # find_full_scans
    def find_full_scans(self, *, limit: int = 20, min_total_ms: float = 0.0, query_chars: int = 300) -> dict[str, Any]:
        """Statements whose recorded plan contains a sequential scan, ranked by total time."""
        limit = _clamp(limit, 1, 200, "limit")
        _, live = self.live.caps()
        if not live.stat_plans:
            raise LiveError(
                "yb_pg_stat_plans is not available on this YugabyteDB version; use top_queries(sort_by='scan_ratio') "
                "to find statements that scan far more rows than they return"
            )
        plans = self._recorded_plans(seq_scans_only=True, limit=1000)
        stmt_rows, _ = self._statements(None)
        stats_of = {m["queryid"]: m for m in stmt_rows}
        agg: dict[tuple[str, str], dict[str, Any]] = {}
        for p in plans:
            if (p.get("query") or "").startswith(SQL_SERVICE_TAG):
                continue
            summary = summarize_plan(p["plan"])
            if not summary:
                continue
            for s in summary["full_scans"]:
                rel = s["relation"].split(".")[-1]
                if rel.startswith("pg_") or rel.startswith("sql_"):  # system tables
                    continue
                expr = s.get("storage_filter") or s.get("ysql_filter")
                key = (p["query_id"], s["relation"])
                a = agg.setdefault(
                    key,
                    {"query_id": p["query_id"], "relation": s["relation"], "database": p.get("dbname"),
                     "query": p.get("query"), "calls": 0, "total": 0.0, "max_ms": 0.0, "plan_ids": [], "filter": None},
                )
                a["calls"] += p["calls"]
                a["total"] += p["avg_exec_ms"] * p["calls"]
                a["max_ms"] = max(a["max_ms"], p["max_exec_ms"])
                a["plan_ids"].append(p["planid"])
                # Prefer the filter that keeps its $1 markers
                if expr and (a["filter"] is None or "$" in expr):
                    a["filter"] = expr
        out: list[dict[str, Any]] = []
        for a in agg.values():
            if a["total"] < min_total_ms:
                continue
            avg = a["total"] / a["calls"] if a["calls"] else 0.0
            cand = suggest_index(a["relation"], a["filter"])
            fcols = filter_columns(a["filter"])
            st = stats_of.get(a["query_id"]) or {}
            sel = selectivity(st.get("rows_per_call"), st.get("docdb_rows_scanned_per_call"))
            broad = sel is not None and sel >= BROAD_SELECTIVITY
            row: dict[str, Any] = {
                "query_id": a["query_id"],
                "database": a["database"],
                "relation": a["relation"],
                "filter": a["filter"],
                "calls": a["calls"],
                "avg_exec_ms": _r(avg, 2),
                "max_exec_ms": _r(a["max_ms"], 2),
                "total_exec_ms": _r(a["total"], 1),
                "query": _trunc(a["query"], query_chars),
                "candidate_index": None if broad else (cand["ddl"] if cand else None),
                "returns_share_of_table": round(sel, 3) if sel is not None else None,
                "severity": "low" if broad
                else "high" if a["filter"] and avg >= 5 else "medium" if a["filter"] else "info",
            }
            if broad:
                row["problem"] = (
                    f"returns ~{sel:.0%} of the table, so a scan is the right plan and an index would not help; "
                    "narrow the predicate, paginate, or select fewer columns"
                )
            if broad:
                pass
            elif fcols["wrapped_exprs"]:
                row["problem"] = (
                    f"filter applies {', '.join(fcols['wrapped_exprs'])}: a plain index on the column is unusable; "
                    "compare the bare column or use the expression index above"
                )
            elif fcols["cast"]:
                row["problem"] = (
                    f"{', '.join(fcols['cast'])} is implicitly cast for the comparison (parameter/column type mismatch), "
                    "which defeats any index: bind the parameter with the column's own type"
                )
            out.append(row)
        out.sort(key=lambda r: r["total_exec_ms"], reverse=True)
        return {
            "full_scans": out[:limit],
            "total_found": len(out),
            "notes": [
                "A full scan with no filter is often intentional (small table, export). With a filter, it usually means a "
                "missing or unusable index. candidate_index is a starting point: verify with explain_query before creating it.",
                "Plans come from yb_pg_stat_plans, which is a bounded per-node cache; rarely-run statements may be absent.",
            ],
        }

    # explain_query
    def explain_query(
        self,
        *,
        query_id: Optional[str] = None,
        sql: Optional[str] = None,
        database: Optional[str] = None,
        analyze: bool = False,
    ) -> dict[str, Any]:
        """The plan for a statement: by ``query_id`` (generic plan + recorded real plans) or for given SQL."""
        if (query_id is None) == (sql is None):
            raise LiveError("provide exactly one of query_id or sql")
        database = self.live.check_database(database)
        notes: list[str] = []
        recorded: list[dict[str, Any]] = []
        qid_int: Optional[int] = None
        if query_id is not None:
            qid_int = _query_id_arg(query_id)
            merged, _ = self._statements(database)
            cands = [m for m in merged if m["queryid"] == str(qid_int)]
            if not cands:
                raise LiveError(f"query_id {qid_int} not found in pg_stat_statements (never run here, evicted or reset)")
            best = max(cands, key=lambda m: m["total_ms"])
            sql = best.get("query") or ""
            database = database or best.get("dbname")
            if len(cands) > 1:
                notes.append(f"query_id exists in several databases ({', '.join(str(c.get('dbname')) for c in cands)}); used {database}.")
            recorded = self._recorded_plans(qid_int, limit=20)
        text = _check_single_statement(sql or "")
        nparams = extract_params(text)
        if analyze and not _is_select(text):
            raise LiveError("analyze=true executes the statement, so it is only allowed for SELECT/WITH/VALUES")
        if analyze and nparams:
            raise LiveError("analyze=true needs literal values: pass sql with the $N placeholders replaced")

        with self.live._conn(database=database) as conn:
            mode = "plan"
            if nparams:
                mode = "generic_plan"
                args = ", ".join(["NULL"] * nparams)
                try:
                    conn.execute("PREPARE ybtop_explain AS " + text)
                except psycopg.Error as exc:
                    raise LiveError(
                        "could not prepare the statement to plan it without parameter values "
                        f"({str(exc).splitlines()[0]}). Call explain_query with sql= and literal values instead."
                    ) from None
                conn.execute("SET plan_cache_mode = force_generic_plan")
                target = f"EXECUTE ybtop_explain({args})"
                notes.append("Generic plan (parameters unknown). The recorded plans below reflect real parameter values.")
            else:
                target = text
            json_plan = conn.execute("EXPLAIN (FORMAT JSON) " + target).fetchone()
            summary = summarize_plan(_first(json_plan))
            opts = "ANALYZE, DIST, FORMAT TEXT" if analyze else "FORMAT TEXT"
            if analyze:
                mode = "analyze"
                notes.append("ANALYZE executed the SELECT in a read-only session (statement_timeout applies).")
            plan_text = "\n".join(str(_first(r)) for r in conn.execute(f"EXPLAIN ({opts}) " + target).fetchall())

        out: dict[str, Any] = {
            "query_id": str(qid_int) if qid_int is not None else None,
            "database": database,
            "mode": mode,
            "sql": _trunc(text, 4000),
            "plan": plan_text,
            "plan_summary": summary,
            "notes": notes,
        }
        if recorded:
            out["recorded_plans"] = [
                {
                    "plan_id": p["planid"],
                    "calls": p["calls"],
                    "avg_exec_ms": _r(p["avg_exec_ms"], 2),
                    "max_exec_ms": _r(p["max_exec_ms"], 2),
                    "slowest_call_params": p.get("max_exec_params"),
                    "summary": summarize_plan(p["plan"]),
                }
                for p in recorded[:5]
            ]
        return out

    # diagnose_query
    def diagnose_query(self, *, query_id: str, window_seconds: int = 300, database: Optional[str] = None) -> dict[str, Any]:
        """Evidence + findings + recommendations for one query_id."""
        qid = _query_id_arg(query_id)
        window_seconds = _clamp(window_seconds, 5, 600, "window_seconds")
        database = self.live.check_database(database)
        limitations: list[str] = []

        stats_resp = self.live.statement_stats(query_id=str(qid), query_chars=4000)
        if not stats_resp.get("found"):
            raise LiveError(stats_resp.get("note") or "query_id not found")
        dbs = stats_resp["per_database"]
        if database:
            dbs = [d for d in dbs if d.get("dbname") == database] or dbs
        stats = max(dbs, key=lambda d: d["total_ms"])
        database = database or stats.get("dbname")
        latency = stats_resp.get("latency_ms_approx")

        all_stmts, _ = self._statements(None)
        total_ms = sum(m["total_ms"] for m in all_stmts) or 1.0
        findings = statement_findings(stats, latency)

        # Cost and waits right now
        profile: Optional[dict[str, Any]] = None
        cluster_share: Optional[float] = None
        try:
            profile = self.live._wait_profile(
                window_seconds, query_id=qid, pid=None, nodes=self.live.nodes(), describe=True, top=8
            )
            findings += wait_findings(profile)
            cluster_share = self._ash_by_query(window_seconds).get(str(qid), {}).get("pct")
        except LiveError as exc:
            limitations.append(str(exc))

        # Plans: recorded first, EXPLAIN as fallback
        plans = self._recorded_plans(qid, limit=20)
        plan_rows: list[dict[str, Any]] = []
        dominant: Optional[dict[str, Any]] = None
        for p in plans:
            summ = summarize_plan(p["plan"])
            plan_rows.append(
                {
                    "plan_id": p["planid"], "calls": p["calls"], "avg_exec_ms": _r(p["avg_exec_ms"], 2),
                    "max_exec_ms": _r(p["max_exec_ms"], 2), "slowest_call_params": p.get("max_exec_params"),
                    "scans": (summ or {}).get("scans"),
                }
            )
            if summ and (dominant is None or p["avg_exec_ms"] * p["calls"] > dominant["_w"]):
                dominant = {**summ, "_w": p["avg_exec_ms"] * p["calls"]}
        flip = plan_instability_finding(
            [{"planid": p["planid"], "avg_exec_ms": p["avg_exec_ms"], "calls": p["calls"]} for p in plans]
        )
        if flip:
            findings.append(flip)
        explain_error: Optional[str] = None
        if dominant is None:
            try:
                ex = self.explain_query(query_id=str(qid), database=database)
                dominant = ex.get("plan_summary")
                limitations.append("No recorded plan (yb_pg_stat_plans); used a generic EXPLAIN with unknown parameters.")
            except LiveError as exc:
                explain_error = str(exc)
                limitations.append(f"Could not obtain a plan: {exc}")

        # Tables in the plan
        tables: dict[str, dict[str, Any]] = {}
        if dominant:
            rels = sorted({s["relation"] for s in dominant["scans"]})
            tables = self._table_context(database, rels, limitations)
        findings += plan_findings(
            dominant,
            mean_ms=stats.get("mean_ms") or 0.0,
            scanned_per_call=stats.get("docdb_rows_scanned_per_call"),
            rows_per_call=stats.get("rows_per_call"),
            table_info=tables,
        )
        if not findings:
            findings.append(
                {"severity": "info", "category": "none", "title": "No obvious problem found",
                 "evidence": "Scan efficiency, retries, waits and plan shape all look normal.",
                 "recommendation": "If it is still slow for callers, look outside the database: network, client-side "
                 "serialization, pool saturation. Compare client-measured and server-side latency (mean_ms)."}
            )
        return {
            "query_id": str(qid),
            "database": database,
            "query": _trunc(stats.get("query"), 3000),
            "summary": {
                "calls": stats["calls"],
                "mean_ms": stats["mean_ms"],
                "total_ms": stats["total_ms"],
                "share_of_total_time_pct": round(100.0 * stats["total_ms"] / total_ms, 2),
                "ash_share_pct_recent": cluster_share,
                "avg_active_sessions_recent": (profile or {}).get("avg_active_sessions"),
                "latency_ms_approx": latency,
                "rows_per_call": stats.get("rows_per_call"),
                "docdb_rows_scanned_per_call": stats.get("docdb_rows_scanned_per_call"),
                "docdb_rows_returned_per_call": stats.get("docdb_rows_returned_per_call"),
            },
            "findings": _sort_findings(findings),
            "waits_recent": None
            if not profile
            else {
                "window_seconds": window_seconds,
                "top_wait_events": profile["by_wait_event"][:5],
                "objects": profile["by_object"][:5],
                "hottest_tablets": profile["by_tablet"][:3],
            },
            "plans": plan_rows,
            "tables": tables,
            "limitations": limitations,
            "next_steps": [
                "explain_query(query_id) for the full plan text; add analyze=true with literal SQL to measure it.",
                "After changing an index, re-run diagnose_query: pg_stat_statements is cumulative, so compare mean_ms "
                "on new calls (or reset stats) rather than expecting the totals to drop.",
            ],
            **({"explain_error": explain_error} if explain_error else {}),
        }

    def _table_context(
        self, database: Optional[str], rels: list[str], limitations: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Per-table facts the rules need: size, analyze status, and its indexes with usage."""
        out: dict[str, dict[str, Any]] = {}
        if not rels:
            return out
        bare = sorted({r.split(".")[-1] for r in rels})
        try:
            with self.live._conn(database=database) as conn:
                infos = Q.table_info_rows(conn, bare)
            idx_rows, meta = self._index_report(database)
        except Exception as exc:  # noqa: BLE001
            limitations.append(f"table/index context unavailable: {str(exc).splitlines()[0]}")
            return out
        if meta["node_errors"]:
            limitations.append(f"index usage counters missing from nodes: {', '.join(meta['node_errors'])}")
        for rel in rels:
            name = rel.split(".")[-1]
            match = [i for i in infos if i["relname"] == name and ("." not in rel or i["schema"] == rel.split(".")[0])]
            if not match:
                continue
            t = match[0]
            qual = f"{t['schema']}.{t['relname']}"
            out[rel] = {
                "table": qual,
                "approx_rows": t["n_live_tup"],
                "size_mb": round(t["bytes"] / 1048576, 1),
                "last_analyze": (t["last_analyze"] or t["last_autoanalyze"]).isoformat()
                if (t["last_analyze"] or t["last_autoanalyze"]) else None,
                "never_analyzed": not (t["last_analyze"] or t["last_autoanalyze"]),
                "indexes": [
                    {"name": i["index"], "definition": i["definition"], "scans": i["scans"],
                     "size_mb": round(i["bytes"] / 1048576, 1), "valid": bool(i["valid"] and i["ready"])}
                    for i in idx_rows if i["table"] == qual
                ],
            }
        return out

    # index_health
    def index_health(
        self, *, database: Optional[str] = None, schema: Optional[str] = None, table: Optional[str] = None, limit: int = 25
    ) -> dict[str, Any]:
        """Broken, duplicate, redundant and unused indexes in one database."""
        limit = _clamp(limit, 1, 200, "limit")
        database = self.live.check_database(database)
        rows, meta = self._index_report(database, schema, table)
        res = analyze_indexes(rows)
        savings = sum(x.get("size_bytes") or 0 for k in ("duplicate", "redundant", "unused") for x in res[k])
        notes = [
            "Scan counters are node-local and reset on restart; 'unused' sums every node that answered.",
        ]
        if meta["nodes_reporting"] < meta["nodes_total"]:
            notes.append(
                f"WARNING: only {meta['nodes_reporting']}/{meta['nodes_total']} nodes reported usage "
                f"({meta['node_errors']}); treat 'unused' as unreliable."
            )
        out = {
            "database": database or "(connection default)",
            "indexes_examined": len(rows),
            "summary": {k: len(v) for k, v in res.items()},
            "potential_savings_mb": round(savings / 1048576, 1),
            **{k: v[:limit] for k, v in res.items()},
            "usage_coverage": {"nodes_reporting": meta["nodes_reporting"], "nodes_total": meta["nodes_total"]},
            "notes": notes,
        }
        return out

    # verify_index
    def verify_index(
        self, *, index: str, schema: str = "public", database: Optional[str] = None, timeout_seconds: int = 60
    ) -> dict[str, Any]:
        """Run ``yb_index_check`` (consistency of an index vs its table). Reads the whole index: use off-peak."""
        timeout_seconds = _clamp(timeout_seconds, 1, 600, "timeout_seconds")
        database = self.live.check_database(database)
        _, live = self.live.caps()
        self.live._require(live.index_check, "yb_index_check()")
        qualified = '"%s"."%s"' % (schema.replace('"', '""'), index.replace('"', '""'))
        with self.live._conn(database=database, timeout_ms=timeout_seconds * 1000) as conn:
            exists = fetch_all(
                conn,
                "SELECT c.relkind::text AS k FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = %(s)s AND c.relname = %(i)s",
                {"s": schema, "i": index},
            )
            if not exists or exists[0]["k"] not in ("i", "I"):
                raise LiveError(f"{schema}.{index} is not an index in database {database or '(default)'}")
            try:
                fetch_all(conn, "SELECT yb_index_check(to_regclass(%(q)s))", {"q": qualified})
            except psycopg.errors.QueryCanceled:
                return {"index": f"{schema}.{index}", "ok": None,
                        "result": f"timed out after {timeout_seconds}s; retry with a larger timeout_seconds"}
            except psycopg.Error as exc:
                return {
                    "index": f"{schema}.{index}", "ok": False,
                    "result": str(exc).strip(),
                    "recommendation": "The index is inconsistent with its table. DROP INDEX and recreate it "
                    "(CREATE INDEX rebuilds it from the table), then run verify_index again.",
                }
        return {"index": f"{schema}.{index}", "ok": True,
                "result": "yb_index_check found no inconsistencies between the index and its table."}

    def _tablet_counts(self, nodes: list[Any]) -> dict[str, int]:
        """``namespace.table`` → number of distinct tablets, from every node's yb_local_tablets."""
        results, _ = self.live._fan_out(lambda conn, _n: Q.yb_local_tablets_rows(conn), nodes)
        seen: dict[str, set[str]] = defaultdict(set)
        for rows in results.values():
            for r in rows:
                seen[f"{r.get('namespace_name')}.{r.get('table_name')}"].add(r["tablet_id"])
        return {k: len(v) for k, v in seen.items()}

    # cluster_bottlenecks
    def cluster_bottlenecks(self, *, window_seconds: int = 300, top: int = 5, query_chars: int = 200) -> dict[str, Any]:
        """Cluster-level triage from ASH: what class of wait dominates, which nodes/tablets are hot, who causes it."""
        window_seconds = _clamp(window_seconds, 30, 3600, "window_seconds")
        top = _clamp(top, 1, 25, "top")
        nodes = self.live.nodes()
        prof = self.live._wait_profile(
            window_seconds, query_id=None, pid=None, nodes=nodes, describe=True, top=max(top, 50)
        )
        total = prof["samples"]
        findings: list[dict[str, Any]] = []
        if not total:
            return {"window_seconds": window_seconds, "samples": 0, "findings": [],
                    "note": "No active-session samples in the window: the cluster was idle (or ASH is disabled)."}

        by_event = {w["wait_event"]: w for w in prof["by_wait_event"]}
        by_type = {t["type"]: t for t in prof["by_wait_type"]}

        def share(*events: str) -> float:
            return sum(by_event[e]["pct"] for e in events if e in by_event)

        lock = share("ConflictResolution_WaitOnConflictingTxns")
        if lock >= 10:
            findings.append({"severity": "high" if lock >= 30 else "medium", "category": "lock_contention",
                             "title": "Lock contention is a major source of waiting",
                             "evidence": f"{lock:.0f}% of active time is waiting on conflicting transactions.",
                             "recommendation": "Run blocking_locks and long_transactions; fix idle-in-transaction sessions and hot-row updates."})
        passive = share("OnCpu_Passive")
        if passive >= 10:
            findings.append({"severity": "high" if passive >= 25 else "medium", "category": "cpu_saturation",
                             "title": "Requests are queueing for a thread/CPU on the tablet servers",
                             "evidence": f"{passive:.0f}% of active time is OnCpu_Passive (waiting for a thread to pick the work up).",
                             "recommendation": "tserver CPU or RPC thread saturation. Reduce load from the top queries below, "
                             "check per-node CPU, and consider adding nodes if sustained."})
        cat = share("CatalogRead")
        if cat >= 10:
            findings.append({"severity": "medium", "category": "catalog",
                             "title": "Significant time reading the catalog from master",
                             "evidence": f"{cat:.0f}% of active time is CatalogRead.",
                             "recommendation": "Use a connection pooler, avoid reconnect storms and DDL on the hot path."})
        reads = share("TableRead", "IndexRead", "StorageRead")
        if reads >= 50:
            findings.append({"severity": "info", "category": "read_bound",
                             "title": "Workload is read-bound on DocDB",
                             "evidence": f"{reads:.0f}% of active time is storage read RPCs.",
                             "recommendation": "Look for full scans (find_full_scans) and queries with a high scan_ratio (top_queries)."})
        # Node imbalance
        per_node = prof["by_node"]
        if len(per_node) >= 2:
            vals = list(per_node.values())
            mean = sum(vals) / len(vals)
            worst = max(per_node, key=lambda k: per_node[k])
            if mean and per_node[worst] / mean >= 1.5 and per_node[worst] >= 20:
                findings.append({"severity": "medium", "category": "node_imbalance",
                                 "title": f"Load is skewed toward {worst}",
                                 "evidence": f"{worst} has {per_node[worst] / mean:.1f}× the average active-session samples.",
                                 "recommendation": "Check connection distribution (use a load-balancing driver), "
                                 "tablet-leader balance and any hot tablet below."})
        # Hot tablets, advice depends on the tablet count
        per_obj: dict[str, int] = defaultdict(int)
        for t in prof["by_tablet"]:
            per_obj[t["object"]] += t["samples"]
        tablet_counts: Optional[dict[str, int]] = None
        for t in prof["by_tablet"]:
            tot = per_obj[t["object"]]
            if tot < 20 or t["samples"] / tot < 0.6 or t["pct"] < 5:
                continue
            if tablet_counts is None:
                tablet_counts = self._tablet_counts(nodes)
            n_tablets = tablet_counts.get(t["object"])
            if n_tablets is not None and n_tablets <= 1:
                if len(nodes) >= 2 and t["pct"] >= 20:
                    findings.append({"severity": "medium", "category": "single_tablet_table",
                                     "title": f"{t['object']} has a single tablet carrying {t['pct']:.0f}% of cluster load",
                                     "evidence": "All requests for this table are served by one tablet leader, i.e. one node, "
                                     f"while the cluster has {len(nodes)} nodes.",
                                     "recommendation": "Split the table across more tablets (create with SPLIT INTO n TABLETS, "
                                     "or ALTER ... SPLIT / enable automatic tablet splitting) so the load spreads over all nodes."})
                break
            findings.append({"severity": "medium", "category": "hot_tablet",
                             "title": f"One tablet dominates load on {t['object']}",
                             "evidence": f"tablet {t['tablet_id_prefix']}… takes {100 * t['samples'] / tot:.0f}% of that table's "
                             f"tablet-side samples although the table has {n_tablets if n_tablets else 'several'} tablets.",
                             "recommendation": "Sequential/skewed keys (timestamps, counters) concentrate writes on one tablet: "
                             "use a hash-sharded leading key, pre-split, or spread hot keys."})
            break

        # Top contributors
        stmts, _ = self._statements(None)
        text_of = {m["queryid"]: m for m in stmts}
        contributors = []
        for q in prof.get("by_query_id", [])[:top]:
            m = text_of.get(q["query_id"])
            contributors.append(
                {
                    "query_id": q["query_id"],
                    "ash_share_pct": q["pct"],
                    "avg_active_sessions": round(q["samples"] / window_seconds, 3),
                    "mean_ms": m["mean_ms"] if m else None,
                    "flags": statement_flags(m) if m else [],
                    "query": _trunc(m.get("query"), query_chars) if m else None,
                }
            )
        return {
            "window_seconds": window_seconds,
            "samples": total,
            "avg_active_sessions": prof["avg_active_sessions"],
            "findings": _sort_findings(findings),
            "wait_types": prof["by_wait_type"][: top * 2],
            "top_wait_events": prof["by_wait_event"][:top],
            "per_node_samples": per_node,
            "hottest_tables": prof["by_object"][:top],
            "hottest_tablets": prof["by_tablet"][:top],
            "top_contributing_queries": contributors,
            "next_steps": ["diagnose_query(query_id) on the top contributors", "find_full_scans", "index_health"],
            "errors": prof.get("errors", {}),
        }
