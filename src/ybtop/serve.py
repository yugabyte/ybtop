from __future__ import annotations

import mimetypes
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from rich.console import Console

from ybtop import __version__ as _ybtop_version

_console = Console()


def _web_dir() -> Path:
    return Path(__file__).resolve().parent / "web"


class YbtopHTTPRequestHandler(BaseHTTPRequestHandler):
    data_dir: Path = Path(".")

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _send_bytes(self, data: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

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
        parsed = urlparse(self.path)
        path = unquote(parsed.path) or "/"
        if path.startswith("/static/"):
            self._send_head_for(_web_dir(), path[len("/static/") :])
            return
        if path.endswith(".json"):
            name = path.lstrip("/")
            if ".." in name or "/" in name.strip("/"):
                self.send_error(403)
                return
            self._send_head_for(type(self).data_dir, name)
            return
        self.send_error(404)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path) or "/"

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

        if path.endswith(".json"):
            name = path.lstrip("/")
            if ".." in name or "/" in name.strip("/"):
                self.send_error(403)
                return
            self._send_file_from(type(self).data_dir, name)
            return

        self.send_error(404)


def run_serve(*, data_dir: str, host: str, port: int) -> None:
    root = Path(data_dir).resolve()
    if not root.is_dir():
        raise SystemExit(f"Data directory does not exist or is not a directory: {root}")

    YbtopHTTPRequestHandler.data_dir = root

    httpd = ThreadingHTTPServer((host, port), YbtopHTTPRequestHandler)
    url = f"http://{host}:{port}/"
    _console.print(
        f"ybtop serve: [link={url}]{url}[/link]  (data dir: {root})"
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")


def start_serve_background(*, data_dir: str, host: str, port: int) -> bool:
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
