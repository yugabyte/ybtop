"""QPM plan panel logic (src/ybtop/web/app.js): per-node fold -> plan grouping -> verdict.

Runs the real browser functions under node; skipped when node is not installed. Functions are
lifted out of the app.js IIFE by name (two-space indented, ending at a line containing only `  }`),
so a reformat of those functions means updating the lifter, not the assertions.

The cases that matter here and are not reachable from live data:
  - avg_exec_time must recombine call-weighted across nodes, not as a mean of means
  - one planid can carry two plan texts (AND-clause order) -> two groups
  - two planids can carry one plan text (costs differ, omitted unless verbose) -> flagged
  - a fastest plan that is no longer used is a regression, not a win

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
const cb = (n) => {
  const a = src.indexOf(`\n  const ${n} =`);
  if (a < 0) throw new Error("const not found: " + n);
  return src.slice(a, src.indexOf(";\n", a) + 2);
};
const NAMES = ["qpmPlanShapeSignature", "qpmScopeQueryIds", "aggregateQpmPlans",
  "qpmPlanVerdict", "qpmVerdictHeadline", "qpmFmtMs", "qpmParamsHaveValues",
  "qpmPanelMode", "qpmPinEffectWarnings", "qpmCollectionOffState", "qpmCollectionHeaderLabel", "qpmPinRowMode",
  "qpmDbidsForName", "qpmDatabaseLabel", "qpmRowQueryIds", "qpmPlanIndex", "annotateRowsWithQpmPlans",
  "withQpmPlansColumn", "qpmEffectivePinned", "qpmPanelDbids", "qpmPinButtonStates",
  "qpmMaskPlanParams", "qpmPlanTwinKey", "qpmTwinKeysFor", "qpmVariantsNote", "qpmCallsText"];
const A = new Function([
  cb("QPM_PLAN_ACTIVE_WINDOW_MS"), cb("QPM_LOW_CONFIDENCE_CALLS"), cb("QPM_LOW_CONFIDENCE_SHARE"),
  cb("QPM_TRACK_MODES_ON"), cb("QPM_PLANS_COL"),
  cb("QPM_TYPE_NAME_SRC"), cb("QPM_MASK_CONST"), cb("QPM_MASK_ARRAY"), cb("QPM_MASK_CAST"),
  ...NAMES.map(fn), "return {" + NAMES.join(",") + ", QPM_PLANS_COL};"].join("\n"))();

const NOW = Date.parse("2026-09-17T12:00:00Z");
const iso = (msAgo) => new Date(NOW - msAgo).toISOString();
const MIN = 60000, HOUR = 3600000;

// One QPM row as the collector writes it.
const row = (queryid, planid, ref, calls, avg, lastAgo, extra) => ({
  queryid, planid, plan_ref: ref, calls, avg_exec_time: avg, max_exec_time: avg * 10,
  avg_est_cost: 1, first_used: iso(7 * 24 * HOUR), last_used: iso(lastAgo), ...(extra || {}) });
const section = (perNode, plans) => ({ supported: true, per_node: perNode, plans: plans || {} });
const verdictOf = (perNode, qids) => {
  const g = A.aggregateQpmPlans(section(perNode), new Set(qids || ["q1"]));
  const v = A.qpmPlanVerdict(g);
  return { groups: g, v, headline: A.qpmVerdictHeadline(v) };
};
const out = {};

{ // Pin -> Remove -> Pin: each redraw sets every button, so Remove is never left dead
  const ready = (pinned, gucsOn) => ({ available: true, pinned,
    prereq: { enable_hint_table: gucsOn, use_query_id_for_hinting: gucsOn } });
  out.pinButtons = {
    pinned: A.qpmPinButtonStates(ready(true, true)),
    unpinned: A.qpmPinButtonStates(ready(false, true)),
    gucsOff: A.qpmPinButtonStates(ready(false, false)),
    installable: A.qpmPinButtonStates({ available: false, installable: true }),
    unavailable: A.qpmPinButtonStates({ available: false, installable: false, reason: "off" }),
  };
}

{ // no database named: prefer the live ones over a dropped database's plans
  const sec = { databases: { "16645": "app" }, per_node: { n1: [
    { queryid: "q1", dbid: "16640" }, { queryid: "q1", dbid: "16645" }, { queryid: "q2", dbid: "16385" } ] } };
  const ids = (x) => (x ? Array.from(x).sort() : null);
  out.panelDbids = {
    named: ids(A.qpmPanelDbids(sec, "app", new Set(["q1"]))),
    liveFirst: ids(A.qpmPanelDbids(sec, null, new Set(["q1"]))),
    onlyDropped: A.qpmPanelDbids(sec, null, new Set(["q2"])),
    noMap: A.qpmPanelDbids({ per_node: sec.per_node }, null, new Set(["q1"])),
  };
}

{ // "slowest params" belong to the slowest node's execution, not the first node read
  const g = A.aggregateQpmPlans(section({
    n1: [row("q1", "P", "rP", 10, 1.0, 0, { max_exec_time: 216, max_exec_time_params: "$1 = '573'" })],
    n3: [row("q1", "P", "rP", 10, 1.0, 0, { max_exec_time: 221, max_exec_time_params: "$1 = '82'" })],
    n2: [row("q1", "P", "rP", 10, 1.0, 0, { max_exec_time: 207, max_exec_time_params: "$1 = '1197'" })],
  }), new Set(["q1"]));
  out.slowestParams = { max: g[0].max_exec_time, params: g[0].max_exec_time_params };
}

{ // fastest plan is also the one taking the calls -> ok, no warning
  const r = verdictOf({ n1: [row("q1", "FAST", "rF", 900, 1.0, 0), row("q1", "SLOW", "rS", 100, 3.0, 0)] });
  out.ok = { status: r.v.status, planCount: r.v.planCount, active: r.v.activeCount,
    fastest: r.v.fastest.planid, current: r.v.current.planid, headline: r.headline };
}
{ // fastest plan is live but a minority of calls run it -> underused, share quoted
  const r = verdictOf({ n1: [row("q1", "FAST", "rF", 100, 1.0, 0), row("q1", "SLOW", "rS", 900, 3.0, 0)] });
  out.underused = { status: r.v.status, fastest: r.v.fastest.planid, current: r.v.current.planid,
    share: r.v.fastestShare, ratio: r.v.slowerRatio, headline: r.headline };
}
{ // fastest plan has not run for an hour while a slower one has -> abandoned
  const r = verdictOf({ n1: [row("q1", "FAST", "rF", 100, 1.0, HOUR), row("q1", "SLOW", "rS", 900, 4.0, 0)] });
  out.abandoned = { status: r.v.status, fastestActive: r.v.fastest.active, current: r.v.current.planid,
    ratio: r.v.slowerRatio, headline: r.headline };
}
{ // one plan on record -> nothing to compare, and no scary wording
  const r = verdictOf({ n1: [row("q1", "ONLY", "rO", 500, 2.0, 0)] });
  out.single = { status: r.v.status, planCount: r.v.planCount, headline: r.headline };
}
{ // fastest by average over a handful of calls -> flagged provisional
  const r = verdictOf({ n1: [row("q1", "FAST", "rF", 10, 1.0, 0), row("q1", "SLOW", "rS", 5000, 2.0, 0)] });
  out.lowConf = { status: r.v.status, lowConfidence: r.v.lowConfidence };
}
{ // THE aggregation test: same plan, lopsided calls across nodes.
  // mean-of-means would say 50.5 ms; call-weighted says ~1.98 ms.
  const r = verdictOf({ n1: [row("q1", "P", "rP", 1000, 1.0, 0)], n2: [row("q1", "P", "rP", 10, 100.0, 0)] });
  out.weighted = { groups: r.groups.length, calls: r.groups[0].calls, avg: r.groups[0].avg_exec_time,
    nodes: r.groups[0].nodes.length, meanOfMeans: 50.5 };
}
{ // one planid, two plan texts (AND-clause order): two groups, neither hidden
  const r = verdictOf({ n1: [row("q1", "SAME", "refA", 100, 1.0, 0), row("q1", "SAME", "refB", 100, 2.0, 0)] });
  out.splitText = { groups: r.groups.length, refs: r.groups.map((g) => g.plan_ref).sort() };
}
{ // two planids, one plan text: kept separate but flagged as indistinguishable
  const r = verdictOf({ n1: [row("q1", "P1", "sameRef", 100, 1.0, 0), row("q1", "P2", "sameRef", 100, 2.0, 0)] });
  out.sharedText = { groups: r.groups.length, sameTextAs: r.groups.map((g) => g.sameTextAs.slice()) };
}
{ // the same plan text in two databases is two databases, not two planids for one text
  const r = verdictOf({ n1: [Object.assign(row("q1", "P", "sameRef", 100, 1.0, 0), { dbid: "1" }),
    Object.assign(row("q1", "P", "sameRef", 100, 2.0, 0), { dbid: "2" })] });
  out.sharedTextTwoDbs = r.groups.map((g) => g.sameTextAs.length);
}
{ // rows for other queryids must not leak into this query's plan set
  const r = verdictOf({ n1: [row("q1", "MINE", "rM", 100, 1.0, 0), row("q9", "THEIRS", "rT", 100, 0.1, 0)] });
  out.scoped = { groups: r.groups.length, fastest: r.v.fastest.planid };
}
{ // a canonical family spans several queryids; the panel must union them
  const ids = A.qpmScopeQueryIds("q1", { queryIds: new Set(["q1", "q2"]) });
  const r = verdictOf({ n1: [row("q1", "A", "rA", 100, 1.0, 0), row("q2", "B", "rB", 100, 2.0, 0)] }, ["q1", "q2"]);
  out.family = { scope: Array.from(ids).sort(), groups: r.groups.length };
}
{ // a query that simply stopped running is not a regression: both plans equally stale
  const r = verdictOf({ n1: [row("q1", "FAST", "rF", 900, 1.0, 6 * HOUR), row("q1", "SLOW", "rS", 100, 3.0, 6 * HOUR)] });
  out.allStale = { status: r.v.status, fastestActive: r.v.fastest.active };
}
{ // hints -> shape fingerprint: Set(...) boilerplate dropped, join clauses kept
  out.sig = A.qpmPlanShapeSignature(
    "/*+ IndexOnlyScan(reports reports_idx) Set(yb_enable_cbo on) Set(geqo false) MergeJoin(a b) */");
}
{ // formatting + redacted-params suppression
  out.fmt = { small: A.qpmFmtMs(0.742), mid: A.qpmFmtMs(23.54), big: A.qpmFmtMs(15035), huge: A.qpmFmtMs(61000) };
  out.params = { redacted: A.qpmParamsHaveValues("$1 = '?'"),
    multiRedacted: A.qpmParamsHaveValues("$1 = '?', $2 = '?'"),
    real: A.qpmParamsHaveValues("$1 = 'acct-42'"), empty: A.qpmParamsHaveValues("") };
}
{ // guardrail: what block each snapshot shape calls for
  const m = (q) => A.qpmPanelMode(q);
  out.mode = {
    legacy: m(null),
    unsupported: m({ supported: false, track: "all" }),
    trackNone: m({ supported: true, track: "none", collected: false }),
    trackNoneEvenIfCollected: m({ supported: true, track: "none", collected: true }),
    trackUpper: m({ supported: true, track: "NONE", collected: false }),
    trackAll: m({ supported: true, track: "all", collected: false }),
    trackTop: m({ supported: true, track: "top", collected: false }),
    collected: m({ supported: true, track: "all", collected: true }),
    trackAbsent: m({ supported: true, collected: true }),
  };
}
{ // a pinned hint does nothing unless both pg_hint_plan GUCs are on
  out.pinWarn = {
    bothOff: A.qpmPinEffectWarnings({ enable_hint_table: false, use_query_id_for_hinting: false }).length,
    tableOff: A.qpmPinEffectWarnings({ enable_hint_table: false, use_query_id_for_hinting: true }),
    qidOff: A.qpmPinEffectWarnings({ enable_hint_table: true, use_query_id_for_hinting: false }),
    bothOn: A.qpmPinEffectWarnings({ enable_hint_table: true, use_query_id_for_hinting: true }).length,
    missing: A.qpmPinEffectWarnings(null).length,
  };
}
{ // collection state when the snapshot on screen has no plans
  const st = (state, newest) => A.qpmCollectionOffState(state, newest);
  const ON = { query_plans: true, writable: true }, OFF = { query_plans: false, writable: true };
  out.offState = {
    noEndpoint: st(null, true),
    offNewest: st(OFF, true),
    offOlder: st(OFF, false),
    offReadonly: st({ query_plans: false, writable: false }, true),
    onNewest: st(ON, true),
    // The reported bug: enabled, then navigated -- the viewer is still pinned to a
    // snapshot from before the click. That must never read as "off" again.
    onOlder: st(ON, false),
    // Enabled from another viewer; this one cannot write, but must still show "on".
    onReadonly: st({ query_plans: true, writable: false }, true),
  };
  out.header = {
    on: A.qpmCollectionHeaderLabel(ON),
    off: A.qpmCollectionHeaderLabel(OFF),
    unknown: A.qpmCollectionHeaderLabel(null),
  };
}
out.pinRow = {
  none: A.qpmPinRowMode(null),
  ready: A.qpmPinRowMode({ available: true }),
  installable: A.qpmPinRowMode({ available: false, installable: true }),
  neither: A.qpmPinRowMode({ available: false, installable: false }),
  readyWins: A.qpmPinRowMode({ available: true, installable: true }),
};
{ // databases: the same queryid often has plans in several, some long dropped
  const live = "16640", dropped = "16385";
  const db = (dbid, planid, ref, calls, avg) => Object.assign(row("q1", planid, ref, calls, avg, 0), { dbid });
  const sec = { supported: true, databases: { [live]: "app", "13665": "yugabyte" },
    per_node: { n1: [db(live, "P", "rP", 900, 5.0), db(dropped, "P", "rP", 100, 1.0), db(dropped, "Q", "rQ", 50, 0.5)] } };
  const all = A.aggregateQpmPlans(sec, new Set(["q1"]));
  const scoped = A.aggregateQpmPlans(sec, new Set(["q1"]), A.qpmDbidsForName(sec, "app"));
  out.db = {
    unscopedGroups: all.length,
    samePlanTwoDbs: all.filter((g) => g.planid === "P").map((g) => g.dbid).sort(),
    scopedGroups: scoped.length,
    scopedDbid: scoped.map((g) => g.dbid),
    // without scoping, the dropped database's 0.5 ms plan would be "fastest"
    unscopedFastestDb: A.qpmPlanVerdict(all).fastest.dbid,
    scopedFastestDb: A.qpmPlanVerdict(scoped).fastest.dbid,
    emptyFilter: A.aggregateQpmPlans(sec, new Set(["q1"]), new Set()).length,
    idsForName: Array.from(A.qpmDbidsForName(sec, "app")),
    idsNoMap: A.qpmDbidsForName({}, "app"),
    idsNoName: A.qpmDbidsForName(sec, null),
    idsUnknownName: Array.from(A.qpmDbidsForName(sec, "nope")),
    labelLive: A.qpmDatabaseLabel(sec, live),
    labelDropped: A.qpmDatabaseLabel(sec, dropped),
    labelNoMap: A.qpmDatabaseLabel({}, "7"),
  };
}
{ // PGSS "plans" column: count must equal what the drilldown shows for the row
  const A_DB = "16640", B_DB = "13665";
  const r = (queryid, planid, ref, dbid) => ({ queryid, planid, plan_ref: ref, dbid, calls: 1, avg_exec_time: 1 });
  const sec = {
    databases: { [A_DB]: "app", [B_DB]: "yugabyte" },
    pinned: { [A_DB]: ["q1"] },
    per_node: {
      n1: [r("q1", "P1", "a", A_DB), r("q1", "P2", "b", A_DB), r("q1", "P3", "c", B_DB), r("q2", "P2", "b", A_DB)],
      n2: [r("q1", "P1", "a", A_DB), r("q2", "P4", "d", A_DB)],
    },
  };
  const ann = (row) => A.annotateRowsWithQpmPlans([row], sec)[0];
  const drill = (qids, db) => A.aggregateQpmPlans(sec, new Set(qids), A.qpmDbidsForName(sec, db)).length;
  out.col = {
    plain: ann({ queryid: "q1", dbname: "app" }),
    plainDrill: drill(["q1"], "app"),
    otherDb: ann({ queryid: "q1", dbname: "yugabyte" }),
    template: ann({ queryid: "tmpl text", _tmpl_queryids: ["q1", "q2"], dbname: "app" }),
    templateDrill: drill(["q1", "q2"], "app"),
    summary: ann({ query_template: "t", query_members: [{ query_id: "q2" }], dbname: "app" }),
    unknownDb: ann({ queryid: "q1", dbname: "dropped_db" }),
    noDbname: ann({ queryid: "q1" }),
    legacyNoMap: A.annotateRowsWithQpmPlans([{ queryid: "q1", dbname: "app" }], { per_node: sec.per_node })[0],
    notInQpm: ann({ queryid: "q9", dbname: "app" }),
    doesNotMutate: (() => { const row = { queryid: "q1", dbname: "app" }; A.annotateRowsWithQpmPlans([row], sec); return !("qpm_plans" in row); })(),
    colOrder: A.withQpmPlansColumn([{ key: "query" }, { key: "dbname" }, { key: "rows" }]).map((c) => c.key),
    colNoDb: A.withQpmPlansColumn([{ key: "query" }, { key: "rows" }]).map((c) => c.key),
    sortPinnedFirst: A.QPM_PLANS_COL.sortValue({ qpm_plans: 2, qpm_pinned: true }) > A.QPM_PLANS_COL.sortValue({ qpm_plans: 2, qpm_pinned: false }),
    sortCountStillWins: A.QPM_PLANS_COL.sortValue({ qpm_plans: 3, qpm_pinned: false }) > A.QPM_PLANS_COL.sortValue({ qpm_plans: 2, qpm_pinned: true }),
  };
}
{ // your own pin shows as P at once on snapshots taken before it; later ones are the truth
  const sec = { pinned: { "16640": ["q1"] } };
  const edits = new Map([
    ["16640|q2", { pinned: true, at: 100 }],   // just pinned q2
    ["16640|q1", { pinned: false, at: 100 }],  // just unpinned q1
    ["13665|-7", { pinned: true, at: 100 }],   // negative queryid, other database
  ]);
  const view = (t) => {
    const m = A.qpmEffectivePinned(sec, t, edits);
    const o = {}; m.forEach((v, k) => { o[k] = Array.from(v).sort(); }); return o;
  };
  out.overlay = {
    before: view(50),        // snapshot predates the edits: edits apply
    after: view(150),        // snapshot taken after: its own data only
    noTime: view(NaN),       // unknown snapshot time: edits apply
    noEdits: (() => { const m = A.qpmEffectivePinned(sec, 50, new Map()); return Array.from(m.get("16640")); })(),
    rowP: (() => {
      const secP = { databases: { "16640": "app" }, pinned: {}, per_node: { n1: [{ queryid: "q2", planid: "P", plan_ref: "a", dbid: "16640" }] } };
      const pinned = A.qpmEffectivePinned(secP, 50, new Map([["16640|q2", { pinned: true, at: 100 }]]));
      return A.annotateRowsWithQpmPlans([{ queryid: "q2", dbname: "app" }], secP, pinned)[0].qpm_pinned;
    })(),
  };
}
{ // parameters masked: a prepared statement's custom and generic plans read the same
  const m = A.qpmMaskPlanParams;
  out.mask = {
    typed: m("((id = '?'::bigint) AND (region = '?'::region_enum))"),
    generic: m("((id = $1) AND (region = $2))"),
    multiWord: m("(stream_at < '?'::timestamp without time zone) AND (x = $3)"),
    typmod: m("(a = '?'::character varying(20)) AND (b = '?'::numeric(10,2)) AND (c = '?'::time(3) with time zone)"),
    quoted: m("(a = '?'::\"Mixed Type\") AND (b = '?'::sch.enum_t) AND (c = '?'::double precision)"),
    arrays: m("(a = ANY ('?'::text[])) AND (b = ANY (ARRAY[$1, $2, $3, ..., $9])) AND (c <> ALL ((ARRAY[$4, $5])::text[]))"),
    bare: m("(n = ?) AND (m = '?')"),
    noOverreach: m("x = '?'::bigint AND region = $2"),
    bnl: m("(ROW(id, region) = ANY (ARRAY[ROW(a.id, a.region), ROW($1, $1025)]))"),
  };
}
{ // one plan recorded as custom + generic: one card, one plans-column count
  const H = "/*+ IndexScan(t t_pkey) Set(geqo false) */";
  const plans = {
    gen: { plan: '{"Index Cond": "((id = $1) AND (region = $2))"}', hints: H },
    cus: { plan: "{\"Index Cond\": \"((id = '?'::bigint) AND (region = '?'::region_enum))\"}", hints: H },
    newIdx: { plan: '{"Index Name": "t_new", "Index Cond": "((id = $1) AND (region = $2))"}',
      hints: "/*+ IndexScan(t t_new) Set(geqo false) */" },
    genOtherHints: { plan: '{"Index Cond": "((id = $1) AND (region = $2))"}', hints: "/*+ IndexScan(t t_pkey) Set(geqo true) */" },
    sorted: { plan: '{"Node Type": "Sort"}', hints: H },
    incr: { plan: '{"Node Type": "Incremental Sort"}', hints: H },
  };
  const sec = (rows) => ({ supported: true, databases: { "16640": "app", "16897": "other" }, plans, per_node: { n1: rows } });
  const r = (planid, ref, calls, avg, extra) => Object.assign(row("q1", planid, ref, calls, avg, 0), { dbid: "16640" }, extra || {});
  const twins = sec([r("G", "gen", 15400, 1.3), r("C", "cus", 24, 2.8)]);
  const g = A.aggregateQpmPlans(twins, new Set(["q1"]));
  out.twins = {
    groups: g.length, planid: g[0].planid, plan_ref: g[0].plan_ref, calls: g[0].calls, avg: g[0].avg_exec_time,
    variants: g[0].variants.map((x) => [x.planid, x.plan_ref, x.calls]),
    headline: A.qpmVerdictHeadline(A.qpmPlanVerdict(g)),
    column: A.annotateRowsWithQpmPlans([{ queryid: "q1", dbname: "app" }], twins)[0].qpm_plans,
    note: A.qpmVariantsNote(g[0].variants, plans),
  };
  const count = (rows) => A.aggregateQpmPlans(sec(rows), new Set(["q1"])).length;
  const column = (rows) => A.annotateRowsWithQpmPlans([{ queryid: "q1", dbname: "app" }], sec(rows))[0].qpm_plans;
  const otherIndex = [r("G", "gen", 100, 1.0), r("N", "newIdx", 100, 2.0)];
  out.notTwins = {
    otherIndex: count(otherIndex),
    otherIndexColumn: column(otherIndex),
    sameHintsOtherShape: count([r("S", "sorted", 100, 1.0), r("I", "incr", 100, 2.0)]),
    sameTextOtherHints: count([r("G", "gen", 100, 1.0), r("H", "genOtherHints", 100, 2.0)]),
    otherDatabase: count([r("G", "gen", 100, 1.0), r("C", "cus", 100, 2.0, { dbid: "16897" })]),
    noText: A.aggregateQpmPlans({ per_node: { n1: [r("G", "gen", 100, 1.0), r("C", "cus", 100, 2.0)] } }, new Set(["q1"])).length,
  };
  // same hints, different plan (Sort vs Incremental Sort): two cards, each naming the other
  const sh = A.aggregateQpmPlans(sec([r("S", "sorted", 100, 1.0), r("I", "incr", 100, 2.0), r("G", "gen", 100, 3.0),
    r("C", "cus", 5, 3.0), r("X", "sorted", 100, 1.0, { dbid: "16897" })]), new Set(["q1"]));
  out.sameHints = Object.fromEntries(sh.map((x) => [x.planid + "@" + x.dbid, x.sameHintsAs.slice().sort()]));
  // byte-identical text under two planids (verbose plans off): one card that says why
  const same = A.aggregateQpmPlans(sec([r("P1", "gen", 100, 1.0), r("P2", "gen", 300, 2.0)]), new Set(["q1"]));
  A.qpmPlanVerdict(same);
  out.sameText = { groups: same.length, planid: same[0].planid, sameTextAs: same[0].sameTextAs,
    note: A.qpmVariantsNote(same[0].variants, plans) };
}
{ // the headline hedges a "fastest" measured on a handful of calls
  out.hedge = {
    underused: verdictOf({ n1: [row("q1", "FAST", "rF", 10, 1.0, 0), row("q1", "SLOW", "rS", 5000, 2.0, 0)] }).headline,
    abandoned: verdictOf({ n1: [row("q1", "FAST", "rF", 7, 1.0, HOUR), row("q1", "SLOW", "rS", 5000, 2.0, 0)] }).headline,
    oneCall: verdictOf({ n1: [row("q1", "FAST", "rF", 1, 1.0, 0), row("q1", "SLOW", "rS", 5000, 2.0, 0)] }).headline,
  };
}
console.log(JSON.stringify(out));
"""


