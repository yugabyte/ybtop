from __future__ import annotations

import glob
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import psycopg.conninfo

from ybtop import queries as Q
from ybtop.capabilities import detect_capabilities
from ybtop.config import MANIFEST_FILENAME, SNAPSHOT_FILE_PREFIX
from ybtop.db import connect
from ybtop.merge import top_ash_table_ids
from ybtop.table_schema import collect_table_schemas, lookup_tablet_meta_by_table_id, resolve_table_engine
from ybtop.topology import discover_ysql_nodes, dsn_for_node, node_id


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
) -> dict[str, Any]:
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
    caps = detect_capabilities(seed_dsn)

    statements_per_node_out: dict[str, list[dict[str, Any]]] = {}
    ycql_per_node_out: dict[str, list[dict[str, Any]]] = {}
    ash_per_node_out: dict[str, list[dict[str, Any]]] = {}
    tablets_per_node_out: dict[str, list[dict[str, Any]]] = {}

    if ensure_ycql_extension:
        with connect(seed_dsn) as conn:
            Q.ensure_yb_ycql_utils_extension(conn)

    for n in nodes:
        nid = node_id(n)
        dsn = dsn_for_node(seed_dsn, n)
        with connect(dsn) as conn:
            statements_per_node_out[nid] = _serialize_rows(
                Q.pg_stat_statements_top(conn, statements_per_node, caps)
            )
            ycql_per_node_out[nid] = _serialize_rows(
                Q.ycql_stat_statements_top(conn, statements_per_node)
            )
            ash_per_node_out[nid] = _serialize_rows(
                Q.ash_aggregated(conn, ash_start, ash_end, caps, outer_limit=ash_per_node)
            )
            tablets_per_node_out[nid] = _serialize_rows(Q.yb_local_tablets_rows(conn))

    top_tables: list[dict[str, Any]] = []
    table_schemas: dict[str, Any] = {}
    if ash_top_tables > 0:
        top_tables = top_ash_table_ids(ash_per_node_out.values(), limit=ash_top_tables)
        if collect_table_ddl and top_tables:
            tablet_meta = lookup_tablet_meta_by_table_id(
                tablets_per_node_out,
                [str(t["table_id"]) for t in top_tables],
            )
            for ent in top_tables:
                tid = str(ent["table_id"])
                ent["engine"] = resolve_table_engine(
                    tid,
                    tablet=tablet_meta.get(tid.lower()),
                )
            raw_schemas = collect_table_schemas(seed_dsn, top_tables, tablet_meta)
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
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = _parse_iso_utc(document["generated_at_utc"])
    name = _snapshot_filename_ts(ts)
    snap_path = output_dir / name
    _atomic_write_json(snap_path, document)

    manifest_path = output_dir / MANIFEST_FILENAME
    rel_name = name
    entry = {"file": rel_name, "utc": document["generated_at_utc"]}

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
    output_dir = output_dir.resolve()
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=retention_hours)
    pattern = str(output_dir / f"{SNAPSHOT_FILE_PREFIX}*.json")
    manifest_path = output_dir / MANIFEST_FILENAME

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
            except OSError:
                pass

    if not manifest_path.is_file():
        return
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if isinstance(raw, list):
        entries = raw
    elif isinstance(raw, dict) and isinstance(raw.get("entries"), list):
        entries = list(raw["entries"])
    else:
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
