from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, Optional, Union

import psycopg
from psycopg.rows import dict_row

# Prepended to every YSQL statement so logs / pg_stat / ASH can attribute load to ybtop.
SQL_SERVICE_TAG = "/* service:ybtop */"


def tag_sql(sql: str) -> str:
    """Prefix ``SQL_SERVICE_TAG`` unless the statement is already tagged that way."""
    if not sql or not sql.strip():
        return sql
    body = sql.lstrip()
    if body.startswith(SQL_SERVICE_TAG):
        return body
    # ``body`` drops leading newline/indent from ``"""\\n    SELECT ...`` style strings so
    # the tag is not followed by an extra blank line in pg_stat / logs.
    return f"{SQL_SERVICE_TAG} {body}"


def _connect_with_hint(dsn: str) -> psycopg.Connection:
    try:
        return psycopg.connect(dsn, row_factory=dict_row)
    except psycopg.OperationalError as exc:
        err = str(exc).lower()
        if "no password supplied" in err or "password not supplied" in err:
            raise psycopg.OperationalError(
                f"{exc}\n"
                "Hint: this server requires a password. Use --password, set YBTOP_PASSWORD, "
                "or put the password in the DSN, e.g. postgresql://yugabyte:YOURPASS@host:5433/yugabyte"
            ) from exc
        raise


@contextmanager
def connect(dsn: str) -> Iterator[psycopg.Connection]:
    conn = _connect_with_hint(dsn)
    try:
        yield conn
    finally:
        conn.close()


def fetch_all(
    conn: psycopg.Connection,
    sql: str,
    params: Optional[Union[Sequence[Any], Mapping[str, Any]]] = None,
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(tag_sql(sql), params or ())
        return list(cur.fetchall())


def execute_ddl(conn: psycopg.Connection, sql: str) -> None:
    """Run a DDL statement and commit (e.g. CREATE EXTENSION)."""
    with conn.cursor() as cur:
        cur.execute(tag_sql(sql))
    conn.commit()
