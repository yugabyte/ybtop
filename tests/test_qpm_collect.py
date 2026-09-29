"""QPM rows per node (src/ybtop/queries.py, snapshot_write._collect_one_node).

A node's QPM cache can hold more plans than the per-node row cap. What the cap
keeps first must be the plans of the statements the snapshot is scoped to -- the
node's pg_stat_statements top N -- or a heavy statement's older plans are cut
before scoping ever sees them.

Run:  python -m unittest discover -s tests
"""

import contextlib
import unittest
from datetime import datetime, timezone

from ybtop import queries as Q
from ybtop import snapshot_write
from ybtop.capabilities import Capabilities
from ybtop.topology import YsqlNode


class _Cursor:
    def __init__(self, seen):
        self.seen = seen

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        self.seen["sql"], self.seen["params"] = sql, params

    def fetchall(self):
        return []


class _Conn:
    def __init__(self, seen):
        self.seen = seen

    def cursor(self):
        return _Cursor(self.seen)


class QpmRowsQueryTest(unittest.TestCase):
    def test_the_nodes_top_statements_are_kept_first(self):
        seen = {}
        Q.yb_pg_stat_plans_rows(_Conn(seen), 2000, ["-5", "7"])
        self.assertEqual(seen["params"], {"limit": 2000, "top": ["-5", "7"]})
        self.assertIn(
            "ORDER BY (p.queryid = ANY(%(top)s::bigint[])) DESC, p.last_used DESC LIMIT %(limit)s",
            " ".join(seen["sql"].split()),
        )
        Q.yb_pg_stat_plans_rows(_Conn(seen), 10)
        self.assertEqual(seen["params"]["top"], [])
        # Recorded parameter values are application data; nothing here uses them.
        self.assertNotIn("max_exec_time_params", seen["sql"])

    def test_collect_passes_the_nodes_top_queryids(self):
        passed = {}
        saved = (snapshot_write.connect, Q.pg_stat_statements_top, Q.ycql_stat_statements_top,
                 Q.ash_aggregated, Q.yb_local_tablets_rows, Q.yb_pg_stat_plans_rows)

        @contextlib.contextmanager
        def fake_connect(dsn):
            yield object()

        def fake_plans(conn, limit, top_queryids=None):
            passed["args"] = (limit, top_queryids)
            return []

        snapshot_write.connect = fake_connect
        Q.pg_stat_statements_top = lambda conn, n, caps, include_latency_histogram=False: [
            {"queryid": "11", "query": "a"}, {"queryid": "-22", "query": "b"}, {"queryid": None, "query": "c"}]
        Q.ycql_stat_statements_top = lambda conn, n: []
        Q.ash_aggregated = lambda conn, a, b, caps, outer_limit=None: []
        Q.yb_local_tablets_rows = lambda conn: []
        Q.yb_pg_stat_plans_rows = fake_plans
        try:
            now = datetime.now(timezone.utc)
            snapshot_write._collect_one_node(
                seed_dsn="host=seed port=5433 dbname=yugabyte user=yugabyte",
                node=YsqlNode(host="10.0.0.5", port=5433, server_uuid="u"),
                node_count=1,
                caps=Capabilities(pg_stat_use_exec_time=True, yb_ash_range_function=True,
                                  pg_stat_docdb_metrics=False, pg_stat_latency_histogram=False,
                                  qpm_stat_plans=True),
                ash_start=now, ash_end=now, ash_window_sec=60.0,
                statements_per_node=200, ash_per_node=10,
                collect_latency_histograms=False, collect_query_plans=True, query_plans_per_node=2000,
            )
        finally:
            (snapshot_write.connect, Q.pg_stat_statements_top, Q.ycql_stat_statements_top,
             Q.ash_aggregated, Q.yb_local_tablets_rows, Q.yb_pg_stat_plans_rows) = saved
        self.assertEqual(passed["args"], (2000, ["11", "-22"]))


class QpmStatusTest(unittest.TestCase):
    """The seed connection for QPM status: every checkpoint only while plans are collected."""

    def setUp(self):
        self.calls = {"connect": 0, "status": 0, "databases": 0}
        self.saved = (snapshot_write.connect, Q.qpm_status, Q.database_names, Q.role_names,
                      snapshot_write.QPM_STATUS_OFF_MAX_AGE_S)
        snapshot_write._qpm_status_cache.clear()

        @contextlib.contextmanager
        def fake_connect(dsn):
            self.calls["connect"] += 1
            yield object()

        def fake_status(conn):
            self.calls["status"] += 1
            return {"view_present": True, "track": "all"}

        def fake_databases(conn):
            self.calls["databases"] += 1
            return {"16640": "app"}

        snapshot_write.connect, Q.qpm_status, Q.database_names = fake_connect, fake_status, fake_databases
        Q.role_names = lambda conn: {"16384": "app_user"}

    def tearDown(self):
        (snapshot_write.connect, Q.qpm_status, Q.database_names, Q.role_names,
         snapshot_write.QPM_STATUS_OFF_MAX_AGE_S) = self.saved
        snapshot_write._qpm_status_cache.clear()

    @staticmethod
    def caps(qpm):
        return Capabilities(pg_stat_use_exec_time=True, yb_ash_range_function=True, pg_stat_docdb_metrics=False,
                            pg_stat_latency_histogram=False, qpm_stat_plans=qpm)

    def test_a_cluster_without_qpm_is_not_asked(self):
        self.assertEqual(snapshot_write._qpm_status_for_snapshot("dsn", self.caps(False), True), ({}, {}, {}))
        self.assertEqual(self.calls["connect"], 0)

    def test_collecting_reads_status_and_names_every_checkpoint(self):
        for _ in range(3):
            st, dbs, roles = snapshot_write._qpm_status_for_snapshot("dsn", self.caps(True), True)
        self.assertEqual((st["track"], dbs, roles), ("all", {"16640": "app"}, {"16384": "app_user"}))
        self.assertEqual(self.calls, {"connect": 3, "status": 3, "databases": 3})

    def test_off_reads_status_now_and_then_and_never_the_names(self):
        for _ in range(3):
            st, dbs, roles = snapshot_write._qpm_status_for_snapshot("dsn", self.caps(True), False)
        self.assertEqual((st["track"], dbs, roles), ("all", {}, {}))  # the viewer still sees track
        self.assertEqual(self.calls, {"connect": 1, "status": 1, "databases": 0})
        snapshot_write.QPM_STATUS_OFF_MAX_AGE_S = 0.0
        snapshot_write._qpm_status_for_snapshot("dsn", self.caps(True), False)
        self.assertEqual(self.calls["status"], 2)


if __name__ == "__main__":
    unittest.main()
