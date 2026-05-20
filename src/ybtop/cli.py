from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from rich.console import Console, Group
from rich.live import Live
from rich.markup import escape
from rich.style import Style
from rich.text import Text

from ybtop import __version__
from ybtop import collect
from ybtop.config import (
    DEFAULT_ASH_WINDOW_MINUTES,
    DEFAULT_LOG_BACKUP_COUNT,
    DEFAULT_LOG_LEVEL,
    DEFAULT_LOG_MAX_BYTES,
    DEFAULT_NODE_PARALLELISM,
    DEFAULT_REFRESH_INTERVAL_SEC,
    DEFAULT_SERVE_HOST,
    DEFAULT_SERVE_PORT,
    DEFAULT_SNAPSHOT_OUTPUT_DIR,
    DEFAULT_SNAPSHOT_RETENTION_HOURS,
    DEFAULT_YSQL_DBNAME,
    DEFAULT_YSQL_PORT,
    DEFAULT_YSQL_USER,
    SNAPSHOT_ASH_PER_NODE,
    SNAPSHOT_ASH_TOP_TABLES,
    SNAPSHOT_STATEMENTS_PER_NODE,
    Settings,
    load_dsn_from_env_or_none,
    resolve_ash_range,
    resolve_seed_dsn,
)
from ybtop.log import checkpoint_context, get_logger, init_logging, log_event, resolve_log_path
from ybtop.pg_stat_display import live_top5_statements_table
from ybtop.render import crz_ash_summary_rows, live_top5_nodes_by_active_session_sec, table_from_rows
from ybtop.snapshot_write import (
    build_snapshot_document,
    gc_snapshots_and_manifest,
    write_snapshot_and_update_manifest,
)


def _parse_ts(raw: str) -> datetime:
    """Parse ISO-8601 timestamps; assume UTC if no offset is given."""
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _watch_header_line(*, viewer_url: Optional[str], out_dir: Path) -> Text:
    root = str(out_dir.resolve())
    if viewer_url:
        return Text.assemble(
            "ybtop viewer: ",
            Text(viewer_url, style=Style(bold=True, link=viewer_url)),
            f"  (data dir: {escape(root)})",
        )
    return Text(f"ybtop watch: (data dir: {root})")


