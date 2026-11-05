"""MCP (Model Context Protocol) server over streamable HTTP for in-flight query inspection.

Opt-in: started by ``ybtop serve --mcp`` / ``ybtop watch --mcp``. It runs in a background
thread on its own port, separate from the static viewer, and exposes **read-only** tools backed
by :class:`ybtop.live.LiveContext` (every DB session is read-only with a statement timeout).

Security knobs (both opt-in / explicit):

* **bind address** — ``--mcp-bind`` (default ``127.0.0.1``). On loopback the MCP SDK also turns on
  Host-header (DNS-rebinding) validation.
* **bearer token** — ``--mcp-auth-token`` or ``YBTOP_MCP_TOKEN``. When set, every request must send
  ``Authorization: Bearer <token>`` (constant-time compare). Binding to a non-loopback address
  without a token prints a warning: tool output includes SQL text with literal values.
"""

from __future__ import annotations

import hmac
import importlib.metadata
import ipaddress
import socket
import sys
import threading
from collections.abc import Callable
from typing import Any, Optional

import psycopg

from ybtop import __version__
from ybtop.config import DEFAULT_NODE_PARALLELISM
from ybtop.diagnose import Diagnostics
from ybtop.live import LiveContext, LiveError

MCP_TOKEN_ENV = "YBTOP_MCP_TOKEN"
DEFAULT_MCP_HOST = "127.0.0.1"
DEFAULT_MCP_PORT = 8766
MCP_PATH = "/mcp"

INSTALL_HINT = "The MCP server needs the optional dependency: pip3 install 'ybtop[mcp]'"


def mcp_unavailable_reason() -> Optional[str]:
    """None when the MCP dependencies import, otherwise why not."""
    try:
        import uvicorn  # noqa: F401
        from mcp.server.mcpserver import MCPServer  # noqa: F401
    except ImportError as exc:
        try:
            found = f"found mcp {importlib.metadata.version('mcp')}"
        except importlib.metadata.PackageNotFoundError:
            found = "mcp is not installed"
        detail = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        return f"{INSTALL_HINT} ({found}, ybtop needs mcp>=2: {detail})"
    return None


def mcp_available() -> bool:
    return mcp_unavailable_reason() is None


def is_loopback(bind: str) -> bool:
    if bind.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(bind.strip("[]")).is_loopback
    except ValueError:
        return False


class BearerAuthMiddleware:
    """Pure-ASGI bearer-token gate. Non-HTTP scopes (lifespan) pass straight through."""

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self._token = token.encode("utf-8")

    def _authorized(self, scope: dict[str, Any]) -> bool:
        for k, v in scope.get("headers", []):
            if k == b"authorization":
                scheme, _, cred = v.partition(b" ")
                return scheme.lower() == b"bearer" and hmac.compare_digest(cred.strip(), self._token)
        return False

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http" and not self._authorized(scope):
            body = b'{"error":"unauthorized"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                        (b"www-authenticate", b'Bearer realm="ybtop-mcp"'),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def _bind_socket(bind: str, port: int) -> socket.socket:
    """Bind + listen now (so failures surface to the caller as ``OSError``)."""
    family, _, _, _, addr = socket.getaddrinfo(
        bind.strip("[]"), port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
    )[0]
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(addr)
        sock.listen(128)
    except OSError:
        sock.close()
        raise
    sock.set_inheritable(True)
    return sock


def http_app(server: Any, *, host: str) -> Any:
    """The streamable HTTP app for a server from build_mcp.

    Stateless with plain JSON responses: each request stands alone, so there is no session
    state to leak and any HTTP client can call a tool with one POST. On a loopback host the
    SDK also validates the Host header (DNS rebinding protection).
    """
    return server.streamable_http_app(
        streamable_http_path=MCP_PATH, stateless_http=True, json_response=True, host=host
    )


