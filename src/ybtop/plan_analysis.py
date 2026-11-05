"""Pure plan / statement analysis used by the MCP diagnostic tools (no database access).

Everything here takes plain dicts (parsed EXPLAIN JSON, merged ``pg_stat_statements`` rows,
catalog rows) and returns plain dicts, so the rules are unit-testable without a cluster.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator, Optional

# Thresholds
FULL_SCAN_RATIO = 50.0  # DocDB rows scanned per row returned
FULL_SCAN_MIN_ROWS = 1000  # minimum rows scanned per call
RETRY_PER_CALL = 0.1  # conflict / read-restart retries per call
CATALOG_WAIT_SHARE = 0.2  # share of mean latency spent waiting on catalog reads
CATALOG_WAIT_MIN_MS = 5.0
BROAD_SELECTIVITY = 0.10  # share of the table above which an index stops helping
WIDE_RESULT_ROWS = 10_000  # rows per call that suggest a missing LIMIT
TAIL_RATIO = 20.0  # p99 / p50 latency spread worth calling out
PLAN_FLIP_RATIO = 3.0  # slowest vs fastest plan time

_KEYWORDS = {
    "and", "or", "not", "null", "true", "false", "any", "all", "is", "in", "like", "ilike",
    "between", "case", "when", "then", "else", "end", "distinct", "from", "array", "interval",
    "timestamp", "date", "text", "numeric", "integer", "bigint", "boolean", "character", "varying",
}
_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_CMP = r"(=|<>|!=|<=|>=|<|>|~~\*?|!~~\*?|~~|IS\s+(?:NOT\s+)?(?:DISTINCT\s+FROM|NULL)|= ANY|<> ALL)"
_COL_OP = re.compile(rf"(?<![\w.'\"])({_IDENT})\s*{_CMP}", re.IGNORECASE)
_FUNC_ON_COL = re.compile(rf"\b({_IDENT})\(\s*(?:\(?)({_IDENT})\)?\s*(?:::\w+)?\s*\)\s*{_CMP}", re.IGNORECASE)
_CAST_ON_COL = re.compile(rf"\(?({_IDENT})\)::(?:text|bigint|integer|smallint|numeric|double precision|date|timestamp\w*|uuid)\)?\s*{_CMP}", re.IGNORECASE)
_PARAM = re.compile(r"\$(\d+)")


# Plan parsing
def parse_plan_json(raw: Any) -> Optional[dict[str, Any]]:
    """``EXPLAIN (FORMAT JSON)`` / ``yb_pg_stat_plans.plan`` value → the root plan node."""
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if isinstance(raw, list) and raw:
        raw = raw[0]
    if isinstance(raw, dict):
        plan = raw.get("Plan", raw)
        return plan if isinstance(plan, dict) and "Node Type" in plan else None
    return None


def walk_plan(node: dict[str, Any], depth: int = 0) -> Iterator[tuple[int, dict[str, Any]]]:
    yield depth, node
    for child in node.get("Plans") or []:
        yield from walk_plan(child, depth + 1)


def _filters_of(n: dict[str, Any]) -> dict[str, str]:
    """Filter-like attributes of a scan node, by where they are evaluated."""
    out: dict[str, str] = {}
    for key, where in (
        ("Index Cond", "index_cond"),
        ("Storage Index Filter", "storage_index_filter"),
        ("Remote Index Filter", "storage_index_filter"),
        ("Storage Filter", "storage_filter"),
        ("Remote Filter", "storage_filter"),
        ("Filter", "ysql_filter"),
        ("Recheck Cond", "recheck_cond"),
    ):
        if n.get(key):
            out[where] = str(n[key])
    return out


def summarize_plan(raw: Any) -> Optional[dict[str, Any]]:
    """Reduce a JSON plan to the facts the diagnosis rules care about."""
    root = parse_plan_json(raw)
    if root is None:
        return None
    scans: list[dict[str, Any]] = []
    other: list[dict[str, Any]] = []
    for depth, n in walk_plan(root):
        t = str(n.get("Node Type", ""))
        rel = n.get("Relation Name")
        if "Scan" in t and rel:
            scans.append(
                {
                    "type": t,
                    "relation": f"{n['Schema']}.{rel}" if n.get("Schema") else rel,
                    "index": n.get("Index Name"),
                    "index_only": t == "Index Only Scan",
                    **_filters_of(n),
                }
            )
        elif t == "Sort" or t == "Incremental Sort":
            other.append({"type": t, "sort_key": n.get("Sort Key")})
        elif "Join" in t or t == "Nested Loop":
            other.append({"type": t})
        elif t in ("Limit", "Aggregate", "HashAggregate", "GroupAggregate"):
            other.append({"type": t})
    return {
        "root": root.get("Node Type"),
        "est_total_cost": root.get("Total Cost"),
        "scans": scans,
        "full_scans": [s for s in scans if s["type"] in ("Seq Scan", "YB Seq Scan")],
        "other_nodes": other,
        "has_limit": any(o["type"] == "Limit" for o in other),
        "sorts": [o for o in other if "Sort" in o["type"]],
    }


# Filter and index hints
def filter_columns(expr: Optional[str]) -> dict[str, Any]:
    """Columns referenced by a filter expression, split by predicate shape.

    Best-effort text analysis of EXPLAIN output (not a SQL parser): good enough to propose a
    *candidate* index, which the caller must still verify.
    """
    out: dict[str, Any] = {"equality": [], "range": [], "wrapped": [], "wrapped_exprs": [], "cast": []}
    if not expr:
        return out
    text = str(expr)
    for m in _FUNC_ON_COL.finditer(text):
        func, col = m.group(1).lower(), m.group(2)
        if func not in _KEYWORDS and col.lower() not in _KEYWORDS and col not in out["wrapped"]:
            out["wrapped"].append(col)
            out["wrapped_exprs"].append(f"{func}({col})")
    for m in _CAST_ON_COL.finditer(text):
        if m.group(1) not in out["cast"]:
            out["cast"].append(m.group(1))
    # Casts inside a function call belong to the function
    out["cast"] = [c for c in out["cast"] if c not in out["wrapped"]]
    skip = set(out["wrapped"]) | set(out["cast"])
    for m in _COL_OP.finditer(text):
        col, op = m.group(1), m.group(2).upper().replace(" ", "")
        if col.lower() in _KEYWORDS or col in skip:
            continue
        # Function names are not columns
        bucket = "equality" if op in ("=", "=ANY", "ISNULL") else "range"
        if col not in out["equality"] and col not in out["range"]:
            out[bucket].append(col)
        elif bucket == "equality" and col in out["range"]:
            out["range"].remove(col)
            out["equality"].append(col)
    return out


def suggest_index(relation: str, expr: Optional[str]) -> Optional[dict[str, Any]]:
    """A *candidate* ``CREATE INDEX`` for a scan filter, with the reasoning attached."""
    cols = filter_columns(expr)
    eq = cols["equality"] + cols["wrapped_exprs"]  # Expression index serves equality
    rng = cols["range"]
    if not eq and not rng:
        return None
    parts: list[str] = []
    if eq:
        parts.append(f"{eq[0]}")  # First column is hash-sharded by default
        parts += [f"{c} ASC" for c in eq[1:]]
        parts += [f"{c} ASC" for c in rng[:1]]
    else:
        parts.append(f"{rng[0]} ASC")  # Range predicates need an ASC first column
    core = re.sub(r"\W+", "_", f"{relation.split('.')[-1]}_{'_'.join(eq + rng[:1])}").strip("_")
    name = f"{core[:55]}_idx"
    return {
        "ddl": f"CREATE INDEX {name} ON {relation} ({', '.join(parts)});",
        "equality_columns": cols["equality"],
        "expression_columns": cols["wrapped_exprs"],
        "range_columns": rng[:1],
        "ignored_range_columns": rng[1:],
        "caveat": "Candidate only: check selectivity (a low-cardinality column rarely helps), "
        "write overhead, and confirm with EXPLAIN after creating it. YugabyteDB builds indexes online "
        "by default, but the backfill still reads the whole table.",
    }


# Statement findings
def _f(sev: str, cat: str, title: str, evidence: str, rec: str, **extra: Any) -> dict[str, Any]:
    return {"severity": sev, "category": cat, "title": title, "evidence": evidence, "recommendation": rec, **extra}


def scan_ratio(row: dict[str, Any]) -> Optional[float]:
    """DocDB rows scanned per row the *client* receives.

    Measured against the statement's own ``rows`` (not ``docdb_rows_returned``): when a predicate
    cannot be pushed down (function on the column, implicit cast), DocDB returns every row it
    scans and YSQL filters afterwards, so DocDB's own returned count hides the waste.
    """
    scanned = row.get("docdb_rows_scanned_per_call")
    if scanned is None or scanned < FULL_SCAN_MIN_ROWS:
        return None
    sent = row.get("rows_per_call")
    if sent is None:
        sent = row.get("docdb_rows_returned_per_call")
    if sent is None:
        return None
    return scanned / max(float(sent), 1.0)


def selectivity(rows_per_call: Optional[float], scanned_per_call: Optional[float]) -> Optional[float]:
    """Share of the table a full scan keeps (rows delivered / rows scanned); ``None`` if unknown.

    A sequential scan reads about the whole table, so scanned ≈ table size.
    """
    if rows_per_call is None or not scanned_per_call:
        return None
    return min(1.0, float(rows_per_call) / float(scanned_per_call))


def statement_flags(row: dict[str, Any]) -> list[str]:
    """Cheap per-statement red flags from a merged pg_stat_statements row (for rankings)."""
    flags: list[str] = []
    ratio = scan_ratio(row)
    if ratio is not None and ratio >= FULL_SCAN_RATIO:
        flags.append("scans_far_more_than_returned")
    if (row.get("conflict_retries_per_call") or 0) >= RETRY_PER_CALL:
        flags.append("conflict_retries")
    if (row.get("read_restart_retries_per_call") or 0) >= RETRY_PER_CALL:
        flags.append("read_restart_retries")
    cat = row.get("catalog_wait_time_per_call") or 0
    mean = row.get("mean_ms") or 0
    if cat >= CATALOG_WAIT_MIN_MS and mean and cat / mean >= CATALOG_WAIT_SHARE:
        flags.append("catalog_wait_heavy")
    if (row.get("rows_per_call") or 0) >= WIDE_RESULT_ROWS:
        flags.append("returns_many_rows")
    return flags


def statement_findings(stats: dict[str, Any], latency: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    calls = stats.get("calls") or 0
    mean = stats.get("mean_ms") or 0
    scanned = stats.get("docdb_rows_scanned_per_call")
    ratio = scan_ratio(stats)
    if ratio is not None and ratio >= FULL_SCAN_RATIO:
        sent = stats.get("rows_per_call") or 0
        out.append(
            _f(
                "high", "scan_efficiency", "Reads far more rows than it needs",
                f"DocDB scans ~{scanned:,.0f} rows/call to hand ~{sent:,.0f} row(s) to the client "
                f"(ratio {ratio:,.0f}:1) over {calls:,} calls.",
                "Add or fix an index so the filter is applied inside the index lookup instead of "
                "after scanning; see the plan findings for a candidate.",
            )
        )
    for key, label, rec in (
        ("conflict_retries_per_call", "Transaction conflict retries",
         "Hot rows are updated concurrently. Shorten transactions, update rows in a consistent order, "
         "batch less per transaction, or use explicit row locks (SELECT ... FOR UPDATE) / "
         "retry with backoff. Use blocking_locks to find the chain."),
        ("read_restart_retries_per_call", "Read restart retries",
         "Reads race with concurrent writes within clock skew. Prefer read-only transactions with "
         "snapshot semantics (SET TRANSACTION READ ONLY / follower reads where acceptable), "
         "or make single-statement reads so the server can restart transparently."),
    ):
        v = stats.get(key) or 0
        if v >= RETRY_PER_CALL:
            out.append(_f("medium", "contention", label, f"{v:.2f} retries per call.", rec))
    cat = stats.get("catalog_wait_time_per_call") or 0
    if cat >= CATALOG_WAIT_MIN_MS and mean and cat / mean >= CATALOG_WAIT_SHARE:
        out.append(
            _f(
                "medium", "catalog", "Time lost waiting on catalog reads",
                f"{cat:.1f} ms of a {mean:.1f} ms mean call is catalog wait.",
                "Usually a cold or constantly invalidated catalog cache: connection churn without a pooler, "
                "frequent DDL, or many short-lived sessions. Use a connection pool and avoid DDL on the hot path.",
            )
        )
    rows = stats.get("rows_per_call") or 0
    if rows >= WIDE_RESULT_ROWS:
        out.append(
            _f(
                "medium", "result_size", "Returns a very large result per call",
                f"~{rows:,.0f} rows/call.",
                "Add LIMIT / keyset pagination, select only the needed columns, or aggregate in the database.",
            )
        )
    if latency:
        p50, p99 = latency.get("p50"), latency.get("p99")
        if p50 and p99 and p99 / p50 >= TAIL_RATIO:
            out.append(
                _f(
                    "medium", "latency_variance", "Long latency tail",
                    f"p99 ≈ {p99:,.1f} ms vs p50 ≈ {p50:,.1f} ms (×{p99 / p50:,.0f}).",
                    "Compare the fast and slow calls: the slowest parameters are listed under plans "
                    "(max_exec_time_params). Typical causes are skewed values, leader vs. follower reads, "
                    "cache misses, lock waits or a plan that flips between parameter values.",
                )
            )
    return out


def plan_findings(
    plan: Optional[dict[str, Any]],
    *,
    mean_ms: float = 0.0,
    scanned_per_call: Optional[float] = None,
    rows_per_call: Optional[float] = None,
    table_info: Optional[dict[str, dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """Findings from the dominant plan. ``table_info`` maps relation → {indexes, last_analyze, ...}."""
    out: list[dict[str, Any]] = []
    if not plan:
        return out
    table_info = table_info or {}
    slow_enough = mean_ms >= 5 or (scanned_per_call or 0) >= FULL_SCAN_MIN_ROWS
    broad = selectivity(rows_per_call, scanned_per_call)
    for s in plan["full_scans"]:
        if broad is not None and broad >= BROAD_SELECTIVITY and len(plan["full_scans"]) == 1:
            out.append(
                _f(
                    "low", "broad_scan", f"Full scan on {s['relation']} returns ~{broad:.0%} of the table",
                    f"The query hands ~{rows_per_call:,.0f} of ~{scanned_per_call:,.0f} scanned rows to the client; "
                    "an index would not be chosen (or would be slower) for a predicate this unselective.",
                    "Do not add an index for this. Reduce the work instead: a more selective predicate, LIMIT / keyset "
                    "pagination, only the needed columns, or pre-aggregate / materialize the result.",
                    relation=s["relation"],
                )
            )
            continue
        rel = s["relation"]
        expr = s.get("storage_filter") or s.get("ysql_filter")
        cand = suggest_index(rel, expr)
        cols = filter_columns(expr)
        if expr:
            title = f"Full table scan on {rel}"
            ev = f"Seq Scan with filter ({'DocDB-side' if s.get('storage_filter') else 'YSQL-side'}): {expr}"
        else:
            title = f"Full table scan on {rel} (no filter)"
            ev = "Seq Scan reads every row of the table."
        sev = "high" if (expr and slow_enough) else "medium" if expr else "info"
        rec = (
            "Create an index that matches the filter so the scan becomes an index lookup."
            if expr
            else "Fine for a small table or an intentional full read; otherwise add a WHERE clause the planner can use."
        )
        if cols["wrapped"]:
            rec += (
                f" Column(s) {', '.join(cols['wrapped'])} are wrapped in a function ({', '.join(cols['wrapped_exprs'])}), "
                "so a plain index on the column cannot be used. Either rewrite the predicate to compare the bare column, "
                "or create an expression index on exactly that expression (see candidate_index)."
            )
        if cols["cast"]:
            rec += (
                f" Column(s) {', '.join(cols['cast'])} are implicitly cast (type mismatch with the parameter), "
                "which prevents index use: bind the parameter with the column's type."
            )
        existing = (table_info.get(rel) or {}).get("indexes") or []
        extra: dict[str, Any] = {"relation": rel}
        if cand:
            extra["candidate_index"] = cand
        if existing and (cols["equality"] or cols["range"]):
            extra["existing_indexes"] = [e["definition"] for e in existing][:10]
            rec += " Existing indexes on this table did not match this predicate (listed for reference)."
        if cols["cast"] and not cand:
            extra["problem"] = (
                f"{', '.join(cols['cast'])} is cast to another type for the comparison, so no index on the column can be used"
            )
        out.append(_f(sev, "full_scan", title, ev, rec, **extra))
    for s in plan["scans"]:
        expr = s.get("storage_index_filter") or s.get("storage_filter") or s.get("ysql_filter")
        if s["type"] in ("Index Scan", "YB Index Scan") and expr:
            out.append(
                _f(
                    "medium", "index_coverage", f"Index {s.get('index')} does not cover the whole predicate",
                    f"{s['relation']} uses {s.get('index')} with Index Cond [{s.get('index_cond', '-')}] "
                    f"but still filters afterwards: {expr}.",
                    "Add the filtered column(s) to the index (after the equality columns) so they are matched "
                    "inside the index, or INCLUDE them to avoid extra table lookups.",
                    relation=s["relation"],
                )
            )
    if plan["sorts"] and not plan["has_limit"] and (mean_ms >= 20):
        keys = plan["sorts"][0].get("sort_key")
        out.append(
            _f(
                "low", "sort", "Explicit sort in the plan",
                f"Sort on {keys}.",
                "If this is a hot query, an index whose column order matches the ORDER BY (and equality filters) "
                "lets YugabyteDB return rows already ordered and enables LIMIT push-down.",
            )
        )
    for rel, info in table_info.items():
        if info.get("never_analyzed"):
            out.append(
                _f(
                    "medium", "statistics", f"No planner statistics for {rel}",
                    "The table has never been analyzed, so the optimizer is guessing row counts.",
                    f"Run ANALYZE {rel}; (and consider enabling auto analyze) so plans stop flipping and indexes get chosen.",
                    relation=rel,
                )
            )
    return out


def plan_instability_finding(plans: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Several recorded plans for one query with very different latency → plan flip."""
    timed = [p for p in plans if p.get("avg_exec_ms") and p.get("calls")]
    if len(timed) < 2:
        return None
    fast = min(timed, key=lambda p: p["avg_exec_ms"])
    slow = max(timed, key=lambda p: p["avg_exec_ms"])
    if slow["avg_exec_ms"] / max(fast["avg_exec_ms"], 1e-9) < PLAN_FLIP_RATIO:
        return None
    return _f(
        "medium", "plan_instability", "The same query runs with different plans of very different speed",
        f"Plan {slow['planid']} averages {slow['avg_exec_ms']:.1f} ms over {slow['calls']} calls vs "
        f"{fast['avg_exec_ms']:.1f} ms for plan {fast['planid']}.",
        "ANALYZE the tables involved, check for parameter skew (see max_exec_time_params), and if one plan is clearly "
        "better pin it with a pg_hint_plan hint or add an index that makes the good plan dominant.",
    )


