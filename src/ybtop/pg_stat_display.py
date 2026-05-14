"""pg_stat_statements merge / delta helpers for the watch-mode live table (aligned with web/app.js)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ybtop.merge import merge_pg_stat_statements
from ybtop.pg_stat_constants import PG_STAT_DOCDB_OPTIONAL_COLUMNS
from ybtop.render import keyed_table
from ybtop.snapshot_write import load_snapshot_json, read_manifest_entries


def _dt_from_iso(s: str) -> Optional[datetime]:
    t = s.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    try:
        d = datetime.fromisoformat(t)
    except (TypeError, ValueError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def _format_generated_at_utc_human(iso_s: str) -> str:
    """Readable UTC wall time for live TUI titles (from snapshot ``generated_at_utc`` ISO strings)."""
    raw = str(iso_s or "").strip()
    if not raw:
        return "?"
    dt = _dt_from_iso(raw)
    if dt is None:
        return raw
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def snapshot_interval_seconds(older_iso: str, newer_iso: str) -> float:
    t1 = _dt_from_iso(older_iso)
    t2 = _dt_from_iso(newer_iso)
    if t1 is None or t2 is None or t2 <= t1:
        return 0.0
    return (t2 - t1).total_seconds()


def _statement_merge_key(r: dict[str, Any]) -> str:
    db = r.get("dbname")
    dbs = "" if db is None else str(db).strip()
    return f"{r.get('queryid', '')}\0{dbs}"


def _delta_src_from_merged(r: dict[str, Any]) -> dict[str, Any]:
    calls = int(float(r.get("calls", 0) or 0))
    out: dict[str, Any] = {
        "calls": calls,
        "total_exec_time": float(r.get("total_ms") or 0),
        "doc": {},
    }
    if "rows" in r and r.get("rows") is not None:
        out["rows"] = float(r.get("rows") or 0)
    for k in PG_STAT_DOCDB_OPTIONAL_COLUMNS:
        pk = f"{k}_per_call"
        if pk in r and r.get(pk) is not None:
            out["doc"][k] = float(r.get(pk) or 0) * calls
    return out


def delta_pg_stat_merged_rows(
    cur_rows: list[dict[str, Any]], prev_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Per-statement Δ between two merged result sets (same as web/app.js deltaPgStatMergedRows)."""
    prev_map = {_statement_merge_key(r): r for r in (prev_rows or [])}
    raw: list[dict[str, Any]] = []
    for cur in cur_rows or []:
        p = prev_map.get(_statement_merge_key(cur))
        sc = _delta_src_from_merged(cur)
        sp: dict[str, Any]
        if p is not None:
            sp = _delta_src_from_merged(p)
        else:
            sp = {"calls": 0, "total_exec_time": 0.0, "rows": 0.0, "doc": {}}
        d_calls = float(sc["calls"]) - float(sp.get("calls") or 0)
        d_exec = float(sc["total_exec_time"]) - float(sp.get("total_exec_time") or 0)
        has_rows = "rows" in cur
        d_rows = 0.0
        if has_rows:
            cr = float(sc.get("rows", 0) or 0)
            pr = float(sp.get("rows", 0) or 0) if p is not None else 0.0
            d_rows = cr - pr

        doc_key_set: set[str] = set()
        for k in PG_STAT_DOCDB_OPTIONAL_COLUMNS:
            if (sc.get("doc", {}).get(k) is not None) or (sp.get("doc", {}).get(k) is not None):
                doc_key_set.add(k)
            if f"{k}_per_call" in cur or (p is not None and f"{k}_per_call" in p):
                doc_key_set.add(k)

        row: dict[str, Any] = {
            "calls": round(d_calls, 2),
            "total_ms": round(d_exec, 2),
            "mean_ms": round((d_exec / d_calls) * 100, 2) / 100 if d_calls > 0 else 0.0,
            "query": cur.get("query"),
            "queryid": cur.get("queryid"),
        }
        if "dbname" in cur:
            row["dbname"] = cur.get("dbname")
        if has_rows:
            row["rows"] = round(d_rows, 2)
            row["rows_per_call"] = round((d_rows / d_calls) * 100, 2) / 100 if d_calls > 0 else 0.0
        sdoc = sc.get("doc") or {}
        pdoc = sp.get("doc") or {}
        for dk in doc_key_set:
            ctot = float(sdoc.get(dk) or 0) if sdoc.get(dk) is not None else 0.0
            ptot = float(pdoc.get(dk) or 0) if pdoc.get(dk) is not None else 0.0
            dtot = ctot - ptot
            row[f"{dk}_per_call"] = (
                round((dtot / d_calls) * 100, 2) / 100 if d_calls > 0 else 0.0
            )
        raw.append(row)

    def is_nonzero(r: dict[str, Any]) -> bool:
        if (r.get("calls") or 0) != 0 or (r.get("total_ms") or 0) != 0:
            return True
        if r.get("rows") is not None and (r.get("rows") or 0) != 0:
            return True
        for k in PG_STAT_DOCDB_OPTIONAL_COLUMNS:
            pk = f"{k}_per_call"
            if pk in r and float(r.get(pk) or 0) != 0:
                return True
        return False

    filtered = [r for r in raw if is_nonzero(r)]
    filtered.sort(key=lambda x: float(x.get("total_ms") or 0), reverse=True)
    return filtered


