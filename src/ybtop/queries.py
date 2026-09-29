from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

import psycopg
from psycopg import errors as pg_errors
from psycopg import sql as pg_sql

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


# pg_hint_plan hint-table rows are keyed (norm_query_string, application_name); ybtop
# pins under the empty application_name, which pg_hint_plan treats as "any client".
HINT_TABLE_APPLICATION_NAME = ""


def qpm_status(conn: psycopg.Connection) -> dict[str, Any]:
    """QPM availability, tracking mode, and plan-pinning prerequisites.

    Read fresh on the seed connection each checkpoint rather than through the
    cached Capabilities, because an operator can change yb_pg_stat_plans_track
    without restarting the collector and the viewer's guardrail has to notice.

    These are the seed node's effective settings. QPM storage is per node, so a
    cluster with divergent tserver flags could differ elsewhere; the viewer labels
    the value as the seed's for that reason.
    """
    sql = """
    SELECT
        (SELECT EXISTS (
            SELECT 1 FROM pg_catalog.pg_views WHERE viewname = 'yb_pg_stat_plans'
        )) AS view_present,
        (SELECT current_setting('yb_pg_stat_plans_track', true)) AS track,
        (SELECT current_setting('yb_pg_stat_plans_plan_format', true)) AS plan_format,
        (SELECT current_setting('yb_pg_stat_plans_verbose_plans', true)) AS verbose_plans,
        (SELECT EXISTS (
            SELECT 1 FROM pg_catalog.pg_extension WHERE extname = 'pg_hint_plan'
        )) AS hint_plan_installed,
        (SELECT EXISTS (
            SELECT 1 FROM pg_catalog.pg_available_extensions WHERE name = 'pg_hint_plan'
        )) AS hint_plan_available,
        (SELECT EXISTS (
            SELECT 1
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'hint_plan' AND c.relname = 'hints'
        )) AS hint_table_present,
        (SELECT current_setting('pg_hint_plan.enable_hint_table', true)) AS enable_hint_table,
        (SELECT current_setting('pg_hint_plan.yb_use_query_id_for_hinting', true))
            AS use_query_id_for_hinting
    /* __YB_STAT_PLANS_SKIP */;
    """
    rows = fetch_all(conn, sql)
    return dict(rows[0]) if rows else {}


# Both must be ON in a session for a hint-table row keyed by queryid to be applied.
# Hardcoded, never taken from a request: they are interpolated into ALTER DATABASE
# as SQL text (a GUC name is not an identifier and cannot be quoted as one).
HINT_TABLE_GUCS = (
    "pg_hint_plan.enable_hint_table",
    "pg_hint_plan.yb_use_query_id_for_hinting",
)

_HINT_GUC_NAME_OK = re.compile(r"^pg_hint_plan\.[a-z_]+$")


def set_hint_table_gucs(conn: psycopg.Connection, enable: bool) -> dict[str, Any]:
    """Turn pg_hint_plan's hint-table lookup on or off for the current database.

    ``ALTER DATABASE ... SET`` rather than ``SET``: a GUC set in ybtop's own session
    would do nothing for the application whose plans you are trying to change.

    Only affects sessions started afterwards. Existing connections -- and backends
    held by a pooler such as YSQL Connection Manager -- keep the old value until
    they are recycled, so a pinned hint may not bite immediately.

    The database name is taken from the live connection (never a request) and quoted
    as an identifier; the GUC names are module constants, re-checked here so a later
    edit cannot turn this into an injection point.
    """
    for guc in HINT_TABLE_GUCS:
        if not _HINT_GUC_NAME_OK.match(guc):
            raise ValueError("refusing to ALTER DATABASE with GUC name %r" % guc)
    rows = fetch_all(conn, "SELECT current_database() AS db /* __YB_STAT_PLANS_SKIP */")
    dbname = str(rows[0]["db"]) if rows else ""
    if not dbname:
        raise ValueError("could not determine current database")
    applied: list[str] = []
    with conn.cursor() as cur:
        for guc in HINT_TABLE_GUCS:
            if enable:
                stmt = pg_sql.SQL("ALTER DATABASE {db} SET " + guc + " = on").format(
                    db=pg_sql.Identifier(dbname)
                )
            else:
                stmt = pg_sql.SQL("ALTER DATABASE {db} RESET " + guc).format(
                    db=pg_sql.Identifier(dbname)
                )
            cur.execute(stmt)
            applied.append(guc)
    conn.commit()
    return {"database": dbname, "enabled": bool(enable), "gucs": applied}