def _run_node():
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node not installed")
    env = dict(os.environ, APP_JS=APP_JS)
    proc = subprocess.run([node, "-e", HARNESS], capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise AssertionError("node harness failed:\n" + proc.stderr)
    return json.loads(proc.stdout)


class ViewerQpmPlansTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = _run_node()

    def test_pin_buttons_are_set_in_full_every_time(self):
        b = self.out["pinButtons"]
        self.assertEqual(b["pinned"]["remove"], {"hidden": False, "disabled": False})
        self.assertEqual(b["unpinned"]["remove"]["hidden"], True)
        self.assertEqual(b["pinned"]["pin"], {"hidden": False, "disabled": False})
        self.assertEqual((b["gucsOff"]["guc"]["enable"], b["pinned"]["guc"]["enable"]), (True, False))
        self.assertEqual((b["installable"]["install"]["hidden"], b["installable"]["pin"]["disabled"]), (False, True))
        self.assertTrue(b["unavailable"]["remove"]["hidden"] and b["unavailable"]["guc"]["hidden"])
        for state in b.values():  # every button, every time
            self.assertEqual(set(state), {"install", "pin", "remove", "guc"})

    def test_panel_prefers_live_databases(self):
        self.assertEqual(self.out["panelDbids"], {
            "named": ["16645"], "liveFirst": ["16645"], "onlyDropped": None, "noMap": None})

    def test_slowest_params_come_with_the_slowest_execution(self):
        self.assertEqual(self.out["slowestParams"], {"max": 221, "params": "$1 = '82'"})

    def test_fastest_plan_in_use_reads_ok(self):
        o = self.out["ok"]
        self.assertEqual(o["status"], "ok")
        self.assertEqual(o["planCount"], 2)
        self.assertEqual(o["active"], 2)
        self.assertEqual(o["fastest"], "FAST")
        self.assertEqual(o["current"], "FAST")
        self.assertIn("fastest plan is the one in use", o["headline"])

    def test_fastest_plan_serving_minority_of_calls_is_underused(self):
        o = self.out["underused"]
        self.assertEqual(o["status"], "underused")
        self.assertEqual(o["fastest"], "FAST")
        self.assertEqual(o["current"], "SLOW")
        self.assertAlmostEqual(o["share"], 0.1, places=6)
        self.assertAlmostEqual(o["ratio"], 3.0, places=6)
        self.assertIn("serves 10% of calls", o["headline"])
        self.assertIn("3.00", o["headline"])

    def test_fastest_plan_no_longer_used_is_abandoned(self):
        o = self.out["abandoned"]
        self.assertEqual(o["status"], "abandoned")
        self.assertFalse(o["fastestActive"])
        self.assertEqual(o["current"], "SLOW")
        self.assertAlmostEqual(o["ratio"], 4.0, places=6)
        self.assertIn("no longer in use", o["headline"])
        self.assertIn("4.00", o["headline"])

    def test_single_plan_states_no_change_rather_than_a_problem(self):
        o = self.out["single"]
        self.assertEqual(o["status"], "single")
        self.assertEqual(o["planCount"], 1)
        self.assertIn("no plan change recorded", o["headline"])
        self.assertNotIn("slower", o["headline"])

    def test_fastest_on_few_calls_is_marked_low_confidence(self):
        o = self.out["lowConf"]
        self.assertEqual(o["status"], "underused")
        self.assertTrue(o["lowConfidence"])

    def test_cross_node_average_is_call_weighted_not_mean_of_means(self):
        o = self.out["weighted"]
        self.assertEqual(o["groups"], 1)
        self.assertEqual(o["calls"], 1010)
        self.assertEqual(o["nodes"], 2)
        # (1.0*1000 + 100.0*10) / 1010
        self.assertAlmostEqual(o["avg"], 2000.0 / 1010.0, places=9)
        self.assertNotAlmostEqual(o["avg"], o["meanOfMeans"], places=1)

    def test_one_planid_with_two_plan_texts_stays_two_groups(self):
        o = self.out["splitText"]
        self.assertEqual(o["groups"], 2)
        self.assertEqual(o["refs"], ["refA", "refB"])

    def test_two_planids_sharing_plan_text_are_flagged(self):
        o = self.out["sharedText"]
        self.assertEqual(o["groups"], 2)
        self.assertEqual(sorted(sorted(x) for x in o["sameTextAs"]), [["P1"], ["P2"]])

    def test_same_text_in_another_database_is_not_flagged(self):
        self.assertEqual(self.out["sharedTextTwoDbs"], [0, 0])

    def test_other_queryids_do_not_leak_in(self):
        o = self.out["scoped"]
        self.assertEqual(o["groups"], 1)
        self.assertEqual(o["fastest"], "MINE")

    def test_canonical_family_unions_its_queryids(self):
        o = self.out["family"]
        self.assertEqual(o["scope"], ["q1", "q2"])
        self.assertEqual(o["groups"], 2)

    def test_query_that_stopped_running_is_not_reported_as_regression(self):
        o = self.out["allStale"]
        self.assertTrue(o["fastestActive"])
        self.assertEqual(o["status"], "ok")

    def test_shape_signature_drops_set_boilerplate(self):
        sig = self.out["sig"]
        self.assertIn("IndexOnlyScan(reports reports_idx)", sig)
        self.assertIn("MergeJoin(a b)", sig)
        self.assertNotIn("Set(", sig)
        self.assertNotIn("/*+", sig)

    def test_guardrail_picks_the_right_block_per_snapshot(self):
        m = self.out["mode"]
        self.assertEqual(m["legacy"], "legacy")
        self.assertEqual(m["unsupported"], "unsupported")
        # Tracking off must win over everything: enabling would collect empty sets.
        self.assertEqual(m["trackNone"], "tracking-off")
        self.assertEqual(m["trackNoneEvenIfCollected"], "tracking-off")
        self.assertEqual(m["trackUpper"], "tracking-off")
        self.assertEqual(m["trackAll"], "collection-off")
        self.assertEqual(m["trackTop"], "collection-off")
        self.assertEqual(m["collected"], "plans")
        # An older snapshot with no `track` field must not read as tracking-off.
        self.assertEqual(m["trackAbsent"], "plans")

    def test_pin_effect_warnings_name_each_missing_guc(self):
        w = self.out["pinWarn"]
        self.assertEqual(w["bothOff"], 2)
        self.assertEqual(w["tableOff"], ["pg_hint_plan.enable_hint_table is off"])
        self.assertEqual(
            w["qidOff"], ["pg_hint_plan.yb_use_query_id_for_hinting is off"]
        )
        self.assertEqual(w["bothOn"], 0)
        self.assertEqual(w["missing"], 0)

    def test_collection_state_follows_the_toggle_not_just_the_snapshot(self):
        o = self.out["offState"]
        self.assertEqual(o["noEndpoint"], "unavailable")
        self.assertEqual(o["offNewest"], "off")
        self.assertEqual(o["offOlder"], "off")
        self.assertEqual(o["offReadonly"], "readonly")
        self.assertEqual(o["onNewest"], "enabling")
        self.assertEqual(o["onReadonly"], "enabling")

    def test_enabled_then_navigated_never_offers_enable_again(self):
        # Regression: after enabling, every re-render on the pre-click snapshot
        # showed the Enable button, because the snapshot says "not collected".
        self.assertEqual(self.out["offState"]["onOlder"], "predates")
        self.assertNotEqual(self.out["offState"]["onOlder"], "off")

    def test_header_label_reflects_live_toggle(self):
        h = self.out["header"]
        self.assertEqual(h["on"], "disable collection")
        self.assertEqual(h["off"], "collection off \u00b7 enable")
        self.assertEqual(h["unknown"], "collection off \u00b7 enable")

    def test_pin_row_offers_install_instead_of_a_dead_end(self):
        r = self.out["pinRow"]
        self.assertEqual(r["none"], "unavailable")
        self.assertEqual(r["ready"], "ready")
        self.assertEqual(r["installable"], "installable")
        self.assertEqual(r["neither"], "unavailable")
        self.assertEqual(r["readyWins"], "ready")

    def test_same_plan_in_two_databases_is_two_groups(self):
        d = self.out["db"]
        self.assertEqual(d["unscopedGroups"], 3)
        self.assertEqual(d["samePlanTwoDbs"], ["16385", "16640"])

    def test_drilldown_database_excludes_dropped_database_plans(self):
        # Regression: a dropped database's faster plan was reported as "fastest"
        # for a query that now only runs in the live database.
        d = self.out["db"]
        self.assertEqual(d["unscopedFastestDb"], "16385")
        self.assertEqual(d["scopedGroups"], 1)
        self.assertEqual(d["scopedDbid"], ["16640"])
        self.assertEqual(d["scopedFastestDb"], "16640")
        self.assertEqual(d["emptyFilter"], 0)

    def test_database_name_mapping_and_labels(self):
        d = self.out["db"]
        self.assertEqual(d["idsForName"], ["16640"])
        self.assertIsNone(d["idsNoMap"])
        self.assertIsNone(d["idsNoName"])
        self.assertEqual(d["idsUnknownName"], [])
        self.assertEqual(d["labelLive"], "app")
        self.assertEqual(d["labelDropped"], "oid 16385 (dropped)")
        self.assertEqual(d["labelNoMap"], "oid 7")

    def test_plans_column_count_matches_the_drilldown(self):
        c = self.out["col"]
        self.assertEqual(c["plain"]["qpm_plans"], 2)
        self.assertEqual(c["plain"]["qpm_plans"], c["plainDrill"])
        self.assertEqual(c["template"]["qpm_plans"], 3)  # P1,P2 from q1 + P4 from q2; shared P2 once
        self.assertEqual(c["template"]["qpm_plans"], c["templateDrill"])
        self.assertEqual(c["summary"]["qpm_plans"], 2)

    def test_plans_column_is_scoped_to_the_rows_database(self):
        c = self.out["col"]
        self.assertEqual(c["otherDb"]["qpm_plans"], 1)
        self.assertFalse(c["otherDb"]["qpm_pinned"])  # pinned in app, not in yugabyte
        self.assertEqual(c["unknownDb"]["qpm_plans"], 0)
        self.assertEqual(c["noDbname"]["qpm_plans"], 3)  # no name to scope by: all dbs
        self.assertEqual(c["legacyNoMap"]["qpm_plans"], 3)
        self.assertEqual(c["notInQpm"]["qpm_plans"], 0)

    def test_plans_column_pinned_flag(self):
        c = self.out["col"]
        self.assertTrue(c["plain"]["qpm_pinned"])
        self.assertTrue(c["template"]["qpm_pinned"])  # a member (q1) is pinned
        self.assertFalse(c["summary"]["qpm_pinned"])  # q2 alone is not

    def test_plans_column_placement_and_sort(self):
        c = self.out["col"]
        self.assertEqual(c["colOrder"], ["query", "qpm_plans", "dbname", "rows"])
        self.assertEqual(c["colNoDb"], ["query", "rows", "qpm_plans"])
        self.assertTrue(c["sortPinnedFirst"])
        self.assertTrue(c["sortCountStillWins"])
        self.assertTrue(c["doesNotMutate"])

    def test_fresh_pin_shows_before_the_collector_sees_it(self):
        # The reported case: pinned from the drilldown, P missing in PGSS until a
        # later snapshot landed and the viewer was moved to it.
        o = self.out["overlay"]
        self.assertTrue(o["rowP"])
        self.assertEqual(o["before"]["16640"], ["q2"])     # q2 pinned, q1 unpinned
        self.assertEqual(o["before"]["13665"], ["-7"])     # key split keeps the minus sign
        self.assertEqual(o["noTime"]["16640"], ["q2"])

    def test_snapshot_taken_after_an_edit_is_left_as_recorded(self):
        o = self.out["overlay"]
        self.assertEqual(o["after"]["16640"], ["q1"])
        self.assertNotIn("13665", o["after"])
        self.assertEqual(o["noEdits"], ["q1"])

    def test_duration_and_redacted_param_formatting(self):
        f = self.out["fmt"]
        self.assertEqual(f["small"], "0.742 ms")
        self.assertEqual(f["mid"], "23.54 ms")
        self.assertEqual(f["big"], "15.04 s")  # 15035 ms -> 15.035 s, rounds up
        self.assertEqual(f["huge"], "61 s")
        p = self.out["params"]
        self.assertFalse(p["redacted"])
        self.assertFalse(p["multiRedacted"])
        self.assertFalse(p["empty"])
        self.assertTrue(p["real"])


    def test_parameters_are_masked_in_every_form_qpm_writes(self):
        m = self.out["mask"]
        self.assertEqual(m["typed"], "((id = ?) AND (region = ?))")
        self.assertEqual(m["typed"], m["generic"])
        self.assertEqual(m["multiWord"], "(stream_at < ?) AND (x = ?)")
        self.assertEqual(m["typmod"], "(a = ?) AND (b = ?) AND (c = ?)")
        self.assertEqual(m["quoted"], "(a = ?) AND (b = ?) AND (c = ?)")
        self.assertEqual(m["arrays"], "(a = ANY (?)) AND (b = ANY (?)) AND (c <> ALL (?))")
        self.assertEqual(m["bare"], "(n = ?) AND (m = ?)")
        # A type name ends at the space: "AND region" is not part of it.
        self.assertEqual(m["noOverreach"], "x = ? AND region = ?")
        # A batched nested loop's own array keeps its column reference.
        self.assertEqual(m["bnl"], "(ROW(id, region) = ANY (ARRAY[ROW(a.id, a.region), ROW(?, ?)]))")

    def test_custom_and_generic_plan_of_one_statement_are_one_plan(self):
        # A JDBC workload: 500 plan cards for 196 statements, 125 of them
        # "2 plans" that were one plan recorded twice.
        t = self.out["twins"]
        self.assertEqual(t["groups"], 1)
        self.assertEqual((t["planid"], t["plan_ref"]), ("G", "gen"))  # most calls stands for it
        self.assertEqual(t["variants"], [["G", "gen", 15400], ["C", "cus", 24]])
        self.assertEqual(t["calls"], 15424)
        self.assertAlmostEqual(t["avg"], (1.3 * 15400 + 2.8 * 24) / 15424, places=9)
        self.assertEqual(t["headline"], "1 plan \u00b7 no plan change recorded")
        self.assertEqual(t["column"], 1)
        self.assertIn("G (generic plan, ", t["note"])
        self.assertIn("C (custom plan, ", t["note"])
        self.assertIn("one pin covers them all", t["note"])
        self.assertNotIn("verbose_plans", t["note"])  # their texts differ; that sentence is for identical ones

    def test_different_plans_are_never_merged(self):
        n = self.out["notTwins"]
        self.assertEqual(n["otherIndex"], 2)
        self.assertEqual(n["otherIndexColumn"], 2)
        self.assertEqual(n["sameHintsOtherShape"], 2)  # Sort vs Incremental Sort, same hints
        self.assertEqual(n["sameTextOtherHints"], 2)  # a pin would not know which hints
        self.assertEqual(n["otherDatabase"], 2)
        self.assertEqual(n["noText"], 2)  # without the text, QPM's own identity

    def test_different_plans_with_the_same_hints_say_a_pin_allows_both(self):
        # A pin fixes scans and joins; the aggregate strategy or a Sort is the planner's call.
        self.assertEqual(self.out["sameHints"], {
            "S@16640": ["G", "I"], "I@16640": ["G", "S"], "G@16640": ["I", "S"],  # G and C are one plan
            "X@16897": [],  # another database's hint table
        })

    def test_identical_text_under_two_planids_is_one_card_that_says_why(self):
        s = self.out["sameText"]
        self.assertEqual(s["groups"], 1)
        self.assertEqual(s["planid"], "P2")
        self.assertEqual(s["sameTextAs"], [])
        self.assertIn("yb_pg_stat_plans_verbose_plans", s["note"])
        self.assertNotIn("generic plan", s["note"])

    def test_headline_hedges_a_fastest_plan_on_few_calls(self):
        h = self.out["hedge"]
        self.assertEqual(
            h["underused"],
            "2 plans \u00b7 fastest plan (10 calls, provisional) serves 0% of calls \u00b7 current plan 2.00\u00d7 slower",
        )
        self.assertEqual(
            h["abandoned"],
            "2 plans \u00b7 fastest plan (7 calls, provisional) no longer in use \u00b7 current plan 2.00\u00d7 slower",
        )
        self.assertNotIn("provisional", self.out["underused"]["headline"])
        self.assertIn("fastest plan (1 call, provisional)", h["oneCall"])

if __name__ == "__main__":
    unittest.main()
