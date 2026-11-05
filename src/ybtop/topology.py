from __future__ import annotations

from dataclasses import dataclass

import psycopg.conninfo
import psycopg.errors

from ybtop.db import connect, fetch_all


@dataclass(frozen=True)
class YsqlNode:
    """One YSQL endpoint discovered from yb_servers()."""

    host: str
    port: int
    server_uuid: str
    cloud: str = ""
    region: str = ""
    zone: str = ""


def _discover_sql_extended() -> str:
    return """
    SELECT
        host::text AS host,
        port::int AS port,
        uuid::text AS server_uuid,
        COALESCE(cloud::text, '') AS cloud,
        COALESCE(region::text, '') AS region,
        COALESCE(zone::text, '') AS zone
    FROM yb_servers();
    """


def _discover_sql_minimal() -> str:
    return """
    SELECT
        host::text AS host,
        port::int AS port,
        uuid::text AS server_uuid
    FROM yb_servers();
    """


def discover_ysql_nodes(seed_dsn: str) -> list[YsqlNode]:
    """Return all nodes' YSQL host/port (and placement when available) from yb_servers()."""
    with connect(seed_dsn) as conn:
        try:
            rows = fetch_all(conn, _discover_sql_extended())
            extended = True
        except psycopg.errors.UndefinedColumn:
            rows = fetch_all(conn, _discover_sql_minimal())
            extended = False
    nodes: list[YsqlNode] = []
    for row in rows:
        host = row.get("host")
        port = row.get("port")
        if host is None or port is None:
            continue
        nodes.append(
            YsqlNode(
                host=str(host),
                port=int(port),
                server_uuid=str(row.get("server_uuid") or ""),
                cloud=str(row.get("cloud") or "") if extended else "",
                region=str(row.get("region") or "") if extended else "",
                zone=str(row.get("zone") or "") if extended else "",
            )
        )
    if not nodes:
        raise RuntimeError("yb_servers() returned no rows; cannot fan out across the cluster.")
    return nodes


def dsn_for_node(seed_dsn: str, node: YsqlNode) -> str:
    """Reuse credentials/options from seed DSN but point at another node's YSQL port."""
    info = psycopg.conninfo.conninfo_to_dict(seed_dsn)
    info["host"] = node.host
    info["port"] = str(node.port)
    return psycopg.conninfo.make_conninfo(**info)


def dsn_with_database(dsn: str, dbname: str) -> str:
    """Same connection as ``dsn`` but to another database."""
    info = psycopg.conninfo.conninfo_to_dict(dsn)
    info["dbname"] = dbname
    return psycopg.conninfo.make_conninfo(**info)


def node_id(node: YsqlNode) -> str:
    return f"{node.host}:{node.port}"