def run_watch(settings: Settings, *, viewer_url: Optional[str] = None) -> None:
    console = Console()
    out_dir = Path(settings.snapshot_output_dir)
    watch_log = get_logger("watch")
    iteration = 0
    log_event(watch_log, "watch_started", output_dir=str(out_dir.resolve()))
    with Live(
        console=console,
        refresh_per_second=1,
        screen=True,
        redirect_stdout=False,
        redirect_stderr=False,
        vertical_overflow="visible",
    ) as live:
        while True:
            iteration += 1
            started = time.monotonic()
            doc: Any = None
            snapshot_err: Optional[str] = None
            with checkpoint_context(iteration) as ckpt_summary:
                log_event(watch_log, "checkpoint_start", checkpoint=iteration)
                if iteration == 1:
                    live.update(
                        Group(
                            _watch_header_line(viewer_url=viewer_url, out_dir=out_dir),
                            Text(
                                "Collecting data for first checkpoint. Please wait...",
                                style="dim",
                            ),
                        )
                    )
                try:
                    ash_start, ash_end = resolve_ash_range(settings)
                    doc = build_snapshot_document(
                        seed_dsn=settings.seed_dsn,
                        ash_start=ash_start,
                        ash_end=ash_end,
                        statements_per_node=settings.snapshot_statements_per_node,
                        ash_per_node=settings.snapshot_ash_per_node,
                        ensure_ycql_extension=(iteration == 1),
                        ash_top_tables=settings.snapshot_ash_top_tables,
                    collect_table_ddl=settings.snapshot_collect_table_ddl,
                    node_parallelism=settings.node_parallelism,
                )
                    write_snapshot_and_update_manifest(output_dir=out_dir, document=doc)
                    gc_snapshots_and_manifest(
                        output_dir=out_dir,
                        retention_hours=settings.snapshot_retention_hours,
                    )
                except Exception as exc:  # noqa: BLE001
                    doc = None
                    snapshot_err = str(exc)
                    log_event(
                        watch_log,
                        "checkpoint_error",
                        level=logging.ERROR,
                        error=snapshot_err,
                        checkpoint=iteration,
                    )
                tick_ms = round((time.monotonic() - started) * 1000.0, 2)
                summary = ckpt_summary.summary_fields(total_ms=tick_ms)
                if snapshot_err is None:
                    summary["status"] = "ok"
                    if doc and isinstance(doc, dict):
                        summary["node_count"] = len(doc.get("nodes") or [])
                else:
                    summary["status"] = "error"
                    summary["error"] = snapshot_err
                log_event(watch_log, "checkpoint_summary", **summary)
                log_event(
                    watch_log,
                    "checkpoint_complete",
                    checkpoint=iteration,
                    duration_ms=tick_ms,
                    status=summary.get("status"),
                )
            utc_now = datetime.now(timezone.utc)
            ts_str = utc_now.strftime("%Y-%m-%d %H:%M:%S")
            table_block: list[Any] = []
            if doc is not None and isinstance(doc, dict):
                table_block.append(live_top5_statements_table(doc, out_dir))
                table_block.append(live_top5_nodes_by_active_session_sec(doc))
                crz = crz_ash_summary_rows(doc)
                if crz:
                    table_block.append(
                        table_from_rows(
                            "cloud · region · zone  (nodes, active sessions/s, load %)",
                            crz,
                        )
                    )
                else:
                    table_block.append(
                        Text("Placement / ASH summary: (no rows)", style="dim"),
                    )
            else:
                u = " (unavailable; snapshot not written this tick)"
                table_block.append(
                    Text(f"Top 5 — pg_stat_statements{u}", style="dim"),
                )
                table_block.append(
                    Text(f"Top 5 — nodes (by active sessions/sec){u}", style="dim"),
                )
                table_block.append(
                    Text(
                        f"Placement / ASH summary{u}",
                        style="dim",
                    ),
                )
            elapsed = time.monotonic() - started
            sleep_for = max(0.1, settings.refresh_interval - elapsed)
            deadline = time.monotonic() + sleep_for
            while True:
                now = time.monotonic()
                rem = int(max(0.0, deadline - now))
                rem_verb = "1 sec" if rem == 1 else f"{rem} secs"
                status = Text.assemble(
                    "Checkpoint ",
                    Text(f"#{iteration}", style="bold"),
                    " @ ",
                    Text(f"{ts_str} UTC", style="bold"),
                    ";  Next checkpoint in: ",
                    Text(rem_verb, style="bold"),
                    ".",
                )
                banner: list[Any] = []
                if snapshot_err is not None:
                    banner.append(
                        Text(
                            f"snapshot write failed: {snapshot_err}",
                            style="bold red",
                        )
                    )
                live.update(
                    Group(
                        _watch_header_line(viewer_url=viewer_url, out_dir=out_dir),
                        status,
                        *banner,
                        *table_block,
                    )
                )
                if now >= deadline - 1e-9:
                    break
                time.sleep(min(1.0, max(0.0, deadline - now)))


def run_reset_pg_stat_statements(settings: Settings) -> None:
    console = Console()
    rows = collect.reset_pg_stat_statements_cluster(settings.seed_dsn)
    console.print(
        table_from_rows(
            "pg_stat_statements_reset() per node",
            rows,
        )
    )
    failed = [r for r in rows if r.get("status") != "ok"]
    if failed:
        raise SystemExit(1)


def _connection_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("connection (any one YSQL node in the universe)")
    g.add_argument("--dsn", help="Libpq URL for one node (overrides env).")
    g.add_argument("--host", help="Seed node host/IP (alternative to --dsn).")
    g.add_argument(
        "--port",
        type=int,
        default=DEFAULT_YSQL_PORT,
        help="YSQL port when using --host.",
    )
    g.add_argument("--user", default=DEFAULT_YSQL_USER, help="User when using --host.")
    g.add_argument(
        "--password",
        default=None,
        help="Password when using --host (default: YBTOP_PASSWORD env if set).",
    )
    g.add_argument("--dbname", default=DEFAULT_YSQL_DBNAME, help="Database when using --host.")


def _ash_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("ASH time range")
    g.add_argument(
        "--ash-window-minutes",
        type=int,
        default=DEFAULT_ASH_WINDOW_MINUTES,
        metavar="MINS",
        help=(
            "ASH rolling window length in minutes when --ash-start/--ash-end are omitted "
            "(each watch refresh ends at UTC now)."
        ),
    )
    g.add_argument(
        "--ash-start",
        metavar="ISO8601",
        help="Window start (timestamptz). If set, --ash-end is required.",
    )
    g.add_argument(
        "--ash-end",
        metavar="ISO8601",
        help="Window end (timestamptz). If set, --ash-start is required.",
    )


