"""Live (in-flight) query inspection across a YugabyteDB universe.

Pure-Python, MCP-agnostic: every public method of :class:`LiveContext` returns plain
JSON-serializable dicts so the MCP layer (``ybtop.mcp_server``) stays a thin wrapper.

All queries run on short-lived **read-only** sessions with a ``statement_timeout``
(see ``ybtop.db.connect_readonly``), fanned out across the nodes discovered from
``yb_servers()``. Partial failures are reported under ``errors`` instead of failing the call.
"""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from threading import Lock
from typing import Any, Callable, Optional

import psycopg
import psycopg.conninfo

from ybtop import queries as Q
from ybtop.capabilities import Capabilities
from ybtop.config import DEFAULT_NODE_PARALLELISM
from ybtop.db import connect_readonly, fetch_all
from ybtop.histogram import normalize_query_template, parse_histogram_buckets
from ybtop.merge import merge_pg_stat_statements
from ybtop.topology import YsqlNode, discover_ysql_nodes, dsn_for_node, dsn_with_database, node_id

_IN_FLIGHT_STATES = ("active", "fastpath function call")
_MAX_LIMIT = 500
_NODES_TTL_SEC = 30.0
_CONNECT_TIMEOUT_SEC = 5


class LiveError(Exception):
    """A user-facing problem (bad argument, unsupported feature)."""


@dataclass(frozen=True)
class LiveCaps:
    """Which live-inspection features this YugabyteDB version exposes."""

    activity_query_id: bool
    activity_backend_xid: bool
    lock_status: bool
    terminated_queries: bool
    ash: bool
    stat_plans: bool
    index_check: bool


# Helpers
def _clamp(value: Any, lo: int, hi: int, name: str) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise LiveError(f"{name} must be an integer") from None
    return max(lo, min(hi, n))


def _query_id_arg(query_id: Any) -> int:
    try:
        return int(str(query_id).strip())
    except (TypeError, ValueError):
        raise LiveError("query_id must be an integer (as shown by pg_stat_activity / ASH)") from None


def _jsonable(v: Any) -> Any:
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


def _trunc(text: Optional[str], n: int) -> Optional[str]:
    """Collapse whitespace and cut to ``n`` characters (``n <= 0`` keeps everything)."""
    if text is None:
        return None
    flat = " ".join(str(text).split())
    if n > 0 and len(flat) > n:
        return flat[: max(0, n - 1)] + "…"
    return flat


def _r(v: Optional[float], nd: int = 1) -> Optional[float]:
    return None if v is None else round(float(v), nd)


def _is_idle_in_xact(state: Optional[str]) -> bool:
    return bool(state) and str(state).startswith("idle in transaction")


def _running_ms(r: dict[str, Any]) -> Optional[float]:
    """Active: time in the current query. Otherwise: time in the current state."""
    key = "query_age_ms" if r.get("state") in _IN_FLIGHT_STATES else "state_age_ms"
    return r.get(key)


def _shape_backend(node: str, r: dict[str, Any], query_chars: int) -> dict[str, Any]:
    out: dict[str, Any] = {
        "node": node,
        "pid": r["pid"],
        "database": r.get("datname"),
        "user": r.get("usename"),
        "application": r.get("application_name") or None,
        "client_addr": r.get("client_addr"),
        "state": r.get("state"),
        "running_ms": _r(_running_ms(r)),
        "xact_age_ms": _r(r.get("xact_age_ms")),
        "wait_event_type": r.get("wait_event_type"),
        "wait_event": r.get("wait_event"),
        "query_id": r.get("query_id"),
        "txn_id": r.get("txn_id"),
        "query_start": _jsonable(r.get("query_start")),
        "xact_start": _jsonable(r.get("xact_start")),
        "query": _trunc(r.get("query"), query_chars),
    }
    return out


