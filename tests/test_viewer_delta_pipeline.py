"""Viewer delta pipeline (src/ybtop/web/app.js): per-queryid delta -> template collapse -> derived.

Runs the real browser functions under node; skipped when node is not installed. Functions are
lifted out of the app.js IIFE by name (two-space indented, ending at a line containing only `  }`),
so a reformat of those functions means updating the lifter, not the assertions.

Run:  python -m unittest discover -s tests
"""

import json
import os
import shutil
import subprocess
import unittest

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
const cb = (n) => { const a = src.indexOf(`\n  const ${n} =`); return src.slice(a, src.indexOf(";\n", a) + 2); };
const norm = src.slice(src.indexOf("const HIST_REWRITE_COMMENT_RE"), src.indexOf("function queryTemplateKey("));
const NAMES = ["queryTemplateKey", "ycqlPreparedTruthy", "deltaSrcFromRowFallback", "statementMergeKey",
  "pgStatDeltaRowHasActivity", "deltaPgStatMergedRows", "collapseStatementsByTemplate",
  "snapshotIntervalSeconds", "withPgStatDeltaDerivedRows", "statementRowAshLink"];
const A = new Function(["let mergeSimilarSql = true;", cb("PG_STAT_DOCDB_KEYS"), norm, ...NAMES.map(fn),
  "return {" + NAMES.join(",") + "};"].join("\n"))();

const T0 = "2026-01-01T00:00:00Z", T1 = "2026-01-01T00:01:00Z";           // 60 s window
const T = "SELECT a FROM t WHERE id = $1 -- ";                            // per-call tag differs per node
const cum = (queryid, query, calls, ms, seeks, extra) => ({ queryid, query, calls, total_ms: ms,
  mean_ms: calls ? ms / calls : 0, dbname: "db", docdb_seeks_per_call: calls ? seeks / calls : 0,
  _deltaSrc: { calls, total_exec_time: ms, doc: { docdb_seeks: seeks } }, ...(extra || {}) });
// The grouped delta path as the panels run it: per-queryid delta, derive, collapse, filter, derive.
const grouped = (cur, prev) => A.withPgStatDeltaDerivedRows(
  A.collapseStatementsByTemplate(A.withPgStatDeltaDerivedRows(A.deltaPgStatMergedRows(cur, prev), T0, T1))
    .filter(A.pgStatDeltaRowHasActivity), T0, T1);
const out = {};
{ // two members grow: exact totals, derived fields idempotent under a second derive pass
  const prev = [cum("q1", T + "r1", 1000, 2000, 3000), cum("q2", T + "r2", 500, 1000, 500)];
  const cur = [cum("q1", T + "r1", 1600, 3200, 4800), cum("q2", T + "r2", 800, 1600, 800)];
  const rows = grouped(cur, prev); const g = rows[0]; const again = A.withPgStatDeltaDerivedRows(rows, T0, T1)[0];
  out.growth = { rows: rows.length, calls: g.calls, calls_per_sec: g.calls_per_sec, total_ms: g.total_ms,
    time_pct: g.time_pct, mean_ms: g.mean_ms, seeks_per_call: g.docdb_seeks_per_call,
    seeks_total: g._deltaSrc.doc.docdb_seeks, members: g._tmpl_member_count, primary: g._tmpl_primary_queryid,
    idempotent: again.calls_per_sec === g.calls_per_sec && again.time_pct === g.time_pct && again.total_ms === g.total_ms };
}
{ // counters reset between snapshots: every member negative, row still shown, link kept, ratios 0
  const prev = [cum("q1", T + "r1", 1000, 2000, 3000), cum("q2", T + "r2", 500, 1000, 500)];
  const cur = [cum("q1", T + "r1", 10, 20, 30), cum("q2", T + "r2", 5, 10, 5)];
  const g = grouped(cur, prev)[0];
  out.reset = { calls: g.calls, mean_ms: g.mean_ms, seeks_per_call: g.docdb_seeks_per_call,
    primary: g._tmpl_primary_queryid, link: A.statementRowAshLink(g) };
}
{ // members cancel exactly: no row (caller shows its "No Δ row" fallback)
  const prev = [cum("q1", T + "r1", 1000, 2000, 3000), cum("q2", T + "r2", 500, 1000, 500)];
  const cur = [cum("q1", T + "r1", 1500, 3000, 4500), cum("q2", T + "r2", 0, 0, 0)];
  out.cancel = { rows: grouped(cur, prev).length };
}
{ // ycql: is_prepared re-attached to delta rows survives the collapse (any member prepared)
  const prev = [cum("q1", T + "r1", 100, 200, 0, { is_prepared: false }), cum("q2", T + "r2", 100, 200, 0, { is_prepared: true })];
  const cur = [cum("q1", T + "r1", 150, 300, 0, { is_prepared: false }), cum("q2", T + "r2", 150, 300, 0, { is_prepared: true })];
  const base = A.deltaPgStatMergedRows(cur, prev).map((r) => ({ ...r, is_prepared: cur.find((c) => c.queryid === r.queryid).is_prepared }));
  out.ycql = { is_prepared: A.collapseStatementsByTemplate(base)[0].is_prepared };
}
{ // cumulative mode (no prior snapshot): collapse of cumulative rows
  const c = A.collapseStatementsByTemplate([cum("q1", T + "r1", 1000, 2000, 3000), cum("q2", T + "r2", 500, 1000, 500)])[0];
  out.cumulative = { calls: c.calls, total_ms: c.total_ms, mean_ms: c.mean_ms, seeks_per_call: c.docdb_seeks_per_call, primary: c._tmpl_primary_queryid };
}
process.stdout.write(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "node not installed")
class ViewerDeltaPipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run = subprocess.run(["node", "-e", HARNESS], capture_output=True, text=True, encoding="utf-8",
                             env={**os.environ, "APP_JS": APP_JS}, check=True)
        cls.out = json.loads(run.stdout)

    def test_growth_exact_totals_and_idempotent_derive(self):
        g = self.out["growth"]
        self.assertEqual(g["rows"], 1)
        self.assertEqual((g["calls"], g["calls_per_sec"], g["total_ms"], g["time_pct"], g["mean_ms"]), (900, 15, 1800, 100, 2))
        self.assertEqual((g["seeks_total"], g["seeks_per_call"]), (2100, 2.33))   # exact, not rounded per-member
        self.assertEqual((g["members"], g["primary"]), (2, "q1"))
        self.assertTrue(g["idempotent"])

    def test_reset_keeps_link_and_zero_ratios(self):
        r = self.out["reset"]
        self.assertEqual(r["calls"], -1485)
        self.assertEqual((r["mean_ms"], r["seeks_per_call"]), (0, 0))
        self.assertIsNotNone(r["primary"])
        self.assertTrue(r["link"] and r["link"]["canonicalize"])

    def test_cancelling_members_yield_no_row(self):
        self.assertEqual(self.out["cancel"]["rows"], 0)

    def test_ycql_is_prepared_survives_collapse(self):
        self.assertIs(self.out["ycql"]["is_prepared"], True)

    def test_cumulative_collapse(self):
        c = self.out["cumulative"]
        self.assertEqual((c["calls"], c["total_ms"], c["mean_ms"], c["seeks_per_call"], c["primary"]), (1500, 3000, 2, 2.33, "q1"))


if __name__ == "__main__":
    unittest.main()