def build_parser() -> argparse.ArgumentParser:
    fmt = argparse.ArgumentDefaultsHelpFormatter
    p = argparse.ArgumentParser(
        prog="ybtop",
        formatter_class=fmt,
        description=(
            "YugabyteDB observability: connect to one YSQL node, discover the rest via yb_servers(), "
            "merge per-node stats, write snapshot JSON + manifest on watch, and serve a browser UI."
        ),
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    w = sub.add_parser(
        "watch",
        help=(
            "Live multi-panel dashboard; writes ybtop.out.*.json and ybtop.manifest.json each tick; "
            "starts the browser viewer (HTTP) by default."
        ),
        formatter_class=fmt,
    )
    _connection_args(w)
    _ash_args(w)
    w.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_REFRESH_INTERVAL_SEC,
        metavar="SEC",
        help="Seconds between dashboard refresh and snapshot write.",
    )
    w.add_argument(
        "--output-dir",
        default=DEFAULT_SNAPSHOT_OUTPUT_DIR,
        help="Directory for ybtop.out.*.json and ybtop.manifest.json.",
    )
    w.add_argument(
        "--snapshot-retention-hours",
        type=float,
        default=DEFAULT_SNAPSHOT_RETENTION_HOURS,
        metavar="HOURS",
        help="Delete snapshot files older than this (manifest pruned accordingly). 0 disables GC.",
    )
    w.add_argument(
        "--snapshot-statements-per-node",
        type=int,
        default=SNAPSHOT_STATEMENTS_PER_NODE,
        metavar="N",
        help="Top N statements per node stored in each snapshot file.",
    )
    w.add_argument(
        "--snapshot-ash-per-node",
        type=int,
        default=SNAPSHOT_ASH_PER_NODE,
        metavar="N",
        help="Top N ASH groups per node stored in each snapshot file.",
    )
    w.add_argument(
        "--snapshot-ash-top-tables",
        type=int,
        default=SNAPSHOT_ASH_TOP_TABLES,
        metavar="N",
        help=(
            "After per-node collection, rank table_id values by ASH samples cluster-wide "
            "and store the top N (0 disables)."
        ),
    )
    w.add_argument(
        "--snapshot-table-ddl",
        action="store_true",
        help=(
            "Fetch CREATE TABLE/INDEX DDL for ash_top_tables on the seed connection "
            "(default: skip DDL collection)."
        ),
    )
    w.add_argument(
        "--node-parallelism",
        type=int,
        default=DEFAULT_NODE_PARALLELISM,
        metavar="N",
        help=(
            "Max concurrent YSQL nodes when collecting per-node snapshot data "
            "(pg_stat, ASH, tablets, etc.)."
        ),
    )
    v = w.add_argument_group("viewer (HTTP; same as ybtop serve)")
    v.add_argument(
        "--no-serve",
        action="store_true",
        help="Do not start the browser viewer; only the terminal dashboard and snapshot files.",
    )
    v.add_argument(
        "--serve-bind",
        default=DEFAULT_SERVE_HOST,
        help="HTTP listen address for the embedded viewer (not YSQL; see connection group for --port).",
    )
    v.add_argument(
        "--serve-port",
        type=int,
        default=DEFAULT_SERVE_PORT,
        help="HTTP listen port for the embedded viewer.",
    )
    lg = w.add_argument_group("logging")
    lg.add_argument(
        "--log-file",
        default=None,
        metavar="PATH",
        help="Structured JSON log file (default: OUTPUT_DIR/ybtop.log).",
    )
    lg.add_argument(
        "--log-level",
        default=DEFAULT_LOG_LEVEL,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Minimum severity written to the log file.",
    )
    lg.add_argument(
        "--log-max-bytes",
        type=int,
        default=DEFAULT_LOG_MAX_BYTES,
        metavar="BYTES",
        help="Rotate the log file when it exceeds this size.",
    )
    lg.add_argument(
        "--log-backup-count",
        type=int,
        default=DEFAULT_LOG_BACKUP_COUNT,
        metavar="N",
        help="Number of rotated log files to retain.",
    )
    lg.add_argument(
        "--no-log-file",
        action="store_true",
        help="Disable structured file logging.",
    )

    reset_p = sub.add_parser(
        "reset_pg_stat_statements",
        help="Run SELECT pg_stat_statements_reset() on every YSQL node (via yb_servers()).",
        formatter_class=fmt,
        epilog=(
            "Requires permission to reset statement statistics on each node (typically the "
            "yugabyte superuser). Clears counters only; the pg_stat_statements extension stays loaded."
        ),
    )
    _connection_args(reset_p)

    serve_p = sub.add_parser(
        "serve",
        help="HTTP server for the static viewer (reads snapshot dir; does not modify manifest).",
        formatter_class=fmt,
    )
    serve_p.add_argument(
        "--data-dir",
        required=True,
        help="Directory containing ybtop.manifest.json and ybtop.out.*.json (same as watch --output-dir).",
    )
    serve_p.add_argument(
        "--bind",
        default=DEFAULT_SERVE_HOST,
        help="Listen address for HTTP.",
    )
    serve_p.add_argument(
        "--port",
        type=int,
        default=DEFAULT_SERVE_PORT,
        help="Listen port for HTTP.",
    )
    return p