def _percentiles(
    buckets: list[tuple[float, float, int]], overflow: int, qs: tuple[float, ...]
) -> dict[str, Optional[float]]:
    """Approximate percentiles from ``(lo, hi, count)`` buckets by linear interpolation.

    A percentile that falls in the open-ended overflow bucket is reported as ``None``.
    """
    total = sum(c for _, _, c in buckets) + overflow
    out: dict[str, Optional[float]] = {}
    for q in qs:
        label = f"p{int(round(q * 100))}"
        if total == 0:
            out[label] = None
            continue
        target = q * total
        cum = 0
        val: Optional[float] = None
        for lo, hi, c in buckets:
            if cum + c >= target:
                frac = (target - cum) / c if c else 0.0
                val = lo + (hi - lo) * frac
                break
            cum += c
        out[label] = _r(val, 3)
    return out


# Context
class LiveContext:
    """Holds the seed DSN and caches node discovery / capability probes."""

    def __init__(
        self,
        seed_dsn: str,
        *,
        node_parallelism: int = DEFAULT_NODE_PARALLELISM,
        statement_timeout_ms: int = 10_000,
    ) -> None:
        self.seed_dsn = seed_dsn
        self.node_parallelism = max(1, int(node_parallelism))
        self.statement_timeout_ms = int(statement_timeout_ms)
        self._lock = Lock()
        self._nodes: Optional[list[YsqlNode]] = None
        self._nodes_at = 0.0
        self._preferred: Optional[tuple[str, int]] = None  # (host, port) that answered last
        self._caps: Optional[tuple[Capabilities, LiveCaps]] = None

    # Discovery and fan-out
    def nodes(self) -> list[YsqlNode]:
        with self._lock:
            if self._nodes is None or time.monotonic() - self._nodes_at > _NODES_TTL_SEC:
                self._nodes = self._discover()
                self._nodes_at = time.monotonic()
            return list(self._nodes)

    def _discover(self) -> list[YsqlNode]:
        """Ask yb_servers() on the seed, or on any other known node if the seed is down."""
        last: Optional[Exception] = None
        for hostport, dsn in self._candidates(None):
            try:
                found = discover_ysql_nodes(dsn)
            except Exception as exc:  # noqa: BLE001, try the next node
                last = exc
                continue
            self._preferred = hostport
            return found
        raise last or RuntimeError("no node to discover the universe from")

    def _select_nodes(self, node: Optional[str]) -> list[YsqlNode]:
        nodes = self.nodes()
        if not node:
            return nodes
        want = node.strip().lower()
        hit = [
            n
            for n in nodes
            if want in (n.host.lower(), node_id(n).lower(), n.server_uuid.lower())
        ]
        if not hit:
            known = ", ".join(node_id(n) for n in nodes)
            raise LiveError(f"unknown node {node!r}; known nodes: {known}")
        return hit

    def _dsn(self, node: Optional[YsqlNode] = None, database: Optional[str] = None) -> str:
        dsn = dsn_for_node(self.seed_dsn, node) if node else self.seed_dsn
        if database:
            dsn = dsn_with_database(dsn, database)
        info = psycopg.conninfo.conninfo_to_dict(dsn)
        info.setdefault("connect_timeout", str(_CONNECT_TIMEOUT_SEC))  # a down host must not hang a call
        return psycopg.conninfo.make_conninfo(**info)

    def _candidates(self, database: Optional[str]) -> list[tuple[tuple[str, int], str]]:
        """Seed first, then every known node; the node that answered last goes to the front."""
        out: list[tuple[tuple[str, int], str]] = []
        seen: set[tuple[str, int]] = set()
        nodes: list[Optional[YsqlNode]] = [None, *(self._nodes or [])]
        for n in nodes:
            dsn = self._dsn(n, database)
            info = psycopg.conninfo.conninfo_to_dict(dsn)
            key = (str(info.get("host", "")), int(info.get("port") or 5433))
            if key not in seen:
                seen.add(key)
                out.append((key, dsn))
        out.sort(key=lambda c: c[0] != self._preferred)  # stable: preferred first
        return out

    @contextmanager
    def _any_conn(self, database: Optional[str], timeout_ms: int) -> Iterator[psycopg.Connection]:
        """A connection to the first node that accepts one (for questions any node can answer)."""
        last: Optional[Exception] = None
        with ExitStack() as stack:
            for hostport, dsn in self._candidates(database):
                try:
                    conn = stack.enter_context(connect_readonly(dsn, statement_timeout_ms=timeout_ms))
                except psycopg.OperationalError as exc:
                    last = exc
                    continue
                self._preferred = hostport
                yield conn
                return
        raise last or RuntimeError("no node to connect to")

    def _conn(
        self,
        node: Optional[YsqlNode] = None,
        *,
        database: Optional[str] = None,
        timeout_ms: Optional[int] = None,
    ):
        """A read-only connection: to ``node``, or with node=None to any reachable node."""
        timeout = timeout_ms or self.statement_timeout_ms
        if node is not None:
            return connect_readonly(self._dsn(node, database), statement_timeout_ms=timeout)
        return self._any_conn(database, timeout)

    def databases(self) -> list[str]:
        with self._conn() as conn:
            rows = fetch_all(
                conn, "SELECT datname::text AS d FROM pg_database WHERE NOT datistemplate AND datallowconn ORDER BY 1"
            )
        return [r["d"] for r in rows]

    def check_database(self, database: Optional[str]) -> Optional[str]:
        """Validate a user-supplied database name against the cluster's list."""
        if not database:
            return None
        known = self.databases()
        if database not in known:
            raise LiveError(f"unknown database {database!r}; known databases: {', '.join(known)}")
        return database

    def _fan_out(
        self,
        fn: Callable[[psycopg.Connection, YsqlNode], Any],
        nodes: Optional[list[YsqlNode]] = None,
        *,
        database: Optional[str] = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Run ``fn(conn, node)`` on each node in parallel → (results by node id, errors by node id)."""
        nodes = nodes if nodes is not None else self.nodes()
        results: dict[str, Any] = {}
        errors: dict[str, str] = {}

        def run(n: YsqlNode) -> tuple[str, Any, Optional[str]]:
            try:
                with self._conn(n, database=database) as conn:
                    return node_id(n), fn(conn, n), None
            except Exception as exc:  # noqa: BLE001, errors are reported per node
                return node_id(n), None, str(exc).strip().splitlines()[0] if str(exc).strip() else repr(exc)

        with ThreadPoolExecutor(max_workers=min(self.node_parallelism, max(1, len(nodes)))) as pool:
            for nid, res, err in pool.map(run, nodes):
                if err is not None:
                    errors[nid] = err
                else:
                    results[nid] = res
        return results, errors

    def caps(self) -> tuple[Capabilities, LiveCaps]:
        with self._lock:
            if self._caps is not None:
                return self._caps
        with self._conn() as conn:
            base = Capabilities.detect(conn)
            row = fetch_all(
                conn,
                """
                SELECT
                  EXISTS (SELECT 1 FROM information_schema.columns
                          WHERE table_name = 'pg_stat_activity' AND column_name = 'query_id') AS qid,
                  EXISTS (SELECT 1 FROM information_schema.columns
                          WHERE table_name = 'pg_stat_activity' AND column_name = 'yb_backend_xid') AS xid,
                  EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'yb_lock_status') AS locks,
                  EXISTS (SELECT 1 FROM pg_class WHERE relname = 'yb_terminated_queries') AS term,
                  EXISTS (SELECT 1 FROM pg_class WHERE relname = 'yb_active_session_history') AS ash,
                  EXISTS (SELECT 1 FROM pg_class WHERE relname = 'yb_pg_stat_plans') AS plans,
                  EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'yb_index_check') AS idxcheck
                """,
            )[0]
        live = LiveCaps(
            activity_query_id=bool(row["qid"]),
            activity_backend_xid=bool(row["xid"]),
            lock_status=bool(row["locks"]),
            terminated_queries=bool(row["term"]),
            ash=bool(row["ash"]),
            stat_plans=bool(row["plans"]),
            index_check=bool(row["idxcheck"]),
        )
        with self._lock:
            self._caps = (base, live)
        return base, live

    def _require(self, ok: bool, what: str) -> None:
        if not ok:
            raise LiveError(f"{what} is not available on this YugabyteDB version")

    # Raw activity
    def _all_backends(self, node: Optional[str] = None) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, str]]:
        _, live = self.caps()
        nodes = self._select_nodes(node)
        results, errors = self._fan_out(
            lambda conn, _n: Q.pg_stat_activity_rows(
                conn,
                has_query_id=live.activity_query_id,
                has_backend_xid=live.activity_backend_xid,
            ),
            nodes,
        )
        rows = [(nid, r) for nid, chunk in results.items() for r in chunk]
        return rows, errors

    # Tools
    def list_nodes(self) -> dict[str, Any]:
        _, live = self.caps()
        return {
            "nodes": [
                {
                    "node": node_id(n),
                    "host": n.host,
                    "port": n.port,
                    "server_uuid": n.server_uuid,
                    "cloud": n.cloud or None,
                    "region": n.region or None,
                    "zone": n.zone or None,
                }
                for n in self.nodes()
            ],
            "capabilities": {
                "activity_query_id": live.activity_query_id,
                "lock_status": live.lock_status,
                "terminated_queries": live.terminated_queries,
                "ash": live.ash,
                "recorded_plans": live.stat_plans,
                "index_check": live.index_check,
            },
            "databases": self.databases(),
        }

    def active_queries(
        self,
        *,
        min_duration_ms: int = 0,
        include_idle: bool = False,
        database: Optional[str] = None,
        user: Optional[str] = None,
        application: Optional[str] = None,
        node: Optional[str] = None,
        query_id: Optional[str] = None,
        text_contains: Optional[str] = None,
        sort_by: str = "running",
        limit: int = 50,
        query_chars: int = 500,
    ) -> dict[str, Any]:
        """Backends that are running a query or holding an open transaction right now."""
        limit = _clamp(limit, 1, _MAX_LIMIT, "limit")
        min_duration_ms = _clamp(min_duration_ms, 0, 10**9, "min_duration_ms")
        if sort_by not in ("running", "xact_age"):
            raise LiveError("sort_by must be 'running' or 'xact_age'")
        qid = str(_query_id_arg(query_id)) if query_id is not None else None
        rows, errors = self._all_backends(node)

        def keep(r: dict[str, Any]) -> bool:
            state = r.get("state")
            if not include_idle and state not in _IN_FLIGHT_STATES and not _is_idle_in_xact(state):
                return False
            if (_running_ms(r) or 0) < min_duration_ms:
                return False
            if database and (r.get("datname") or "").lower() != database.lower():
                return False
            if user and (r.get("usename") or "").lower() != user.lower():
                return False
            if application and application.lower() not in (r.get("application_name") or "").lower():
                return False
            if qid and r.get("query_id") != qid:
                return False
            if text_contains and text_contains.lower() not in (r.get("query") or "").lower():
                return False
            return True

        kept = [(nid, r) for nid, r in rows if keep(r)]
        key = (lambda x: x[1].get("xact_age_ms") or 0) if sort_by == "xact_age" else (lambda x: _running_ms(x[1]) or 0)
        kept.sort(key=key, reverse=True)
        return {
            "total_matching": len(kept),
            "returned": min(len(kept), limit),
            "queries": [_shape_backend(nid, r, query_chars) for nid, r in kept[:limit]],
            "errors": errors,
            "notes": [
                "running_ms: time in the current query for 'active' backends, otherwise time in the "
                "current state (e.g. how long a session has been idle in transaction).",
                "xact_age_ms: age of the open transaction (null when none).",
            ],
        }

    def summary(self, *, top: int = 5, query_chars: int = 200) -> dict[str, Any]:
        """One-call overview of what the universe is doing right now."""
        top = _clamp(top, 1, 50, "top")
        rows, errors = self._all_backends()
        by_state: Counter[str] = Counter()
        by_db: Counter[str] = Counter()
        per_node: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"active": 0, "idle_in_transaction": 0, "idle": 0, "oldest_active_ms": 0.0}
        )
        waits: Counter[str] = Counter()
        templates: dict[str, dict[str, Any]] = {}
        active: list[tuple[str, dict[str, Any]]] = []
        open_txns: list[tuple[str, dict[str, Any]]] = []

        for nid, r in rows:
            state = r.get("state") or "unknown"
            by_state[state] += 1
            pn = per_node[nid]
            if state in _IN_FLIGHT_STATES:
                active.append((nid, r))
                pn["active"] += 1
                pn["oldest_active_ms"] = max(pn["oldest_active_ms"], r.get("query_age_ms") or 0.0)
                if r.get("datname"):
                    by_db[r["datname"]] += 1
                waits[f"{r.get('wait_event_type') or '-'}:{r.get('wait_event') or '-'}"] += 1
                tpl = normalize_query_template(r.get("query") or "")
                t = templates.setdefault(
                    tpl, {"running": 0, "max_running_ms": 0.0, "query_id": r.get("query_id")}
                )
                t["running"] += 1
                t["max_running_ms"] = max(t["max_running_ms"], r.get("query_age_ms") or 0.0)
            elif _is_idle_in_xact(state):
                pn["idle_in_transaction"] += 1
            else:
                pn["idle"] += 1
            if r.get("xact_age_ms") is not None:
                open_txns.append((nid, r))

        active.sort(key=lambda x: x[1].get("query_age_ms") or 0, reverse=True)
        open_txns.sort(key=lambda x: x[1].get("xact_age_ms") or 0, reverse=True)
        return {
            "backends": len(rows),
            "by_state": dict(by_state),
            "per_node": {
                nid: {**v, "oldest_active_ms": _r(v["oldest_active_ms"])} for nid, v in sorted(per_node.items())
            },
            "active_by_database": dict(by_db.most_common()),
            "active_wait_events": [
                {"wait": w, "backends": c} for w, c in waits.most_common(top)
            ],
            "longest_running": [_shape_backend(n, r, query_chars) for n, r in active[:top]],
            "oldest_open_transactions": [_shape_backend(n, r, query_chars) for n, r in open_txns[:top]],
            "busiest_query_templates": [
                {
                    "query_template": _trunc(tpl, query_chars),
                    "running_now": v["running"],
                    "max_running_ms": _r(v["max_running_ms"]),
                    "example_query_id": v["query_id"],
                }
                for tpl, v in sorted(templates.items(), key=lambda kv: kv[1]["running"], reverse=True)[:top]
            ],
            "errors": errors,
        }

    def backend(self, *, node: str, pid: int, window_seconds: int = 60, query_chars: int = 4000) -> dict[str, Any]:
        """Everything known about one backend: its state plus its recent ASH wait profile."""
        window_seconds = _clamp(window_seconds, 5, 600, "window_seconds")
        pid = _clamp(pid, 1, 2**31 - 1, "pid")
        (n,) = self._select_nodes(node)[:1]
        rows, errors = self._all_backends(node_id(n))
        match = [r for _, r in rows if r["pid"] == pid]
        if not match:
            raise LiveError(f"no client backend with pid {pid} on {node_id(n)} (it may have finished)")
        r = match[0]
        out: dict[str, Any] = {"backend": _shape_backend(node_id(n), r, query_chars)}
        _, live = self.caps()
        if live.ash:
            out["wait_profile"] = self._wait_profile(
                window_seconds, query_id=None, pid=pid, nodes=[n], describe=True
            )
        # Lock waits involving this backend
        if live.lock_status and live.activity_backend_xid and r.get("txn_id"):
            try:
                graph = self._lock_graph()
                tid = r["txn_id"]
                out["lock_waits"] = {
                    "blocked_by": [b for w in graph["waiters"] if w["waiter"].get("txn_id") == tid for b in w["blockers"]],
                    "blocking": [w["waiter"] for w in graph["waiters"] if any(b.get("txn_id") == tid for b in w["blockers"])],
                }
            except Exception as exc:  # noqa: BLE001
                out["lock_waits"] = {"error": str(exc)}
        out["errors"] = errors
        return out

    # ASH
    def _wait_event_descriptions(self, events: list[str]) -> dict[str, str]:
        if not events:
            return {}
        try:
            with self._conn() as conn:
                rows = fetch_all(
                    conn,
                    "SELECT wait_event::text AS wait_event, wait_event_description::text AS description "
                    "FROM yb_wait_event_desc WHERE wait_event = ANY(%(e)s::text[])",
                    {"e": events},
                )
            return {r["wait_event"]: r["description"] for r in rows if r.get("description")}
        except Exception:  # noqa: BLE001, descriptions are optional
            return {}

    def _wait_profile(
        self,
        window_seconds: int,
        *,
        query_id: Optional[int],
        pid: Optional[int],
        nodes: list[YsqlNode],
        describe: bool,
        top: int = 15,
    ) -> dict[str, Any]:
        _, live = self.caps()
        self._require(live.ash, "Active Session History")
        results, errors = self._fan_out(
            lambda conn, n: Q.ash_recent_profile(
                conn, window_seconds, query_id=query_id, pid=pid, node_uuid=n.server_uuid if pid else None
            ),
            nodes,
        )
        total = 0
        waits: Counter[tuple[str, str, str]] = Counter()
        objects: Counter[str] = Counter()
        wait_types: Counter[str] = Counter()
        tablets: Counter[tuple[str, str]] = Counter()
        per_node: Counter[str] = Counter()
        per_query: Counter[str] = Counter()
        for nid, chunk in results.items():
            for r in chunk:
                s = int(r["samples"])
                total += s
                waits[(r.get("wait_event_component") or "", r.get("wait_event_type") or "", r.get("wait_event") or "")] += s
                wait_types[f"{r.get('wait_event_component') or ''}:{r.get('wait_event_type') or ''}"] += s
                if r.get("object_name"):
                    objects[f"{r.get('namespace_name') or '?'}.{r['object_name']}"] += s
                    if r.get("wait_event_aux") and (r.get("wait_event_component") or "").upper() != "YCQL":
                        tablets[(f"{r.get('namespace_name') or '?'}.{r['object_name']}", r["wait_event_aux"])] += s
                per_node[nid] += s
                per_query[r.get("query_id") or "none"] += s

        def pct(s: int) -> float:
            return round(100.0 * s / total, 1) if total else 0.0

        top_waits = waits.most_common(top)
        desc = self._wait_event_descriptions([w[2] for w, _ in top_waits]) if describe else {}
        out: dict[str, Any] = {
            "window_seconds": window_seconds,
            "samples": total,
            # ASH samples each active session about once a second
            "avg_active_sessions": round(total / window_seconds, 2),
            "by_wait_event": [
                {
                    "component": c,
                    "type": t,
                    "wait_event": e,
                    "samples": s,
                    "pct": pct(s),
                    **({"description": desc[e]} if e in desc else {}),
                }
                for (c, t, e), s in top_waits
            ],
            "by_wait_type": [{"type": t, "samples": s, "pct": pct(s)} for t, s in wait_types.most_common()],
            "by_object": [{"object": o, "samples": s, "pct": pct(s)} for o, s in objects.most_common(top)],
            "by_tablet": [
                {"object": o, "tablet_id_prefix": t, "samples": s, "pct": pct(s)} for (o, t), s in tablets.most_common(top)
            ],
            "by_node": {n: s for n, s in per_node.most_common()},
            "errors": errors,
        }
        if query_id is None and pid is None:
            out["by_query_id"] = [{"query_id": q, "samples": s, "pct": pct(s)} for q, s in per_query.most_common(top)]
        return out

    def wait_profile(
        self,
        *,
        query_id: Optional[str] = None,
        window_seconds: int = 60,
        node: Optional[str] = None,
    ) -> dict[str, Any]:
        """Where time is going (wait events, tables, nodes) over the last ``window_seconds``.

        With ``query_id`` it profiles that statement; without it, the whole workload (and ranks
        the statements by ASH samples)."""
        window_seconds = _clamp(window_seconds, 5, 600, "window_seconds")
        qid = _query_id_arg(query_id) if query_id is not None else None
        return self._wait_profile(window_seconds, query_id=qid, pid=None, nodes=self._select_nodes(node), describe=True)

    # Statement stats
    def statement_stats(self, *, query_id: str, query_chars: int = 2000) -> dict[str, Any]:
        """Cluster-merged cumulative ``pg_stat_statements`` numbers for one query_id."""
        qid = _query_id_arg(query_id)
        base, _ = self.caps()
        results, errors = self._fan_out(lambda conn, _n: Q.pg_stat_statements_for_query(conn, qid, base))
        chunks = [rows for rows in results.values() if rows]
        if not chunks:
            return {"found": False, "query_id": str(qid), "errors": errors,
                    "note": "no pg_stat_statements entry (never executed here, evicted, or stats were reset)"}
        from ybtop.snapshot_write import normalize_latency_histogram

        merged = merge_pg_stat_statements(chunks, include_docdb_per_call=True, include_rows_total=True)
        hist: Counter[str] = Counter()
        for rows in chunks:
            for r in rows:
                for label, cnt in normalize_latency_histogram(r.get("yb_latency_histogram")).items():
                    hist[label] += cnt
        out: dict[str, Any] = {
            "found": True,
            "query_id": str(qid),
            "per_database": [{**m, "query": _trunc(m.get("query"), query_chars)} for m in merged],
            "calls_by_node": {nid: sum(int(r["calls"]) for r in rows) for nid, rows in results.items() if rows},
            "errors": errors,
            "note": "Cumulative since the last pg_stat_statements reset; not a rate.",
        }
        if hist:
            buckets, overflow = parse_histogram_buckets(dict(hist))
            out["latency_ms_approx"] = {
                **_percentiles(buckets, overflow, (0.5, 0.9, 0.99)),
                "calls_over_histogram_max": overflow,
            }
        return out

    # Locks
    def _lock_graph(self, query_chars: int = 300) -> dict[str, Any]:
        base, live = self.caps()
        self._require(live.lock_status, "yb_lock_status()")
        self._require(live.activity_backend_xid, "pg_stat_activity.yb_backend_xid")
        # yb_lock_status() is cluster-wide, any node works
        lock_rows: Optional[list[dict[str, Any]]] = None
        db_oid: Optional[int] = None
        rel_names: dict[int, str] = {}
        last_err = ""
        for n in self.nodes():
            try:
                with self._conn(n) as conn:
                    lock_rows = Q.yb_lock_status_rows(conn)
                    cur_db = fetch_all(
                        conn, "SELECT oid::int AS oid FROM pg_database WHERE datname = current_database()"
                    )
                    db_oid = int(cur_db[0]["oid"]) if cur_db else None
                    rel_oids = sorted(
                        {int(r["relation_oid"]) for r in lock_rows if r.get("relation_oid") and r.get("database_oid") == db_oid}
                    )
                    rel_names = Q.relation_names(conn, rel_oids)
                break
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
        if lock_rows is None:
            raise LiveError(f"could not read yb_lock_status(): {last_err}")

        backends, errors = self._all_backends()
        by_txn = {r["txn_id"]: (nid, r) for nid, r in backends if r.get("txn_id")}

        waiting: dict[str, dict[str, Any]] = {}
        for r in lock_rows:
            if r["granted"]:
                continue
            w = waiting.setdefault(
                r["transaction_id"],
                {"blocked_by": set(), "wait_ms": 0.0, "tablets": set(), "relations": set()},
            )
            w["blocked_by"].update(r.get("blocked_by") or [])
            w["wait_ms"] = max(w["wait_ms"], r.get("wait_ms") or 0.0)
            if r.get("tablet_id"):
                w["tablets"].add(r["tablet_id"])
            if r.get("relation_oid"):
                name = rel_names.get(int(r["relation_oid"])) if r.get("database_oid") == db_oid else None
                w["relations"].add(name or f"oid:{r['relation_oid']} (db oid {r.get('database_oid')})")

        def who(txn: str) -> dict[str, Any]:
            hit = by_txn.get(txn)
            if not hit:
                return {"txn_id": txn, "backend": None}
            return {**_shape_backend(hit[0], hit[1], query_chars)}

        waiters = []
        blocked_counts: Counter[str] = Counter()
        for txn, w in waiting.items():
            for b in w["blocked_by"]:
                blocked_counts[b] += 1
            waiters.append(
                {
                    "waiter": who(txn),
                    "wait_ms": _r(w["wait_ms"]),
                    "relations": sorted(w["relations"]),
                    "tablets": sorted(w["tablets"]),
                    "blockers": [who(b) for b in sorted(w["blocked_by"])],
                }
            )
        waiters.sort(key=lambda x: x["wait_ms"] or 0, reverse=True)
        return {
            "waiters": waiters,
            "blocked_counts": blocked_counts,
            "waiting_txns": set(waiting),
            "who": who,
            "errors": errors,
        }

    def blocking(self, *, limit: int = 20, query_chars: int = 500) -> dict[str, Any]:
        """Lock-wait chains: who is blocked, by whom, on what, and which blockers are the roots."""
        limit = _clamp(limit, 1, _MAX_LIMIT, "limit")
        g = self._lock_graph(query_chars)
        # Root blockers are not waiting on anyone
        roots = [
            {"blocked_transactions": cnt, "blocker": g["who"](txn)}
            for txn, cnt in g["blocked_counts"].most_common()
            if txn not in g["waiting_txns"]
        ]
        return {
            "blocked_count": len(g["waiters"]),
            "root_blockers": roots[:limit],
            "waiters": g["waiters"][:limit],
            "errors": g["errors"],
            "notes": [
                "A blocker with state 'idle in transaction' is a client that opened a transaction and "
                "stopped talking; its 'query' is the last statement it ran, not what is blocking.",
            ],
        }

    def long_transactions(self, *, min_age_seconds: int = 30, limit: int = 20, query_chars: int = 500) -> dict[str, Any]:
        """Open transactions older than ``min_age_seconds``, flagged when they block others."""
        min_age_seconds = _clamp(min_age_seconds, 0, 10**7, "min_age_seconds")
        limit = _clamp(limit, 1, _MAX_LIMIT, "limit")
        rows, errors = self._all_backends()
        old = [
            (nid, r)
            for nid, r in rows
            if r.get("xact_age_ms") is not None and r["xact_age_ms"] >= min_age_seconds * 1000
        ]
        old.sort(key=lambda x: x[1]["xact_age_ms"], reverse=True)
        blocked: Counter[str] = Counter()
        lock_note: Optional[str] = None
        try:
            blocked = self._lock_graph(query_chars)["blocked_counts"]
        except LiveError as exc:
            lock_note = f"blocking info unavailable: {exc}"
        out_rows = []
        for nid, r in old[:limit]:
            item = _shape_backend(nid, r, query_chars)
            item["blocking_transactions"] = blocked.get(r.get("txn_id") or "", 0)
            out_rows.append(item)
        out: dict[str, Any] = {"total_matching": len(old), "transactions": out_rows, "errors": errors}
        if lock_note:
            out["notes"] = [lock_note]
        return out

    # Terminated queries
    def terminated_queries(self, *, window_minutes: int = 60, limit: int = 50, query_chars: int = 500) -> dict[str, Any]:
        """Queries the server terminated recently (errors, OOM, cancellations)."""
        window_minutes = _clamp(window_minutes, 1, 24 * 60, "window_minutes")
        limit = _clamp(limit, 1, _MAX_LIMIT, "limit")
        _, live = self.caps()
        self._require(live.terminated_queries, "yb_terminated_queries")
        results, errors = self._fan_out(
            lambda conn, _n: Q.terminated_queries_rows(conn, window_minutes, limit)
        )
        rows = [{"node": nid, **r} for nid, chunk in results.items() for r in chunk]
        rows.sort(key=lambda r: r.get("query_end_time") or datetime.min, reverse=True)
        reasons = Counter(r.get("termination_reason") or "unknown" for r in rows)
        return {
            "window_minutes": window_minutes,
            "by_reason": dict(reasons.most_common()),
            "queries": [
                {
                    "node": r["node"],
                    "database": r.get("dbname"),
                    "pid": r.get("pid"),
                    "query_id": r.get("query_id"),
                    "reason": r.get("termination_reason"),
                    "query_start": _jsonable(r.get("query_start_time")),
                    "query_end": _jsonable(r.get("query_end_time")),
                    "query": _trunc(r.get("query"), query_chars),
                }
                for r in rows[:limit]
            ],
            "errors": errors,
        }
