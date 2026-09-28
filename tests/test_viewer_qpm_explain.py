"""EXPLAIN ANALYZE in the viewer (src/ybtop/web/app.js), against the server's rules.

The page decides which recorded execution to replay and shows its statement with
the values inlined; the server re-derives the statement and values for that row
from the snapshot and runs them. If the two disagree, the dialog shows one query
and the collector runs another -- so the parsing, scanning and target rules are
checked for parity against src/ybtop/explain.py, and the page's chosen row is
resolved by the server's own code.

Runs the real browser functions under node; skipped when node is not installed.
"""

import json
import os
import shutil
import subprocess
import unittest

from ybtop import explain as X

try:
    from test_explain_analyze import _doc  # unittest discover -s tests
except ImportError:  # python -m unittest tests.test_viewer_qpm_explain
    from tests.test_explain_analyze import _doc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_JS = os.path.join(ROOT, "src", "ybtop", "web", "app.js")

HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.env.APP_JS, "utf8");
const fn = (n) => {
  const a = src.indexOf(`\n  function ${n}(`);
  if (a < 0) throw new Error("function not found: " + n);
  return src.slice(a, src.indexOf("\n  }\n", a) + 4);
};
const cb = (n) => {
  const a = src.indexOf(`\n  const ${n} =`);
  if (a < 0) throw new Error("const not found: " + n);
  return src.slice(a, src.indexOf(";\n", a) + 2);
};
const NAMES = ["qpmParseParamText", "qpmIdentChar", "qpmScanSql", "qpmPlaceholders", "qpmSqlLiteral",
  "qpmInlineLiterals", "qpmUtf8Length", "qpmParamTextProblem", "qpmBindValues", "qpmFirstVerb",
  "qpmStatementKind", "qpmExplainOptions", "qpmStatementText", "qpmExplainTarget", "qpmPlanSignature",
  "qpmMatchRecordedPlan", "qpmReplayPlanNote", "qpmQuoteIdent", "qpmExplainSequence", "qpmExplainSummary", "qpmClampTimeout",
  "qpmCodeOnly", "qpmSideEffectCall", "qpmSideEffectReason", "qpmNotReplayableReason",
  "qpmApplyExplainResponse", "qpmViewScopeKey", "qpmRunQueryIds",
  "qpmFmtMs", "qpmFmtClock", "qpmRatioText", "qpmExplainStripModel", "qpmExplainBannerModel"];
const A = new Function([
  cb("QPM_PARAM_TEXT_SLOT_BYTES"), cb("QPM_EXPLAIN_DEFAULT_TIMEOUT_S"), cb("QPM_EXPLAIN_MAX_TIMEOUT_S"),
  cb("QPM_READ_VERBS"), cb("QPM_WRITE_VERBS"), cb("QPM_SIDE_EFFECT_CALL"),
  ...NAMES.map(fn), "return {" + NAMES.join(",") + "};"].join("\n"))();

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const tryIt = (f) => { try { return { ok: f() }; } catch (e) { return { err: true }; } };
const out = {};
out.params = input.params.map((t) => ({
  parsed: tryIt(() => A.qpmParseParamText(t)),
  problem: A.qpmParamTextProblem(t),
}));
out.sql = input.sql.map((q) => ({
  refs: Array.from(A.qpmPlaceholders(q)).sort((a, b) => a - b),
  inlined: A.qpmInlineLiterals(q, { 1: "v1", 2: "it's", 10: null }),
  verb: A.qpmFirstVerb(q),
}));
out.bind = input.bind.map(([q, t]) => A.qpmBindValues(q, t));
out.kind = input.kind.map(([q, p]) => A.qpmStatementKind(q, p));
out.sideEffect = input.sql.concat(input.sideEffect).map((q) => A.qpmSideEffectCall(q));
out.notReplayable = input.kind.map(([q]) => A.qpmNotReplayableReason(q, A.qpmStatementKind(q, null).label));
out.clamp2 = input.clamp.map((v) => A.qpmClampTimeout(v));
{ // a bulk INSERT with 130k placeholders must not throw (Math.max(...refs) did)
  const n = 130000;
  const q = "insert into t values " + Array.from({ length: n / 2 }, (_, k) => "($" + (2 * k + 1) + ",$" + (2 * k + 2) + ")").join(",");
  let err = null;
  try { A.qpmBindValues(q, null); A.qpmBindValues(q, "$1 = 'a'"); } catch (e) { err = String(e); }
  out.huge = { err, refs: A.qpmPlaceholders(q).size };
}

