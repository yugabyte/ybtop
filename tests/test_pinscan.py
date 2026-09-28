"""Pinned-hint scan across databases (src/ybtop/pinscan.py).

Each database costs a TLS + auth handshake (~350 ms measured), so the scan must
not re-contact databases already known to have no hint table -- except right
after an Install, which calls forget_database(). Errors are never cached.

Run:  python -m unittest discover -s tests
"""

import contextlib
import unittest

import psycopg.conninfo as ci

from ybtop import pinscan

SEED = ci.make_conninfo(host="10.0.0.1", port="5433", user="yugabyte", dbname="yugabyte")
OIDS = {"app": "16640", "empty": "13665", "broken": "5"}


class PinScanTest(unittest.TestCase):
    def setUp(self):
        pinscan.reset()
        self.contacted = []
        self.errors = []
        self._orig_connect = pinscan.connect
        self._orig_pinned = pinscan.Q.pinned_queryids

        @contextlib.contextmanager
        def fake_connect(dsn):
            name = ci.conninfo_to_dict(dsn)["dbname"]
            self.contacted.append(name)
            if name == "broken":
                raise RuntimeError("connection refused")
            yield name

        # pinned_queryids gets the "connection", which the fake makes the db name.
        pinscan.connect = fake_connect
        pinscan.Q.pinned_queryids = lambda conn: {"app": ["-7", "42"], "empty": None}[conn]

    def tearDown(self):
        pinscan.connect = self._orig_connect
        pinscan.Q.pinned_queryids = self._orig_pinned
        pinscan.reset()

    def scan(self, now):
        self.contacted = []
        return pinscan.scan_pinned(
            SEED, ["app", "empty", "broken"], OIDS, now=now,
            on_error=lambda name, exc: self.errors.append(name),
        )

    def test_first_scan_reads_every_database(self):
        out = self.scan(now=1000.0)
        self.assertEqual(out, {"16640": ["-7", "42"]})
        self.assertEqual(sorted(self.contacted), ["app", "broken", "empty"])
        self.assertEqual(self.errors, ["broken"])

    def test_tableless_database_is_skipped_until_recheck(self):
        self.scan(now=1000.0)
        self.scan(now=1001.0)
        self.assertNotIn("empty", self.contacted)
        self.assertIn("app", self.contacted)  # has a table: pins can change, always read
        self.scan(now=1000.0 + pinscan.NO_TABLE_RECHECK_SEC + 1)
        self.assertIn("empty", self.contacted)

    def test_forget_database_forces_a_recheck(self):
        # What the Install button does, so pins show from the next checkpoint.
        self.scan(now=1000.0)
        pinscan.forget_database("empty")
        self.scan(now=1001.0)
        self.assertIn("empty", self.contacted)

    def test_errors_are_retried_not_cached(self):
        self.scan(now=1000.0)
        self.scan(now=1001.0)
        self.assertIn("broken", self.contacted)

    def test_unknown_names_are_ignored(self):
        self.contacted = []
        out = pinscan.scan_pinned(SEED, ["not_a_db"], OIDS, now=1.0)
        self.assertEqual(out, {})
        self.assertEqual(self.contacted, [])


if __name__ == "__main__":
    unittest.main()
