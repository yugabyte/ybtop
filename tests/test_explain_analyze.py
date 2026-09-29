"""EXPLAIN ANALYZE of recorded statements (src/ybtop/explain.py, /api/explain).

What matters here, and is not reachable safely from a live cluster:
  - recorded parameter values are parsed exactly, and are only ever BOUND, never
    spliced into SQL -- they are whatever an application once sent
  - a parameter string QPM may have cut short (its 255-byte slot) is refused
  - statements are replayed READ ONLY unless they write, always rolled back, as the
    role that ran them, with the timeout set before the statement
  - the endpoint takes only a recorded row's identity; SQL, values, database, node
    and role all come from the snapshot

Run:  python -m unittest discover -s tests
"""

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from ybtop import explain as X


class ParseParamTextTest(unittest.TestCase):
    def test_values_nulls_and_doubled_quotes(self):
        self.assertEqual(X.parse_param_text("$1 = '82', $2 = 'default'"), {1: "82", 2: "default"})
        self.assertEqual(X.parse_param_text("$1 = 'it''s', $2 = NULL"), {1: "it's", 2: None})
        self.assertEqual(X.parse_param_text("$1 = ''"), {1: ""})
        self.assertEqual(X.parse_param_text(""), {})
        self.assertEqual(X.parse_param_text(None), {})

    def test_a_value_that_looks_like_more_parameters_stays_one_value(self):
        self.assertEqual(X.parse_param_text("$1 = 'x'', $2 = ''y'"), {1: "x', $2 = 'y"})

    def test_parameter_numbers_are_ascii_digits(self):
        with self.assertRaises(ValueError):
            X.parse_param_text("$\u0661 = 'a'")  # an Arabic-Indic one: not a parameter to PostgreSQL
        self.assertEqual(X.placeholders("select $\u0661, $1"), {1})
        self.assertEqual(X._first_verb("\u00a0select 1"), "")  # NBSP is not SQL whitespace
        self.assertEqual(X._first_verb("\t\v\f(select 1)"), "SELECT")

    def test_anything_else_is_refused(self):
        for bad in (
            "$1 = 'a",  # cut mid-value
            "$1 = 'a',",
            "$1 = 'a', ",
            "$2 = 'a'",  # not numbered from 1
            "$1 = 'a', $1 = 'b'",
            "$1 = a",
            "1 = 'a'",
            "$1 = 'a'$2 = 'b'",
        ):
            with self.assertRaises(ValueError, msg=bad):
                X.parse_param_text(bad)


class PlaceholderScanTest(unittest.TestCase):
    def test_only_real_references_count(self):
        sql = (
            "select * from t where a = $1 and b = '$2' and c = $2 /* $3 /* $4 */ $5 */"
            " -- $6\n and d = foo$7 and e = $$ $8 $$ and f = $tag$ $9 $tag$"
            " and g = E'\\' $11' and h = \"$12\" and i = $10"
        )
        self.assertEqual(X.placeholders(sql), {1, 2, 10})

    def test_inline_literals_is_display_only_quoting(self):
        self.assertEqual(
            X.inline_literals("where (id, s) = ($1,$2) and x = $10 and y = '$1'", {1: "82", 2: "it's", 10: None}),
            "where (id, s) = ('82','it''s') and x = NULL and y = '$1'",
        )


class ReplayabilityTest(unittest.TestCase):
    def test_binds_values_in_order(self):
        self.assertEqual(
            X.bind_values("where (oe.id, oe.scope) = ($1,$2)", "$1 = '82', $2 = 'default'"),
            (["82", "default"], None),
        )
        self.assertEqual(X.bind_values("select 1", None), ([], None))

    def test_normalised_constants_have_nothing_to_replay(self):
        values, why = X.bind_values("where a = $1", None)
        self.assertIsNone(values)
        self.assertIn("replaced this statement's constants", why)

    def test_missing_or_extra_values_are_refused(self):
        values, why = X.bind_values("where a = $1 and b = $2", "$1 = '5'")
        self.assertIsNone(values)
        self.assertIn("constant in this statement with $2", why)
        self.assertIsNone(X.bind_values("where a = $1", "$1 = '5', $2 = '6'")[0])
        self.assertIsNone(X.bind_values("select 1", "$1 = '5'")[0])

    def test_redacted_parameters_are_explained(self):
        why = X.param_text_problem("$1 = '?', $2 = '?'")
        self.assertIn("yb_pg_stat_plans_show_max_exec_params", why)

    def test_a_255_byte_parameter_string_is_never_trusted(self):
        # QPM stores the text in 256 bytes; one that did not compress is cut to 255,
        # which can end between the two quotes of an escaped '' and still parse.
        text = "$1 = '" + "a" * 248 + "'"
        self.assertEqual(len(text.encode()), 255)
        self.assertEqual(X.parse_param_text(text), {1: "a" * 248})  # parses fine...
        self.assertIn("255-byte", X.param_text_problem(text))  # ...and is refused anyway
        self.assertIsNone(X.bind_values("where a = $1", text)[0])
        self.assertIsNone(X.param_text_problem("$1 = '" + "a" * 247 + "'"))
        self.assertIsNone(X.param_text_problem("$1 = '" + "a" * 400 + "'"))  # decompressed: complete


