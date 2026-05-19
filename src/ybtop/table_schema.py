from __future__ import annotations

import re
from typing import Any, Optional

import psycopg
import psycopg.conninfo

from ybtop.db import connect, fetch_all

_YSQL_TABLE_ID_RE = re.compile(
    r"^0000[0-9a-f]{4}00003000800000000000[0-9a-f]{4}$",
    re.IGNORECASE,
)


def is_ysql_table_id(table_id: str) -> bool:
    return bool(_YSQL_TABLE_ID_RE.match(str(table_id).strip()))


def parse_ysql_table_id(table_id: str) -> Optional[tuple[int, int]]:
    """Return (database_oid, relfilenode) for YSQL DocDB table_id hex, else None."""
    tid = str(table_id).strip().lower()
    if not _YSQL_TABLE_ID_RE.match(tid):
        return None
    try:
        return int(tid[4:8], 16), int(tid[-4:], 16)
    except ValueError:
        return None


def _norm_table_id(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def lookup_tablet_meta_by_table_id(
    tablets_per_node: dict[str, list[dict[str, Any]]],
    table_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """First yb_local_tablets row per table_id across all nodes."""
    want = {str(t).strip().lower() for t in table_ids if str(t).strip()}
    out: dict[str, dict[str, Any]] = {}
    if not want:
        return out
    for rows in tablets_per_node.values():
        for r in rows:
            tid = _norm_table_id(r.get("table_id"))
            if tid is None:
                continue
            key = tid.lower()
            if key not in want or key in out:
                continue
            out[key] = {
                "table_id": tid,
                "table_type": r.get("table_type"),
                "namespace_name": r.get("namespace_name"),
                "table_name": r.get("table_name"),
            }
    return out


def _dsn_with_dbname(seed_dsn: str, dbname: str) -> str:
    info = psycopg.conninfo.conninfo_to_dict(seed_dsn)
    info["dbname"] = dbname
    return psycopg.conninfo.make_conninfo(**info)


def _fetch_datname_for_db_oid(conn: psycopg.Connection, db_oid: int) -> Optional[str]:
    rows = fetch_all(
        conn,
        "SELECT datname::text AS datname FROM pg_database WHERE oid = %(oid)s LIMIT 1;",
        {"oid": db_oid},
    )
    if not rows:
        return None
    name = rows[0].get("datname")
    return None if name is None else str(name).strip() or None


def _fetch_ysql_relation(
    conn: psycopg.Connection, *, relfilenode: int
) -> Optional[dict[str, Any]]:
    rows = fetch_all(
        conn,
        """
        SELECT
            c.oid::bigint AS rel_oid,
            c.relkind::text AS relkind,
            n.nspname::text AS schema_name,
            c.relname::text AS rel_name
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relfilenode = %(relfilenode)s
        ORDER BY c.oid
        LIMIT 1;
        """,
        {"relfilenode": relfilenode},
    )
    return rows[0] if rows else None


def _fetch_ysql_index_ddl(conn: psycopg.Connection, rel_oid: int) -> Optional[str]:
    rows = fetch_all(
        conn,
        "SELECT pg_get_indexdef(%(oid)s::oid) AS ddl;",
        {"oid": rel_oid},
    )
    if not rows:
        return None
    ddl = rows[0].get("ddl")
    return None if ddl is None else str(ddl).strip() or None


def _fetch_ysql_table_ddl(conn: psycopg.Connection, rel_oid: int) -> Optional[str]:
    rows = fetch_all(
        conn,
        """
        WITH rel AS (
            SELECT c.oid, n.nspname, c.relname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.oid = %(oid)s AND c.relkind = 'r'
        ),
        cols AS (
            SELECT string_agg(
                '  ' || quote_ident(a.attname) || ' ' || format_type(a.atttypid, a.atttypmod)
                || CASE WHEN a.attnotnull THEN ' NOT NULL' ELSE '' END
                || CASE
                    WHEN ad.adbin IS NOT NULL
                    THEN ' DEFAULT ' || pg_get_expr(ad.adbin, ad.adrelid)
                    ELSE ''
                   END,
                E',\n' ORDER BY a.attnum
            ) AS body
            FROM rel r
            JOIN pg_attribute a ON a.attrelid = r.oid AND a.attnum > 0 AND NOT a.attisdropped
            LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
        ),
        pk AS (
            SELECT pg_get_constraintdef(con.oid, true) AS pkdef
            FROM rel r
            JOIN pg_constraint con ON con.conrelid = r.oid AND con.contype = 'p'
            LIMIT 1
        )
        SELECT
            'CREATE TABLE ' || quote_ident((SELECT nspname FROM rel)) || '.'
            || quote_ident((SELECT relname FROM rel))
            || E' (\n' || COALESCE((SELECT body FROM cols), '')
            || COALESCE(E',\n  ' || (SELECT pkdef FROM pk), '')
            || E'\n)' AS ddl
        FROM rel;
        """,
        {"oid": rel_oid},
    )
    if not rows:
        return None
    ddl = rows[0].get("ddl")
    return None if ddl is None else str(ddl).strip() or None


def _fetch_yb_table_properties_comment(conn: psycopg.Connection, rel_oid: int) -> Optional[str]:
    """Return a trailing comment with yb_table_properties when available."""
    try:
        rows = fetch_all(
            conn,
            """
            SELECT
                (yb_table_properties(format('%I.%I', n.nspname, c.relname)::regclass)).num_tablets
                    AS num_tablets,
                (yb_table_properties(format('%I.%I', n.nspname, c.relname)::regclass))
                    .num_hash_key_columns AS num_hash_key_columns,
                (yb_table_properties(format('%I.%I', n.nspname, c.relname)::regclass))
                    .is_colocated AS is_colocated
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.oid = %(oid)s;
            """,
            {"oid": rel_oid},
        )
    except psycopg.Error:
        return None
    if not rows:
        return None
    r = rows[0]
    parts: list[str] = []
    for key in ("num_tablets", "num_hash_key_columns", "is_colocated", "colocation_id"):
        if key in r and r[key] is not None:
            parts.append(f"{key}={r[key]}")
    if not parts:
        return None
    return "-- yb_table_properties: " + ", ".join(parts)


def _fetch_ysql_ddl(conn: psycopg.Connection, rel_oid: int, relkind: str) -> Optional[str]:
    if relkind == "i":
        return _fetch_ysql_index_ddl(conn, rel_oid)
    if relkind == "r":
        ddl = _fetch_ysql_table_ddl(conn, rel_oid)
        if ddl:
            props = _fetch_yb_table_properties_comment(conn, rel_oid)
            if props:
                ddl = ddl + "\n" + props
        return ddl
    return None


def resolve_table_engine(
    table_id: str,
    *,
    ash_engine: Optional[str] = None,
    tablet: Optional[dict[str, Any]] = None,
) -> str:
    """Classify YSQL vs YCQL; table_id hex layout and tablet metadata beat ASH row hints."""
    tid = str(table_id).strip()
    if is_ysql_table_id(tid):
        return "YSQL"
    if tablet:
        tt = tablet.get("table_type")
        if tt is not None:
            tts = str(tt).strip().upper()
            if tts == "YSQL":
                return "YSQL"
            if tts == "YCQL":
                return "YCQL"
    if ash_engine in ("YSQL", "YCQL"):
        return ash_engine
    return "YCQL"


def fetch_table_ddl(
    seed_dsn: str,
    *,
    table_id: str,
    engine: str,
    namespace_name: Optional[str],
    object_name: Optional[str],
    catalog_conn: Optional[psycopg.Connection] = None,
) -> dict[str, Any]:
    """Fetch DDL for one table_id; uses seed DSN (one node) and optional shared catalog conn."""
    tid = _norm_table_id(table_id) or str(table_id).strip()
    base: dict[str, Any] = {
        "table_id": tid,
        "engine": engine,
        "namespace_name": namespace_name,
        "object_name": object_name,
        "ddl": None,
        "error": None,
    }
    if engine != "YSQL":
        return base

    parsed = parse_ysql_table_id(tid)
    if parsed is None:
        base["error"] = "not a YSQL table_id hex"
        return base
    db_oid, relfilenode = parsed

    def _run(catalog: psycopg.Connection) -> dict[str, Any]:
        datname = _fetch_datname_for_db_oid(catalog, db_oid)
        if not datname:
            out = dict(base)
            out["error"] = f"database oid {db_oid} not found"
            return out
        if datname != (catalog.info.dbname or ""):
            with connect(_dsn_with_dbname(seed_dsn, datname)) as db_conn:
                return _run_on_db(db_conn, relfilenode, datname)
        return _run_on_db(catalog, relfilenode, datname)

    def _run_on_db(db_conn: psycopg.Connection, relfilenode: int, datname: str) -> dict[str, Any]:
        out = dict(base)
        out["namespace_name"] = out.get("namespace_name") or datname
        rel = _fetch_ysql_relation(db_conn, relfilenode=relfilenode)
        if not rel:
            out["error"] = f"relation relfilenode {relfilenode} not found in database {datname}"
            return out
        rel_oid = int(rel["rel_oid"])
        relkind = str(rel.get("relkind") or "")
        schema_name = str(rel.get("schema_name") or "")
        rel_name = str(rel.get("rel_name") or "")
        if schema_name and rel_name:
            out["object_name"] = rel_name
            if not out.get("namespace_name"):
                out["namespace_name"] = datname
            out["schema_name"] = schema_name
            out["qualified_name"] = f"{schema_name}.{rel_name}"
        ddl = _fetch_ysql_ddl(db_conn, rel_oid, relkind)
        if ddl:
            out["ddl"] = ddl
            out["object_kind"] = "index" if relkind == "i" else "table"
        else:
            out["error"] = f"unsupported relkind {relkind!r} or empty DDL"
        return out

    try:
        if catalog_conn is not None:
            return _run(catalog_conn)
        with connect(seed_dsn) as conn:
            return _run(conn)
    except psycopg.Error as exc:
        out = dict(base)
        out["error"] = str(exc).strip()
        return out


def collect_table_schemas(
    seed_dsn: str,
    top_tables: list[dict[str, Any]],
    tablet_meta: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Collect YSQL DDL for top ASH table_ids using a single seed-node connection."""
    by_id: dict[str, dict[str, Any]] = {}
    if not top_tables:
        return by_id
    with connect(seed_dsn) as catalog_conn:
        for ent in top_tables:
            tid = _norm_table_id(ent.get("table_id"))
            if tid is None:
                continue
            tablet = tablet_meta.get(tid.lower())
            engine = resolve_table_engine(tid, ash_engine=ent.get("engine"), tablet=tablet)
            if engine != "YSQL":
                continue
            ns = ent.get("namespace_name")
            ob = ent.get("object_name")
            if tablet:
                ns = ns or tablet.get("namespace_name")
                ob = ob or tablet.get("table_name")
            schema = fetch_table_ddl(
                seed_dsn,
                table_id=tid,
                engine=engine,
                namespace_name=None if ns is None else str(ns),
                object_name=None if ob is None else str(ob),
                catalog_conn=catalog_conn,
            )
            by_id[tid] = schema
    return by_id