def _settings_from_args(args: argparse.Namespace) -> Settings:
    env_dsn = load_dsn_from_env_or_none()
    password = args.password if args.password is not None else os.environ.get("YBTOP_PASSWORD")
    if args.dsn:
        seed = args.dsn
    elif args.host:
        seed = resolve_seed_dsn(
            dsn=None,
            host=args.host,
            port=int(args.port),
            user=args.user,
            password=password,
            dbname=args.dbname,
        )
    elif env_dsn:
        seed = env_dsn
    else:
        raise SystemExit("Provide --dsn, or --host, or set YBTOP_DSN / DATABASE_URL.")
    ash_start_raw = getattr(args, "ash_start", None)
    ash_end_raw = getattr(args, "ash_end", None)
    ash_start = _parse_ts(ash_start_raw) if ash_start_raw else None
    ash_end = _parse_ts(ash_end_raw) if ash_end_raw else None
    if (ash_start is None) ^ (ash_end is None):
        raise SystemExit("Provide both --ash-start and --ash-end, or neither.")
    return Settings(
        seed_dsn=seed,
        refresh_interval=float(getattr(args, "interval", DEFAULT_REFRESH_INTERVAL_SEC)),
        ash_window_minutes=int(getattr(args, "ash_window_minutes", DEFAULT_ASH_WINDOW_MINUTES)),
        ash_start=ash_start,
        ash_end=ash_end,
        snapshot_output_dir=str(getattr(args, "output_dir", DEFAULT_SNAPSHOT_OUTPUT_DIR)),
        snapshot_retention_hours=float(
            getattr(args, "snapshot_retention_hours", DEFAULT_SNAPSHOT_RETENTION_HOURS)
        ),
        snapshot_statements_per_node=int(
            getattr(args, "snapshot_statements_per_node", SNAPSHOT_STATEMENTS_PER_NODE)
        ),
        snapshot_ash_per_node=int(getattr(args, "snapshot_ash_per_node", SNAPSHOT_ASH_PER_NODE)),
        snapshot_ash_top_tables=int(
            getattr(args, "snapshot_ash_top_tables", SNAPSHOT_ASH_TOP_TABLES)
        ),
        snapshot_collect_table_ddl=bool(getattr(args, "snapshot_table_ddl", False)),
        log_enabled=not bool(getattr(args, "no_log_file", False)),
        log_file=getattr(args, "log_file", None),
        log_level=str(getattr(args, "log_level", DEFAULT_LOG_LEVEL)),
        log_max_bytes=int(getattr(args, "log_max_bytes", DEFAULT_LOG_MAX_BYTES)),
        log_backup_count=int(getattr(args, "log_backup_count", DEFAULT_LOG_BACKUP_COUNT)),
        node_parallelism=int(getattr(args, "node_parallelism", DEFAULT_NODE_PARALLELISM)),
    )


def main(argv: Optional[list[str]] = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        from ybtop.serve import run_serve

        run_serve(data_dir=args.data_dir, host=args.bind, port=args.port)
        return

    settings = _settings_from_args(args)

    if args.command == "watch":
        if settings.log_enabled:
            init_logging(
                log_path=resolve_log_path(settings.snapshot_output_dir, settings.log_file),
                level=settings.log_level,
                max_bytes=settings.log_max_bytes,
                backup_count=settings.log_backup_count,
            )
        else:
            init_logging(log_path=None)
        if not args.no_serve:
            from ybtop.serve import start_serve_background

            if not start_serve_background(
                data_dir=settings.snapshot_output_dir,
                host=args.serve_bind,
                port=int(args.serve_port),
            ):
                tail = (
                    "Use --no-serve to run the terminal dashboard without HTTP."
                )
                print(
                    "ybtop: exiting (embedded viewer did not start). " + tail,
                    file=sys.stderr,
                    flush=True,
                )
                raise SystemExit(1)
        try:
            run_watch(
                settings,
                viewer_url=None
                if args.no_serve
                else f"http://{args.serve_bind}:{int(args.serve_port)}/",
            )
        except KeyboardInterrupt:
            sys.exit(0)
    elif args.command == "reset_pg_stat_statements":
        run_reset_pg_stat_statements(settings)
    else:
        raise SystemExit("Unknown command")