class StatementKindTest(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(X.statement_kind("select * from t", None), ("read", "SELECT"))
        self.assertEqual(X.statement_kind("/* c */ ( select 1 )", None), ("read", "SELECT"))
        self.assertEqual(X.statement_kind("UPDATE t SET a = 1", None), ("write", "UPDATE"))
        self.assertEqual(X.statement_kind("insert into t values ($1)", None), ("write", "INSERT"))
        self.assertEqual(
            X.statement_kind("select * from t for update", '[{"Plan": {"Node Type": "LockRows"}}]'),
            ("write", "SELECT … FOR UPDATE"),
        )
        self.assertEqual(
            X.statement_kind(
                "with u as (update t set a = 1 returning *) select * from u",
                '{"Node Type": "ModifyTable", "Operation": "Update"}',
            ),
            ("write", "WITH … UPDATE"),
        )

    def test_not_replayable(self):
        for sql in ("call p()", "do $$ begin end $$", "copy t to stdout", "set x = 1", ""):
            self.assertIsNone(X.statement_kind(sql, None)[0], sql)
        self.assertIn("starts with CALL", X.not_replayable_reason("call p()", "CALL"))
        self.assertIn("does not show this statement", X.not_replayable_reason("<insufficient privilege>", "?"))

    def test_debug_implies_dist(self):
        self.assertEqual(X.explain_options(False, False), ["ANALYZE"])
        self.assertEqual(X.explain_options(True, False), ["ANALYZE", "DIST"])
        self.assertEqual(X.explain_options(False, True), ["ANALYZE", "DIST", "DEBUG"])

    def test_timeout_is_clamped(self):
        self.assertEqual(X.clamp_timeout(None), 30)
        self.assertEqual(X.clamp_timeout("abc"), 30)
        self.assertEqual(X.clamp_timeout(0), 1)
        self.assertEqual(X.clamp_timeout(99999), 600)
        self.assertEqual(X.clamp_timeout("45"), 45)
        # An emptied box is "use the default", never a 1 s replay.
        self.assertEqual([X.clamp_timeout(v) for v in ("", "  ", "0x10", "1_000", "inf", True)], [30] * 6)
        self.assertEqual([X.clamp_timeout(v) for v in ("2.5", " 7 ", 12.4, "1e2")], [3, 7, 12, 100])


def _doc():
    return {
        "pg_stat_statements": {
            "per_node": {
                "n1:5433": [
                    {"queryid": "7", "dbname": "app", "query": "/* n1 */ select * from t where id = $1"},
                    {"queryid": "8", "dbname": "app", "query": "update t set v = $2 where id = $1"},
                ],
                "n2:5433": [{"queryid": "7", "dbname": "app", "query": "/* n2 */ select * from t where id = $1"}],
            }
        },
        "yb_pg_stat_plans": {
            "databases": {"16640": "app"},
            "roles": {"16384": "app_user"},
            "plans": {
                "aa": {"plan": '[{"Plan": {"Node Type": "Index Scan"}}]'},
                "bb": {"plan": '[{"Plan": {"Node Type": "ModifyTable", "Operation": "Update"}}]'},
            },
            "per_node": {
                "n1:5433": [
                    {"queryid": "7", "planid": "1", "plan_ref": "aa", "dbid": "16640", "userid": "16384",
                     "max_exec_time": 5.0, "max_exec_time_params": "$1 = '11'"},
                    {"queryid": "8", "planid": "2", "plan_ref": "bb", "dbid": "16640", "userid": "16384",
                     "max_exec_time": 9.0, "max_exec_time_params": "$1 = '3', $2 = 'it''s'"},
                    {"queryid": "7", "planid": "1", "plan_ref": "aa", "dbid": "16385", "userid": "16384",
                     "max_exec_time": 50.0, "max_exec_time_params": "$1 = '12'"},
                ],
                "n2:5433": [
                    {"queryid": "7", "planid": "1", "plan_ref": "aa", "dbid": "16640", "userid": "16384",
                     "max_exec_time": 8.0, "max_exec_time_params": "$1 = '?'"},
                ],
            },
        },
    }


class ResolveTargetTest(unittest.TestCase):
    def test_everything_comes_from_the_named_row(self):
        t, why = X.resolve_target(_doc(), queryid="7", planid="1", plan_ref="aa", dbid="16640", node="n1:5433", userid="16384")
        self.assertIsNone(why)
        self.assertEqual(t["values"], ["11"])
        self.assertEqual(t["statement"], "/* n1 */ select * from t where id = $1")  # the node's own text
        self.assertEqual(t["sql_display"], "/* n1 */ select * from t where id = '11'")
        self.assertEqual((t["datname"], t["userid"], t["role"], t["kind"]), ("app", "16384", "app_user", "read"))
        self.assertEqual(t["recorded_max_ms"], 5.0)

    def test_writes_are_marked(self):
        t, _ = X.resolve_target(_doc(), queryid="8", planid="2", plan_ref="bb", dbid="16640", node="n1:5433", userid="16384")
        self.assertEqual((t["kind"], t["label"], t["values"]), ("write", "UPDATE", ["3", "it's"]))

    def test_refusals(self):
        doc = _doc()
        cases = {
            ("7", "1", "aa", "16640", "n9:5433"): "not recorded",
            ("7", "9", "aa", "16640", "n1:5433"): "not recorded",
            ("7", "1", "aa", "16385", "n1:5433"): "no longer exists",  # dropped database
            ("7", "1", "aa", "16640", "n2:5433"): "show_max_exec_params",  # redacted on that node
        }
        for (q, p, r, d, n), expect in cases.items():
            t, why = X.resolve_target(doc, queryid=q, planid=p, plan_ref=r, dbid=d, node=n, userid="16384")
            self.assertIsNone(t)
            self.assertIn(expect, why)
        no_role = _doc()
        no_role["yb_pg_stat_plans"]["per_node"]["n1:5433"][0]["userid"] = ""
        t, why = X.resolve_target(no_role, queryid="7", planid="1", plan_ref="aa", dbid="16640", node="n1:5433", userid="")
        self.assertIsNone(t)  # never quietly replay as ybtop's own (superuser) login
        self.assertIn("which role", why)
        del doc["pg_stat_statements"]
        self.assertIn("text for this query_id", X.resolve_target(
            doc, queryid="7", planid="1", plan_ref="aa", dbid="16640", node="n1:5433", userid="16384")[1])

    def test_the_row_is_the_one_named_including_its_role(self):
        # QPM keys on (database, user, queryid, planid): two roles, two rows, one identity otherwise.
        doc = _doc()
        doc["yb_pg_stat_plans"]["roles"]["16385"] = "report_user"
        doc["yb_pg_stat_plans"]["per_node"]["n1:5433"].insert(0, {
            "queryid": "7", "planid": "1", "plan_ref": "aa", "dbid": "16640", "userid": "16385",
            "max_exec_time": 900.0, "max_exec_time_params": "$1 = '99'"})
        a, _ = X.resolve_target(doc, queryid="7", planid="1", plan_ref="aa", dbid="16640", node="n1:5433", userid="16384")
        b, _ = X.resolve_target(doc, queryid="7", planid="1", plan_ref="aa", dbid="16640", node="n1:5433", userid="16385")
        self.assertEqual((a["values"], a["role"]), (["11"], "app_user"))
        self.assertEqual((b["values"], b["role"]), (["99"], "report_user"))

    def test_statements_whose_effects_a_rollback_cannot_undo_are_refused(self):
        for sql, fn in [
            ("select pg_terminate_backend(pid) from pg_stat_activity where usename = $1", "pg_terminate_backend"),
            ("SELECT pg_catalog.PG_CANCEL_BACKEND($1)", "pg_cancel_backend"),
            ('select "setval"($1, 1)', "setval"),
            ("select dblink_exec($1, $2)", "dblink_exec"),
            ("select pg_stat_reset()", "pg_stat_reset"),
            ("select yb_pg_stat_plans_reset(null, null, null, null)", "yb_pg_stat_plans_reset"),
        ]:
            self.assertEqual(X.side_effect_call(sql), fn, sql)
        for sql in (
            "select * from t where note = $1 -- pg_terminate_backend(",
            "select my_setval($1)", "select nextval($1)", "select pg_terminate_backendx($1)",
            "select \u00e9setval(1)",
        ):
            self.assertIsNone(X.side_effect_call(sql), sql)
        doc = _doc()
        doc["pg_stat_statements"]["per_node"]["n1:5433"][0]["query"] = "select pg_terminate_backend($1)"
        t, why = X.resolve_target(doc, queryid="7", planid="1", plan_ref="aa", dbid="16640", node="n1:5433", userid="16384")
        self.assertIsNone(t)
        self.assertIn("pg_terminate_backend()", why)


class _Res:
    def __init__(self, status, rows=(), err=None, state=None):
        self.status, self._rows, self._err, self._state = status, list(rows), err, state
        self.ntuples = len(self._rows)
        self.error_message = (err or "").encode()

    def get_value(self, r, c):
        v = self._rows[r]
        return None if v is None else v.encode()

    def error_field(self, f):
        return self._state.encode() if self._state else None


class _FakePg:
    """Records every statement and its bound values; scripted answers by prefix."""

    def __init__(self, fail_on=None, sqlstate=None, block=None, txn_writes="off"):
        self.calls, self.fail_on, self.sqlstate, self.block = [], fail_on, sqlstate, block
        self.txn_writes = txn_writes
        self.cancelled = threading.Event()

    def exec_params(self, sql, values):
        from psycopg import pq

        sql = sql.decode()
        self.calls.append((sql, [None if v is None else v.decode() for v in values]))
        if self.fail_on and self.fail_on in sql:
            if self.block:
                self.cancelled.wait(5)
            return _Res(pq.ExecStatus.FATAL_ERROR, err="ERROR:  boom", state=self.sqlstate)
        if "FROM pg_roles" in sql:
            return _Res(pq.ExecStatus.TUPLES_OK, ["app_user"])
        if "pg_db_role_setting" in sql:
            return _Res(pq.ExecStatus.TUPLES_OK, ["search_path=app, public", "work_mem=64MB", "no_such.guc=1",
                                                  "transaction_isolation=serializable", "statement_timeout=1"])
        if "FROM pg_settings" in sql:
            return _Res(pq.ExecStatus.TUPLES_OK, ["search_path", "work_mem", "statement_timeout", "transaction_isolation"])
        if "current_setting('yb_disable_transactional_writes'" in sql:
            return _Res(pq.ExecStatus.TUPLES_OK, [self.txn_writes])
        if "FORMAT JSON" in sql:
            return _Res(pq.ExecStatus.TUPLES_OK, ['[{"Plan": {"Node Type": "Index Scan"}}]'])
        if sql.startswith(X.SQL_PREFIX + "EXPLAIN (ANALYZE"):
            return _Res(pq.ExecStatus.TUPLES_OK, ["Index Scan on t", "Planning Time: 0.1 ms", "Execution Time: 2.5 ms"])
        return _Res(pq.ExecStatus.COMMAND_OK if not sql.startswith(X.SQL_PREFIX + "SELECT") else pq.ExecStatus.TUPLES_OK)

    def get_cancel(self):
        fake = self

        class C:
            def cancel(self):
                fake.cancelled.set()

        return C()


class _FakeConn:
    def __init__(self, pg):
        self.pgconn, self.closed = pg, False

    def add_notice_handler(self, fn):
        pass

    def close(self):
        self.closed = True


def _target(kind="read", **kw):
    t = {"queryid": "7", "planid": "1", "plan_ref": "aa", "dbid": "16640", "datname": "app",
         "node": "n1:5433", "userid": "16384", "statement": "select * from t where v = $1",
         "values": ["1'; drop table t; --"], "kind": kind, "label": "SELECT",
         "sql_display": "select * from t where v = '1''; drop table t; --'"}
    t.update(kw)
    return t


class RunExplainTest(unittest.TestCase):
    def run_with(self, pg, target, options=("ANALYZE",), timeout=30, **kw):
        conn = _FakeConn(pg)
        res = X.run_explain("dsn", target, list(options), timeout, connect=lambda *a, **k: conn, **kw)
        return res, [c[0] for c in pg.calls], conn

    def test_statement_sequence_for_a_read(self):
        pg = _FakePg()
        res, sqls, conn = self.run_with(pg, _target(), ("ANALYZE", "DIST"))
        body = [s.replace(X.SQL_PREFIX, "") for s in sqls if "pg_roles" not in s and "pg_settings" not in s
                and "pg_db_role_setting" not in s and "set_config" not in s and "current_setting" not in s]
        self.assertEqual(body, [
            "SET statement_timeout = '30s'",  # session-wide first: the preamble has a deadline too
            "BEGIN READ ONLY",
            'SET LOCAL ROLE "app_user"',
            "SET LOCAL statement_timeout = '30s'",
            "EXPLAIN (FORMAT JSON) select * from t where v = $1",
            "EXPLAIN (ANALYZE, DIST) select * from t where v = $1",
            "ROLLBACK",
        ])
        self.assertIsNone(res["error"])
        self.assertEqual((res["execution_ms"], res["planning_ms"], res["role"]), (2.5, 0.1, "app_user"))
        self.assertTrue(conn.closed)

    def test_connection_detects_a_dead_peer(self):
        seen = {}
        pg = _FakePg()
        conn = _FakeConn(pg)

        def connect(dsn, **kw):
            seen.update(kw)
            return conn

        X.run_explain("dsn", _target(), ["ANALYZE"], 30, connect=connect)
        self.assertEqual((seen["keepalives"], seen["application_name"]), (1, "ybtop-explain"))
        self.assertLessEqual(seen["keepalives_idle"] + seen["keepalives_interval"] * seen["keepalives_count"], 60)
        self.assertTrue(seen["tcp_user_timeout"] > 0)

    def test_cancel_before_the_connection_exists_runs_nothing(self):
        stop = threading.Event()
        stop.set()
        called = []
        res = X.run_explain("dsn", _target(), ["ANALYZE"], 30, connect=lambda *a, **k: called.append(1), stop=stop)
        self.assertEqual(called, [])
        self.assertTrue(res["cancelled"])

    def test_cancel_during_the_preamble_stops_before_the_analyze(self):
        stop = threading.Event()
        pg = _FakePg()
        orig = pg.exec_params

        def exec_params(sql, values):
            if b"FORMAT JSON" in sql:
                stop.set()  # the user clicks Cancel while the plan-only EXPLAIN runs
            return orig(sql, values)

        pg.exec_params = exec_params
        res, sqls, _ = self.run_with(pg, _target(), stop=stop)
        self.assertTrue(res["cancelled"])
        self.assertFalse(any("EXPLAIN (ANALYZE" in q for q in sqls))
        self.assertEqual(sqls[-1], "ROLLBACK")

    def test_values_are_bound_never_spliced(self):
        pg = _FakePg()
        self.run_with(pg, _target())
        for sql, values in pg.calls:
            self.assertNotIn("drop table", sql)
            if "EXPLAIN" in sql:
                self.assertEqual(values, ["1'; drop table t; --"])

    def test_role_settings_applied_before_the_role_switch_and_filtered(self):
        pg = _FakePg()
        res, sqls, _ = self.run_with(pg, _target())
        cfg = [v for s, v in pg.calls if "set_config($1, $2, true)" in s]
        self.assertEqual(cfg, [["search_path", "app, public"], ["work_mem", "64MB"]])  # unknown GUC skipped
        self.assertEqual(res["role_settings"], ["search_path", "work_mem"])
        self.assertLess(
            max(i for i, s in enumerate(sqls) if "set_config($1, $2, true)" in s),
            next(i for i, s in enumerate(sqls) if "SET LOCAL ROLE" in s),
        )
        # statement_timeout from the role never overrides the one the user chose
        self.assertNotIn(["statement_timeout"], [v[:1] for v in cfg])

    def test_every_write_stays_inside_the_transaction(self):
        # yb_disable_transactional_writes makes YugabyteDB write DML outside the
        # transaction, so ROLLBACK would undo nothing: forced off, and checked.
        pg = _FakePg()
        _, sqls, _ = self.run_with(pg, _target(kind="write", statement="update t set v = $1"))
        off = next(i for i, q in enumerate(sqls) if "set_config(name, 'off', true)" in q and "yb_disable_transactional_writes" in q)
        check = next(i for i, q in enumerate(sqls) if "current_setting('yb_disable_transactional_writes'" in q)
        analyze = next(i for i, q in enumerate(sqls) if "EXPLAIN (ANALYZE" in q)
        self.assertLess(next(i for i, q in enumerate(sqls) if "SET LOCAL ROLE" in q), off)
        self.assertLess(off, check)
        self.assertLess(check, analyze)

    def test_a_session_that_keeps_non_transactional_writes_is_refused(self):
        pg = _FakePg(txn_writes="on")
        res, sqls, _ = self.run_with(pg, _target(kind="write", statement="update t set v = $1"))
        self.assertIn("could not be rolled back", res["error"])
        self.assertFalse(any("EXPLAIN (ANALYZE" in q for q in sqls))
        self.assertEqual(sqls[-1], "ROLLBACK")

    def test_notices_are_capped(self):
        pg = _FakePg()
        conn = _FakeConn(pg)
        handler = {}
        conn.add_notice_handler = lambda fn: handler.setdefault("fn", fn)
        orig = pg.exec_params

        def noisy(sql, values):
            if b"EXPLAIN (ANALYZE" in sql:
                for k in range(500):
                    handler["fn"](type("D", (), {"severity": "NOTICE", "message_primary": "x" * 900})())
            return orig(sql, values)

        pg.exec_params = noisy
        res = X.run_explain("dsn", _target(), ["ANALYZE"], 30, connect=lambda *a, **k: conn)
        self.assertEqual(len(res["notices"]), X.MAX_NOTICES)
        self.assertTrue(all(len(n) <= 500 for n in res["notices"]))

    def test_writes_run_read_write_and_are_rolled_back(self):
        pg = _FakePg()
        _, sqls, _ = self.run_with(pg, _target(kind="write", statement="update t set v = $1"))
        self.assertIn("BEGIN", sqls)
        self.assertNotIn("BEGIN READ ONLY", sqls)
        self.assertEqual(sqls[-1], "ROLLBACK")
        self.assertFalse(any("COMMIT" in s for s in sqls))

    def test_failure_still_rolls_back_and_reports(self):
        pg = _FakePg(fail_on="EXPLAIN (ANALYZE", sqlstate="42P01")
        res, sqls, conn = self.run_with(pg, _target())
        self.assertEqual(sqls[-1], "ROLLBACK")
        self.assertEqual((res["error"], res["sqlstate"]), ("ERROR:  boom", "42P01"))
        self.assertTrue(conn.closed)

    def test_client_cancels_at_the_timeout(self):
        pg = _FakePg(fail_on="EXPLAIN (ANALYZE", sqlstate="57014", block=True)
        t0 = time.monotonic()
        res, _, _ = self.run_with(pg, _target(), timeout=1)
        self.assertTrue(pg.cancelled.is_set())
        self.assertLess(time.monotonic() - t0, 4)
        self.assertTrue(res["timed_out"])
        self.assertIn("1 s statement timeout", res["error"])

    def test_user_cancel_is_reported_as_such(self):
        pg = _FakePg(fail_on="EXPLAIN (ANALYZE", sqlstate="57014", block=True)
        grabbed = {}
        th = threading.Thread(target=lambda: grabbed.setdefault("res", self.run_with(
            pg, _target(), timeout=30, on_cancel_ready=lambda fn: grabbed.setdefault("cancel", fn))[0]))
        th.start()
        for _ in range(100):
            if "cancel" in grabbed:
                break
            time.sleep(0.01)
        grabbed["cancel"]()
        th.join(5)
        self.assertTrue(grabbed["res"]["cancelled"])
        self.assertFalse(grabbed["res"]["timed_out"])

    def test_read_only_refusal_is_explained(self):
        pg = _FakePg(fail_on="EXPLAIN (ANALYZE", sqlstate="25006")
        res, _, _ = self.run_with(pg, _target())
        self.assertIn("only replayed read-only", res["error"])


class ExplainRunsTest(unittest.TestCase):
    def test_one_at_a_time_and_latest_per_family(self):
        runs = X.ExplainRuns()
        gate = threading.Event()

        def slow(run, ready):
            gate.wait(5)
            return {"plan_text": "x", "error": None}

        a, busy = runs.start(_target(queryid="7"), ["ANALYZE"], 30, slow)
        self.assertIsNone(busy)
        self.assertEqual(a["state"], "running")
        b, busy = runs.start(_target(queryid="8"), ["ANALYZE"], 30, slow)
        self.assertIsNone(b)
        self.assertEqual(busy["queryid"], "7")
        gate.set()
        for _ in range(200):
            if runs.active() is None:
                break
            time.sleep(0.01)
        self.assertIsNone(runs.active())
        done = runs.latest("16640", ["7", "8"])
        self.assertEqual((done["queryid"], done["state"]), ("7", "done"))
        self.assertNotIn("_t0", done)
        c, busy = runs.start(_target(queryid="8"), ["ANALYZE"], 30, lambda r, ready: {"error": "nope"})
        for _ in range(200):
            if runs.active() is None:
                break
            time.sleep(0.01)
        self.assertEqual(runs.latest("16640", ["7", "8"])["queryid"], "8")
        self.assertEqual(runs.latest("16640", ["8"])["state"], "error")

    def test_a_run_that_goes_silent_gives_up_its_slot(self):
        runs = X.ExplainRuns()
        hang, late = threading.Event(), threading.Event()

        def work(run, ready):
            hang.wait(5)
            late.set()
            return {"plan_text": "late", "error": None}

        saved = X.ABANDON_GRACE_S, X._CONNECT_OPTS["connect_timeout"]
        X.ABANDON_GRACE_S, X._CONNECT_OPTS["connect_timeout"] = 0, 0
        try:
            runs.start(_target(queryid="7"), ["ANALYZE"], 1, work)
            self.assertIsNotNone(runs.active())
            time.sleep(1.2)
            self.assertIsNone(runs.active())  # reaped: the slot is free again
            reaped = runs.latest("16640", ["7"])
            self.assertEqual(reaped["state"], "error")
            self.assertIn("ybtop-explain", reaped["error"])
            nxt, busy = runs.start(_target(queryid="8"), ["ANALYZE"], 30, lambda r, ready: (time.sleep(0.3), {"error": None})[1])
            self.assertIsNone(busy)
            hang.set()
            late.wait(5)
            time.sleep(0.05)
            # The late worker neither overwrote the reaped run nor freed the new run's slot.
            self.assertEqual(runs.latest("16640", ["7"])["state"], "error")
            self.assertEqual(runs.active()["queryid"], "8")
        finally:
            X.ABANDON_GRACE_S, X._CONNECT_OPTS["connect_timeout"] = saved
            hang.set()

    def test_cancel_before_the_connection_is_seen_by_the_run(self):
        runs = X.ExplainRuns()
        seen = {}
        entered = threading.Event()

        def work(run, ready):
            entered.set()
            for _ in range(200):  # "connecting": no cancel hook registered yet
                if run["_stop"].is_set():
                    seen["stopped"] = True
                    return {"cancelled": True, "error": "Cancelled."}
                time.sleep(0.01)
            return {"error": None}

        runs.start(_target(), ["ANALYZE"], 30, work)
        entered.wait(5)
        runs.cancel("16640", "7")
        for _ in range(200):
            if runs.active() is None:
                break
            time.sleep(0.01)
        self.assertTrue(seen.get("stopped"))
        self.assertEqual(runs.latest("16640", ["7"])["state"], "cancelled")

    def test_cancel_reaches_the_running_statement(self):
        runs = X.ExplainRuns()
        stop = threading.Event()

        def work(run, ready):
            ready(stop.set)
            stop.wait(5)
            return {"cancelled": True, "error": "Cancelled."}

        runs.start(_target(), ["ANALYZE"], 30, work)
        for _ in range(100):
            if runs._cancels:
                break
            time.sleep(0.01)
        runs.cancel("16640", "7")
        for _ in range(200):
            if runs.active() is None:
                break
            time.sleep(0.01)
        self.assertEqual(runs.latest("16640", ["7"])["state"], "cancelled")


class ExplainEndpointTest(unittest.TestCase):
    """The HTTP contract, through a real server; the database call is faked."""

    def setUp(self):
        from ybtop import serve

        self.serve = serve
        self.tmp = tempfile.TemporaryDirectory()
        self.file = "ybtop.out.20260925_130000.json"
        with open(os.path.join(self.tmp.name, self.file), "w") as f:
            json.dump(_doc(), f)
        H = serve.YbtopHTTPRequestHandler
        self.saved = (H.data_dir, H.seed_dsn, H.allow_explain_analyze, H.explain_runs, serve.X.run_explain)
        H.data_dir = __import__("pathlib").Path(self.tmp.name)
        H.seed_dsn = "host=seed port=5433 dbname=yugabyte user=yugabyte"
        H.allow_explain_analyze = True
        H.explain_runs = X.ExplainRuns()
        self.seen = []
        self.release = threading.Event()

        def fake_run(dsn, target, options, timeout_s, on_cancel_ready=None, connect=None, stop=None):
            self.seen.append((dsn, target, options, timeout_s))
            self.release.wait(5)
            return {"plan_text": "ok", "execution_ms": 1.0, "error": None}

        serve.X.run_explain = fake_run
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def tearDown(self):
        self.release.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        H = self.serve.YbtopHTTPRequestHandler
        H.data_dir, H.seed_dsn, H.allow_explain_analyze, H.explain_runs, self.serve.X.run_explain = self.saved
        self.tmp.cleanup()

    def post(self, body, route="/api/explain", headers=None):
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        req = urllib.request.Request(self.base + route, data=json.dumps(body).encode(), headers=h, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def get(self, qs):
        with urllib.request.urlopen(self.base + "/api/explain?" + qs) as r:
            return json.loads(r.read())

    def row(self, **kw):
        b = {"queryid": "7", "planid": "1", "plan_ref": "aa", "dbid": "16640", "node": "n1:5433",
             "userid": "16384", "file": self.file, "dist": True, "debug": False, "timeout_s": 45}
        b.update(kw)
        return b

    def test_off_unless_the_collector_opted_in(self):
        self.serve.YbtopHTTPRequestHandler.allow_explain_analyze = False
        status, body = self.post(self.row())
        self.assertEqual(status, 403)
        self.assertIn("--allow-explain-analyze", body["error"])
        self.assertFalse(self.get("dbid=16640&queryids=7")["available"])
        self.assertEqual(self.seen, [])

    def test_runs_what_the_snapshot_recorded_on_its_node(self):
        status, body = self.post(self.row(statement="drop table t", values=["x"], sql="drop table t"))
        self.assertEqual(status, 202)
        self.assertEqual(body["run"]["state"], "running")
        dsn, target, options, timeout_s = self.seen[0]
        self.assertEqual(target["statement"], "/* n1 */ select * from t where id = $1")  # request's sql ignored
        self.assertEqual(target["values"], ["11"])
        self.assertIn("host=n1", dsn)
        self.assertIn("dbname=app", dsn)
        self.assertEqual((options, timeout_s), (["ANALYZE", "DIST"], 45))
        # one at a time
        status, body = self.post(self.row(queryid="8", planid="2", plan_ref="bb"))
        self.assertEqual(status, 409)
        self.assertEqual(body["active"]["queryid"], "7")
        self.release.set()
        for _ in range(200):
            got = self.get("dbid=16640&queryids=7,8")
            if got["run"] and got["run"]["state"] == "done":
                break
            time.sleep(0.01)
        self.assertEqual((got["run"]["state"], got["run"]["plan_text"]), ("done", "ok"))
        self.assertIsNone(got["active"])

    def test_cross_site_posts_are_refused(self):
        host = self.base.split("//", 1)[1]
        for headers in (
            {"Content-Type": "text/plain"},  # a plain HTML form: no CORS preflight
            {"Sec-Fetch-Site": "cross-site"},
            {"Sec-Fetch-Site": "same-site"},
            {"Origin": "http://evil.example"},
            {"Origin": "null"},
        ):
            status, body = self.post(self.row(), headers=headers)
            self.assertEqual(status, 403, headers)
            status, _ = self.post({"query_plans": False}, route="/api/control", headers=headers)
            self.assertEqual(status, 403, headers)
        self.assertEqual(self.seen, [])
        status, _ = self.post(self.row(), headers={"Origin": "http://" + host, "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(status, 202)

    def test_a_rebound_host_is_refused(self):
        H = self.serve.YbtopHTTPRequestHandler
        saved = H.bind_host, H.allowed_hosts
        try:
            H.bind_host, H.allowed_hosts = "127.0.0.1", frozenset()
            req = urllib.request.Request(self.base + "/api/explain?dbid=16640&queryids=7", headers={"Host": "evil.example:8765"})
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req)
            self.assertEqual(cm.exception.code, 403)
            self.assertIn("--serve-allowed-host evil.example", cm.exception.read().decode())
            status, _ = self.post(self.row(), headers={"Host": "evil.example:8765"})
            self.assertEqual(status, 403)
            self.assertEqual(self.seen, [])
            self.assertTrue(self.get("dbid=16640&queryids=7")["available"])  # 127.0.0.1 itself is fine
        finally:
            H.bind_host, H.allowed_hosts = saved

    def test_a_compressed_snapshot_is_read_the_same(self):
        import gzip

        packed = self.file + ".gz"
        with open(os.path.join(self.tmp.name, packed), "wb") as f:
            f.write(gzip.compress(json.dumps(_doc()).encode()))
        status, body = self.post(self.row(file=packed))
        self.assertEqual(status, 202, body)
        self.assertEqual(self.seen[0][1]["values"], ["11"])
        self.assertEqual(self.seen[0][1]["snapshot_file"], packed)

    def test_bad_requests(self):
        self.assertEqual(self.post(self.row(node="evil.example:5433"))[0], 404)  # never a host from the request
        self.assertEqual(self.post(self.row(file="../etc/passwd"))[0], 404)
        self.assertEqual(self.post(self.row(queryid="7; drop"))[0], 400)
        status, body = self.post(self.row(userid=""))  # a page from before userid was sent
        self.assertEqual(status, 400)
        self.assertIn("reload", body["error"])
        self.assertEqual(self.post(self.row(userid="99999"))[0], 409)  # not that role's row
        self.assertEqual(self.post(self.row(dist="yes"))[0], 400)
        status, body = self.post(self.row(node="n2:5433"))  # redacted there
        self.assertEqual(status, 409)
        self.assertIn("show_max_exec_params", body["error"])
        self.assertEqual(self.seen, [])


if __name__ == "__main__":
    unittest.main()