const doc = input.doc;
const S = (a) => new Set(a);
out.target = {
  q7: A.qpmExplainTarget(doc, S(["7"]), S(["16640"])),
  family: A.qpmExplainTarget(doc, S(["7", "8"]), S(["16640"])),
  anyDb: A.qpmExplainTarget(doc, S(["7"]), null),
  none: A.qpmExplainTarget(doc, S(["99"]), null),
};
{ // slowest on n2 with real values: n2's own statement text, not n1's
  const d = JSON.parse(JSON.stringify(doc));
  d.yb_pg_stat_plans.per_node["n2:5433"][0].max_exec_time_params = "$1 = '21'";
  out.target.onN2 = A.qpmExplainTarget(d, S(["7"]), S(["16640"]));
  out.docN2 = d;
}
{ // two roles ran the same statement: QPM keeps a row each, same identity but for userid
  const d = JSON.parse(JSON.stringify(doc));
  d.yb_pg_stat_plans.roles["16385"] = "report_user";
  d.yb_pg_stat_plans.per_node["n1:5433"].push({ queryid: "7", planid: "1", plan_ref: "aa", dbid: "16640",
    userid: "16385", max_exec_time: 900, max_exec_time_params: "$1 = '99'" });
  out.target.twoRoles = A.qpmExplainTarget(d, S(["7"]), S(["16640"]));
  out.docTwoRoles = d;
}
{ // rows the page must never offer: no userid, no plan
  const d = JSON.parse(JSON.stringify(doc));
  d.yb_pg_stat_plans.per_node["n1:5433"].push({ queryid: "7", planid: "1", plan_ref: "aa", dbid: "16640",
    userid: null, max_exec_time: 990, max_exec_time_params: "$1 = '5'" });
  d.yb_pg_stat_plans.per_node["n1:5433"].push({ queryid: "7", planid: "9", plan_ref: null, dbid: "16640",
    userid: "16384", max_exec_time: 980, max_exec_time_params: "$1 = '6'" });
  out.target.skipsBadRows = A.qpmExplainTarget(d, S(["7"]), S(["16640"]));
}
{ // a statement whose effect a rollback cannot undo is never offered
  const d = JSON.parse(JSON.stringify(doc));
  Object.values(d.pg_stat_statements.per_node).forEach((rows) => rows.forEach((r) => {
    if (r.queryid === "7") r.query = "select pg_terminate_backend($1)";
  }));
  out.target.sideEffect = A.qpmExplainTarget(d, S(["7"]), S(["16640"]));
}
{ // every recorded execution unreplayable -> not ok, with the slowest one's reason
  const d = JSON.parse(JSON.stringify(doc));
  Object.values(d.yb_pg_stat_plans.per_node).forEach((rows) =>
    rows.forEach((r) => { r.max_exec_time_params = "$1 = '?'"; }));
  out.target.unreplayable = A.qpmExplainTarget(d, S(["7"]), S(["16640"]));
}

const shape = (kids, extra) => JSON.stringify([{ Plan: Object.assign({ "Node Type": "Nested Loop",
  "Join Type": "Inner", Plans: kids }, extra || {}) }]);