def build_mcp(ctx: LiveContext) -> Any:
    """Create the MCP server with all tools registered against ctx."""
    import anyio.to_thread
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations

    mcp = MCPServer(
        "ybtop",
        instructions=(
            "Inspect and diagnose queries on a YugabyteDB universe (read-only). "
            "Right now: active_summary → list_active_queries → backend_detail / query_wait_profile; "
            "blocking_locks and long_transactions when things stall. "
            "Why is it slow / what do I change: cluster_bottlenecks (what limits the cluster), "
            "top_queries (rank by cost and red flags), find_full_scans (missing indexes), then "
            "diagnose_query(query_id) for evidence-backed findings and recommendations, explain_query "
            "for plans, index_health (duplicate/unused/broken indexes) and verify_index (consistency). "
            "query_id is shared by pg_stat_activity, ASH, pg_stat_statements and recorded plans."
        ),
    )
    diag = Diagnostics(ctx)
    ro = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    async def run(fn: Callable[..., Any], **kwargs: Any) -> Any:
        # Run blocking DB calls in a thread
        try:
            return await anyio.to_thread.run_sync(lambda: fn(**kwargs))
        except LiveError as exc:
            raise ToolError(str(exc)) from exc  # expected error, shown to the caller
        except psycopg.Error as exc:
            first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
            raise ToolError(f"database error: {first_line}") from exc

    @mcp.tool(annotations=ro)
    async def list_nodes() -> dict[str, Any]:
        """List the YSQL nodes of the universe (from yb_servers()) with placement, and which
        live-inspection features this YugabyteDB version supports."""
        return await run(ctx.list_nodes)

    @mcp.tool(annotations=ro)
    async def active_summary(top: int = 5, query_chars: int = 200) -> dict[str, Any]:
        """One-call overview of what the universe is doing right now: backends by state and
        node, active sessions per database, current wait events, longest-running queries,
        oldest open transactions and the busiest query templates. Start here.

        Args:
            top: how many entries per ranked list (1-50).
            query_chars: truncate SQL text to this many characters (0 = no truncation).
        """
        return await run(ctx.summary, top=top, query_chars=query_chars)

    @mcp.tool(annotations=ro)
    async def list_active_queries(
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
        """List queries in flight across all nodes: backends running a statement or holding an
        open transaction ('idle in transaction'). Each row has node, pid, user, database,
        state, how long it has been running, the current wait event, query_id and SQL text.

        Args:
            min_duration_ms: only backends running/in-state at least this long.
            include_idle: also list plain 'idle' sessions.
            database / user: exact match. application: substring of application_name.
            node: host, host:port or server uuid to restrict to one node.
            query_id: only backends currently running this query_id.
            text_contains: case-insensitive substring of the SQL text.
            sort_by: 'running' (default) or 'xact_age' (oldest open transaction first).
            limit: max rows (1-500). query_chars: truncate SQL (0 = no truncation).
        """
        return await run(
            ctx.active_queries,
            min_duration_ms=min_duration_ms,
            include_idle=include_idle,
            database=database,
            user=user,
            application=application,
            node=node,
            query_id=query_id,
            text_contains=text_contains,
            sort_by=sort_by,
            limit=limit,
            query_chars=query_chars,
        )

    @mcp.tool(annotations=ro)
    async def backend_detail(node: str, pid: int, window_seconds: int = 60, query_chars: int = 4000) -> dict[str, Any]:
        """Deep dive on one backend (node + pid from list_active_queries): its full state, its
        recent Active Session History wait profile (what it was waiting on, which tables,
        with descriptions of each wait event) and any lock-wait relationship with other backends.

        Args:
            node: host, host:port or server uuid of the node the backend is on.
            pid: backend process id on that node.
            window_seconds: ASH look-back window (5-600).
            query_chars: truncate SQL text (0 = no truncation).
        """
        return await run(ctx.backend, node=node, pid=pid, window_seconds=window_seconds, query_chars=query_chars)

    @mcp.tool(annotations=ro)
    async def query_wait_profile(
        query_id: Optional[str] = None, window_seconds: int = 60, node: Optional[str] = None
    ) -> dict[str, Any]:
        """Where time is going over the last few minutes, from Active Session History: wait
        events (with explanations), tables/indexes, and nodes. Pass a query_id to profile one
        statement; omit it to profile the whole workload and rank statements by ASH samples.
        avg_active_sessions ≈ how many sessions of that query were active on average.

        Args:
            query_id: statement to profile (from list_active_queries); omit for everything.
            window_seconds: look-back window (5-600; ASH retention is limited).
            node: restrict to one node.
        """
        return await run(ctx.wait_profile, query_id=query_id, window_seconds=window_seconds, node=node)

    @mcp.tool(annotations=ro)
    async def query_stats(query_id: str, query_chars: int = 2000) -> dict[str, Any]:
        """Cluster-merged cumulative pg_stat_statements numbers for one query_id: calls, total
        and mean time, rows, DocDB per-call counters, calls per node, and approximate
        p50/p90/p99 latency from yb_latency_histogram. Cumulative since the last stats reset.

        Args:
            query_id: the query_id to look up.
            query_chars: truncate SQL text (0 = no truncation).
        """
        return await run(ctx.statement_stats, query_id=query_id, query_chars=query_chars)

    @mcp.tool(annotations=ro)
    async def blocking_locks(limit: int = 20, query_chars: int = 500) -> dict[str, Any]:
        """Lock-wait chains right now (from yb_lock_status): which backends are blocked, for how
        long, on which tables/tablets, who blocks them, and the root blockers ranked by how many
        transactions they hold up (often an 'idle in transaction' client).

        Args:
            limit: max waiters / root blockers (1-500). query_chars: truncate SQL text.
        """
        return await run(ctx.blocking, limit=limit, query_chars=query_chars)

    @mcp.tool(annotations=ro)
    async def long_transactions(min_age_seconds: int = 30, limit: int = 20, query_chars: int = 500) -> dict[str, Any]:
        """Open transactions older than a threshold, oldest first, flagged with how many other
        transactions each one is blocking. Long idle-in-transaction sessions hold locks and
        pin old data versions.

        Args:
            min_age_seconds: minimum transaction age. limit: max rows (1-500).
            query_chars: truncate SQL text.
        """
        return await run(ctx.long_transactions, min_age_seconds=min_age_seconds, limit=limit, query_chars=query_chars)

    @mcp.tool(annotations=ro)
    async def recently_terminated_queries(window_minutes: int = 60, limit: int = 50, query_chars: int = 500) -> dict[str, Any]:
        """Queries the server recently terminated abnormally (e.g. out-of-memory kills,
        cancellations), from yb_terminated_queries, with a count by termination reason.

        Args:
            window_minutes: look-back (1-1440). limit: max rows. query_chars: truncate SQL text.
        """
        return await run(ctx.terminated_queries, window_minutes=window_minutes, limit=limit, query_chars=query_chars)

    # Diagnosis tools
    @mcp.tool(annotations=ro)
    async def cluster_bottlenecks(window_seconds: int = 300, top: int = 5, query_chars: int = 200) -> dict[str, Any]:
        """What is limiting the cluster, from Active Session History: which kind of wait dominates
        (lock contention, tserver CPU/thread saturation, catalog reads, storage reads), whether one
        node or one tablet is hot, and which queries are responsible. Returns ranked findings with
        recommendations. Use it first when 'the whole cluster is slow'.

        Args:
            window_seconds: look-back (30-3600; ASH retention is limited).
            top: entries per ranked list. query_chars: truncate SQL text.
        """
        return await run(diag.cluster_bottlenecks, window_seconds=window_seconds, top=top, query_chars=query_chars)

    @mcp.tool(annotations=ro)
    async def top_queries(
        sort_by: str = "total_time",
        limit: int = 20,
        min_calls: int = 1,
        database: Optional[str] = None,
        ash_window_seconds: int = 300,
        query_chars: int = 300,
    ) -> dict[str, Any]:
        """Rank statements by what they cost the cluster. Each row has its share of all statement
        time, its recent share of active sessions (ASH), DocDB rows scanned vs returned
        (scan_ratio: a high value means full scans / missing or unusable indexes), retries, catalog
        waits, and red-flag labels. Follow up with diagnose_query(query_id).

        Args:
            sort_by: total_time | mean_time | calls | rows_scanned | scan_ratio.
            limit: rows (1-200). min_calls: ignore rarely-run statements.
            database: restrict to one database. ash_window_seconds: window for the live-load share.
            query_chars: truncate SQL text.
        """
        return await run(
            diag.top_queries, sort_by=sort_by, limit=limit, min_calls=min_calls, database=database,
            ash_window_seconds=ash_window_seconds, query_chars=query_chars,
        )

    @mcp.tool(annotations=ro)
    async def find_full_scans(limit: int = 20, min_total_ms: float = 0.0, query_chars: int = 300) -> dict[str, Any]:
        """Find statements whose recorded plan does a sequential (full table) scan, ranked by total
        execution time, with the filter that is not being served by an index and a candidate
        CREATE INDEX. This is the quickest way to find missing indexes. Needs yb_pg_stat_plans.

        Args:
            limit: rows (1-200). min_total_ms: ignore plans with less cumulative time.
            query_chars: truncate SQL text.
        """
        return await run(diag.find_full_scans, limit=limit, min_total_ms=min_total_ms, query_chars=query_chars)

    @mcp.tool(annotations=ro)
    async def diagnose_query(query_id: str, window_seconds: int = 300, database: Optional[str] = None) -> dict[str, Any]:
        """Diagnose why one query is slow and what to change. Combines cumulative statistics,
        latency percentiles, live wait profile, recorded plans (including the parameters of the
        slowest call), and the indexes/statistics of the tables involved, then returns severity-
        ranked findings (full scan, index that does not cover the predicate, function/cast defeating
        an index, lock contention, read/conflict retries, catalog waits, plan flips, stale
        statistics, huge results) each with evidence and a concrete recommendation or candidate
        CREATE INDEX. Recommendations are heuristics: verify with explain_query.

        Args:
            query_id: from top_queries / list_active_queries.
            window_seconds: look-back for the live wait profile (5-600).
            database: pick one when the query_id exists in several databases.
        """
        return await run(diag.diagnose_query, query_id=query_id, window_seconds=window_seconds, database=database)

    @mcp.tool(annotations=ro)
    async def explain_query(
        query_id: Optional[str] = None, sql: Optional[str] = None, database: Optional[str] = None, analyze: bool = False
    ) -> dict[str, Any]:
        """Show the execution plan. With query_id: the planner's generic plan (parameters unknown)
        plus the real plans recorded by YugabyteDB with their average/max time and the parameters
        of the slowest call. With sql: the plan for that exact statement; analyze=true actually
        runs it (SELECT/WITH only, in a read-only session with a statement timeout) and adds
        DocDB request/row counts (EXPLAIN ANALYZE, DIST). Provide exactly one of query_id / sql.

        Args:
            query_id: statement to explain. sql: a single statement with literal values.
            database: needed when the statement lives in a non-default database.
            analyze: execute and measure (requires sql with literals).
        """
        return await run(diag.explain_query, query_id=query_id, sql=sql, database=database, analyze=analyze)

    @mcp.tool(annotations=ro)
    async def index_health(
        database: Optional[str] = None, schema: Optional[str] = None, table: Optional[str] = None, limit: int = 25
    ) -> dict[str, Any]:
        """Index hygiene for one database: broken indexes (invalid / not ready, e.g. after a failed
        CREATE INDEX), exact duplicates, redundant prefixes (covered by a wider index) and unused
        indexes (no scans on any node), each with size and a drop recommendation, plus the total
        space/write cost recoverable.

        Args:
            database: default is the connection's database (see list_nodes for the list).
            schema / table: narrow the scope. limit: rows per category (1-200).
        """
        return await run(diag.index_health, database=database, schema=schema, table=table, limit=limit)

    @mcp.tool(annotations=ro)
    async def verify_index(
        index: str, schema: str = "public", database: Optional[str] = None, timeout_seconds: int = 60
    ) -> dict[str, Any]:
        """Check that an index is consistent with its table (yb_index_check). Detects corrupt or
        missing entries that index_health cannot see. Reads the whole index and table, so run it
        off-peak on large tables.

        Args:
            index: index name. schema: its schema. database: its database.
            timeout_seconds: abort after this long (1-600).
        """
        return await run(diag.verify_index, index=index, schema=schema, database=database, timeout_seconds=timeout_seconds)

    return mcp


def start_mcp_background(
    seed_dsn: str,
    *,
    bind: str = DEFAULT_MCP_HOST,
    port: int = DEFAULT_MCP_PORT,
    auth_token: Optional[str] = None,
    node_parallelism: int = DEFAULT_NODE_PARALLELISM,
    statement_timeout_ms: int = 10_000,
) -> bool:
    """Start the MCP HTTP server in a daemon thread. Returns ``False`` (error on stderr) on failure.

    The listen socket is bound in the calling thread so a busy port is reported before the
    caller goes on to run its own UI.
    """
    reason = mcp_unavailable_reason()
    if reason:
        print(f"ybtop: {reason}", file=sys.stderr, flush=True)
        return False
    import uvicorn

    ctx = LiveContext(
        seed_dsn, node_parallelism=node_parallelism, statement_timeout_ms=statement_timeout_ms
    )
    app: Any = http_app(build_mcp(ctx), host=bind)
    token = (auth_token or "").strip()
    if token:
        app = BearerAuthMiddleware(app, token)
    config = uvicorn.Config(app, host=bind, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    try:
        sock = _bind_socket(bind, port)
    except OSError as exc:
        print(f"ybtop: could not start MCP server on http://{bind}:{port}{MCP_PATH} ({exc})", file=sys.stderr, flush=True)
        return False

    threading.Thread(
        target=lambda: server.run(sockets=[sock]), name="ybtop-mcp-http", daemon=True
    ).start()

    shown = f"[{bind}]" if ":" in bind and not bind.startswith("[") else bind
    print(
        f"ybtop MCP (v{__version__}): http://{shown}:{port}{MCP_PATH}  "
        f"auth={'bearer token' if token else 'NONE'}",
        file=sys.stderr,
        flush=True,
    )
    if not token and not is_loopback(bind):
        print(
            f"ybtop: WARNING: MCP is listening on {bind} with no auth token; anyone who can reach "
            f"the port can read live SQL text. Set --mcp-auth-token or {MCP_TOKEN_ENV}.",
            file=sys.stderr,
            flush=True,
        )
    return True


__all__ = [
    "BearerAuthMiddleware",
    "DEFAULT_MCP_HOST",
    "DEFAULT_MCP_PORT",
    "MCP_TOKEN_ENV",
    "build_mcp",
    "http_app",
    "is_loopback",
    "mcp_available",
    "mcp_unavailable_reason",
    "start_mcp_background",
]