def with_pg_stat_time_percent_cumulative(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    arr = list(rows or [])
    total_ms = sum(float(r.get("total_ms") or 0) for r in arr)
    out: list[dict[str, Any]] = []
    for r in arr:
        ms = float(r.get("total_ms") or 0)
        t_pct = (
            round(10000.0 * (ms / total_ms)) / 100.0 if total_ms > 0 else 0.0
        )
        out.append({**r, "time_pct": t_pct})
    return out


def with_pg_stat_delta_derived(
    rows: list[dict[str, Any]], older_iso: str, newer_iso: str
) -> list[dict[str, Any]]:
    sec = snapshot_interval_seconds(older_iso, newer_iso)
    arr = list(rows or [])
    total_ms = sum(float(r.get("total_ms") or 0) for r in arr)
    out: list[dict[str, Any]] = []
    for r in arr:
        calls = float(r.get("calls") or 0)
        ms = float(r.get("total_ms") or 0)
        cps = round((calls / sec) * 100) / 100 if sec > 0 else 0.0
        t_pct = round(10000.0 * (ms / total_ms)) / 100.0 if total_ms > 0 else 0.0
        out.append({**r, "calls_per_sec": cps, "time_pct": t_pct})
    return out


def _pg_per_node_lists(doc: dict[str, Any]) -> list[list[dict[str, Any]]]:
    st = (doc.get("pg_stat_statements") or {}).get("per_node")
    if not isinstance(st, dict):
        return []
    return [v for v in st.values() if isinstance(v, list)]


def _has_pg_stat_data(doc: Optional[dict[str, Any]]) -> bool:
    if not doc:
        return False
    st = (doc.get("pg_stat_statements") or {}).get("per_node")
    if not isinstance(st, dict):
        return False
    for v in st.values():
        if isinstance(v, list) and len(v) > 0:
            return True
    return False


def live_top5_statements_table(doc: dict[str, Any], out_dir: Path) -> Any:
    """Rich table: Top 5 pg_stat (delta vs previous manifest file when available)."""
    from rich.text import Text

    per = _pg_per_node_lists(doc)
    if not per:
        return Text("Top 5 — pg_stat_statements: (no per_node data in snapshot)", style="dim")

    merged = merge_pg_stat_statements(per)
    if not merged:
        return Text("Top 5 — pg_stat_statements: (no rows after merge)", style="dim")

    entries = read_manifest_entries(out_dir)
    prev_doc: Optional[dict[str, Any]] = None
    if len(entries) >= 2:
        name = entries[-2].get("file")
        if name:
            prev_doc = load_snapshot_json(out_dir, str(name))

    first_in_manifest = len(entries) == 1
    use_delta = (not first_in_manifest) and _has_pg_stat_data(prev_doc) and (prev_doc is not None)
    gen = str(doc.get("generated_at_utc") or "")

    if use_delta and prev_doc is not None:
        prev_merged = merge_pg_stat_statements(_pg_per_node_lists(prev_doc))
        delta_rows = delta_pg_stat_merged_rows(merged, prev_merged)
        prev_t = str(prev_doc.get("generated_at_utc") or "")
        derived = with_pg_stat_delta_derived(delta_rows, prev_t, gen)
        top = derived[:5]
        prev_h = _format_generated_at_utc_human(prev_t)
        cur_h = _format_generated_at_utc_human(gen)
        title = f"Top 5 — pg_stat_statements (Δ {prev_h} → {cur_h})"
        fr, keys = _format_pg_rows_for_table(
            top,
            "calls/sec",
            include_db=any(
                (r.get("dbname") is not None and str(r.get("dbname")).strip())
                for r in (merged or [])
            ),
            has_rows=any("rows" in r for r in merged),
        )
        return keyed_table(title, fr, keys)

    work = with_pg_stat_time_percent_cumulative(merged)
    top = work[:5]
    fr, keys = _format_pg_rows_for_table(
        top,
        "calls",
        include_db=any(
            (r.get("dbname") is not None and str(r.get("dbname")).strip())
            for r in (merged or [])
        ),
        has_rows=any("rows" in r for r in merged),
    )
    return keyed_table("Top 5 — pg_stat_statements", fr, keys)


def _format_pg_rows_for_table(
    rows: list[dict[str, Any]],
    first_col_key: str,
    *,
    include_db: bool,
    has_rows: bool,
) -> tuple[list[dict[str, str]], list[str]]:
    """
    first_col_key is 'calls' (cumulative) or 'calls/sec' (delta).
    Produces display dicts in column order: first col, total time (ms), time %, mean time (ms), query, [dbname], [rows/call], queryid.
    """
    keys: list[str] = [
        first_col_key,
        "total time (ms)",
        "time %",
        "mean time (ms)",
        "query",
    ]
    if include_db:
        keys.append("dbname")
    if has_rows:
        keys.append("rows/call")
    keys.append("queryid")

    out: list[dict[str, str]] = []
    for r in rows:
        if first_col_key == "calls/sec":
            c = r.get("calls_per_sec")
            if c is None:
                c = r.get("calls")
            c_str = "" if c is None else f"{float(c):.2f}"
        else:
            c = r.get("calls")
            if c is None:
                c_str = ""
            else:
                fc = float(c)
                c_str = str(int(fc)) if fc == int(fc) else f"{fc:.2f}"
        tms = r.get("total_ms")
        tpct = r.get("time_pct")
        mm = r.get("mean_ms")
        q = r.get("query")
        d = {
            first_col_key: c_str,
            "total time (ms)": f"{float(tms):.2f}" if tms is not None else "",
            "time %": f"{float(tpct):.2f}" if tpct is not None else "",
            "mean time (ms)": f"{float(mm):.2f}" if mm is not None else "",
            "query": ("" if q is None else str(q)),
        }
        if include_db:
            d["dbname"] = str(r.get("dbname") or "")
        if has_rows:
            d["rows/call"] = f"{float(r.get('rows_per_call', 0) or 0):.2f}"
        d["queryid"] = str(r.get("queryid") or "")
        out.append(d)

    return out, keys