const idx = (rel, index) => ({ "Node Type": "Index Scan", "Relation Name": rel, "Index Name": index });
const recordedA = shape([idx("a", "a_pkey"), idx("b", "b_pkey")]);
const recordedB = shape([idx("b", "b_pkey"), idx("a", "a_pkey")]);   // join order swapped
const groups = [{ planid: "1", plan_ref: "rA" }, { planid: "2", plan_ref: "rB" }];
const texts = { rA: { plan: recordedA }, rB: { plan: recordedB } };
// EXPLAIN adds costs and actuals; only the shape should decide the match.
const explained = shape([Object.assign(idx("b", "b_pkey"), { "Total Cost": 9 }), idx("a", "a_pkey")],
  { "Startup Cost": 1, "Actual Rows": 3 });
out.match = {
  swapped: A.qpmMatchRecordedPlan(explained, groups, texts),
  unknown: A.qpmMatchRecordedPlan(shape([idx("a", "a_idx2"), idx("b", "b_pkey")]), groups, texts),
  noPlan: A.qpmMatchRecordedPlan("not json", groups, texts),
  sigA: A.qpmPlanSignature(recordedA),
};
out.match.swapped = out.match.swapped && { index: out.match.swapped.index, planid: out.match.swapped.group.planid };
// Recorded plans in text format (or none): no "not recorded" claim either way.
out.match.textPlans = A.qpmMatchRecordedPlan(explained, groups, { rA: { plan: "Nested Loop" }, rB: {} });
{ // the note says whether the replay chose the plan the slowest execution ran
  const gs = [
    { planid: "FAST", plan_ref: "f", active: true,
      variants: [{ planid: "FAST", plan_ref: "f" }, { planid: "FASTc", plan_ref: "fc" }] },
    { planid: "SLOW", plan_ref: "s", active: false, variants: [{ planid: "SLOW", plan_ref: "s" }] },
  ];
  const note = (match, planid, ref) => A.qpmReplayPlanNote(match, { planid, plan_ref: ref }, gs);
  out.replayNote = {
    none: note(null, "SLOW", "s"),
    sameAsRan: note({ group: gs[1], index: 1 }, "SLOW", "s"),
    notRan: note({ group: gs[0], index: 0 }, "SLOW", "s"),
    ranVariant: note({ group: gs[0], index: 0 }, "FASTc", "fc"),
    samePlanidOtherText: note({ group: gs[0], index: 0 }, "FAST", "fc"),
    notRecorded: note({ group: null, index: -1 }, "SLOW", "s"),
    notRecordedRanUnknown: note({ group: null, index: -1 }, "GONE", "g"),
  };
}

{ // the page's run cache follows the server: a restarted collector's lost run is dropped
  const cache = new Map();
  cache.set("16640|7", { id: "a", dbid: "16640", queryid: "7", state: "running" });
  cache.set("16640|8", { id: "b", dbid: "16640", queryid: "8", state: "done" });
  cache.set("99|7", { id: "c", dbid: "99", queryid: "7", state: "running" });
  A.qpmApplyExplainResponse(cache, "16640", ["7", "8"], { run: null, active: null });
  out.cacheAfterRestart = Array.from(cache.keys());
  A.qpmApplyExplainResponse(cache, "16640", ["7"], { run: { id: "d", dbid: "16640", queryid: "7", state: "done" },
    active: { id: "e", dbid: "5", queryid: "1", state: "running" } });
  out.cacheAfterAnswer = Array.from(cache.entries()).map(([k, v]) => k + "=" + v.id).sort();
}
out.scope = [
  A.qpmViewScopeKey("7", false, null), A.qpmViewScopeKey("7", true, "app"), A.qpmViewScopeKey("7", true, null),
];
{
  const many = Array.from({ length: 250 }, (_, k) => String(k));
  const ids = A.qpmRunQueryIds(many, { ok: true, queryid: "240" });
  out.runIds = { first: ids[0], len: ids.length, hasTarget: ids.indexOf("240") >= 0, dupFree: new Set(ids).size === ids.length };
}
out.match.unknown = out.match.unknown && { index: out.match.unknown.index, group: out.match.unknown.group };

