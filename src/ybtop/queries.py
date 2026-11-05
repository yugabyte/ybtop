from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import psycopg

from ybtop.capabilities import Capabilities
from ybtop.db import execute_ddl, fetch_all
from ybtop.pg_stat_constants import PG_STAT_DOCDB_OPTIONAL_COLUMNS


def _pg_stat_time_select(caps: Capabilities) -> str:
    if caps.pg_stat_use_exec_time:
        return """s.total_exec_time::float8 AS total_exec_time,
        s.mean_exec_time::float8 AS mean_exec_time"""
    return """s.total_time::float8 AS total_exec_time,
        s.mean_time::float8 AS mean_exec_time"""


def _pg_stat_order_by_time(caps: Capabilities) -> str:
    return "s.total_exec_time DESC" if caps.pg_stat_use_exec_time else "s.total_time DESC"


def _ash_from_clause(caps: Capabilities) -> str:
    if caps.yb_ash_range_function:
        return "FROM yb_active_session_history(%(t1)s::timestamptz, %(t2)s::timestamptz) ash"
    return (
        "FROM yb_active_session_history ash\n"
        "        WHERE ash.sample_time >= %(t1)s::timestamptz AND ash.sample_time < %(t2)s::timestamptz"
    )


def _ash_is_ycql_component_sql() -> str:
    return "UPPER(BTRIM(COALESCE(s.wait_event_component::text, ''))) = 'YCQL'"


def _ash_namespace_name_select_sql() -> str:
    """YCQL keyspace comes from yb_local_tablets; other components may fall back to pg_database."""
    is_ycql = _ash_is_ycql_component_sql()
    return f"""NULLIF(BTRIM(
        CASE
            WHEN {is_ycql} THEN lt.namespace_name::text
            ELSE COALESCE(lt.namespace_name::text, d.datname::text)
        END
    ), '') AS namespace_name"""


def _ash_table_id_select_sql() -> str:
    """Full catalog table_id from yb_local_tablets (never the short wait_event_aux prefix)."""
    return "NULLIF(BTRIM(lt.table_id::text), '') AS table_id"


def _ash_local_tablets_lateral_join_sql() -> str:
    """
    Resolve namespace/table from yb_local_tablets.

    Non-YCQL: wait_event_aux is a tablet_id prefix (first 15 chars).
    YCQL: wait_event_aux is a table_id prefix (first 15 chars); snapshot stores full table_id.
    """
    is_ycql = _ash_is_ycql_component_sql()
    return f"""
    LEFT JOIN LATERAL (
        SELECT
            lt1.namespace_name::text AS namespace_name,
            lt1.table_name::text AS table_name,
            lt1.table_id::text AS table_id
        FROM yb_local_tablets lt1
        WHERE s.wait_event_aux IS NOT NULL
          AND (
            ({is_ycql} AND s.wait_event_aux = SUBSTRING(lt1.table_id::text, 1, 15))
            OR (NOT ({is_ycql}) AND s.wait_event_aux = SUBSTRING(lt1.tablet_id::text, 1, 15))
          )
        LIMIT 1
    ) lt ON TRUE"""


def _pg_stat_rows_and_docdb(caps: Capabilities) -> str:
    parts = ["s.rows::float8 AS rows"]
    if caps.pg_stat_docdb_metrics:
        for c in PG_STAT_DOCDB_OPTIONAL_COLUMNS:
            parts.append(f"s.{c}::float8 AS {c}")
    return ",\n        ".join(parts)


def pg_stat_statements_raw(conn: psycopg.Connection, caps: Capabilities) -> list[dict[str, Any]]:
    time_cols = _pg_stat_time_select(caps)
    extra = _pg_stat_rows_and_docdb(caps)
    sql = f"""
    SELECT
        s.queryid::text AS queryid,
        s.query,
        s.calls::bigint AS calls,
        {time_cols},
        {extra},
        NULLIF(BTRIM(db.datname::text), '') AS dbname
    FROM pg_stat_statements s
    LEFT JOIN pg_database db ON db.oid = s.dbid
    """
    return fetch_all(conn, sql)