def wait_findings(profile: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """Findings from an ASH wait profile (as built by LiveContext._wait_profile)."""
    out: list[dict[str, Any]] = []
    if not profile or not profile.get("samples"):
        return out
    by = {w["wait_event"]: w for w in profile.get("by_wait_event", [])}
    lock = by.get("ConflictResolution_WaitOnConflictingTxns")
    if lock and lock["pct"] >= 10:
        out.append(
            _f(
                "high" if lock["pct"] >= 30 else "medium", "lock_contention",
                "Spends time waiting for conflicting transactions",
                f"{lock['pct']}% of its active time is ConflictResolution_WaitOnConflictingTxns.",
                "Another transaction holds the rows it needs. Run blocking_locks to see the blocker; "
                "typical fixes are shorter transactions, no idle-in-transaction sessions, consistent update order.",
            )
        )
    cat = by.get("CatalogRead")
    if cat and cat["pct"] >= 10:
        out.append(
            _f("medium", "catalog", "Waiting on catalog reads from master",
               f"{cat['pct']}% of active time is CatalogRead.",
               "Catalog cache misses: use a connection pooler and avoid frequent DDL / new connections.")
        )
    reads = [w for w in profile.get("by_wait_event", []) if w["wait_event"] in ("TableRead", "IndexRead", "StorageRead")]
    if reads and sum(w["pct"] for w in reads) >= 50:
        out.append(
            _f("info", "read_bound", "Mostly waiting on DocDB reads",
               f"{sum(w['pct'] for w in reads):.0f}% of active time is storage read RPCs.",
               "Reduce rows read per call (index, LIMIT, narrower projection) or increase locality of the data it touches.")
        )
    return out


# Index hygiene
def analyze_indexes(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Classify catalog + usage rows into broken / duplicate / redundant / unused indexes.

    Each row: ``table, index, valid, ready, live, unique, primary, cols(list[str]), opclasses,
    options, predicate, has_expr, definition, definition_norm, bytes, scans``.

    The recommendations are *consistent as a set*: the scans of an index that is recommended for
    dropping are credited to the index that replaces it, so following every recommendation never
    leaves a used access path without an index (e.g. a duplicate pair plus a wider index must not
    all be reported as droppable).
    """
    broken: list[dict[str, Any]] = []
    duplicate: list[dict[str, Any]] = []
    redundant: list[dict[str, Any]] = []
    unused: list[dict[str, Any]] = []
    by_table: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_table.setdefault(r["table"], []).append(r)
        if not (r["valid"] and r["ready"] and r["live"]):
            broken.append(
                {
                    "table": r["table"], "index": r["index"], "definition": r["definition"],
                    "valid": r["valid"], "ready": r["ready"], "size_bytes": r["bytes"],
                    "problem": "index is not valid/ready (a failed or interrupted CREATE INDEX leaves it unusable "
                    "but still maintained on every write)",
                    "recommendation": "DROP INDEX and recreate it; run verify_index afterwards.",
                }
            )
    for table, all_idxs in by_table.items():
        idxs = [r for r in all_idxs if r["valid"] and r["ready"] and r["live"]]  # broken indexes are reported separately
        eff = {r["index"]: int(r["scans"]) for r in idxs}
        drop: set[str] = set()
        # Exact duplicates: keep the first, credit it with their scans
        sig: dict[tuple[Any, ...], dict[str, Any]] = {}
        for r in idxs:
            key = (tuple(r["cols"]), tuple(r["opclasses"]), tuple(r["options"]), r["predicate"], r["has_expr"],
                   r["unique"], r["definition_norm"] if r["has_expr"] else "")
            keep = sig.setdefault(key, r)
            if keep is r:
                continue
            eff[keep["index"]] += int(r["scans"])
            drop.add(r["index"])
            duplicate.append(
                {
                    "table": table, "index": r["index"], "duplicate_of": keep["index"],
                    "definition": r["definition"], "size_bytes": r["bytes"], "scans": int(r["scans"]),
                    "recommendation": f"DROP INDEX {r['index']}; it is identical to {keep['index']} "
                    "and only adds write amplification and storage.",
                }
            )
        # Leading-prefix redundancy, shortest first
        for a in sorted(idxs, key=lambda x: len(x["cols"])):
            if a["index"] in drop or a["primary"] or a["unique"] or a["has_expr"] or not a["cols"]:
                continue
            n = len(a["cols"])
            for b in idxs:
                if b is a or b["index"] in drop or b["has_expr"] or len(b["cols"]) <= n:
                    continue
                if (
                    b["cols"][:n] == a["cols"]
                    and b["opclasses"][:n] == a["opclasses"]
                    and b["options"][:n] == a["options"]
                    and b["predicate"] == a["predicate"]
                ):
                    eff[b["index"]] += eff[a["index"]]
                    drop.add(a["index"])
                    redundant.append(
                        {
                            "table": table, "index": a["index"], "covered_by": b["index"],
                            "definition": a["definition"], "size_bytes": a["bytes"],
                            "scans": eff[a["index"]],
                            "recommendation": f"{a['index']} is a leading prefix of {b['index']}; lookups on its columns "
                            f"can use {b['index']} (its {eff[a['index']]:,} scans would move there). Consider DROP INDEX "
                            f"{a['index']}, after checking that nothing relies on its ordering or a narrower scan.",
                        }
                    )
                    break
        # Unused: judged on credited scans, skips indexes already being dropped
        for r in idxs:
            if r["primary"] or r["unique"] or r["index"] in drop:
                continue
            if eff[r["index"]] == 0:
                unused.append(
                    {
                        "table": table, "index": r["index"], "definition": r["definition"], "size_bytes": r["bytes"],
                        "recommendation": "No scans recorded on any node since the stats were last reset. If that window "
                        "covers a full business cycle, DROP INDEX saves write cost and space.",
                    }
                )
    key = lambda x: -(x.get("size_bytes") or 0)  # noqa: E731
    return {
        "broken": broken,
        "duplicate": sorted(duplicate, key=key),
        "redundant": sorted(redundant, key=key),
        "unused": sorted(unused, key=key),
    }


_INDEXDEF_NAME = re.compile(r"^(CREATE (?:UNIQUE )?INDEX )(?:CONCURRENTLY )?\S+( ON .*)$")


def normalize_indexdef(defn: str) -> str:
    """``pg_get_indexdef`` text with the index name removed (for duplicate detection)."""
    m = _INDEXDEF_NAME.match(defn or "")
    return f"{m.group(1)}{m.group(2)}" if m else defn


def extract_params(sql: str) -> int:
    """Highest ``$N`` placeholder in a normalized statement (0 if none)."""
    nums = [int(x) for x in _PARAM.findall(sql or "")]
    return max(nums) if nums else 0
