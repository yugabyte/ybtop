from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import psycopg.conninfo

# CLI / Settings defaults (single source of truth for help text and dataclass fallbacks)
DEFAULT_REFRESH_INTERVAL_SEC = 60.0
DEFAULT_ASH_WINDOW_MINUTES = 5
DEFAULT_YSQL_PORT = 5433
DEFAULT_YSQL_USER = "yugabyte"
DEFAULT_YSQL_DBNAME = "yugabyte"

# Snapshot JSON (watch mode)
DEFAULT_SNAPSHOT_OUTPUT_DIR = "."
DEFAULT_SNAPSHOT_RETENTION_HOURS = 3.0
SNAPSHOT_STATEMENTS_PER_NODE = 200
SNAPSHOT_ASH_PER_NODE = 1000
SNAPSHOT_ASH_TOP_TABLES = 25
MANIFEST_FILENAME = "ybtop.manifest.json"
SNAPSHOT_FILE_PREFIX = "ybtop.out."

DEFAULT_SERVE_HOST = "127.0.0.1"
DEFAULT_SERVE_PORT = 8765


@dataclass(frozen=True)
class Settings:
    seed_dsn: str
    refresh_interval: float = DEFAULT_REFRESH_INTERVAL_SEC
    ash_window_minutes: int = DEFAULT_ASH_WINDOW_MINUTES
    ash_start: Optional[datetime] = None
    ash_end: Optional[datetime] = None
    snapshot_output_dir: str = DEFAULT_SNAPSHOT_OUTPUT_DIR
    snapshot_retention_hours: float = DEFAULT_SNAPSHOT_RETENTION_HOURS
    snapshot_statements_per_node: int = SNAPSHOT_STATEMENTS_PER_NODE
    snapshot_ash_per_node: int = SNAPSHOT_ASH_PER_NODE
    snapshot_ash_top_tables: int = SNAPSHOT_ASH_TOP_TABLES
    snapshot_collect_table_ddl: bool = False


def load_dsn_from_env_or_none() -> Optional[str]:
    return os.environ.get("YBTOP_DSN") or os.environ.get("DATABASE_URL")


def resolve_seed_dsn(
    *,
    dsn: Optional[str],
    host: Optional[str],
    port: int,
    user: str,
    password: Optional[str],
    dbname: str,
) -> str:
    if dsn:
        return dsn
    if not host:
        raise SystemExit(
            "Provide a seed connection: --dsn (or YBTOP_DSN / DATABASE_URL), or --host for any node."
        )
    parts: dict[str, str] = {
        "host": host,
        "port": str(port),
        "user": user,
        "dbname": dbname,
    }
    if password:
        parts["password"] = password
    return psycopg.conninfo.make_conninfo(**parts)


def resolve_ash_range(settings: Settings) -> tuple[datetime, datetime]:
    """ASH window: explicit [ash_start, ash_end) or rolling window ending at UTC now."""
    if settings.ash_start is not None and settings.ash_end is not None:
        return settings.ash_start, settings.ash_end
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=settings.ash_window_minutes)
    return start, end
