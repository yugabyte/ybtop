"""YCQL ASH banner is_prepared (src/ybtop/web/app.js).

deltaPgStatMergedRows() does not carry is_prepared, so in delta mode the banner re-attaches it from
the current merged rows before folding by template (as the YCQL Top 25 does). Runs the real banner
function under node with only its DOM sinks stubbed; skipped when node is not installed. Functions
are lifted out of the app.js IIFE by name (two-space indented, ending at a line containing only
`  }`), so a reformat of those functions means updating the lifter, not the assertions.

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
// Banner metric rows are collected rather than rendered; only the DOM sinks are stubbed.
const STUBS = `
  const rowsOut = [];
  function ashBannerMetricRow(noteEl, label, value) { rowsOut.push([label, value]); }
  function ashSnapshotClusterNodeCount() { return 2; }
  function statementCallsContributorsPerNodeMap() { return new Map(); }
  function summarizeAshNodeLoadPct() { return {}; }
  function appendAshBannerCallsDistributionRow() {}
  function formatPgStatPerCallMetric(v) { return String(v); }`;
const NAMES = ["normQid", "pgStatPerNodeHasRowsColumn", "ycqlPreparedTruthy", "mergeYcqlStatements",
  "formatYcqlPrepared", "pickMergedPgStatRowForQueryId", "statementMergeKey", "deltaSrcFromRowFallback",
  "deltaPgStatMergedRows", "pgStatDeltaRowHasActivity", "snapshotIntervalSeconds",
  "withPgStatDeltaDerivedRows", "withPgStatTimePercent", "queryTemplateKey", "collapseStatementsByTemplate",
  "statementRowMatchesCanonicalFamily", "formatPgStatMsTwoDecimals", "appendAshScopedStatementSourceLines"];
const A = new Function(["let mergeSimilarSql = true;", cb("PG_STAT_DOCDB_KEYS"), norm, STUBS, ...NAMES.map(fn),
  "return { run: appendAshScopedStatementSourceLines, rowsOut, normalizeQueryTemplate, mergeYcqlStatements };"].join("\n"))();

const Q = "SELECT a FROM ks.t WHERE id = ? -- ";   // per-node route tag differs; one template
const perNode = (calls) => ({
  "10.0.0.1:9042": [{ queryid: 101, query: Q + "r1", calls, total_time: 2 * calls, is_prepared: false }],
  "10.0.0.2:9042": [{ queryid: 102, query: Q + "r2", calls, total_time: 2 * calls, is_prepared: "t" }],
});
const doc = { generated_at_utc: "2026-01-01T00:01:00Z" };       // 60 s window
const prevDoc = { generated_at_utc: "2026-01-01T00:00:00Z" };
const family = { source: "ycql", template: A.normalizeQueryTemplate(Q + "r1") };
const out = {};
[["family", 101, family], ["plain_unprepared", 101, null], ["plain_prepared", 102, null]].forEach(
  ([name, qF, fam]) => {
    A.rowsOut.length = 0;
    const found = A.run(null, doc, prevDoc, qF, {}, "ycql_stat_statements", perNode(150), perNode(100),
      A.mergeYcqlStatements, { matchDbname: false, showIsPrepared: true, canonicalFamily: fam });
    const m = Object.fromEntries(A.rowsOut);
    out[name] = { found, is_prepared: m.is_prepared, calls_per_sec: m["calls/s"], total_time: m["total time"] };
  }
);
process.stdout.write(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "node not installed")
class YcqlAshBannerIsPreparedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run = subprocess.run(["node", "-e", HARNESS], capture_output=True, text=True, encoding="utf-8",
                             env={**os.environ, "APP_JS": APP_JS}, check=True)
        cls.out = json.loads(run.stdout)

    def test_family_banner_ors_is_prepared_across_members(self):
        self.assertTrue(self.out["family"]["found"])
        self.assertEqual(self.out["family"]["is_prepared"], "true")

    def test_single_statement_banner_keeps_each_flag(self):
        self.assertEqual(self.out["plain_unprepared"]["is_prepared"], "false")
        self.assertEqual(self.out["plain_prepared"]["is_prepared"], "true")

    def test_remap_leaves_metrics_untouched(self):
        f = self.out["family"]
        self.assertEqual((f["calls_per_sec"], f["total_time"]), ("1.67", "200.00 (ms) [100.00%]"))


if __name__ == "__main__":
    unittest.main()