def database_names(conn: psycopg.Connection) -> dict[str, str]:
    """oid (as text) -> datname for every database.

    QPM rows carry only dbid while pg_stat_statements rows carry only dbname, so
    snapshots need this map to tell which database a plan belongs to -- and a
    dbid missing from it is a database that has since been dropped.
    """
    rows = fetch_all(
        conn,
        "SELECT oid::text AS oid, datname::text AS datname FROM pg_catalog.pg_database"
        " /* __YB_STAT_PLANS_SKIP */",
    )
    return {str(r["oid"]): str(r["datname"]) for r in rows}


def role_names(conn: psycopg.Connection) -> dict[str, str]:
    """oid (as text) -> rolname for every role.

    QPM rows carry only userid; with the names in the snapshot, an EXPLAIN
    ANALYZE can name the role it will run as before anything connects.
    """
    rows = fetch_all(
        conn,
        "SELECT oid::text AS oid, rolname::text AS rolname FROM pg_catalog.pg_roles"
        " /* __YB_STAT_PLANS_SKIP */",
    )
    return {str(r["oid"]): str(r["rolname"]) for r in rows}


def database_name(conn: psycopg.Connection, dbid: str) -> Optional[str]:
    """datname for one database oid, or None if it no longer exists."""
    rows = fetch_all(
        conn,
        "SELECT datname::text AS datname FROM pg_catalog.pg_database WHERE oid = %(oid)s::oid"
        " /* __YB_STAT_PLANS_SKIP */",
        {"oid": str(dbid)},
    )
    return str(rows[0]["datname"]) if rows else None


_QUERYID_TEXT = re.compile(r"^-?\d{1,20}$")


def queryid_hint_keys(keys: Any) -> list[str]:
    """The hint-table keys that are queryids, sorted and de-duplicated.

    With yb_use_query_id_for_hinting the key is the queryid as text. Classic
    pg_hint_plan rows keyed by normalized query text are not queryids and cannot be
    matched to a statement row, so they are left out rather than guessed at.
    """
    out = set()
    for k in keys or []:
        t = str(k).strip() if k is not None else ""
        if _QUERYID_TEXT.match(t):
            out.add(t)
    return sorted(out)


def pinned_queryids(conn: psycopg.Connection) -> Optional[list[str]]:
    """queryids with a hint pinned in this database, or None without pg_hint_plan."""
    try:
        rows = fetch_all(
            conn,
            "SELECT DISTINCT norm_query_string AS k FROM hint_plan.hints /* __YB_STAT_PLANS_SKIP */",
        )
    except (pg_errors.UndefinedTable, pg_errors.InvalidSchemaName):
        conn.rollback()
        return None
    return queryid_hint_keys(r.get("k") for r in rows)


def install_hint_plan(conn: psycopg.Connection) -> None:
    """CREATE EXTENSION pg_hint_plan in the current database.

    This only creates the hint_plan schema and its empty hints table. Nothing
    about any plan changes until a hint is pinned and the hint-table GUCs are on.
    """
    execute_ddl(conn, "CREATE EXTENSION IF NOT EXISTS pg_hint_plan")


def hint_table_row(conn: psycopg.Connection, queryid: str) -> Optional[dict[str, Any]]:
    """The hint currently pinned for this queryid, or None.

    Returns None (rather than raising) when pg_hint_plan is not installed, so
    callers can treat "no hint table" and "no hint" the same way.
    """
    sql = """
    SELECT id, norm_query_string, application_name, hints
    FROM hint_plan.hints
    WHERE norm_query_string = %(qid)s AND application_name = %(app)s
    /* __YB_STAT_PLANS_SKIP */;
    """
    try:
        rows = fetch_all(conn, sql, {"qid": str(queryid), "app": HINT_TABLE_APPLICATION_NAME})
    except (pg_errors.UndefinedTable, pg_errors.InvalidSchemaName):
        conn.rollback()
        return None
    return dict(rows[0]) if rows else None


