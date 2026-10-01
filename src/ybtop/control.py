"""Viewer-writable collection toggles, shared by ``ybtop serve`` and ``ybtop watch``.

The viewer and the collector are different threads (``watch``) or different
processes (``serve`` against a directory someone else is filling), so the toggle
lives in a small JSON file in the data directory rather than in memory. ``watch``
re-reads it at the top of every checkpoint, which is what lets a button in the
browser turn plan collection on without restarting the collector.

Writes are atomic (temp file + ``os.replace``) so a collector reading mid-write
never sees a partial file.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CONTROL_FILENAME = "ybtop.control.json"

# Toggles the viewer may set, and what they default to when the file is absent.
# Keep in sync with the switches read in cli.run_watch.
CONTROL_DEFAULTS: dict[str, bool] = {
    "query_plans": False,
}


# Toggles a collector flag has fixed, with the flag that did: written by watch at
# startup, never by a request, so the viewer cannot switch them back.
LOCKS_KEY = "locked"


class ControlLocked(Exception):
    """A request tried to change a toggle that a collector flag has fixed."""

    def __init__(self, toggle: str, flag: str):
        super().__init__("%s is fixed by the collector's %s" % (toggle, flag))
        self.toggle = toggle
        self.flag = flag


def control_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / CONTROL_FILENAME


def _read_doc(data_dir: Path | str) -> dict[str, Any]:
    try:
        doc = json.loads(control_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def read_control(data_dir: Path | str) -> dict[str, bool]:
    """Current toggle state. A missing, unreadable or malformed file reads as defaults.

    Never raises: a collector must not die because someone hand-edited this file.
    """
    out = dict(CONTROL_DEFAULTS)
    doc = _read_doc(data_dir)
    for key in CONTROL_DEFAULTS:
        if key in doc:
            out[key] = bool(doc[key])
    return out


def read_control_locks(data_dir: Path | str) -> dict[str, str]:
    """Toggle -> the collector flag that fixed it. Never raises, like read_control."""
    raw = _read_doc(data_dir).get(LOCKS_KEY)
    if not isinstance(raw, dict):
        return {}
    return {k: str(v) for k, v in raw.items() if k in CONTROL_DEFAULTS and v}


def write_control(
    data_dir: Path | str, updates: dict[str, Any], *, locks: "dict[str, str] | None" = None
) -> dict[str, bool]:
    """Merge `updates` into the control file and return the resulting state.

    Unknown keys are ignored rather than stored, so a stale or hostile request
    cannot plant fields the collector might later be taught to honour.
    `locks` comes from the collector only and replaces the saved locks; without
    it (a request) a locked toggle cannot change, and ControlLocked says why.
    Raises OSError when the directory is not writable (archive dirs served
    read-only), which the caller reports to the viewer.
    """
    state = read_control(data_dir)
    saved_locks = read_control_locks(data_dir)
    if locks is None:
        for key in CONTROL_DEFAULTS:
            if key in updates and key in saved_locks and bool(updates[key]) != state[key]:
                raise ControlLocked(key, saved_locks[key])
        new_locks = saved_locks
    else:
        new_locks = {k: str(v) for k, v in locks.items() if k in CONTROL_DEFAULTS and v}
    for key in CONTROL_DEFAULTS:
        if key in updates:
            state[key] = bool(updates[key])
    doc: dict[str, Any] = dict(state)
    if new_locks:
        doc[LOCKS_KEY] = new_locks
    doc["updated_utc"] = datetime.now(timezone.utc).isoformat()
    target = control_path(data_dir)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".ybtop.control.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, sort_keys=True)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return state