def pg_stat_statements_top(
    conn: psycopg.Connection,
    limit: int,
    caps: Capabilities,
    *,
    include_latency_histogram: bool = False,
) -> list[dict[str, Any]]:
    """Top-N statements by total time (``total_exec_time`` / ``total_time``).

    When ``include_latency_histogram`` is set and the cluster exposes
    ``yb_latency_histogram``, that column is selected as
    ``COALESCE(..., '{}'::jsonb)`` so NULL histograms become empty JSON objects.
    Callers that build the latency-modes section should ignore empty histograms.
    """
    time_cols = _pg_stat_time_select(caps)
    extra = _pg_stat_rows_and_docdb(caps)
    order_by = _pg_stat_order_by_time(caps)
    hist_col = ""
    if include_latency_histogram and caps.pg_stat_latency_histogram:
        hist_col = (
            ",\n        COALESCE(s.yb_latency_histogram, '{}'::jsonb) AS yb_latency_histogram"
        )
    sql = f"""
    SELECT
        s.queryid::text AS queryid,
        s.query::text AS query,
        s.calls::bigint AS calls,
        {time_cols},
        {extra},
        NULLIF(BTRIM(db.datname::text), '') AS dbname{hist_col}
    FROM pg_stat_statements s
    LEFT JOIN pg_database db ON db.oid = s.dbid
    ORDER BY {order_by}
    LIMIT %(limit)s;
    """
    return fetch_all(conn, sql, {"limit": limit})


