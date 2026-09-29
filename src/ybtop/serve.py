from __future__ import annotations

import ipaddress
import json
import mimetypes
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import psycopg.conninfo
from rich.console import Console

from ybtop import __version__ as _ybtop_version
from ybtop import explain as X
from ybtop import pinscan
from ybtop import queries as Q
from ybtop.control import CONTROL_DEFAULTS, ControlLocked, read_control, read_control_locks, write_control
from ybtop.db import connect, dsn_for_database, dsn_for_host

_INT64_TEXT = re.compile(r"^-?\d{1,20}$")


_PLAN_REF_TEXT = re.compile(r"^[0-9a-f]{1,64}$")

def _host_name(host_header: str) -> str:
    """"Host: [::1]:8765" -> "::1"; "Host: example.com:8765" -> "example.com"."""
    h = (host_header or "").strip().lower()
    if h.startswith("["):
        return h[1:].split("]", 1)[0]
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def _is_loopback(name: str) -> bool:
    """localhost, or a literal loopback address (127.0.0.0/8, ::1).

    A name that only starts with "127." -- 127.attacker.example, 127.0.0.1.nip.io --
    is a domain its owner can rebind to this machine, so it is not loopback here.
    """
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def host_allowed(host_header: "str | None", allowed: "frozenset[str]", bind_host: str) -> bool:
    """DNS-rebinding guard: is this Host header one this viewer is really reached by?

    A page on an attacker's domain whose DNS is rebound to this address becomes
    same-origin with the viewer, so it could read snapshots and drive the POST
    endpoints. Only the Host header gives it away. localhost and loopback addresses are always
    allowed; a loopback-bound viewer allows nothing else unless configured. A
    viewer on another address cannot know the names it is reached by, so it
    checks only when --serve-allowed-host lists them.
    """
    if host_header is None:
        return True  # not a browser: HTTP/1.0 clients may omit it
    name = _host_name(host_header)
    if _is_loopback(name) or name in allowed:
        return True
    if allowed:
        return False
    return not _is_loopback(_host_name(bind_host))


