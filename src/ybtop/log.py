from __future__ import annotations

import json
import logging
import logging.handlers
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from ybtop.config import (
    DEFAULT_LOG_BACKUP_COUNT,
    DEFAULT_LOG_FILENAME,
    DEFAULT_LOG_LEVEL,
    DEFAULT_LOG_MAX_BYTES,
)

_checkpoint_var: ContextVar[Optional["CheckpointLog"]] = ContextVar("ybtop_checkpoint", default=None)
_summary_scope_var: ContextVar[Optional[str]] = ContextVar("ybtop_summary_scope", default=None)


@dataclass
class StageStats:
    row_count: Optional[int] = None


@dataclass
class NodeCheckpointLog:
    total_ms: Optional[float] = None
    stages_ms: dict[str, float] = field(default_factory=dict)


@dataclass
class ScopeCheckpointLog:
    total_ms: Optional[float] = None
    stages_ms: dict[str, float] = field(default_factory=dict)
    per_node_ms: dict[str, NodeCheckpointLog] = field(default_factory=dict)


@dataclass
class CheckpointLog:
    checkpoint: int
    stages_ms: dict[str, float] = field(default_factory=dict)
    scopes: dict[str, ScopeCheckpointLog] = field(default_factory=dict)

    def _scope(self, name: str) -> ScopeCheckpointLog:
        return self.scopes.setdefault(name, ScopeCheckpointLog())

    def record(
        self,
        stage: str,
        duration_ms: float,
        *,
        node_id: Optional[str] = None,
        node_total: bool = False,
        scope_total: bool = False,
    ) -> None:
        if scope_total and not node_id:
            scope = self._scope(stage)
            scope.total_ms = (scope.total_ms or 0.0) + duration_ms
            return

        active_scope = _summary_scope_var.get()

        if node_id:
            bucket = self._scope(active_scope).per_node_ms if active_scope else {}
            if active_scope is None:
                return
            node = bucket.setdefault(node_id, NodeCheckpointLog())
            if node_total:
                node.total_ms = (node.total_ms or 0.0) + duration_ms
            else:
                node.stages_ms[stage] = node.stages_ms.get(stage, 0.0) + duration_ms
            return

        if active_scope:
            scope = self._scope(active_scope)
            scope.stages_ms[stage] = scope.stages_ms.get(stage, 0.0) + duration_ms
        else:
            self.stages_ms[stage] = self.stages_ms.get(stage, 0.0) + duration_ms

    @staticmethod
    def _serialize_per_node(per_node: dict[str, NodeCheckpointLog]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for nid, node in sorted(per_node.items()):
            entry: dict[str, Any] = {}
            if node.total_ms is not None:
                entry["total_ms"] = round(node.total_ms, 2)
            if node.stages_ms:
                entry["stages_ms"] = {
                    k: round(v, 2) for k, v in sorted(node.stages_ms.items())
                }
            out[nid] = entry
        return out

    def summary_fields(self, *, total_ms: float) -> dict[str, Any]:
        stages: dict[str, Any] = {
            k: round(v, 2) for k, v in sorted(self.stages_ms.items())
        }
        for name, scope in sorted(self.scopes.items()):
            entry: dict[str, Any] = {}
            if scope.total_ms is not None:
                entry["total_ms"] = round(scope.total_ms, 2)
            if scope.stages_ms:
                entry["stages_ms"] = {
                    k: round(v, 2) for k, v in sorted(scope.stages_ms.items())
                }
            if scope.per_node_ms:
                entry["per_node_ms"] = self._serialize_per_node(scope.per_node_ms)
            stages[name] = entry
        return {
            "checkpoint": self.checkpoint,
            "total_ms": round(total_ms, 2),
            "stages_ms": stages,
        }


class JsonLogFormatter(logging.Formatter):
    """One JSON object per line (Google Cloud Logging–friendly)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "ybtop_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info and record.exc_info[1] is not None:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


def resolve_log_path(output_dir: str, log_file: Optional[str]) -> Optional[Path]:
    if log_file is not None and str(log_file).strip() == "":
        return None
    if log_file:
        p = Path(log_file)
        if not p.is_absolute():
            p = Path(output_dir) / p
        return p
    return Path(output_dir) / DEFAULT_LOG_FILENAME


def init_logging(
    *,
    log_path: Optional[Path],
    level: str = DEFAULT_LOG_LEVEL,
    max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
) -> None:
    """Configure root ybtop logger: JSON lines to a rotating file (if log_path set)."""
    root = logging.getLogger("ybtop")
    root.handlers.clear()
    root.setLevel(logging.DEBUG)
    root.propagate = False

    if log_path is None:
        root.addHandler(logging.NullHandler())
        return

    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(JsonLogFormatter())
    handler.setLevel(_parse_level(level))
    root.addHandler(handler)

    log_event(root, "logging_initialized", log_file=str(log_path.resolve()))


def _parse_level(name: str) -> int:
    return getattr(logging, str(name).upper(), logging.INFO)


def get_logger(name: str) -> logging.Logger:
    if name.startswith("ybtop"):
        return logging.getLogger(name)
    return logging.getLogger(f"ybtop.{name}")


def log_event(logger: logging.Logger, event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    record_fields = {"event": event, **fields}
    ckpt = _checkpoint_var.get()
    if ckpt is not None and "checkpoint" not in record_fields:
        record_fields["checkpoint"] = ckpt.checkpoint
    logger.log(level, event, extra={"ybtop_fields": record_fields})


@contextmanager
def checkpoint_context(checkpoint: int) -> Iterator[CheckpointLog]:
    summary = CheckpointLog(checkpoint=checkpoint)
    token = _checkpoint_var.set(summary)
    try:
        yield summary
    finally:
        _checkpoint_var.reset(token)


@contextmanager
def summary_scope(scope: str) -> Iterator[None]:
    """Nest stage timings under a named scope (e.g. build_snapshot) in checkpoint_summary."""
    token = _summary_scope_var.set(scope)
    try:
        yield
    finally:
        _summary_scope_var.reset(token)


@contextmanager
def stage_timer(
    stage: str,
    logger: logging.Logger,
    *,
    node_id: Optional[str] = None,
    node_total: bool = False,
    scope_total: bool = False,
    **fields: Any,
) -> Iterator[StageStats]:
    stats = StageStats()
    start = time.monotonic()
    ctx = {"stage": stage}
    if node_id:
        ctx["node_id"] = node_id
    ctx.update(fields)
    log_event(logger, "stage_start", **ctx)
    err: Optional[BaseException] = None
    try:
        yield stats
    except BaseException as exc:
        err = exc
        raise
    finally:
        duration_ms = round((time.monotonic() - start) * 1000.0, 2)
        done = {**ctx, "duration_ms": duration_ms}
        if stats.row_count is not None:
            done["row_count"] = stats.row_count
        ckpt = _checkpoint_var.get()
        if ckpt is not None:
            ckpt.record(
                stage,
                duration_ms,
                node_id=node_id,
                node_total=node_total,
                scope_total=scope_total,
            )
        if err is not None:
            done["error"] = str(err)
            log_event(logger, "stage_error", level=logging.ERROR, **done)
        else:
            log_event(logger, "stage_complete", **done)