out.seq = {
  read: A.qpmExplainSequence({ kind: "read", role: 'o"dd' }, A.qpmExplainOptions(false, true), 30),
  write: A.qpmExplainSequence({ kind: "write", role: null, userid: "16384" }, ["ANALYZE"], 5),
};
out.summary = A.qpmExplainSummary([
  "Index Scan using t_pkey on t  (actual time=0.1..0.2 rows=1 loops=1)",
  "  Storage Table Read Requests: 1",
  "  Storage Table Rows Scanned: 1",
  "Planning Time: 0.040 ms",
  "Execution Time: 41.231 ms",
  "Storage Read Requests: 1",
  "Storage Rows Scanned: 100",
  "Storage Write Requests: 50",
  "Peak Memory Usage: 64 kB",
].join("\n"));
out.options = [A.qpmExplainOptions(false, false), A.qpmExplainOptions(true, false), A.qpmExplainOptions(false, true)];
out.clamp = [A.qpmClampTimeout("abc"), A.qpmClampTimeout(0), A.qpmClampTimeout(9999), A.qpmClampTimeout("45")];
{ // what the strip and the banner show, in every state
  const ready = { ok: true, recordedMaxMs: 221.4, node: "n1:5433", role: "app_user", userid: "16384", paramsText: "$1 = '82'" };
  const bad = { ok: false, reason: "no values" };
  const on = { available: true }, off = { available: false, reason: "switched off here" };
  const run = (state, extra) => Object.assign({ id: "r", state, node: "n1:5433", role: "app_user",
    started_utc: "2026-09-25T15:20:00Z", finished_utc: "2026-09-25T15:20:09Z", recorded_max_ms: 106 }, extra || {});
  const cases = {
    ready: [null, ready, on], off: [null, ready, off], unreplayable: [null, bad, on], unknownAvail: [null, ready, null],
    running: [run("running", { run_s: 12.4 }), ready, on],
    done: [run("done", { execution_ms: 16.98 }), ready, on],
    slower: [run("done", { execution_ms: 530 }), ready, on],
    timeout: [run("timeout"), ready, on], cancelled: [run("cancelled"), ready, on], error: [run("error"), ready, on],
    doneButOff: [run("done", { execution_ms: 16.98 }), ready, off],
  };
  out.strip = {};
  out.banner = {};
  Object.keys(cases).forEach((k) => {
    out.strip[k] = A.qpmExplainStripModel(...cases[k]);
    out.banner[k] = A.qpmExplainBannerModel(...cases[k]);
  });
}
process.stdout.write(JSON.stringify(out));
"""

PARAMS = [
    "$1 = '82', $2 = 'default'",
    "$1 = 'it''s', $2 = NULL",
    "",
    "$1 = ''",
    "$1 = '?', $2 = '?'",
    "$1 = 'x'', $2 = ''y'",
    "$1 = 'é''ü', $2 = '日本'",
    "$1 = 'a",
    "$2 = 'a'",
    "$1 = 'a',",
    "$1 = 'a', ",
    "$1 = 'a', $1 = 'b'",
    "$1 = 'a' , $2 = 'b'",
    "$1 = a",
    "$1 = '" + "a" * 248 + "'",  # 255 bytes: possibly cut
    "$1 = '" + "a" * 247 + "'",
    "$1 = '" + "\u00e9" * 124 + "'",  # 255 bytes in multibyte characters
    "$\u0661 = 'a'",
    "$1 = 'a', $\u0662 = 'b'",
]

SQL = [
    "select * from t where a = $1 and b = '$2' and c = $2 /* $3 /* $4 */ $5 */ -- $6\n and d = foo$7"
    " and e = $$ $8 $$ and f = $tag$ $9 $tag$ and g = E'\\' $11' and h = \"$12\" and i = $10",
    "select $1, $2",
    "where x = $10 and y = $1",
    "U&'$1' $2",
    "e'\\'$1' $2",
    "name'$1' $2",
    "-- $1\n$2",
    "/* c */ ( update t set a = $1 )",
    "with x as (select $1) select * from x",
    "\u00fcber$1 = $2",
    "\u20ac$1 = $2",
    "select \U00020000$1, $2",
    "select $\u0661, $1",
    "",
]

BIND = [
    ["where (oe.id, oe.scope) = ($1,$2)", "$1 = '82', $2 = 'default'"],
    ["select 1", None],
    ["where a = $1", None],
    ["where a = $1 and b = $2", "$1 = '5'"],
    ["where a = $1", "$1 = '5', $2 = '6'"],
    ["select 1", "$1 = '5'"],
    ["where a = $1", "$1 = '?'"],
    ["where a = $2", "$1 = 'x', $2 = 'y'"],
]

SIDE_EFFECT = [
    "select pg_terminate_backend(pid) from pg_stat_activity where usename = $1",
    "SELECT pg_catalog.PG_CANCEL_BACKEND($1)",
    'select "setval"($1, 1)',
    "select dblink_exec($1, $2)",
    "select * from t where note = $1 -- pg_terminate_backend(",
    "select my_setval($1)",
    "select nextval($1)",
    "select \u00e9setval(1)",
    "select \U00020000setval(1)",
    "select yb_pg_stat_plans_reset(null, null, null, null)",
    "select pg_stat_reset ()",
]

CLAMP = ["", "  ", "abc", None, "2.5", " 7 ", "0x10", "1_000", "inf", "1e2", "-3", "45", 12.4, True, "600.4", "9999"]

KIND = [
    ["select 1", None],
    ["UPDATE t SET a = 1", None],
    ["  (select 1)", None],
    ["with u as (update t set a = 1 returning *) select * from u", '{"Node Type": "ModifyTable", "Operation": "Update"}'],
    ["select * from t for update", "LockRows  (cost=1..2)\n  ->  Seq Scan on t"],
    ["select * from t", "  ->  Update on t"],
    ["call p()", None],
    ["values (1)", None],
    ["", None],
    ["\u00a0select 1", None],
    ["\ufeffselect 1", None],
    ["\x85select 1", None],
    ["\t\v\f(select 1)", None],
    ["<insufficient privilege>", None],
]


def _py_values(d):
    return {str(k): v for k, v in d.items()}


@unittest.skipUnless(shutil.which("node"), "node not installed")
class ViewerExplainTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = _doc()
        payload = json.dumps({"params": PARAMS, "sql": SQL, "bind": BIND, "kind": KIND, "doc": cls.doc,
                              "sideEffect": SIDE_EFFECT, "clamp": CLAMP})
        res = subprocess.run(
            ["node", "-e", HARNESS],
            input=payload,
            capture_output=True,
            text=True,
            env=dict(os.environ, APP_JS=APP_JS),
            timeout=60,
        )
        if res.returncode != 0:
            raise AssertionError("node harness failed:\n" + res.stderr)
        cls.out = json.loads(res.stdout)

    def test_parameter_parsing_matches_the_server(self):
        for text, js in zip(PARAMS, self.out["params"]):
            try:
                py = {"ok": _py_values(X.parse_param_text(text))}
            except ValueError:
                py = {"err": True}
            self.assertEqual(js["parsed"], py, text)
            self.assertEqual(js["problem"], X.param_text_problem(text), text)

    def test_placeholder_scan_and_inlining_match_the_server(self):
        values = {1: "v1", 2: "it's", 10: None}
        for sql, js in zip(SQL, self.out["sql"]):
            self.assertEqual(js["refs"], sorted(X.placeholders(sql)), sql)
            self.assertEqual(js["inlined"], X.inline_literals(sql, values), sql)
            self.assertEqual(js["verb"], X._first_verb(sql), sql)

    def test_side_effects_refusals_and_timeouts_match_the_server(self):
        for sql, js in zip(SQL + SIDE_EFFECT, self.out["sideEffect"]):
            self.assertEqual(js, X.side_effect_call(sql), sql)
        for (sql, _), js in zip(KIND, self.out["notReplayable"]):
            self.assertEqual(js, X.not_replayable_reason(sql, X.statement_kind(sql, None)[1]), sql)
        for v, js in zip(CLAMP, self.out["clamp2"]):
            self.assertEqual(js, X.clamp_timeout(v), repr(v))
        self.assertEqual(self.out["clamp2"][:2], [30, 30])  # an emptied box is the default, never 1 s

    def test_a_huge_bulk_insert_does_not_throw(self):
        self.assertIsNone(self.out["huge"]["err"])
        self.assertEqual(self.out["huge"]["refs"], 130000)

    def test_bind_and_kind_match_the_server(self):
        for (sql, text), js in zip(BIND, self.out["bind"]):
            values, why = X.bind_values(sql, text)
            self.assertEqual((js["values"], js["error"]), (values, why), sql)
        for (sql, plan), js in zip(KIND, self.out["kind"]):
            kind, label = X.statement_kind(sql, plan)
            self.assertEqual((js["kind"], js["label"]), (kind, label), sql)

    def test_target_is_the_slowest_execution_that_can_be_replayed(self):
        t = self.out["target"]["q7"]
        # n2 recorded the slowest (8 ms) but its values are redacted: use n1's 5 ms.
        self.assertTrue(t["ok"])
        self.assertEqual((t["node"], t["recordedMaxMs"], t["slowestMs"]), ("n1:5433", 5, 8))
        self.assertIn("show_max_exec_params", t["skippedReason"])
        self.assertEqual((t["role"], t["kind"], t["hasParams"]), ("app_user", "read", True))
        fam = self.out["target"]["family"]
        self.assertEqual((fam["queryid"], fam["kind"], fam["label"]), ("8", "write", "UPDATE"))
        self.assertIsNone(fam["skippedReason"])
        # Without a database filter the 50 ms execution is in a dropped database: skipped.
        self.assertEqual(self.out["target"]["anyDb"]["dbid"], "16640")
        self.assertIsNone(self.out["target"]["none"])
        bad = self.out["target"]["unreplayable"]
        self.assertFalse(bad["ok"])
        self.assertIn("show_max_exec_params", bad["reason"])
        fx = self.out["target"]["sideEffect"]
        self.assertFalse(fx["ok"])
        self.assertEqual(fx["reason"], X.side_effect_reason("pg_terminate_backend"))
        skip = self.out["target"]["skipsBadRows"]
        # The 990 ms row has no role and the 980 ms one no plan: neither is offered.
        self.assertEqual((skip["ok"], skip["recordedMaxMs"], skip["userid"]), (True, 5, "16384"))
        self.assertIn("which role ran", skip["skippedReason"])

    def test_the_server_resolves_the_pages_pick_to_what_the_dialog_shows(self):
        n2 = self.out["target"]["onN2"]
        self.assertEqual((n2["node"], n2["sqlDisplay"]), ("n2:5433", "/* n2 */ select * from t where id = '21'"))
        two = self.out["target"]["twoRoles"]
        self.assertEqual((two["userid"], two["role"], two["sqlDisplay"][-4:]), ("16385", "report_user", "'99'"))
        docs = {"onN2": self.out["docN2"], "twoRoles": self.out["docTwoRoles"]}
        for key in ("q7", "family", "anyDb", "onN2", "twoRoles"):
            t = self.out["target"][key]
            srv, why = X.resolve_target(
                docs.get(key, self.doc),
                queryid=t["queryid"], planid=t["planid"], plan_ref=t["plan_ref"],
                dbid=t["dbid"], node=t["node"], userid=t["userid"],
            )
            self.assertIsNone(why, key)
            self.assertEqual(srv["statement"], t["statement"], key)
            self.assertEqual(srv["sql_display"], t["sqlDisplay"], key)
            self.assertEqual((srv["kind"], srv["label"], srv["role"]), (t["kind"], t["label"], t["role"]), key)
            self.assertEqual(srv["params_text"], t["paramsText"], key)

    def test_plan_match_is_by_shape_not_costs(self):
        m = self.out["match"]
        self.assertEqual(m["swapped"], {"index": 1, "planid": "2"})
        self.assertEqual(m["unknown"], {"index": -1, "group": None})
        self.assertIsNone(m["noPlan"])
        self.assertIsNone(m["textPlans"])  # nothing comparable: no false "not recorded" warning
        self.assertEqual(m["sigA"], "0:Nested Loop/Inner// 1:Index Scan//a/a_pkey 1:Index Scan//b/b_pkey")

    def test_result_says_whether_it_is_the_plan_the_slowest_execution_ran(self):
        n = self.out["replayNote"]
        self.assertIsNone(n["none"])
        self.assertEqual(n["sameAsRan"], {"warn": False, "text":
            "Same plan shape as recorded planid SLOW \u2014 not in use lately. It is the plan the slowest execution ran."})
        self.assertEqual(n["notRan"]["text"],
            "Same plan shape as recorded planid FAST \u2014 the fastest recorded plan. "
            "Not the plan the slowest execution ran (planid SLOW).")
        # The slowest ran FAST's custom-plan twin: that is the same plan.
        self.assertTrue(n["ranVariant"]["text"].endswith("It is the plan the slowest execution ran."))
        # planid and plan_ref both name the variant; one alone is not it.
        self.assertNotIn("slowest execution ran", n["samePlanidOtherText"]["text"])
        self.assertTrue(n["notRecorded"]["warn"])
        self.assertTrue(n["notRecorded"]["text"].startswith(
            "The planner chose a plan QPM has not recorded for this statement; the slowest execution ran planid SLOW."))
        self.assertTrue(n["notRecordedRanUnknown"]["text"].startswith(
            "The planner chose a plan QPM has not recorded for this statement. The replay"))

    def test_dialog_shows_exactly_what_runs(self):
        read = self.out["seq"]["read"]
        self.assertEqual(read[0], "SET statement_timeout = '30s';")
        self.assertEqual(read[1], "BEGIN READ ONLY;")
        self.assertTrue(read[2].startswith('SET LOCAL ROLE "o""dd";'))
        self.assertEqual(read[3], "SET LOCAL statement_timeout = '30s';")
        self.assertTrue(read[4].startswith("SET LOCAL yb_disable_transactional_writes = off;"))
        self.assertEqual(read[6], "EXPLAIN (ANALYZE, DIST, DEBUG) \u2026;")
        self.assertEqual(read[-1], "ROLLBACK;")
        write = self.out["seq"]["write"]
        self.assertEqual(write[1], "BEGIN;")
        self.assertIn("<role oid 16384>", write[2])
        self.assertEqual(write[3], "SET LOCAL statement_timeout = '5s';")

    def test_summary_reads_only_the_statement_totals(self):
        self.assertEqual(self.out["summary"], [
            ["Execution Time", "41.231 ms"],
            ["Planning Time", "0.040 ms"],
            ["Storage Read Requests", "1"],
            ["Storage Rows Scanned", "100"],
            ["Storage Write Requests", "50"],
            ["Peak Memory Usage", "64 kB"],
        ])

    def test_run_cache_follows_the_server(self):
        self.assertEqual(self.out["cacheAfterRestart"], ["99|7"])  # other databases untouched
        self.assertEqual(self.out["cacheAfterAnswer"], ["16640|7=d", "5|1=e", "99|7=c"])

    def test_view_scope_and_run_ids(self):
        self.assertEqual(self.out["scope"], ["7|query", "7|family|app", "7|family|"])
        self.assertEqual(self.out["runIds"], {"first": "240", "len": 200, "hasTarget": True, "dupFree": True})

    def test_strip_states(self):
        st = self.out["strip"]
        self.assertEqual(st["ready"]["button"], {"label": "Explain analyze", "action": "open", "muted": False, "running": False})
        self.assertEqual(st["ready"]["parts"], [
            ["k", "replays"], ["v", "the slowest execution, 221 ms on n1:5433"], ["quiet", "as app_user"], ["mono", "$1 = '82'"]])
        self.assertEqual((st["ready"]["link"], st["ready"]["muted"]), (None, False))
        self.assertEqual(st["unknownAvail"]["button"]["muted"], False)  # not asked yet: don't look disabled
        for k, why in (("off", "switched off here"), ("unreplayable", "Cannot be replayed: no values")):
            self.assertTrue(st[k]["muted"] and st[k]["button"]["muted"], k)
            self.assertEqual(st[k]["parts"], [["quiet", why]], k)
            self.assertIsNone(st[k]["link"], k)
        self.assertEqual(st["running"]["button"], {"label": "Running\u2026", "action": "show", "muted": False, "running": True})
        self.assertEqual(st["running"]["parts"], [["k", "on"], ["v", "n1:5433 as app_user"]])
        self.assertEqual(st["running"]["link"], "Show progress \u2192")
        self.assertEqual(st["done"]["button"]["label"], "Run again")
        self.assertEqual(st["done"]["parts"], [
            ["k", "last run"], ["v", "16.98 ms"], ["quiet", "6.2\u00d7 faster than the recorded 106 ms"], ["quiet", "15:20:09 UTC"]])
        self.assertEqual(st["done"]["link"], "Show result \u2192")
        self.assertIn(["quiet", "5.0\u00d7 slower than the recorded 106 ms"], st["slower"]["parts"])
        for k, word in (("timeout", "timed out"), ("cancelled", "cancelled"), ("error", "failed")):
            self.assertEqual(st[k]["parts"][:2], [["k", "last run"], ["v", word]], k)
        # A result stays reachable when the collector has since turned explain off.
        self.assertEqual((st["doneButOff"]["muted"], st["doneButOff"]["button"]["muted"], st["doneButOff"]["link"]),
                         (False, True, "Show result \u2192"))

    def test_banner_states(self):
        b = self.out["banner"]
        self.assertEqual((b["ready"]["label"], b["ready"]["muted"], b["ready"]["last"]), ("Explain analyze", False, None))
        self.assertIn("(221 ms)", b["ready"]["title"])
        self.assertEqual((b["off"]["muted"], b["off"]["title"]), (True, "switched off here"))
        self.assertEqual(b["unreplayable"]["title"], "Cannot be replayed: no values")
        self.assertEqual((b["running"]["label"], b["running"]["running"], b["running"]["last"]), ("Running \u00b7 12 s", True, None))
        self.assertEqual(b["done"]["last"], "Last run 16.98 ms \u00b7 15:20:09 UTC \u2192")
        self.assertEqual(b["timeout"]["last"], "Last run timed out \u00b7 15:20:09 UTC \u2192")
        self.assertEqual(b["error"]["last"], "Last run failed \u00b7 15:20:09 UTC \u2192")

    def test_options_and_timeout(self):
        self.assertEqual(self.out["options"], [["ANALYZE"], ["ANALYZE", "DIST"], ["ANALYZE", "DIST", "DEBUG"]])
        self.assertEqual(self.out["clamp"], [30, 1, 600, 45])


if __name__ == "__main__":
    unittest.main()
