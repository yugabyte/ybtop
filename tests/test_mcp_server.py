"""HTTP checks of the MCP app that need no database.

Requests are plain JSON-RPC over POST (the server runs stateless with JSON responses).
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("mcp.server.mcpserver")  # needs mcp 2.x
pytest.importorskip("starlette")
pytest.importorskip("httpx2")  # HTTP client used by Starlette's TestClient
from starlette.testclient import TestClient  # noqa: E402

from ybtop.live import LiveContext  # noqa: E402
from ybtop.mcp_server import (  # noqa: E402
    BearerAuthMiddleware,
    build_mcp,
    http_app,
    mcp_unavailable_reason,
)

HOST = "127.0.0.1"
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _app() -> Any:
    # The DSN is never used
    ctx = LiveContext("host=127.0.0.1 port=1 user=x dbname=x")
    return http_app(build_mcp(ctx), host=HOST)


def _rpc(client: TestClient, method: str, params: dict[str, Any] | None = None, id_: int = 1, **kw: Any) -> Any:
    body = {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}
    return client.post("/mcp", json=body, headers={**HEADERS, **kw.pop("headers", {})}, **kw)


@pytest.fixture(scope="module")
def client() -> Any:
    with TestClient(_app(), base_url=f"http://{HOST}:8766") as c:
        yield c


def test_dependencies_import() -> None:
    assert mcp_unavailable_reason() is None


def test_lists_all_tools_and_marks_them_read_only(client: TestClient) -> None:
    r = _rpc(client, "tools/list")
    assert r.status_code == 200, r.text
    tools = r.json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"active_summary", "list_active_queries", "diagnose_query", "find_full_scans", "index_health",
            "verify_index", "cluster_bottlenecks", "explain_query", "top_queries"} <= names
    assert len(tools) == 16
    assert all(t["annotations"]["readOnlyHint"] is True for t in tools)


def test_tool_call_path_returns_a_tool_error_for_bad_input(client: TestClient) -> None:
    r = _rpc(client, "tools/call", {"name": "query_stats", "arguments": {"query_id": "abc"}})
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result["isError"] is True
    assert "query_id must be an integer" in result["content"][0]["text"]


def test_tool_call_without_database_validation_error(client: TestClient) -> None:
    r = _rpc(client, "tools/call", {"name": "explain_query", "arguments": {"sql": "select 1", "query_id": "1"}})
    result = r.json()["result"]
    assert result["isError"] is True and "exactly one of" in result["content"][0]["text"]


def test_rejects_foreign_host_header(client: TestClient) -> None:
    """DNS-rebinding protection is on for a loopback bind."""
    r = _rpc(client, "tools/list", headers={"Host": "evil.example:8766"})
    assert r.status_code in (400, 403, 421), r.text


def test_bearer_token_gate() -> None:
    app = BearerAuthMiddleware(_app(), "s3cret")
    with TestClient(app, base_url=f"http://{HOST}:8766") as c:
        assert _rpc(c, "tools/list").status_code == 401
        assert _rpc(c, "tools/list", headers={"Authorization": "Bearer nope"}).status_code == 401
        ok = _rpc(c, "tools/list", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200 and len(ok.json()["result"]["tools"]) == 16
