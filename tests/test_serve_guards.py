"""The viewer's request guards and snapshot loading (src/ybtop/serve.py).

The viewer has no login, and its POST endpoints change what the collector
records and which plans the cluster uses. What stops another page from driving
them:
  - every request must carry a Host this viewer is reached by (DNS rebinding)
  - a POST must be same-origin JSON: a plain HTML form cannot send one

Run:  python -m unittest discover -s tests
"""

import gzip
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from ybtop import serve


class HostGuardTest(unittest.TestCase):
    def test_rules(self):
        none = frozenset()
        some = frozenset({"ybtop.example"})
        cases = [
            # (Host header, allowed, bind, expected)
            ("localhost:8765", none, "127.0.0.1", True),
            ("127.0.0.1:8765", none, "127.0.0.1", True),
            ("[::1]:8765", none, "::1", True),
            ("evil.example:8765", none, "127.0.0.1", False),  # rebinding onto loopback
            ("evil.example:8765", none, "0.0.0.0", True),  # unknowable names: opt-in only
            ("evil.example:8765", some, "0.0.0.0", False),
            ("YBTOP.Example:8765", some, "0.0.0.0", True),
            ("localhost:8766", some, "0.0.0.0", True),  # an ssh tunnel still works
            (None, none, "127.0.0.1", True),
        ]
        for header, allowed, bind, expect in cases:
            self.assertEqual(serve.host_allowed(header, allowed, bind), expect, (header, allowed, bind))

    def test_origin_behind_a_proxy(self):
        h = {"Content-Type": "application/json", "Host": "127.0.0.1:8765", "Origin": "https://ybtop.example"}
        self.assertIsNotNone(serve.cross_site_problem(h))
        self.assertIsNone(serve.cross_site_problem(h, frozenset({"ybtop.example"})))
        self.assertIsNone(serve.cross_site_problem({"Content-Type": "application/json; charset=utf-8"}))


class GuardedEndpointTest(unittest.TestCase):
    """Through a real server, on the collection toggle: no database needed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        H = serve.YbtopHTTPRequestHandler
        self.saved = (H.data_dir, H.bind_host, H.allowed_hosts)
        H.data_dir = Path(self.tmp.name)
        H.bind_host, H.allowed_hosts = "127.0.0.1", frozenset()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        H = serve.YbtopHTTPRequestHandler
        H.data_dir, H.bind_host, H.allowed_hosts = self.saved
        self.tmp.cleanup()

    def request(self, route, body=None, headers=None):
        h = {"Content-Type": "application/json"} if body is not None else {}
        h.update(headers or {})
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + route, data=data, headers=h, method="POST" if data else "GET")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def toggle(self):
        return json.loads(self.request("/api/control")[1])["query_plans"]

    def test_cross_site_posts_are_refused(self):
        host = self.base.split("//", 1)[1]
        for headers in (
            {"Content-Type": "text/plain"},  # a plain HTML form: no CORS preflight
            {"Sec-Fetch-Site": "cross-site"},
            {"Sec-Fetch-Site": "same-site"},
            {"Origin": "http://evil.example"},
            {"Origin": "null"},
        ):
            status, _ = self.request("/api/control", {"query_plans": True}, headers)
            self.assertEqual(status, 403, headers)
            self.assertFalse(self.toggle(), headers)
        status, _ = self.request(
            "/api/control", {"query_plans": True}, {"Origin": "http://" + host, "Sec-Fetch-Site": "same-origin"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(self.toggle())

    def test_a_rebound_host_is_refused(self):
        rebound = {"Host": "evil.example:8765"}
        status, text = self.request("/api/control", headers=rebound)
        self.assertEqual(status, 403)
        self.assertIn("--serve-allowed-host evil.example", json.loads(text)["error"])
        self.assertEqual(self.request("/", headers=rebound)[0], 403)
        self.assertEqual(self.request("/api/control", {"query_plans": True}, rebound)[0], 403)
        self.assertFalse(self.toggle())  # 127.0.0.1 itself is fine


def _qpm_doc():
    row = {"queryid": 7, "planid": 1, "plan_ref": "aa", "dbid": 16640, "userid": 16384, "calls": 3}
    return {
        "yb_pg_stat_plans": {
            "databases": {"16640": "app"},
            "plans": {"aa": {"plan": "{}", "hints": "/*+ SeqScan(t) */"}},
            "per_node": {"n1:5433": [row]},
        }
    }


class SnapshotLoadTest(unittest.TestCase):
    """Pinning names a snapshot file; with --compress-snapshots that file is .json.gz."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        H = serve.YbtopHTTPRequestHandler
        self.saved = (H.data_dir, H.seed_dsn)
        H.data_dir = Path(self.tmp.name)
        H.seed_dsn = "host=seed port=5433 dbname=yugabyte user=yugabyte"
        raw = json.dumps(_qpm_doc()).encode()
        with open(os.path.join(self.tmp.name, "ybtop.out.20260925_130000.json"), "wb") as f:
            f.write(raw)
        with open(os.path.join(self.tmp.name, "ybtop.out.20260925_130100.json.gz"), "wb") as f:
            f.write(gzip.compress(raw))
        # A handler with no request behind it: these methods only read the class state.
        self.h = serve.YbtopHTTPRequestHandler.__new__(serve.YbtopHTTPRequestHandler)

    def tearDown(self):
        H = serve.YbtopHTTPRequestHandler
        H.data_dir, H.seed_dsn = self.saved
        self.tmp.cleanup()

    def test_plain_and_compressed_snapshots_load_alike(self):
        plain = self.h._load_snapshot("ybtop.out.20260925_130000.json")
        packed = self.h._load_snapshot("ybtop.out.20260925_130100.json.gz")
        self.assertEqual(plain, _qpm_doc())
        self.assertEqual(packed, plain)

    def test_only_files_in_the_data_directory(self):
        for name in ("", "../x.json", "sub/x.json", "missing.json", "ybtop.out.20260925_130000.json/.."):
            self.assertIsNone(self.h._load_snapshot(name), name)

    def test_a_pin_resolves_from_a_compressed_snapshot(self):
        fields = {"queryid": "7", "planid": "1", "plan_ref": "aa", "dbid": "16640",
                  "file": "ybtop.out.20260925_130100.json.gz"}
        target, error, status = self.h._resolve_plan_target(fields)
        self.assertEqual((error, status), (None, 200))
        self.assertEqual((target["datname"], target["hints"]), ("app", "/*+ SeqScan(t) */"))
        self.assertIn("dbname=app", target["dsn"])


if __name__ == "__main__":
    unittest.main()