def cross_site_problem(headers: "object", allowed: "frozenset[str]" = frozenset()) -> "str | None":
    """Why a state-changing POST is refused as cross-site, or None.

    The viewer has no login, so any page the operator visits could otherwise post
    here: a text/plain form body parses as JSON just the same, and needs no CORS
    preflight. Requiring application/json forces a preflight this server never
    answers; Sec-Fetch-Site and Origin catch the rest.
    """
    get = getattr(headers, "get")
    ctype = str(get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if ctype != "application/json":
        return "expected Content-Type: application/json"
    site = str(get("Sec-Fetch-Site") or "").strip().lower()
    if site and site != "same-origin":
        return "cross-site request refused"
    origin = get("Origin")
    if origin is not None:
        if str(origin).strip() == "null":
            return "cross-origin request refused"
        o = urlparse(str(origin))
        same = o.netloc.lower() == str(get("Host") or "").strip().lower()
        # Behind a proxy that rewrites Host, the public name is the one configured.
        if not same and _host_name(o.netloc) not in allowed:
            return "cross-origin request refused"
    return None



def unguarded_bind_problem(bind_host: str, allowed: "frozenset[str]", features: "list[str]") -> "str | None":
    """Why the viewer's cluster-writing features must not start on this bind, or None.

    A viewer bound to a non-loopback address checks the Host header only against
    names it is given (see host_allowed); without any, a DNS-rebinding page is
    same-origin with it and could drive every POST endpoint.
    """
    if not features or _is_loopback(_host_name(bind_host)) or allowed:
        return None
    return (
        "%s on a viewer bound to %s needs --serve-allowed-host NAME (the host names it is "
        "reached by), or bind it to 127.0.0.1: otherwise a page whose DNS is rebound to this "
        "address could use them." % (" and ".join(features), bind_host)
    )


def _database_gone(exc: BaseException) -> bool:
    """A connection refused because its database no longer exists.

    psycopg reports it as a bare OperationalError (no SQLSTATE on a failed
    connect), so the server's message is what tells it apart.
    """
    msg = str(exc)
    return "FATAL:" in msg and 'database "' in msg and '" does not exist' in msg


def find_recorded_plan(
    doc: dict, queryid: str, planid: str, plan_ref: str, dbid: str
) -> "tuple[bool, str | None]":
    """(found, hints) for one recorded (queryid, planid, plan_ref, dbid) in a snapshot.

    The database is part of the key because hint tables and ALTER DATABASE settings
    are per database: the same queryid often has plans in several databases (22 of
    52 on one cluster, some of them long dropped), and pinning into the wrong one
    changes nothing while reporting success.
    """
    qpm = doc.get("yb_pg_stat_plans") or {}
    texts = qpm.get("plans") or {}
    for rows in (qpm.get("per_node") or {}).values():
        for r in rows or []:
            if (
                str(r.get("queryid")) == str(queryid)
                and str(r.get("planid")) == str(planid)
                and str(r.get("plan_ref")) == str(plan_ref)
                and str(r.get("dbid")) == str(dbid)
            ):
                hints = (texts.get(str(r.get("plan_ref"))) or {}).get("hints")
                return True, (str(hints) if hints else None)
    return False, None


def pin_state_from_status(status: dict, row: "dict | None") -> dict:
    """What the viewer's pin row can do, from qpm_status() and the pinned hint row.

    Pure, so the pre-pin states are testable without a database:
      available            hint_plan.hints exists; pinning can proceed
      installable          pg_hint_plan ships with the cluster but is not created
                           in this database -- the viewer offers Install instead
                           of an instruction to go run SQL elsewhere
      neither              pg_hint_plan is not shipped, or is half-installed
    """
    prereq = {
        "hint_plan_installed": bool(status.get("hint_plan_installed")),
        "hint_plan_available": bool(status.get("hint_plan_available")),
        "hint_table": bool(status.get("hint_table_present")),
        "enable_hint_table": str(status.get("enable_hint_table") or "").lower() == "on",
        "use_query_id_for_hinting": str(
            status.get("use_query_id_for_hinting") or ""
        ).lower() == "on",
    }
    installable = (
        not prereq["hint_table"]
        and prereq["hint_plan_available"]
        and not prereq["hint_plan_installed"]
    )
    if prereq["hint_table"]:
        reason = None
    elif installable:
        reason = "pg_hint_plan is not installed in this database yet."
    elif prereq["hint_plan_installed"]:
        reason = (
            "pg_hint_plan is installed but hint_plan.hints is missing; drop and "
            "recreate the extension to restore it."
        )
    else:
        reason = "pg_hint_plan is not available on this cluster, so plans cannot be pinned."
    return {
        "available": prereq["hint_table"],
        "installable": installable,
        "reason": reason,
        "pinned": row is not None,
        "hints": (row or {}).get("hints"),
        "prereq": prereq,
    }

_console = Console()


def _web_dir() -> Path:
    return Path(__file__).resolve().parent / "web"


class YbtopHTTPRequestHandler(BaseHTTPRequestHandler):
    data_dir: Path = Path(".")
    # Plan pinning writes to the cluster, so it stays off unless the operator asked
    # for it (`ybtop watch --allow-plan-pinning`) AND we have a DSN to write through.
    # `ybtop serve` over a directory of files has neither and reports pinning
    # unavailable rather than half-working.
    seed_dsn: "str | None" = None
    allow_plan_pinning: bool = False
    # EXPLAIN ANALYZE executes statements, so it has its own opt-in
    # (`ybtop watch --allow-explain-analyze`), independent of pinning.
    allow_explain_analyze: bool = False
    explain_runs: X.ExplainRuns = X.ExplainRuns()
    # DNS-rebinding guard; see host_allowed. The flag differs between watch and serve.
    bind_host: str = "127.0.0.1"
    allowed_hosts: "frozenset[str]" = frozenset()
    allowed_host_flag: str = "--serve-allowed-host"

    def _host_ok(self) -> bool:
        cls = type(self)
        if host_allowed(self.headers.get("Host"), cls.allowed_hosts, cls.bind_host):
            return True
        name = _host_name(self.headers.get("Host") or "")
        msg = (
            "Host %r is not one this viewer is reached by, so the request is refused "
            "(DNS-rebinding guard). If this is how you reach it, start ybtop with "
            "%s %s." % (name, cls.allowed_host_flag, name)
        )
        if urlparse(self.path).path.startswith("/api/"):
            self._send_json({"error": msg}, 403)
        else:
            self._send_bytes(msg.encode("utf-8"), "text/plain; charset=utf-8", 403)
        return False

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _send_bytes(self, data: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, payload: dict[str, object], status: int = 200) -> None:
        self._send_bytes(json.dumps(payload).encode("utf-8"), "application/json", status)

    def _collection_state(self) -> dict[str, object]:
        root = type(self).data_dir
        state: dict[str, object] = dict(read_control(root))
        # The viewer needs to know whether enabling can even be persisted: an
        # archive directory served read-only will reject the write, and an
        # unguarded viewer refuses it (_collection_switch_problem).
        state["writable"] = os.access(str(root), os.W_OK) and self._collection_switch_problem() is None
        state["locked"] = read_control_locks(root).get("query_plans")
        return state

    def _collection_switch_problem(self) -> "str | None":
        """Why the page may not switch plan collection, or None.

        A viewer bound to a non-loopback address with no --serve-allowed-host names
        accepts any Host, so a DNS-rebinding page is same-origin with it.
        """
        cls = type(self)
        if _is_loopback(_host_name(cls.bind_host)) or cls.allowed_hosts:
            return None
        return (
            "Switching plan collection from this page is refused: the viewer is bound to %s "
            "without %s NAME, so it cannot tell its own pages from a DNS-rebinding one. List "
            "the host names it is reached by, or bind it to 127.0.0.1." % (cls.bind_host, cls.allowed_host_flag)
        )

    def _pinning_unavailable_reason(self) -> "str | None":
        """Why pinning cannot be offered, or None when it can."""
        cls = type(self)
        if not cls.allow_plan_pinning:
            return (
                "Pinning is off for this collector. It rewrites production query plans "
                "and this page has no login, so it is switched on where the collector "
                "runs -- start ybtop watch with --allow-plan-pinning -- not from here."
            )
        if not cls.seed_dsn:
            return (
                "This viewer has no database connection (ybtop serve reads files only); "
                "pinning is available from the viewer that ybtop watch starts."
            )
        return None

    def _explain_unavailable_reason(self) -> "str | None":
        """Why EXPLAIN ANALYZE cannot be offered, or None when it can."""
        cls = type(self)
        if not cls.allow_explain_analyze:
            return (
                "EXPLAIN ANALYZE is off for this collector. It executes statements on the "
                "cluster and this page has no login, so it is switched on where the "
                "collector runs -- start ybtop watch with --allow-explain-analyze -- not "
                "from here."
            )
        if not cls.seed_dsn:
            return (
                "This viewer has no database connection (ybtop serve reads files only); "
                "EXPLAIN ANALYZE is available from the viewer that ybtop watch starts."
            )
        return None

    def _handle_explain(self, body: dict) -> None:
        """Start one EXPLAIN ANALYZE of a recorded execution; the run continues in the background.

        The request names a recorded QPM row (queryid, planid, plan_ref, dbid, node)
        in one of our snapshots. The statement, its parameter values, the database,
        the node and the role all come from that snapshot; only the options and the
        timeout come from the request.
        """
        reason = self._explain_unavailable_reason()
        if reason is not None:
            self._send_json({"error": reason}, 403)
            return
        vals = {
            k: str(body.get(k) or "").strip()
            for k in ("queryid", "planid", "plan_ref", "dbid", "node", "file", "userid")
        }
        if not vals["userid"]:
            self._send_json({"error": "this page is older than the collector; reload it"}, 400)
            return
        for k in ("queryid", "planid", "dbid", "userid"):
            if not _INT64_TEXT.match(vals[k]):
                self._send_json({"error": "%s must be an integer" % k}, 400)
                return
        if not _PLAN_REF_TEXT.match(vals["plan_ref"]):
            self._send_json({"error": "plan_ref is malformed"}, 400)
            return
        for k in ("dist", "debug"):
            if k in body and not isinstance(body[k], bool):
                self._send_json({"error": "%s must be true or false" % k}, 400)
                return
        doc = self._load_snapshot(vals["file"])
        if doc is None:
            self._send_json(
                {
                    "error": "snapshot %s is no longer on disk -- ybtop keeps a few hours of "
                    "snapshots. Open the latest snapshot and try again." % (vals["file"] or "(none)")
                },
                404,
            )
            return
        # The node must be one this snapshot recorded -- never a host from the request.
        if vals["node"] not in ((doc.get("yb_pg_stat_plans") or {}).get("per_node") or {}):
            self._send_json({"error": "that node is not in this snapshot"}, 404)
            return
        target, why = X.resolve_target(
            doc,
            queryid=vals["queryid"],
            planid=vals["planid"],
            plan_ref=vals["plan_ref"],
            dbid=vals["dbid"],
            node=vals["node"],
            userid=vals["userid"],
        )
        if target is None:
            self._send_json({"error": why}, 409)
            return
        target["snapshot_file"] = vals["file"]
        host, port = X.split_node(vals["node"])
        dsn = dsn_for_host(dsn_for_database(str(type(self).seed_dsn), target["datname"]), host, port)
        options = X.explain_options(body.get("dist") is True, body.get("debug") is True)
        timeout_s = X.clamp_timeout(body.get("timeout_s"))

        def work(run: dict, cancel_ready: object, may_analyze: object) -> dict:
            return X.run_explain(
                dsn, target, options, timeout_s,
                on_cancel_ready=cancel_ready, stop=run["_stop"], may_analyze=may_analyze,
            )

        run, busy = type(self).explain_runs.start(target, options, timeout_s, work)
        if run is None:
            self._send_json(
                {
                    "error": "Another EXPLAIN ANALYZE is still running (query_id %s); "
                    "one runs at a time." % ((busy or {}).get("queryid") or "?"),
                    "active": busy,
                },
                409,
            )
            return
        self._send_json({"run": run}, 202)

    def _pin_state(self, target: dict, conn: object = None) -> dict[str, object]:
        """Prerequisites plus whether a hint is pinned, in the target plan's database.

        Pass an open connection to reuse it: each connection here is a TLS
        handshake to a remote node, so a pin that opened one to act and another to
        report back took twice as long as it needed to.
        """
        reason = self._pinning_unavailable_reason()
        if reason is not None:
            return {"available": False, "reason": reason, "pinned": False}
        queryid = str(target["queryid"])
        try:
            if conn is not None:
                st = Q.qpm_status(conn)
                row = Q.hint_table_row(conn, queryid) if st.get("hint_table_present") else None
            else:
                with connect(str(target["dsn"])) as own:
                    st = Q.qpm_status(own)
                    row = Q.hint_table_row(own, queryid) if st.get("hint_table_present") else None
        except Exception as exc:  # noqa: BLE001 - surface as a UI message, never a 500
            if _database_gone(exc):
                return {"available": False, "reason": "this plan's database (%s) no longer exists; there is nothing to pin into." % target["datname"], "pinned": False}
            return {"available": False, "reason": "database error: %s" % exc, "pinned": False}
        state = pin_state_from_status(st, row)
        state["database"] = target["datname"]
        return state

    # The last snapshot parsed for the API, by (path, mtime, size): the page asks
    # about one snapshot's plans many times, and a parse is the cost of each ask.
    _snapshot_cache: "tuple[tuple[str, int, int], dict] | None" = None
    _snapshot_cache_lock = threading.Lock()

    def _load_snapshot(self, file_name: str) -> "dict | None":
        """Parse one of our own snapshot files, or None. Never sends a response.

        Resolved here rather than via _resolve_static, which sends a 404 as a side
        effect -- that would commit a response before the caller picks its own.
        The document may be shared with other requests: read it, never change it.
        """
        if not file_name or "/" in file_name or ".." in file_name:
            return None
        base = type(self).data_dir.resolve()
        path = (base / file_name).resolve()
        try:
            path.relative_to(base)
        except ValueError:
            return None
        if not path.is_file():
            return None
        cls = YbtopHTTPRequestHandler
        try:
            st = path.stat()
            key = (str(path), st.st_mtime_ns, st.st_size)
            with cls._snapshot_cache_lock:
                if cls._snapshot_cache is not None and cls._snapshot_cache[0] == key:
                    return cls._snapshot_cache[1]
            raw = path.read_bytes()
            if file_name.endswith(".gz"):
                import gzip

                raw = gzip.decompress(raw)
            doc = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        if isinstance(doc, dict):
            with cls._snapshot_cache_lock:
                cls._snapshot_cache = (key, doc)
        return doc

    def _resolve_plan_target(self, fields: dict) -> "tuple[dict | None, str | None, int]":
        """Validate a pin-row request and resolve the recorded plan it names.

        Returns (target, error, http_status). A caller can only NAME a recorded
        (queryid, planid, plan_ref, database) from one of our snapshots; which
        database is touched and what hint text is written both come from that
        snapshot, never from the request.
        """
        vals = {k: str(fields.get(k) or "").strip() for k in ("queryid", "planid", "plan_ref", "dbid", "file")}
        for k in ("queryid", "planid", "dbid"):
            if not vals[k] or len(vals[k]) > 24 or not _INT64_TEXT.match(vals[k]):
                return None, "%s must be an integer" % k, 400
        if not _PLAN_REF_TEXT.match(vals["plan_ref"]):
            return None, "plan_ref is malformed", 400
        doc = self._load_snapshot(vals["file"])
        if doc is None:
            return None, (
                "snapshot %s is no longer on disk -- ybtop keeps a few hours of snapshots. "
                "Open the latest snapshot and try again." % (vals["file"] or "(none)")
            ), 404
        found, hints = find_recorded_plan(
            doc, vals["queryid"], vals["planid"], vals["plan_ref"], vals["dbid"]
        )
        if not found:
            return None, "that plan is not recorded in %s for this database" % vals["file"], 404
        datname = ((doc.get("yb_pg_stat_plans") or {}).get("databases") or {}).get(vals["dbid"])
        if datname is None:
            # Only a snapshot built while the database map could not be read has no
            # name here; ask the cluster. A database dropped after the snapshot is
            # still in its map -- that shows up when connecting (_database_gone).
            try:
                with connect(str(type(self).seed_dsn)) as conn:
                    rows = Q.database_name(conn, vals["dbid"])
            except Exception as exc:  # noqa: BLE001
                return None, "database error: %s" % exc, 500
            datname = rows
        if not datname:
            return None, (
                "this plan was recorded in a database that no longer exists "
                "(oid %s); there is nothing to pin into." % vals["dbid"]
            ), 409
        target = {
            "queryid": vals["queryid"],
            "dbid": vals["dbid"],
            "datname": str(datname),
            "dsn": dsn_for_database(str(type(self).seed_dsn), str(datname)),
            "hints": hints,
        }
        return target, None, 200

    def _resolve_static(self, base: Path, rel: str) -> Path | None:
        rel = rel.lstrip("/")
        if ".." in rel.split("/"):
            self.send_error(403)
            return None
        path = (base / rel).resolve()
        try:
            path.relative_to(base.resolve())
        except ValueError:
            self.send_error(403)
            return None
        if not path.is_file():
            self.send_error(404)
            return None
        return path

    def _send_file_from(self, base: Path, rel: str) -> None:
        path = self._resolve_static(base, rel)
        if path is None:
            return
        ctype, _ = mimetypes.guess_type(str(path))
        if not ctype:
            ctype = "application/octet-stream"
        self._send_bytes(path.read_bytes(), ctype)

    def _send_file_compressed_json(self, base: Path, rel: str) -> None:
        path = self._resolve_static(base, rel)
        if path is None:
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_head_for(self, base: Path, rel: str) -> None:
        path = self._resolve_static(base, rel)
        if path is None:
            return
        ctype, _ = mimetypes.guess_type(str(path))
        if not ctype:
            ctype = "application/octet-stream"
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_HEAD(self) -> None:  # noqa: N802
        if not self._host_ok():
            return
        parsed = urlparse(self.path)
        path = unquote(parsed.path) or "/"
        if path.startswith("/static/"):
            self._send_head_for(_web_dir(), path[len("/static/") :])
            return
        if path.endswith(".json") or path.endswith(".json.gz"):
            name = path.lstrip("/")
            if ".." in name or "/" in name.strip("/"):
                self.send_error(403)
                return
            self._send_head_for(type(self).data_dir, name)
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        """State-changing endpoints: collection toggle, pinning, EXPLAIN ANALYZE.

        POST-only, same-origin JSON only (see cross_site_problem), from a Host this
        viewer is reached by (see host_allowed).
        """
        if not self._host_ok():
            return
        parsed = urlparse(self.path)
        route = unquote(parsed.path) or "/"
        if route not in (
            "/api/control",
            "/api/pin",
            "/api/unpin",
            "/api/hinting",
            "/api/hint-plan/install",
            "/api/explain",
            "/api/explain/cancel",
        ):
            self.send_error(404)
            return
        problem = cross_site_problem(self.headers, type(self).allowed_hosts)
        if problem is not None:
            self._send_json({"error": problem}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0 or length > 4096:
            self._send_json({"error": "expected a small JSON body"}, 400)
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json({"error": "body is not valid JSON"}, 400)
            return
        if not isinstance(body, dict):
            self._send_json({"error": "body must be a JSON object"}, 400)
            return
        if route in ("/api/pin", "/api/unpin"):
            self._handle_pin(route, body)
            return
        if route == "/api/hinting":
            self._handle_hinting(body)
            return
        if route == "/api/hint-plan/install":
            self._handle_install_hint_plan(body)
            return
        if route == "/api/explain":
            self._handle_explain(body)
            return
        if route == "/api/explain/cancel":
            dbid, queryid = str(body.get("dbid") or ""), str(body.get("queryid") or "")
            if not _INT64_TEXT.match(dbid) or not _INT64_TEXT.match(queryid):
                self._send_json({"error": "expected dbid and queryid"}, 400)
                return
            self._send_json({"run": type(self).explain_runs.cancel(dbid, queryid)})
            return
        problem = self._collection_switch_problem()
        if problem is not None:
            self._send_json({"error": problem}, 403)
            return
        unknown = sorted(set(body) - set(CONTROL_DEFAULTS))
        if unknown:
            self._send_json({"error": "unknown toggle(s): " + ", ".join(unknown)}, 400)
            return
        try:
            write_control(type(self).data_dir, body)
        except ControlLocked as exc:
            self._send_json(
                {"error": "Plan collection is fixed off for this data directory by the "
                 "collector's %s." % exc.flag}, 409
            )
            return
        except OSError as exc:
            self._send_json(
                {"error": "data directory is not writable: %s" % exc}, 409
            )
            return
        self._send_json(self._collection_state())

    def _gated_target(self, fields: dict) -> "dict | None":
        """Gate + resolve, sending the error response itself when either fails."""
        reason = self._pinning_unavailable_reason()
        if reason is not None:
            self._send_json({"error": reason}, 403)
            return None
        target, err, status = self._resolve_plan_target(fields)
        if target is None:
            self._send_json({"error": err}, status)
            return None
        return target

    def _handle_pin(self, route: str, body: dict) -> None:
        target = self._gated_target(body)
        if target is None:
            return
        body_text = None
        if route == "/api/pin":
            body_text = Q.strip_hint_wrapper(target.get("hints") or "")
            if not body_text:
                self._send_json({"error": "no hints recorded for that plan"}, 409)
                return
        try:
            with connect(str(target["dsn"])) as conn:
                # Prerequisites first, so a missing pg_hint_plan surfaces as the
                # install offer rather than raw SQL from an UndefinedTable.
                state = self._pin_state(target, conn)
                if not state.get("available"):
                    self._send_json({"error": state.get("reason") or "pinning unavailable"}, 409)
                    return
                if route == "/api/unpin":
                    removed = Q.unpin_hint(conn, target["queryid"])
                    state = self._pin_state(target, conn)
                    state["removed"] = removed
                else:
                    Q.pin_hint(conn, target["queryid"], body_text)
                    state = self._pin_state(target, conn)
        except Exception as exc:  # noqa: BLE001
            if _database_gone(exc):
                self._send_json({"error": "this plan's database (%s) no longer exists; there is nothing to pin into." % target["datname"]}, 409)
                return
            verb = "remove hint" if route == "/api/unpin" else "pin plan"
            self._send_json({"error": "could not %s: %s" % (verb, exc)}, 500)
            return
        self._send_json(state)

    def _handle_install_hint_plan(self, body: dict) -> None:
        """CREATE EXTENSION pg_hint_plan in the target plan's database.

        Same gate as pinning: a collector started with --allow-plan-pinning may
        already ALTER DATABASE, and this only creates an empty hints table.
        """
        target = self._gated_target(body)
        if target is None:
            return
        try:
            with connect(str(target["dsn"])) as conn:
                st = Q.qpm_status(conn)
                if not st.get("hint_plan_available"):
                    self._send_json({"error": "pg_hint_plan is not available on this cluster"}, 409)
                    return
                if not st.get("hint_plan_installed"):
                    Q.install_hint_plan(conn)
                    # The collector skips table-less databases for a while; this one
                    # has a table now, so its pins should show from the next checkpoint.
                    pinscan.forget_database(target["datname"])
                state = self._pin_state(target, conn)
        except Exception as exc:  # noqa: BLE001
            if _database_gone(exc):
                self._send_json({"error": "this plan's database (%s) no longer exists; there is nothing to pin into." % target["datname"]}, 409)
                return
            self._send_json({"error": "could not install pg_hint_plan: %s" % exc}, 500)
            return
        self._send_json(state)

    def _handle_hinting(self, body: dict) -> None:
        """Turn hint-table lookup on or off for the target plan's database.

        Separate from the collection toggle on purpose: collection only reads, while
        this changes plan selection for every new session in that database.
        """
        if "enable" not in body or not isinstance(body["enable"], bool):
            self._send_json({"error": "expected {\"enable\": true|false} and a plan"}, 400)
            return
        target = self._gated_target(body)
        if target is None:
            return
        try:
            with connect(str(target["dsn"])) as conn:
                result = Q.set_hint_table_gucs(conn, bool(body["enable"]))
        except Exception as exc:  # noqa: BLE001
            if _database_gone(exc):
                self._send_json({"error": "this plan's database (%s) no longer exists; there is nothing to pin into." % target["datname"]}, 409)
                return
            self._send_json({"error": "could not change hint settings: %s" % exc}, 500)
            return
        # Report from a NEW session: ALTER DATABASE ... SET only reaches sessions
        # started afterwards, so the connection that ran it still reads the old values.
        state = self._pin_state(target)
        self._send_json({"applied": result, "prereq": state.get("prereq"), "database": target["datname"]})

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            return
        parsed = urlparse(self.path)
        path = unquote(parsed.path) or "/"

        if path == "/api/control":
            self._send_json(self._collection_state())
            return

        if path == "/api/explain":
            qs = parse_qs(parsed.query or "")
            dbid = (qs.get("dbid") or [""])[0]
            # A canonical family can span hundreds of query_ids; only the first 200
            # are read, so a client lists the one it would replay first.
            queryids = [q for q in ((qs.get("queryids") or [""])[0]).split(",") if q][:200]
            if (dbid and not _INT64_TEXT.match(dbid)) or not all(_INT64_TEXT.match(q) for q in queryids):
                self._send_json({"error": "dbid and queryids must be integers"}, 400)
                return
            reason = self._explain_unavailable_reason()
            runs = type(self).explain_runs
            self._send_json(
                {
                    "available": reason is None,
                    "reason": reason,
                    "run": runs.latest(dbid, queryids) if dbid and queryids else None,
                    "active": runs.active(),
                }
            )
            return

        if path == "/api/pin":
            qs = parse_qs(parsed.query or "")
            fields = {k: (qs.get(k) or [""])[0] for k in ("queryid", "planid", "plan_ref", "dbid", "file")}
            reason = self._pinning_unavailable_reason()
            if reason is not None:
                self._send_json({"available": False, "reason": reason, "pinned": False})
                return
            target, err, status = self._resolve_plan_target(fields)
            if target is None:
                self._send_json({"available": False, "reason": err, "pinned": False}, status)
                return
            self._send_json(self._pin_state(target))
            return

        if path in ("/", "/index.html"):
            idx = _web_dir() / "index.html"
            if not idx.is_file():
                self._send_bytes(b"ybtop web assets missing; reinstall package.", "text/plain", 500)
                return
            html = idx.read_text(encoding="utf-8")
            if "__YBTOP_VERSION__" in html:
                html = html.replace("__YBTOP_VERSION__", f"v{_ybtop_version}")
            self._send_bytes(html.encode("utf-8"), "text/html; charset=utf-8")
            return

        if path.startswith("/static/"):
            rel = path[len("/static/") :]
            self._send_file_from(_web_dir(), rel)
            return

        if path.endswith(".json") or path.endswith(".json.gz"):
            name = path.lstrip("/")
            if ".." in name or "/" in name.strip("/"):
                self.send_error(403)
                return
            if path.endswith(".json.gz"):
                self._send_file_compressed_json(type(self).data_dir, name)
            else:
                self._send_file_from(type(self).data_dir, name)
            return

        self.send_error(404)


def _allowed(hosts: "list[str] | None") -> "frozenset[str]":
    return frozenset(_host_name(h) for h in (hosts or []) if str(h).strip())


def run_serve(*, data_dir: str, host: str, port: int, allowed_hosts: "list[str] | None" = None) -> None:
    root = Path(data_dir).resolve()
    if not root.is_dir():
        raise SystemExit(f"Data directory does not exist or is not a directory: {root}")

    YbtopHTTPRequestHandler.data_dir = root
    YbtopHTTPRequestHandler.bind_host = host
    YbtopHTTPRequestHandler.allowed_hosts = _allowed(allowed_hosts)
    YbtopHTTPRequestHandler.allowed_host_flag = "--allowed-host"

    httpd = ThreadingHTTPServer((host, port), YbtopHTTPRequestHandler)
    url = f"http://{host}:{port}/"
    _console.print(
        f"ybtop serve: [link={url}]{url}[/link]  (data dir: {root})"
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")


def start_serve_background(
    *,
    data_dir: str,
    host: str,
    port: int,
    seed_dsn: str | None = None,
    allow_plan_pinning: bool = False,
    allow_explain_analyze: bool = False,
    allowed_hosts: "list[str] | None" = None,
) -> bool:
    """
    Start the static viewer in a daemon thread (used by ``ybtop watch``).

    The listen socket is bound in the **calling** thread so port / host errors are
    visible before the watch UI runs. Returns ``True`` on success, ``False`` if
    the data directory or HTTP server could not be started (error on stderr).
    """

    def serve_loop(httpd: ThreadingHTTPServer) -> None:
        try:
            httpd.serve_forever()
        except OSError:
            pass

    root = Path(data_dir).resolve()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        msg = f"ybtop: could not create data directory {root}: {exc}"
        print(msg, file=sys.stderr, flush=True)
        return False
    YbtopHTTPRequestHandler.data_dir = root
    # Only the watch-embedded viewer gets a DSN, and only when explicitly allowed.
    YbtopHTTPRequestHandler.seed_dsn = (
        seed_dsn if (allow_plan_pinning or allow_explain_analyze) else None
    )
    YbtopHTTPRequestHandler.allow_plan_pinning = bool(allow_plan_pinning)
    YbtopHTTPRequestHandler.allow_explain_analyze = bool(allow_explain_analyze)
    YbtopHTTPRequestHandler.bind_host = host
    YbtopHTTPRequestHandler.allowed_hosts = _allowed(allowed_hosts)
    try:
        httpd = ThreadingHTTPServer((host, port), YbtopHTTPRequestHandler)
    except OSError as exc:
        msg = f"ybtop: could not start viewer on http://{host}:{port}/ ({exc})"
        print(msg, file=sys.stderr, flush=True)
        return False

    threading.Thread(
        target=serve_loop,
        name="ybtop-viewer-http",
        args=(httpd,),
        daemon=True,
    ).start()
    return True
