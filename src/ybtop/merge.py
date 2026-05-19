from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal
from typing import Any, Optional

from ybtop.pg_stat_constants import PG_STAT_DOCDB_OPTIONAL_COLUMNS
from ybtop.table_schema import resolve_table_engine


def _pg_stmt_merge_key(r: dict[str, Any]) -> tuple[str, str]:
    qid = str(r["queryid"])
    db = r.get("dbname")
    dbn = "" if db is None else str(db).strip()
    return (qid, dbn)


def _num(v: Any) -> float:
    if v is None:
        return 0.0
    if isinstance(v, Decimal):
        return float(v)
    return float(v)


def merge_ycql_stat_statements(per_node: Iterable[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Sum additive counters across nodes; recompute mean time for YCQL statements."""
    acc: dict[str, dict[str, Any]] = {}
    for rows in per_node:
        for r in rows:
            qid = str(r["queryid"])
            calls = int(r["calls"])
            total_ms = _num(r.get("total_time"))
            prepared = bool(r.get("is_prepared"))
            if qid not in acc:
                acc[qid] = {
                    "queryid": qid,
                    "query": r.get("query"),
                    "calls": 0,
                    "total_time": 0.0,
                    "is_prepared": False,
                }
            a = acc[qid]
            a["calls"] += calls
            a["total_time"] += total_ms
            if prepared:
                a["is_prepared"] = True
            if not a.get("query") and r.get("query"):
                a["query"] = r["query"]
    out: list[dict[str, Any]] = []
    for a in acc.values():
        calls = int(a["calls"])
        mean = (a["total_time"] / calls) if calls else 0.0
        out.append(
            {
                "calls": a["calls"],
                "total_ms": round(a["total_time"], 2),
                "mean_ms": round(mean, 2),
                "query": a.get("query"),
                "is_prepared": bool(a["is_prepared"]),
                "queryid": a["queryid"],
            }
        )
    out.sort(key=lambda x: x["total_ms"], reverse=True)
    return out


def merge_pg_stat_statements(
    per_node: Iterable[list[dict[str, Any]]],
    *,
    include_docdb_per_call: bool = True,
    include_rows_total: bool = True,
) -> list[dict[str, Any]]:
    """Sum additive counters across nodes; recompute mean time and optional per-call DocDB / rows metrics."""
    has_rows_col = any("rows" in r for rows in per_node for r in rows)
    if include_docdb_per_call:
        seen_doc: set[str] = set()
        for rows in per_node:
            for r in rows:
                for k in PG_STAT_DOCDB_OPTIONAL_COLUMNS:
                    if k in r and r[k] is not None:
                        seen_doc.add(k)
        doc_keys = [k for k in PG_STAT_DOCDB_OPTIONAL_COLUMNS if k in seen_doc]
    else:
        doc_keys = []

    has_dbname = any(
        r.get("dbname") is not None and str(r.get("dbname")).strip()
        for rows in per_node
        for r in rows
    )

    acc: dict[tuple[str, str], dict[str, Any]] = {}
    for rows in per_node:
        for r in rows:
            mk = _pg_stmt_merge_key(r)
            calls = int(r["calls"])
            total_ms = _num(r.get("total_exec_time"))
            rows_cnt = _num(r.get("rows")) if has_rows_col else 0.0
            if mk not in acc:
                entry: dict[str, Any] = {
                    "queryid": mk[0],
                    "dbname": mk[1] or None,
                    "query": r.get("query"),
                    "calls": 0,
                    "total_exec_time": 0.0,
                }
                if has_rows_col:
                    entry["rows"] = 0.0
                for k in doc_keys:
                    entry[k] = 0.0
                acc[mk] = entry
            a = acc[mk]
            a["calls"] += calls
            a["total_exec_time"] += total_ms
            if not a.get("dbname") and r.get("dbname"):
                a["dbname"] = str(r.get("dbname")).strip() or None
            if has_rows_col:
                a["rows"] += rows_cnt
            for k in doc_keys:
                a[k] += _num(r.get(k))
            if not a.get("query") and r.get("query"):
                a["query"] = r["query"]
    out: list[dict[str, Any]] = []
    for a in acc.values():
        calls = int(a["calls"])
        mean = (a["total_exec_time"] / calls) if calls else 0.0
        # Column order: calls, total_ms, mean_ms, query (4th), …, queryid last (Rich / HTML tables).
        row: dict[str, Any] = {
            "calls": a["calls"],
            "total_ms": round(a["total_exec_time"], 2),
            "mean_ms": round(mean, 2),
            "query": a.get("query"),
        }
        if has_dbname:
            row["dbname"] = a.get("dbname")
        if has_rows_col:
            if include_rows_total:
                row["rows"] = round(float(a["rows"]), 2)
            row["rows_per_call"] = round((float(a["rows"]) / calls), 2) if calls else 0.0
        if include_docdb_per_call:
            for k in doc_keys:
                s = float(a[k])
                row[f"{k}_per_call"] = round((s / calls), 2) if calls else 0.0
        row["queryid"] = a["queryid"]
        out.append(row)
    out.sort(key=lambda x: x["total_ms"], reverse=True)
    return out


def _ash_display_object_name(row: dict[str, Any]) -> Any:
    """YSQL waits often have no tablet aux; show a placeholder instead of empty object_name."""
    comp = row.get("wait_event_component")
    if comp is None or str(comp).strip().upper() != "YSQL":
        return row.get("object_name")
    aux = row.get("wait_event_aux")
    ob = row.get("object_name")
    aux_empty = aux is None or (isinstance(aux, str) and not aux.strip()) or aux == ""
    ob_empty = ob is None or (isinstance(ob, str) and not str(ob).strip()) or ob == ""
    if aux_empty and ob_empty:
        return "[PGLayer]"
    return ob


def _namespace_objname(nn: Any, on: Any, aux: Any) -> str:
    ns = ("" if nn is None else str(nn)).strip()
    ob = ("" if on is None else str(on)).strip()
    if ns and ob:
        return f"{ns}.{ob}"
    if ns:
        return ns
    if ob:
        return ob
    return ("" if aux is None else str(aux)).strip()


def ash_infer_engine_from_row(row: dict[str, Any]) -> Optional[str]:
    """Classify YSQL vs YCQL from one ASH aggregate row."""
    comp_raw = row.get("wait_event_component")
    comp = "" if comp_raw is None else str(comp_raw).strip().upper()
    if comp == "YCQL":
        return "YCQL"
    if comp == "YSQL":
        return "YSQL"
    if comp == "TSERVER":
        dbid = row.get("ysql_dbid")
        try:
            dbid_int = 0 if dbid is None else int(dbid)
        except (TypeError, ValueError):
            dbid_int = 0
        return "YCQL" if dbid_int == 0 else "YSQL"
    return None


def _norm_table_id(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def top_ash_table_ids(
    per_node: Iterable[list[dict[str, Any]]],
    *,
    limit: int = 25,
    tablet_meta: Optional[dict[str, dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """Rank table_id values by total ASH samples across nodes (table_id must be set)."""
    if limit <= 0:
        return []
    acc: dict[str, dict[str, Any]] = {}
    for rows in per_node:
        for r in rows:
            tid = _norm_table_id(r.get("table_id"))
            if tid is None:
                continue
            samples = int(r.get("samples") or 0)
            if tid not in acc:
                acc[tid] = {
                    "table_id": tid,
                    "samples": 0,
                    "engine": None,
                    "namespace_name": r.get("namespace_name"),
                    "object_name": r.get("object_name"),
                }
            ent = acc[tid]
            ent["samples"] = int(ent["samples"]) + samples
            if not ent.get("namespace_name") and r.get("namespace_name"):
                ent["namespace_name"] = r.get("namespace_name")
            if not ent.get("object_name") and r.get("object_name"):
                ent["object_name"] = r.get("object_name")
    out = sorted(acc.values(), key=lambda x: int(x["samples"]), reverse=True)
    meta = tablet_meta or {}
    for ent in out:
        tid = str(ent["table_id"])
        ent["engine"] = resolve_table_engine(tid, tablet=meta.get(tid.lower()))
    return out[:limit]


def merge_ash_groups(
    per_node: Iterable[list[dict[str, Any]]],
    *,
    include_namespace_objname: bool = True,
) -> list[dict[str, Any]]:
    """Sum sample counts for identical ASH dimensions across nodes."""

    def norm_qid(v: Any) -> Any:
        return None if v is None else str(v)

    def ash_merge_object_key(r: dict[str, Any]) -> str:
        tid = r.get("table_id")
        if tid is not None and str(tid).strip() != "":
            return str(tid).strip()
        v = _ash_display_object_name(r)
        return "" if v is None else str(v)

    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    for rows in per_node:
        for r in rows:
            k = (
                norm_qid(r.get("query_id")),
                r.get("wait_event_component"),
                r.get("wait_event"),
                r.get("wait_event_type"),
                ash_merge_object_key(r),
                r.get("ysql_dbid"),
            )
            if k not in merged:
                merged[k] = {
                    "query_id": r.get("query_id"),
                    "wait_event_component": r.get("wait_event_component"),
                    "wait_event": r.get("wait_event"),
                    "wait_event_type": r.get("wait_event_type"),
                    "wait_event_aux": r.get("wait_event_aux"),
                    "ysql_dbid": r.get("ysql_dbid"),
                    "namespace_name": r.get("namespace_name"),
                    "object_name": r.get("object_name"),
                    "table_id": r.get("table_id"),
                    "samples": 0,
                }
            m = merged[k]
            m["samples"] = int(m["samples"]) + int(r.get("samples") or 0)
            m["namespace_name"] = m.get("namespace_name") or r.get("namespace_name")
            m["object_name"] = m.get("object_name") or r.get("object_name")
            if m.get("table_id") is None and r.get("table_id") is not None:
                m["table_id"] = r.get("table_id")
            if m.get("ysql_dbid") is None and r.get("ysql_dbid") is not None:
                m["ysql_dbid"] = r.get("ysql_dbid")
    out: list[dict[str, Any]] = []
    for _k, m in merged.items():
        obj_display = _ash_display_object_name(m)
        parts: list[tuple[str, Any]] = [
            ("samples", int(m["samples"])),
            ("namespace_name", m.get("namespace_name")),
            ("object_name", obj_display),
            ("wait_event_component", m.get("wait_event_component")),
            ("wait_event_type", m.get("wait_event_type")),
            ("wait_event", m.get("wait_event")),
        ]
        if m.get("table_id") is not None:
            parts.insert(3, ("table_id", m.get("table_id")))
        if m.get("ysql_dbid") is not None:
            parts.append(("ysql_dbid", m.get("ysql_dbid")))
        if include_namespace_objname:
            parts.append(
                (
                    "namespace_objname",
                    _namespace_objname(
                        m.get("namespace_name"), obj_display, m.get("wait_event_aux")
                    ),
                )
            )
        parts.append(("query_id", m.get("query_id")))
        out.append(dict(parts))
    out.sort(key=lambda x: int(x["samples"]), reverse=True)
    return out
