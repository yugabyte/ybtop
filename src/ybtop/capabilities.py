from __future__ import annotations

from dataclasses import dataclass

import psycopg
from psycopg import errors as pg_errors

from ybtop.db import connect, fetch_all


@dataclass(frozen=True)
class Capabilities:
    """Version-specific SQL behavior for one YugabyteDB / PostgreSQL cluster."""

    pg_stat_use_exec_time: bool
    yb_ash_range_function: bool
    pg_stat_docdb_metrics: bool
    pg_stat_latency_histogram: bool
    qpm_stat_plans: bool

    @staticmethod
    def detect(conn: psycopg.Connection) -> Capabilities:
        return Capabilities(
            pg_stat_use_exec_time=_pg_stat_use_exec_time_columns(conn),
            yb_ash_range_function=_yb_ash_two_arg_range_function_exists(conn),
            pg_stat_docdb_metrics=_pg_stat_has_docdb_seeks(conn),
            pg_stat_latency_histogram=_pg_stat_has_latency_histogram(conn),
            qpm_stat_plans=_qpm_stat_plans_view_exists(conn),
        )


# Cache by seed DSN so fan-out to many nodes does not re-probe.
_caps_cache: dict[str, Capabilities] = {}


def clear_capabilities_cache() -> None:
    """Mostly for tests; normal CLI reuse is fine."""
    _caps_cache.clear()


def detect_capabilities(seed_dsn: str) -> Capabilities:
    """Probe once per seed DSN (cached); all nodes assumed same major version."""
    if seed_dsn in _caps_cache:
        return _caps_cache[seed_dsn]
    with connect(seed_dsn) as conn:
        caps = Capabilities.detect(conn)
        _caps_cache[seed_dsn] = caps
        return caps


def _server_version_num(conn: psycopg.Connection) -> int:
    try:
        rows = fetch_all(conn, "SELECT current_setting('server_version_num', true) AS v")
        v = rows[0].get("v") if rows else None
        if v is None or v == "":
            return 0
        return int(str(v))
    except (TypeError, ValueError, KeyError, IndexError):
        return 0


def _pg_stat_use_exec_time_columns(conn: psycopg.Connection) -> bool:
    """PG13+ uses total_exec_time / mean_exec_time; PG11-style uses total_time / mean_time."""
    vn = _server_version_num(conn)
    if vn > 0:
        return vn >= 130000
    try:
        fetch_all(conn, "SELECT total_exec_time FROM pg_stat_statements LIMIT 0")
        return True
    except pg_errors.UndefinedColumn:
        conn.rollback()
        return False


def _pg_stat_has_docdb_seeks(conn: psycopg.Connection) -> bool:
    """Newer Yugabyte exposes DocDB counters on pg_stat_statements (probe docdb_seeks)."""
    try:
        fetch_all(conn, "SELECT docdb_seeks FROM pg_stat_statements LIMIT 0")
        return True
    except pg_errors.UndefinedColumn:
        conn.rollback()
        return False


def _pg_stat_has_latency_histogram(conn: psycopg.Connection) -> bool:
    """YugabyteDB exposes a per-statement yb_latency_histogram column on pg_stat_statements."""
    try:
        fetch_all(conn, "SELECT yb_latency_histogram FROM pg_stat_statements LIMIT 0")
        return True
    except pg_errors.UndefinedColumn:
        conn.rollback()
        return False


def _qpm_stat_plans_view_exists(conn: psycopg.Connection) -> bool:
    """Query Plan Management (yb_pg_stat_plans) exists on YB 2025.2.3+; absent on older clusters."""
    try:
        fetch_all(conn, "SELECT planid FROM yb_pg_stat_plans LIMIT 0 /* __YB_STAT_PLANS_SKIP */")
        return True
    except (pg_errors.UndefinedTable, pg_errors.UndefinedColumn, pg_errors.InsufficientPrivilege):
        conn.rollback()
        return False


def _yb_ash_two_arg_range_function_exists(conn: psycopg.Connection) -> bool:
    """True when yb_active_session_history(timestamptz, timestamptz) exists (preferred on newer YB)."""
    try:
        rows = fetch_all(
            conn,
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_proc p
                WHERE p.proname = 'yb_active_session_history'
                  AND p.pronargs = 2
            ) AS e
            """,
        )
        return bool(rows and rows[0].get("e"))
    except Exception:
        return False
