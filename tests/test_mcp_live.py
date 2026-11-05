from __future__ import annotations

import asyncio
import time
from typing import Any

import psycopg
import psycopg.conninfo
import pytest

from ybtop import live
from ybtop.cli import build_parser
from ybtop.mcp_server import BearerAuthMiddleware, is_loopback
from ybtop.topology import YsqlNode


# Bearer auth
def _call(app: Any, headers: list[tuple[bytes, bytes]], scope_type: str = "http") -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request"}

    async def send(msg: dict[str, Any]) -> None:
        sent.append(msg)

    asyncio.run(app({"type": scope_type, "headers": headers}, receive, send))
    return sent


class _Ok:
    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


@pytest.mark.parametrize(
    "header,status",
    [
        (None, 401),
        (b"Bearer wrong", 401),
        (b"Basic secret", 401),
        (b"secret", 401),
        (b"Bearer secret", 200),
        (b"bearer secret", 200),
    ],
)
def test_bearer_auth(header: Any, status: int) -> None:
    app = BearerAuthMiddleware(_Ok(), "secret")
    headers = [(b"authorization", header)] if header is not None else []
    sent = _call(app, headers)
    assert sent[0]["status"] == status
    if status == 401:
        assert (b"www-authenticate", b'Bearer realm="ybtop-mcp"') in sent[0]["headers"]


def test_bearer_auth_lets_lifespan_through() -> None:
    sent = _call(BearerAuthMiddleware(_Ok(), "secret"), [], scope_type="lifespan")
    assert sent[0]["status"] == 200  # inner app was reached


@pytest.mark.parametrize(
    "bind,expected",
    [("127.0.0.1", True), ("localhost", True), ("::1", True), ("[::1]", True),
     ("0.0.0.0", False), ("::", False), ("10.1.2.3", False), ("db.example.com", False)],
)
def test_is_loopback(bind: str, expected: bool) -> None:
    assert is_loopback(bind) is expected


# CLI toggle
def test_mcp_is_off_by_default_and_flags_parse() -> None:
    p = build_parser()
    for cmd in (["serve", "--data-dir", "d"], ["watch", "--host", "h"]):
        a = p.parse_args(cmd)
        assert a.mcp is False and a.mcp_bind == "127.0.0.1" and a.mcp_auth_token is None
    a = p.parse_args(["serve", "--data-dir", "d", "--mcp", "--mcp-port", "9", "--mcp-auth-token", "t", "--db-port", "5434"])
    assert (a.mcp, a.mcp_port, a.mcp_auth_token, a.db_port, a.port) == (True, 9, "t", 5434, 8765)


# Helpers
def test_trunc_and_clamp_and_query_id() -> None:
    assert live._trunc("a\n  b   c", 0) == "a b c"
    assert live._trunc("abcdefghij", 5) == "abcd…"
    assert live._trunc(None, 5) is None
    assert live._clamp("999999", 1, 500, "x") == 500
    with pytest.raises(live.LiveError):
        live._clamp("abc", 1, 5, "x")
    assert live._query_id_arg(" -123 ") == -123
    with pytest.raises(live.LiveError):
        live._query_id_arg("1; drop table t")


def test_percentiles_interpolate_and_overflow() -> None:
    buckets = [(0.0, 10.0, 50), (10.0, 20.0, 50)]
    p = live._percentiles(buckets, 0, (0.5, 0.75, 1.0))
    assert p == {"p50": 10.0, "p75": 15.0, "p100": 20.0}
    assert live._percentiles(buckets, 100, (0.99,))["p99"] is None  # falls in overflow
    assert live._percentiles([], 0, (0.5,))["p50"] is None


# Filtering
def _row(pid: int, state: str, qage: float = 0, sage: float = 0, **kw: Any) -> dict[str, Any]:
    base = {"pid": pid, "state": state, "query_age_ms": qage, "state_age_ms": sage, "xact_age_ms": None,
            "datname": "db", "usename": "u", "application_name": "", "query": f"select {pid}", "query_id": str(pid)}
    base.update(kw)
    return base


@pytest.fixture
def ctx(monkeypatch: pytest.MonkeyPatch) -> live.LiveContext:
    c = live.LiveContext("host=x")
    rows = [
        ("n1:5433", _row(1, "active", qage=5000)),
        ("n1:5433", _row(2, "active", qage=100)),
        ("n2:5433", _row(3, "idle in transaction", sage=9000, xact_age_ms=9500.0)),
        ("n2:5433", _row(4, "idle", sage=99999)),
        ("n2:5433", _row(5, "active", qage=7000, datname="other")),
    ]
    monkeypatch.setattr(c, "_all_backends", lambda node=None: (rows, {}))
    return c