def pin_hint(conn: psycopg.Connection, queryid: str, hints_body: str) -> dict[str, Any]:
    """Pin `hints_body` for `queryid` in pg_hint_plan's hint table.

    `hints_body` is the hint list WITHOUT the surrounding ``/*+`` and ``*/``;
    pg_hint_plan stores the bare body. Both values are bound as parameters -- the
    hint text originates from a snapshot ybtop collected, never from a request
    body, and is never interpolated into SQL.

    Upserts so re-pinning a query replaces its hint instead of failing.
    """
    sql = """
    INSERT INTO hint_plan.hints (norm_query_string, application_name, hints)
    VALUES (%(qid)s, %(app)s, %(hints)s)
    ON CONFLICT (norm_query_string, application_name)
    DO UPDATE SET hints = EXCLUDED.hints
    RETURNING id, norm_query_string, application_name, hints
    /* __YB_STAT_PLANS_SKIP */;
    """
    rows = fetch_all(
        conn,
        sql,
        {"qid": str(queryid), "app": HINT_TABLE_APPLICATION_NAME, "hints": hints_body},
    )
    conn.commit()
    return dict(rows[0]) if rows else {}


def unpin_hint(conn: psycopg.Connection, queryid: str) -> int:
    """Remove this queryid's pinned hint. Returns the number of rows deleted."""
    sql = """
    DELETE FROM hint_plan.hints
    WHERE norm_query_string = %(qid)s AND application_name = %(app)s
    RETURNING id /* __YB_STAT_PLANS_SKIP */;
    """
    rows = fetch_all(conn, sql, {"qid": str(queryid), "app": HINT_TABLE_APPLICATION_NAME})
    conn.commit()
    return len(rows)


def strip_hint_wrapper(hints: str) -> str:
    """``/*+ IndexScan(t) */`` -> ``IndexScan(t)``.

    pg_hint_plan's hint table stores the bare body; the docs do this with
    ``substring(hints from 5 for char_length(hints) - 7)``, which assumes the
    exact ``/*+ `` / ``` */``` spelling. This is the same transformation done
    tolerantly, so an unexpected amount of whitespace does not silently shift the
    text by a character and pin a corrupt hint.
    """
    body = (hints or "").strip()
    if body.startswith("/*+"):
        body = body[3:]
    if body.endswith("*/"):
        body = body[:-2]
    return body.strip()


def yb_pg_stat_plans_rows(
    conn: psycopg.Connection, limit: int, top_queryids: "list[str] | None" = None
) -> list[dict[str, Any]]:
    """QPM plan history for this node. Requires caps.qpm_stat_plans (YB 2025.2.3+).

    yb_pg_stat_plans is per-node storage, so this must run on every node: the same
    queryid can have a different plan set on each one.

    queryid/planid are int64 and are cast to text; JS numbers cannot hold them exactly.
    The __YB_STAT_PLANS_SKIP marker keeps ybtop's own collection out of QPM -- without
    it the collector's queries become QPM entries and pollute the data they report on.
    If `limit` truncates, what it keeps first is the plans of this node's
    pg_stat_statements top N (`top_queryids`) -- the statements the snapshot is
    scoped to afterwards -- and then the most recently used. Ordering by recency
    alone let other statements' recent plans fill the budget and drop a heavy
    statement's older plans before scoping ever saw them.
    """
    sql = """
    SELECT
        p.queryid::text AS queryid,
        p.planid::text AS planid,
        p.dbid::text AS dbid,
        p.userid::text AS userid,
        p.calls::bigint AS calls,
        p.avg_exec_time::float8 AS avg_exec_time,
        p.max_exec_time::float8 AS max_exec_time,
        p.avg_est_cost::float8 AS avg_est_cost,
        p.first_used AS first_used,
        p.last_used AS last_used,
        p.plan::text AS plan,
        p.hints::text AS hints
    FROM yb_pg_stat_plans p
    ORDER BY (p.queryid = ANY(%(top)s::bigint[])) DESC, p.last_used DESC
    LIMIT %(limit)s /* __YB_STAT_PLANS_SKIP */;
    """
    return fetch_all(conn, sql, {"limit": limit, "top": [str(q) for q in (top_queryids or [])]})


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
