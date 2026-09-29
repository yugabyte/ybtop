"""Which statements have a pinned hint, per database, for the PGSS "plans" column.

Hint tables are per database, so finding pins takes one connection per database.
At ~350 ms per TLS + auth handshake that was most of a checkpoint on a
four-database cluster, nearly all of it spent learning "no hint table here". So
this checks databases in parallel, and remembers databases without a hint table
for a while: pg_hint_plan is rarely installed, and when ybtop's own Install
button does it, the server calls forget_database() and the next checkpoint
rechecks at once. Databases that do have a table are read every checkpoint,
since pins come and go.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from ybtop import queries as Q
from ybtop.db import connect, dsn_for_database

NO_TABLE_RECHECK_SEC = 600.0
MAX_WORKERS = 8

_lock = threading.Lock()
_no_table_until: dict[str, float] = {}


def forget_database(datname: str) -> None:
    """Recheck this database on the next scan (e.g. right after installing pg_hint_plan)."""
    with _lock:
        _no_table_until.pop(str(datname), None)


def reset() -> None:
    """Forget every cached result. For tests."""
    with _lock:
        _no_table_until.clear()


def scan_pinned(
    seed_dsn: str,
    names: list[str],
    oid_for_name: dict[str, str],
    *,
    now: Optional[float] = None,
    on_error: Optional[Callable[[str, Exception], None]] = None,
) -> dict[str, list[str]]:
    """dbid -> pinned queryids for `names`, skipping recently seen table-less databases.

    Never raises: pins are advisory, so a database that fails is reported through
    `on_error` and simply retried next time (errors are not cached).
    """
    t = time.monotonic() if now is None else now
    with _lock:
        todo = [n for n in names if n in oid_for_name and _no_table_until.get(n, 0.0) <= t]
    if not todo:
        return {}

    def one(name: str) -> tuple[str, Optional[list[str]], Optional[Exception]]:
        try:
            with connect(dsn_for_database(seed_dsn, name)) as conn:
                return name, Q.pinned_queryids(conn), None
        except Exception as exc:  # noqa: BLE001 - advisory
            return name, None, exc

    out: dict[str, list[str]] = {}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(todo))) as pool:
        for name, pins, err in pool.map(one, todo):
            if err is not None:
                if on_error is not None:
                    on_error(name, err)
                continue
            if pins is None:
                with _lock:
                    _no_table_until[name] = t + NO_TABLE_RECHECK_SEC
            elif pins:
                out[str(oid_for_name[name])] = pins
    return out