def ash_aggregated(
    conn: psycopg.Connection,
    ash_start: datetime,
    ash_end: datetime,
    caps: Capabilities,
    outer_limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    lim_clause = ""
    params: dict[str, Any] = {"t1": ash_start, "t2": ash_end}
    if outer_limit is not None:
        lim_clause = "\n    LIMIT %(outer_limit)s"
        params["outer_limit"] = int(outer_limit)
    ash_from = _ash_from_clause(caps)
    sql = f"""
    SELECT
        s.query_id::text AS query_id,
        s.wait_event_component,
        LEFT(s.wait_event::text, 48) AS wait_event,
        s.wait_event_type,
        s.wait_event_aux,
        s.ysql_dbid,
        s.samples,
        {_ash_namespace_name_select_sql()},
        NULLIF(BTRIM(lt.table_name::text), '') AS object_name,
        {_ash_table_id_select_sql()}
    FROM (
        SELECT
            query_id,
            wait_event_component,
            wait_event,
            wait_event_type,
            wait_event_aux,
            ysql_dbid,
            COUNT(*)::bigint AS samples
        {ash_from}
        GROUP BY
            query_id,
            wait_event_component,
            wait_event,
            wait_event_type,
            wait_event_aux,
            ysql_dbid
    ) s
    LEFT JOIN pg_database d ON d.oid = s.ysql_dbid
    {_ash_local_tablets_lateral_join_sql()}
    ORDER BY s.samples DESC{lim_clause};
    """
    return fetch_all(conn, sql, params)


def ensure_yb_ycql_utils_extension(conn: psycopg.Connection) -> None:
    """Load yb_ycql_utils so ``ycql_stat_statements`` is available (idempotent)."""
    execute_ddl(conn, "CREATE EXTENSION IF NOT EXISTS yb_ycql_utils")


def ycql_stat_statements_top(conn: psycopg.Connection, limit: int) -> list[dict[str, Any]]:
    sql = """
    SELECT
        s.queryid::text AS queryid,
        s.query::text AS query,
        s.calls::bigint AS calls,
        s.total_time::float8 AS total_time,
        s.is_prepared AS is_prepared
    FROM ycql_stat_statements s
    ORDER BY s.total_time DESC
    LIMIT %(limit)s;
    """
    return fetch_all(conn, sql, {"limit": limit})


def yb_local_tablets_rows(conn: psycopg.Connection) -> list[dict[str, Any]]:
    sql = """
    SELECT
        tablet_id::text AS tablet_id,
        table_type::text AS table_type,
        table_id::text AS table_id,
        namespace_name::text AS namespace_name,
        table_name::text AS table_name,
        partition_key_start::text AS partition_key_start,
        partition_key_end::text AS partition_key_end,
        state::text AS state
    FROM yb_local_tablets
    WHERE state != 'TABLET_DATA_TOMBSTONED';
    """
    return fetch_all(conn, sql)


def _pg_activity_select(has_query_id: bool, has_backend_xid: bool) -> str:
    qid = "a.query_id::text AS query_id" if has_query_id else "NULL::text AS query_id"
    xid = "a.yb_backend_xid::text AS txn_id" if has_backend_xid else "NULL::text AS txn_id"
    return f"""
    SELECT
        a.pid::int AS pid,
        a.datname::text AS datname,
        a.usename::text AS usename,
        a.application_name::text AS application_name,
        host(a.client_addr)::text AS client_addr,
        a.state::text AS state,
        a.wait_event_type::text AS wait_event_type,
        a.wait_event::text AS wait_event,
        {qid},
        {xid},
        a.backend_start,
        a.xact_start,
        a.query_start,
        a.state_change,
        (EXTRACT(EPOCH FROM (clock_timestamp() - a.query_start)) * 1000)::float8 AS query_age_ms,
        (EXTRACT(EPOCH FROM (clock_timestamp() - a.xact_start)) * 1000)::float8 AS xact_age_ms,
        (EXTRACT(EPOCH FROM (clock_timestamp() - a.state_change)) * 1000)::float8 AS state_age_ms,
        a.query::text AS query
    FROM pg_stat_activity a
    WHERE a.backend_type = 'client backend'
      AND a.pid <> pg_backend_pid()
      AND NOT starts_with(COALESCE(a.query, ''), '/* service:ybtop */')
    """


def pg_stat_activity_rows(
    conn: psycopg.Connection, *, has_query_id: bool, has_backend_xid: bool
) -> list[dict[str, Any]]:
    """Client backends on this node (excluding this connection and other ybtop sessions)."""
    return fetch_all(conn, _pg_activity_select(has_query_id, has_backend_xid))


def ash_recent_profile(
    conn: psycopg.Connection,
    window_seconds: int,
    *,
    query_id: Optional[int] = None,
    pid: Optional[int] = None,
    node_uuid: Optional[str] = None,
) -> list[dict[str, Any]]:
    """ASH samples of the last ``window_seconds`` (DB clock), grouped like ``ash_aggregated``.

    ``query_id`` filters to one statement; ``pid`` (with ``node_uuid`` so a pid reused on
    another node is not mixed in) filters to one backend's activity.
    """
    where = ["ash.sample_time >= clock_timestamp() - make_interval(secs => %(w)s)"]
    params: dict[str, Any] = {"w": int(window_seconds)}
    if query_id is not None:
        where.append("ash.query_id = %(qid)s::bigint")
        params["qid"] = int(query_id)
    if pid is not None:
        where.append("ash.pid = %(pid)s::int")
        params["pid"] = int(pid)
        if node_uuid:
            where.append("ash.top_level_node_id = %(node_uuid)s::uuid")
            params["node_uuid"] = node_uuid
    sql = f"""
    SELECT
        s.query_id::text AS query_id,
        s.wait_event_component,
        LEFT(s.wait_event::text, 48) AS wait_event,
        s.wait_event_type,
        s.wait_event_aux,
        s.ysql_dbid,
        s.samples,
        {_ash_namespace_name_select_sql()},
        NULLIF(BTRIM(lt.table_name::text), '') AS object_name,
        {_ash_table_id_select_sql()}
    FROM (
        SELECT query_id, wait_event_component, wait_event, wait_event_type,
               wait_event_aux, ysql_dbid, COUNT(*)::bigint AS samples
        FROM yb_active_session_history ash
        WHERE {" AND ".join(where)}
        GROUP BY query_id, wait_event_component, wait_event, wait_event_type,
                 wait_event_aux, ysql_dbid
    ) s
    LEFT JOIN pg_database d ON d.oid = s.ysql_dbid
    {_ash_local_tablets_lateral_join_sql()}
    ORDER BY s.samples DESC
    LIMIT 500;
    """
    return fetch_all(conn, sql, params)


def pg_stat_statements_for_query(
    conn: psycopg.Connection, query_id: int, caps: Capabilities
) -> list[dict[str, Any]]:
    """pg_stat_statements rows (one per database) for a single ``queryid`` on this node."""
    time_cols = _pg_stat_time_select(caps)
    extra = _pg_stat_rows_and_docdb(caps)
    hist = ""
    if caps.pg_stat_latency_histogram:
        hist = ",\n        COALESCE(s.yb_latency_histogram, '{}'::jsonb) AS yb_latency_histogram"
    sql = f"""
    SELECT
        s.queryid::text AS queryid,
        s.query::text AS query,
        s.calls::bigint AS calls,
        {time_cols},
        {extra},
        NULLIF(BTRIM(db.datname::text), '') AS dbname{hist}
    FROM pg_stat_statements s
    LEFT JOIN pg_database db ON db.oid = s.dbid
    WHERE s.queryid = %(qid)s::bigint;
    """
    return fetch_all(conn, sql, {"qid": int(query_id)})


def yb_lock_status_rows(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Cluster-wide DocDB lock holders/waiters (``yb_lock_status``), reduced to one row per
    (transaction, tablet, granted) with the first lock's relation/mode for context."""
    sql = """
    SELECT
        l.transaction_id::text AS transaction_id,
        l.granted AS granted,
        l.node::text AS node,
        l.tablet_id::text AS tablet_id,
        l.database::int AS database_oid,
        l.relation::int AS relation_oid,
        l.locktype::text AS locktype,
        l.mode::text[] AS mode,
        (EXTRACT(EPOCH FROM (clock_timestamp() - l.waitstart)) * 1000)::float8 AS wait_ms,
        (SELECT array_agg(b::text) FROM unnest(l.blocked_by) b) AS blocked_by
    FROM yb_lock_status(NULL, NULL) l;
    """
    return fetch_all(conn, sql)


def relation_names(conn: psycopg.Connection, oids: list[int]) -> dict[int, str]:
    """Resolve relation OIDs to ``schema.name`` in the current database."""
    if not oids:
        return {}
    rows = fetch_all(
        conn,
        """
        SELECT c.oid::int AS oid, n.nspname::text || '.' || c.relname::text AS name
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.oid = ANY(%(oids)s::oid[])
        """,
        {"oids": [int(o) for o in oids]},
    )
    return {int(r["oid"]): str(r["name"]) for r in rows}


def terminated_queries_rows(
    conn: psycopg.Connection, window_minutes: int, limit: int
) -> list[dict[str, Any]]:
    sql = """
    SELECT
        databasename::text AS dbname,
        backend_pid::int AS pid,
        query_id::text AS query_id,
        query_text::text AS query,
        termination_reason::text AS termination_reason,
        query_start_time,
        query_end_time
    FROM yb_terminated_queries
    WHERE query_end_time >= clock_timestamp() - make_interval(mins => %(m)s)
    ORDER BY query_end_time DESC
    LIMIT %(limit)s;
    """
    return fetch_all(conn, sql, {"m": int(window_minutes), "limit": int(limit)})


# Diagnostics: plans and indexes
def stat_plans_rows(
    conn: psycopg.Connection, *, query_id: Optional[int] = None, seq_scans_only: bool = False, limit: int = 500
) -> list[dict[str, Any]]:
    """Recorded plans from ``yb_pg_stat_plans`` (this node's cache), with statement text/time.

    Joined to ``pg_stat_statements`` for the SQL text and for excluding ybtop's own statements.
    """
    where = ["TRUE"]
    params: dict[str, Any] = {"limit": int(limit)}
    if query_id is not None:
        where.append("p.queryid = %(qid)s::bigint")
        params["qid"] = int(query_id)
    if seq_scans_only:
        where.append("strpos(p.plan, 'Seq Scan') > 0")
    sql = f"""
    SELECT
        p.queryid::text AS query_id,
        p.planid::text AS planid,
        p.calls::bigint AS calls,
        p.avg_exec_time::float8 AS avg_exec_ms,
        p.max_exec_time::float8 AS max_exec_ms,
        p.max_exec_time_params::text AS max_exec_params,
        p.avg_est_cost::float8 AS avg_est_cost,
        p.first_used, p.last_used,
        NULLIF(BTRIM(db.datname::text), '') AS dbname,
        p.plan::text AS plan,
        (SELECT s.query::text FROM pg_stat_statements s
          WHERE s.queryid = p.queryid AND s.dbid = p.dbid LIMIT 1) AS query
    FROM yb_pg_stat_plans p
    LEFT JOIN pg_database db ON db.oid = p.dbid
    WHERE {" AND ".join(where)}
    ORDER BY p.calls * p.avg_exec_time DESC
    LIMIT %(limit)s;
    """
    return fetch_all(conn, sql, params)


def index_catalog_rows(
    conn: psycopg.Connection, *, schema: Optional[str] = None, table: Optional[str] = None
) -> list[dict[str, Any]]:
    """Every index in the connected database with the flags needed for hygiene analysis."""
    where = ["n.nspname NOT IN ('pg_catalog', 'information_schema')", "n.nspname !~ '^pg_toast'"]
    params: dict[str, Any] = {}
    if schema:
        where.append("n.nspname = %(schema)s")
        params["schema"] = schema
    if table:
        where.append("t.relname = %(table)s")
        params["table"] = table
    sql = f"""
    SELECT
        x.indexrelid::int AS indexrelid,
        n.nspname::text || '.' || t.relname::text AS "table",
        i.relname::text AS "index",
        x.indisvalid AS valid, x.indisready AS ready, x.indislive AS live,
        x.indisunique AS "unique", x.indisprimary AS "primary",
        string_to_array(x.indkey::text, ' ') AS cols,
        string_to_array(x.indclass::text, ' ') AS opclasses,
        string_to_array(x.indoption::text, ' ') AS options,
        COALESCE(pg_get_expr(x.indpred, x.indrelid), '') AS predicate,
        (x.indexprs IS NOT NULL) AS has_expr,
        pg_get_indexdef(x.indexrelid) AS definition,
        COALESCE(pg_table_size(x.indexrelid), 0)::bigint AS bytes
    FROM pg_index x
    JOIN pg_class i ON i.oid = x.indexrelid
    JOIN pg_class t ON t.oid = x.indrelid
    JOIN pg_namespace n ON n.oid = t.relnamespace
    WHERE {" AND ".join(where)}
    ORDER BY 2, 3;
    """
    return fetch_all(conn, sql, params)


def index_usage_rows(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """This node's per-index scan counters (they are node-local and must be summed across nodes)."""
    return fetch_all(
        conn,
        """
        SELECT indexrelid::int AS indexrelid,
               COALESCE(idx_scan, 0)::bigint AS scans,
               COALESCE(idx_tup_read, 0)::bigint AS tup_read
        FROM pg_stat_user_indexes
        """,
    )


def table_info_rows(conn: psycopg.Connection, relnames: list[str]) -> list[dict[str, Any]]:
    if not relnames:
        return []
    return fetch_all(
        conn,
        """
        SELECT n.nspname::text AS schema, c.relname::text AS relname,
               COALESCE(s.n_live_tup, 0)::bigint AS n_live_tup,
               s.last_analyze, s.last_autoanalyze,
               COALESCE(pg_table_size(c.oid), 0)::bigint AS bytes
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
        -- relname::text: `name = ANY(text[])` is pushed down to DocDB and fails with
          -- "compressed lz4 data is corrupt" on YugabyteDB 2026.1; the cast keeps the filter in YSQL.
          WHERE c.relkind IN ('r', 'p') AND c.relname::text = ANY(%(rels)s::text[])
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
        """,
        {"rels": relnames},
    )
