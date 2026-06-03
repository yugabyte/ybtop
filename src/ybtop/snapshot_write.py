from __future__ import annotations

import contextvars
import glob
import json
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import psycopg.conninfo

from ybtop import queries as Q
from ybtop.capabilities import Capabilities, detect_capabilities
from ybtop.config import DEFAULT_NODE_PARALLELISM, MANIFEST_FILENAME, SNAPSHOT_FILE_PREFIX
from ybtop.db import connect
from ybtop.log import get_logger, log_event, stage_timer, summary_scope
from ybtop.merge import top_ash_table_ids
from ybtop.table_schema import collect_table_schemas, lookup_tablet_meta_by_table_id, resolve_table_engine
from ybtop.topology import YsqlNode, discover_ysql_nodes, dsn_for_node, node_id

_log = get_logger("snapshot")


def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _serialize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return JSON-serializable copies (datetime/decimal safe)."""
    return json.loads(json.dumps(rows, default=_json_default))


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, default=_json_default).encode("utf-8")
    fd, tmp = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def build_snapshot_document(
    *,
    seed_dsn: str,
    ash_start: datetime,
    ash_end: datetime,
    statements_per_node: int,
    ash_per_node: int,
    ensure_ycql_extension: bool = False,
    ash_top_tables: int = 25,
    collect_table_ddl: bool = False,
    node_parallelism: int = DEFAULT_NODE_PARALLELISM,
) -> dict[str, Any]:
    with stage_timer("build_snapshot", _log, scope_total=True):
        with summary_scope("build_snapshot"):
            return _build_snapshot_document_impl(
                seed_dsn=seed_dsn,
                ash_start=ash_start,
                ash_end=ash_end,
                statements_per_node=statements_per_node,
                ash_per_node=ash_per_node,
                ensure_ycql_extension=ensure_ycql_extension,
                ash_top_tables=ash_top_tables,
                collect_table_ddl=collect_table_ddl,
                node_parallelism=node_parallelism,
            )


@dataclass(frozen=True)
class _NodeCollectResult:
    nid: str
    pg_stat: list[dict[str, Any]]
    ycql: list[dict[str, Any]]
    ash: list[dict[str, Any]]
    tablets: list[dict[str, Any]]


def _collect_one_node(
    *,
    seed_dsn: str,
    node: YsqlNode,
    node_count: int,
    caps: Capabilities,
    ash_start: datetime,
    ash_end: datetime,
    ash_window_sec: float,
    statements_per_node: int,
    ash_per_node: int,
) -> _NodeCollectResult:
    nid = node_id(node)
    dsn = dsn_for_node(seed_dsn, node)
    with stage_timer("collect_node", _log, node_id=nid, node_total=True, node_count=node_count):
        with connect(dsn) as conn:
            with stage_timer("pg_stat_statements_top", _log, node_id=nid) as st:
                pg_stat = _serialize_rows(Q.pg_stat_statements_top(conn, statements_per_node, caps))
                st.row_count = len(pg_stat)
            with stage_timer("ycql_stat_statements_top", _log, node_id=nid) as st:
                ycql = _serialize_rows(Q.ycql_stat_statements_top(conn, statements_per_node))
                st.row_count = len(ycql)
            with stage_timer(
                "ash_aggregated",
                _log,
                node_id=nid,
                ash_window_sec=ash_window_sec,
            ) as st:
                ash = _serialize_rows(
                    Q.ash_aggregated(conn, ash_start, ash_end, caps, outer_limit=ash_per_node)
                )
                st.row_count = len(ash)
            with stage_timer("yb_local_tablets_rows", _log, node_id=nid) as st:
                tablets = _serialize_rows(Q.yb_local_tablets_rows(conn))
                st.row_count = len(tablets)
    return _NodeCollectResult(nid=nid, pg_stat=pg_stat, ycql=ycql, ash=ash, tablets=tablets)


def _collect_nodes_parallel(
    *,
    seed_dsn: str,
    nodes: list[YsqlNode],
    caps: Capabilities,
    ash_start: datetime,
    ash_end: datetime,
    ash_window_sec: float,
    statements_per_node: int,
    ash_per_node: int,
    node_parallelism: int,
) -> tuple[
    dict[str, list[dict[str, Any]]],
    dict[str, list[dict[str, Any]]],
    dict[str, list[dict[str, Any]]],
    dict[str, list[dict[str, Any]]],
]:
    statements_out: dict[str, list[dict[str, Any]]] = {}
    ycql_out: dict[str, list[dict[str, Any]]] = {}
    ash_out: dict[str, list[dict[str, Any]]] = {}
    tablets_out: dict[str, list[dict[str, Any]]] = {}
    workers = min(max(1, int(node_parallelism)), len(nodes))
    node_count = len(nodes)
    collect_kw = {
        "seed_dsn": seed_dsn,
        "node_count": node_count,
        "caps": caps,
        "ash_start": ash_start,
        "ash_end": ash_end,
        "ash_window_sec": ash_window_sec,
        "statements_per_node": statements_per_node,
        "ash_per_node": ash_per_node,
    }

    def _run(node: YsqlNode) -> _NodeCollectResult:
        return _collect_one_node(node=node, **collect_kw)

    with stage_timer(
        "collect_nodes",
        _log,
        node_count=node_count,
        node_parallelism=workers,
    ) as st:
        if workers == 1:
            results = [_run(n) for n in nodes]
        else:
            results = []
            with ThreadPoolExecutor(max_workers=workers) as pool:
                # Each worker needs its own Context copy; Context.run() is not re-entrant
                # across threads on the same Context object.
                futures = [
                    pool.submit(contextvars.copy_context().run, _run, n) for n in nodes
                ]
                for fut in as_completed(futures):
                    results.append(fut.result())
        st.row_count = len(results)

    for r in results:
        statements_out[r.nid] = r.pg_stat
        ycql_out[r.nid] = r.ycql
        ash_out[r.nid] = r.ash
        tablets_out[r.nid] = r.tablets
    return statements_out, ycql_out, ash_out, tablets_out


def _build_snapshot_document_impl(
    *,
    seed_dsn: str,
    ash_start: datetime,
    ash_end: datetime,
    statements_per_node: int,
    ash_per_node: int,
    ensure_ycql_extension: bool = False,
    ash_top_tables: int = 25,
    collect_table_ddl: bool = False,
    node_parallelism: int = DEFAULT_NODE_PARALLELISM,
) -> dict[str, Any]:
    ash_window_sec = round((ash_end - ash_start).total_seconds(), 2)
    with stage_timer("discover_ysql_nodes", _log):
        nodes = discover_ysql_nodes(seed_dsn)
    nids = [node_id(n) for n in nodes]
    node_topology: dict[str, dict[str, Any]] = {}
    for n in nodes:
        nid = node_id(n)
        node_topology[nid] = {
            "host": n.host,
            "port": n.port,
            "server_uuid": n.server_uuid,
            "cloud": n.cloud,
            "region": n.region,
            "zone": n.zone,
        }
    seed_info = psycopg.conninfo.conninfo_to_dict(seed_dsn)
    seed_host = seed_info.get("host", "")
    seed_port = int(seed_info.get("port", "5433"))
    with stage_timer("detect_capabilities", _log):
        caps = detect_capabilities(seed_dsn)

    if ensure_ycql_extension:
        with stage_timer("ensure_ycql_extension", _log):
            with connect(seed_dsn) as conn:
                Q.ensure_yb_ycql_utils_extension(conn)

    statements_per_node_out, ycql_per_node_out, ash_per_node_out, tablets_per_node_out = (
        _collect_nodes_parallel(
            seed_dsn=seed_dsn,
            nodes=nodes,
            caps=caps,
            ash_start=ash_start,
            ash_end=ash_end,
            ash_window_sec=ash_window_sec,
            statements_per_node=statements_per_node,
            ash_per_node=ash_per_node,
            node_parallelism=node_parallelism,
        )
    )

    top_tables: list[dict[str, Any]] = []
    table_schemas: dict[str, Any] = {}
    if ash_top_tables > 0:
        with stage_timer("top_ash_table_ids", _log, limit=ash_top_tables) as st:
            top_tables = top_ash_table_ids(ash_per_node_out.values(), limit=ash_top_tables)
            st.row_count = len(top_tables)
        if collect_table_ddl and top_tables:
            with stage_timer("lookup_tablet_meta", _log, table_count=len(top_tables)) as st:
                tablet_meta = lookup_tablet_meta_by_table_id(
                    tablets_per_node_out,
                    [str(t["table_id"]) for t in top_tables],
                )
                st.row_count = len(tablet_meta)
            for ent in top_tables:
                tid = str(ent["table_id"])
                ent["engine"] = resolve_table_engine(
                    tid,
                    tablet=tablet_meta.get(tid.lower()),
                )
            with stage_timer("collect_table_schemas", _log, table_count=len(top_tables)) as st:
                raw_schemas = collect_table_schemas(seed_dsn, top_tables, tablet_meta)
                st.row_count = len(raw_schemas)
            table_schemas = {
                tid: json.loads(json.dumps(entry, default=_json_default))
                for tid, entry in raw_schemas.items()
            }

    now = datetime.now(timezone.utc)
    doc: dict[str, Any] = {
        "format_version": 1,
        "generated_at_utc": now.isoformat(),
        "ash_window": {
            "start_utc": ash_start.isoformat(),
            "end_utc": ash_end.isoformat(),
        },
        "seed": {"host": seed_host, "port": seed_port, "dbname": seed_info.get("dbname", "")},
        "nodes": nids,
        "node_topology": node_topology,
        "pg_stat_statements": {"per_node": statements_per_node_out},
        "ycql_stat_statements": {"per_node": ycql_per_node_out},
        "yb_active_session_history": {"per_node": ash_per_node_out},
        "yb_local_tablets": {"per_node": tablets_per_node_out},
    }
    if ash_top_tables > 0:
        doc["ash_top_tables"] = {
            "limit": ash_top_tables,
            "tables": top_tables,
        }
    if table_schemas:
        doc["table_schemas"] = {"by_table_id": table_schemas}
    return doc


def _sum_calls_per_node(per_node: Any) -> int:
    if not isinstance(per_node, dict):
        return 0
    total = 0
    for rows in per_node.values():
        if not isinstance(rows, list):
            continue
        for r in rows:
            if not isinstance(r, dict):
                continue
            v = r.get("calls")
            try:
                total += int(v) if v is not None else 0
            except (TypeError, ValueError):
                continue
    return total


def _call_totals_from_doc(doc: dict[str, Any]) -> tuple[int, int]:
    """(ysql_total_calls, ycql_total_calls). Cumulative since instance start."""
    pg = doc.get("pg_stat_statements") or {}
    yc = doc.get("ycql_stat_statements") or {}
    ysql = _sum_calls_per_node(pg.get("per_node"))
    ycql = _sum_calls_per_node(yc.get("per_node"))
    return ysql, ycql


def _parse_iso_utc(s: str) -> datetime:
    t = s.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    return datetime.fromisoformat(t).astimezone(timezone.utc)


def read_manifest_entries(output_dir: Path) -> list[dict[str, Any]]:
    """Return manifest `entries` (newest last), or an empty list if missing or invalid."""
    output_dir = output_dir.resolve()
    manifest_path = output_dir / MANIFEST_FILENAME
    if not manifest_path.is_file():
        return []
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if isinstance(raw, list):
        return [e for e in raw if isinstance(e, dict)]
    if isinstance(raw, dict) and isinstance(raw.get("entries"), list):
        return [e for e in raw["entries"] if isinstance(e, dict)]
    return []


def load_snapshot_json(output_dir: Path, filename: str) -> Optional[dict[str, Any]]:
    path = (output_dir / filename).resolve()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _snapshot_filename_ts(when: datetime) -> str:
    """UTC timestamp for ybtop.out.YYYYMMDD_HHMMSS.json"""
    w = when.astimezone(timezone.utc)
    return f"{SNAPSHOT_FILE_PREFIX}{w.strftime('%Y%m%d_%H%M%S')}.json"


def write_snapshot_and_update_manifest(
    *,
    output_dir: Path,
    document: dict[str, Any],
) -> Path:
    """Write snapshot JSON and append relative entry to ybtop.manifest.json."""
    with stage_timer("write_snapshot", _log):
        output_dir = output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        ts = _parse_iso_utc(document["generated_at_utc"])
        name = _snapshot_filename_ts(ts)
        snap_path = output_dir / name
        _atomic_write_json(snap_path, document)
        snap_bytes = snap_path.stat().st_size

        manifest_path = output_dir / MANIFEST_FILENAME
        rel_name = name
        ysql_calls, ycql_calls = _call_totals_from_doc(document)
        entry = {
            "file": rel_name,
            "utc": document["generated_at_utc"],
            "bytes": snap_bytes,
            "ysql_calls": ysql_calls,
            "ycql_calls": ycql_calls,
        }

        entries: list[dict[str, Any]] = []
        if manifest_path.is_file():
            try:
                prev = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(prev, list):
                    entries = prev
                elif isinstance(prev, dict) and isinstance(prev.get("entries"), list):
                    entries = list(prev["entries"])
            except (json.JSONDecodeError, OSError):
                entries = []

        entries.append(entry)
        manifest_payload = {"format_version": 1, "entries": entries}
        _atomic_write_json(manifest_path, manifest_payload)
        log_event(
            _log,
            "snapshot_written",
            snapshot_file=rel_name,
            snapshot_bytes=snap_bytes,
            manifest_entries=len(entries),
        )
        return snap_path


def gc_snapshots_and_manifest(
    *,
    output_dir: Path,
    retention_hours: float,
    now: Optional[datetime] = None,
) -> None:
    """Delete snapshot files older than retention and prune manifest entries."""
    if retention_hours <= 0:
        return
    with stage_timer("gc_snapshots", _log, retention_hours=retention_hours):
        output_dir = output_dir.resolve()
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=retention_hours)
        pattern = str(output_dir / f"{SNAPSHOT_FILE_PREFIX}*.json")
        manifest_path = output_dir / MANIFEST_FILENAME
        deleted = 0

        def file_mtime_utc(p: Path) -> Optional[datetime]:
            try:
                ts = p.stat().st_mtime
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            except OSError:
                return None

        for path in glob.glob(pattern):
            p = Path(path)
            mt = file_mtime_utc(p)
            if mt is not None and mt < cutoff:
                try:
                    p.unlink()
                    deleted += 1
                except OSError:
                    pass

        if not manifest_path.is_file():
            log_event(_log, "gc_complete", deleted_files=deleted, manifest_entries=0)
            return
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log_event(_log, "gc_complete", deleted_files=deleted, manifest_entries=0)
            return
        if isinstance(raw, list):
            entries = raw
        elif isinstance(raw, dict) and isinstance(raw.get("entries"), list):
            entries = list(raw["entries"])
        else:
            log_event(_log, "gc_complete", deleted_files=deleted, manifest_entries=0)
            return

        kept: list[dict[str, Any]] = []
        for e in entries:
            rel = e.get("file")
            if not rel:
                continue
            p = output_dir / rel
            if not p.is_file():
                continue
            mt = file_mtime_utc(p)
            if mt is not None and mt >= cutoff:
                kept.append(e)

        _atomic_write_json(manifest_path, {"format_version": 1, "entries": kept})
        log_event(_log, "gc_complete", deleted_files=deleted, manifest_entries=len(kept))