def test_active_queries_defaults_exclude_idle_and_sort_by_running(ctx: live.LiveContext) -> None:
    out = ctx.active_queries()
    assert [q["pid"] for q in out["queries"]] == [3, 5, 1, 2]
    assert ctx.active_queries(include_idle=True)["total_matching"] == 5


def test_active_queries_filters(ctx: live.LiveContext) -> None:
    assert [q["pid"] for q in ctx.active_queries(min_duration_ms=6000)["queries"]] == [3, 5]
    assert [q["pid"] for q in ctx.active_queries(database="OTHER")["queries"]] == [5]
    assert [q["pid"] for q in ctx.active_queries(query_id="2")["queries"]] == [2]
    assert [q["pid"] for q in ctx.active_queries(sort_by="xact_age")["queries"]][0] == 3
    assert ctx.active_queries(limit=1)["returned"] == 1
    with pytest.raises(live.LiveError):
        ctx.active_queries(sort_by="bogus")
    with pytest.raises(live.LiveError):
        ctx.active_queries(query_id="x")


def test_select_nodes_matching(ctx: live.LiveContext) -> None:
    ctx._nodes = [YsqlNode("a", 5433, "uuid-a"), YsqlNode("b", 5433, "uuid-b")]
    ctx._nodes_at = time.monotonic()  # fresh, no re-discovery
    assert [n.host for n in ctx._select_nodes("b:5433")] == ["b"]
    assert [n.host for n in ctx._select_nodes("UUID-A")] == ["a"]
    with pytest.raises(live.LiveError, match="known nodes"):
        ctx._select_nodes("zzz")


# Seed node fallback
SEED = "host=seed port=5433 user=u dbname=d"
NODES = [YsqlNode("seed", 5433, "u1"), YsqlNode("n2", 5433, "u2"), YsqlNode("n3", 5433, "u3")]


class _FakeConn:
    def __init__(self, host: str) -> None:
        self.host = host

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _hosts_in(dsn: str) -> str:
    return psycopg.conninfo.conninfo_to_dict(dsn)["host"]


class _Down(set):
    """Hosts that refuse connections, plus a log of every connection attempt."""

    def __init__(self) -> None:
        super().__init__()
        self.attempts: list[str] = []


@pytest.fixture
def down(monkeypatch: pytest.MonkeyPatch) -> _Down:
    refused = _Down()
    attempts = refused.attempts

    def fake_connect(dsn: str, **_kw: Any) -> _FakeConn:
        host = _hosts_in(dsn)
        attempts.append(host)
        if host in refused:
            raise psycopg.OperationalError(f"{host} is down")
        return _FakeConn(host)

    def fake_discover(dsn: str) -> list[YsqlNode]:
        fake_connect(dsn)
        return NODES

    monkeypatch.setattr(live, "connect_readonly", fake_connect)
    monkeypatch.setattr(live, "discover_ysql_nodes", fake_discover)
    return refused


def test_discovery_falls_back_when_the_seed_is_down(down: _Down) -> None:
    ctx = live.LiveContext(SEED)
    assert [n.host for n in ctx.nodes()] == ["seed", "n2", "n3"]  # seed up: normal path
    down.add("seed")
    ctx._nodes_at = 0.0  # force re-discovery
    assert [n.host for n in ctx.nodes()] == ["seed", "n2", "n3"]  # answered by another node
    assert ctx._preferred is not None and ctx._preferred[0] != "seed"


def test_any_node_connection_skips_dead_seed_and_remembers_the_survivor(down: _Down) -> None:
    ctx = live.LiveContext(SEED)
    ctx.nodes()
    down.add("seed")
    with ctx._conn() as conn:
        assert conn.host == "n2"
    attempts = down.attempts
    before = len(attempts)
    with ctx._conn() as conn:
        assert conn.host == "n2"
    assert attempts[before:] == ["n2"]  # goes straight to the node that worked, not the dead seed again


def test_all_nodes_down_raises_the_connection_error(down: _Down) -> None:
    ctx = live.LiveContext(SEED)
    ctx.nodes()
    down.update({"seed", "n2", "n3"})
    with pytest.raises(psycopg.OperationalError), ctx._conn():
        pass


def test_explicit_node_connection_does_not_fall_back(down: _Down) -> None:
    ctx = live.LiveContext(SEED)
    down.add("n2")
    with pytest.raises(psycopg.OperationalError), ctx._conn(YsqlNode("n2", 5433, "u2")):
        pass  # a fan-out to one node must report that node's failure, not quietly use another


def test_connections_get_a_connect_timeout() -> None:
    dsn = live.LiveContext(SEED)._dsn()
    assert psycopg.conninfo.conninfo_to_dict(dsn)["connect_timeout"] == "5"
    custom = live.LiveContext(SEED + " connect_timeout=2")._dsn()
    assert psycopg.conninfo.conninfo_to_dict(custom)["connect_timeout"] == "2"  # user setting wins
