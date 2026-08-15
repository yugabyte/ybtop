/* global fetch, document, window, navigator */

(function () {
  const MANIFEST = "ybtop.manifest.json";

  /** Optional DocDB columns on pg_stat_statements (YugabyteDB); keep in sync with pg_stat_constants.py */
  const PG_STAT_DOCDB_KEYS = [
    "docdb_seeks",
    "docdb_nexts",
    "docdb_prevs",
    "docdb_read_rpcs",
    "docdb_write_rpcs",
    "catalog_wait_time",
    "docdb_read_operations",
    "docdb_write_operations",
    "docdb_rows_scanned",
    "docdb_rows_returned",
    "docdb_wait_time",
    "conflict_retries",
    "read_restart_retries",
    "total_retries",
    "docdb_obsolete_rows_scanned",
    "docdb_read_time",
    "docdb_write_time",
  ];

  /**
   * YugabyteDB ASH reserved query_id values (internal / background). Not user SQL.
   * Extend when new ops ship (often ids below ~100). Names match server-side QueryIdTag.
   */
  const YB_BACKGROUND_QUERY_ID_LABELS = {
    1: "LogAppender",
    2: "Flush",
    3: "Compaction",
    4: "RaftUpdateConsensus",
    5: "UncomputedQueryId",
    6: "LogBackgroundSync",
    7: "YSQLBackgroundWorker",
    8: "RemoteBootstrap",
    9: "Snapshot",
    10: "YcqlAuthResponseRequest",
    11: "Walsender",
    12: "XCluster",
    13: "MinRunningHybridTime",
  };

  /** @returns {string|null} Display label for reserved background query_id (currently 1–13), else null. */
  function backgroundAshQueryLabel(queryId) {
    const raw =
      queryId != null && queryId !== undefined ? String(queryId).trim() : "";
    if (!raw || !/^\d+$/.test(raw)) return null;
    const n = Number(raw);
    if (!Number.isFinite(n) || n < 1 || n > 13) return null;
    const label = YB_BACKGROUND_QUERY_ID_LABELS[n];
    return label != null ? label : null;
  }

  const CLIPBOARD_SVG =
    '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';

  const MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  /** Snapshot instant in UTC for the nav line: YYYY/Mon/DD HH:MM:SS */
  function formatSnapshotTakenHuman(isoUtc) {
    if (isoUtc == null || String(isoUtc).trim() === "") return "";
    const d = new Date(String(isoUtc));
    if (Number.isNaN(d.getTime())) return "";
    const y = d.getUTCFullYear();
    const mon = MONTH_ABBR[d.getUTCMonth()];
    const day = String(d.getUTCDate()).padStart(2, "0");
    const hh = String(d.getUTCHours()).padStart(2, "0");
    const mm = String(d.getUTCMinutes()).padStart(2, "0");
    const ss = String(d.getUTCSeconds()).padStart(2, "0");
    return `${y}/${mon}/${day} ${hh}:${mm}:${ss}`;
  }

  /** Older manifests may omit `utc`; filename uses UTC `ybtop.out.YYYYMMDD_HHMMSS.json`. */
  function snapshotHumanFromFilename(file) {
    const m = /^ybtop\.out\.(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})\.json(?:\.gz)?$/i.exec(file || "");
    if (!m) return "";
    const y = Number(m[1]);
    const mo = Number(m[2]);
    const day = Number(m[3]);
    const hh = Number(m[4]);
    const mi = Number(m[5]);
    const ss = Number(m[6]);
    if (mo < 1 || mo > 12 || day < 1 || day > 31 || hh > 23 || mi > 59 || ss > 59) return "";
    const mon = MONTH_ABBR[mo - 1];
    return `${y}/${mon}/${String(day).padStart(2, "0")} ${String(hh).padStart(2, "0")}:${String(mi).padStart(2, "0")}:${String(ss).padStart(2, "0")}`;
  }

  /**
   * Stable per-snapshot key: the UTC `YYYYMMDD_HHMMSS` component of the
   * filename. Used to pin a window in the URL — unlike the manifest index,
   * it survives new snapshots arriving and old ones being GC'd.
   */
  function snapshotTimeKeyFromFile(file) {
    const m = /^ybtop\.out\.(\d{8}_\d{6})\.json(?:\.gz)?$/i.exec(file || "");
    return m ? m[1] : null;
  }

  /** Earliest → latest snapshot UTC across the loaded manifest entries; always shows both dates. */
  function manifestOverallRangeText() {
    if (!manifestEntries.length) return "";
    const first = manifestEntries[0];
    const last = manifestEntries[manifestEntries.length - 1];
    const isoStart = (first && first.utc) || "";
    const isoEnd = (last && last.utc) || "";
    const d1 = formatSnapshotDatePart(isoStart);
    const d2 = formatSnapshotDatePart(isoEnd);
    const t1 = formatSnapshotTimePart(isoStart);
    const t2 = formatSnapshotTimePart(isoEnd);
    if (!d1 || !d2 || !t1 || !t2) return "";
    return `${d1} ${t1} → ${d2} ${t2} UTC`;
  }

  // Sync the editable counter, total and the overall-range label.
  // 1-based for humans; skips the jump box while it has focus so it doesn't
  // fight the user mid-type. `ent`/`docOrNull` retained for call-site compatibility.
  function updateNavDisplay(index, len, _ent, _docOrNull) {
    const jump = document.getElementById("nav-jump");
    if (jump) {
      jump.max = String(len);
      if (document.activeElement !== jump) jump.value = String(index + 1);
    }
    const total = document.getElementById("nav-total");
    if (total) total.textContent = ` / ${len}`;
    const fileEl = document.getElementById("nav-file");
    if (fileEl) {
      const range = manifestOverallRangeText();
      fileEl.textContent = range ? ` — ${range}` : "";
    }
  }

  let manifestEntries = [];
  let currentIndex = -1;
  /** Snapshot time key parsed from the `t` URL param, or null. Resolved to a
   * manifest index via indexForWindowKey() once entries are loaded. */
  let urlWindowKey = null;
  let lastDoc = null;
  /** Prior snapshot (for delta pg_stat), retained for re-rend on tab/URL. */
  let lastPrevDoc = null;

  /** Left nav panel id; survives snapshot navigation. */
  let activeViewerSection = "pgss";
  /** ASH filters from URL / deeplinks; cleared when leaving the ASH tab. */
  let ashQueryIdFilter = null;
  let ashNodeIdFilter = null;
  let ashTableIdFilter = null;

  /** Latency modes tab: include the dip_p column when true (browser-only UI preference). */
  let latencyShowDipP = true;

  const VIEWER_SECTION_IDS = ["pgss", "ycql", "ash", "tablets", "latency"];

  /**
   * subsectionId -> expanded when true; undefined / false => collapsed.
   * `sec-pgss-main` defaults to expanded so the statements table and pager are visible.
   * State survives snapshot Prev/Next.
   */
  const subsectionExpandedState = Object.create(null);

  function isSubsectionExpanded(subsectionId) {
    if (
      (subsectionId === "sec-pgss-main" || subsectionId === "sec-ycql-main") &&
      subsectionExpandedState[subsectionId] === undefined
    ) {
      return true; /* main statements table + pager visible on first open */
    }
    return subsectionExpandedState[subsectionId] === true;
  }

  function setSubsectionExpanded(subsectionId, expanded) {
    subsectionExpandedState[subsectionId] = !!expanded;
  }

  function wireSubsectionCollapse(section, subsectionId, bodyEl, toggleBtn) {
    function sync() {
      const open = isSubsectionExpanded(subsectionId);
      bodyEl.hidden = !open;
      toggleBtn.textContent = open ? "▼" : "▶";
      toggleBtn.setAttribute("aria-expanded", open ? "true" : "false");
      toggleBtn.setAttribute("aria-label", open ? "Collapse section" : "Expand section");
      section.classList.toggle("subsection-expanded", open);
    }
    sync();
    toggleBtn.addEventListener("click", () => {
      setSubsectionExpanded(subsectionId, !isSubsectionExpanded(subsectionId));
      sync();
    });
  }

  /** Manifest index whose file matches the given time key, or -1. */
  function indexForWindowKey(key) {
    if (!key) return -1;
    return manifestEntries.findIndex((e) => snapshotTimeKeyFromFile(e && e.file) === key);
  }

  function readViewerStateFromUrl() {
    const p = new URLSearchParams(window.location.search);
    const t = p.get("t");
    urlWindowKey = t != null && String(t).trim() !== "" ? String(t).trim() : null;
    const v = p.get("view");
    if (
      v === "ash" ||
      v === "tablets" ||
      v === "pgss" ||
      v === "ycql" ||
      v === "latency"
    ) {
      activeViewerSection = v;
    } else {
      activeViewerSection = "pgss";
    }
    const q = p.get("query");
    ashQueryIdFilter = q != null && String(q) !== "" ? String(q) : null;
    const n = p.get("node");
    ashNodeIdFilter = n != null && String(n).trim() !== "" ? String(n).trim() : null;
    const tb = p.get("table_id");
    ashTableIdFilter = tb != null && String(tb).trim() !== "" ? String(tb).trim() : null;
    if (activeViewerSection !== "ash") {
      ashQueryIdFilter = null;
      ashNodeIdFilter = null;
      ashTableIdFilter = null;
    }
  }

  function writeViewerStateToUrl(options) {
    const push = options && options.push;
    const p = new URLSearchParams();
    p.set("view", activeViewerSection);
    // Only pin the window in the URL when it's NOT the newest, so a plain
    // reload defaults to the latest; stepping back makes reloads sticky. Pin
    // by the snapshot's filename time key (stable) rather than the manifest
    // index (shifts as snapshots are added/GC'd).
    const isLatestWindow = currentIndex >= manifestEntries.length - 1;
    const ent = currentIndex >= 0 ? manifestEntries[currentIndex] : null;
    const windowKey = !isLatestWindow && ent ? snapshotTimeKeyFromFile(ent.file) : null;
    if (windowKey) p.set("t", windowKey);
    if (activeViewerSection === "ash") {
      if (ashQueryIdFilter) p.set("query", ashQueryIdFilter);
      if (ashNodeIdFilter) p.set("node", ashNodeIdFilter);
      if (ashTableIdFilter) p.set("table_id", ashTableIdFilter);
    }
    const qs = p.toString();
    const newUrl = `${window.location.pathname}${qs ? "?" + qs : ""}${window.location.hash || ""}`;
    const st = {
      ybtop: true,
      view: activeViewerSection,
      t: windowKey,
      query: ashQueryIdFilter || null,
      node: ashNodeIdFilter || null,
      table_id: ashTableIdFilter || null,
    };
    if (push) {
      history.pushState(st, "", newUrl);
    } else {
      history.replaceState(st, "", newUrl);
    }
  }

  function setViewerSection(id) {
    if (!VIEWER_SECTION_IDS.includes(id)) return;
    const hadAshFilters =
      !!ashQueryIdFilter || !!ashNodeIdFilter || !!ashTableIdFilter;
    if (id !== "ash") {
      ashQueryIdFilter = null;
      ashNodeIdFilter = null;
      ashTableIdFilter = null;
    }
    activeViewerSection = id;
    const app = document.getElementById("app");
    if (!app) return;
    app.querySelectorAll(".app-panel").forEach((p) => {
      const on = p.dataset.viewerSection === id;
      p.classList.toggle("app-panel-active", on);
      p.setAttribute("aria-hidden", on ? "false" : "true");
    });
    const nav = document.getElementById("app-nav");
    if (nav) {
      nav.querySelectorAll(".app-tab").forEach((b) => {
        const on = b.dataset.viewerSection === id;
        b.classList.toggle("app-tab-active", on);
        b.setAttribute("aria-selected", on ? "true" : "false");
      });
    }
    writeViewerStateToUrl();
    if (lastDoc && id !== "ash" && hadAshFilters) {
      renderDoc(lastDoc, lastPrevDoc);
    } else {
      updateAshFilterToolbar();
    }
  }

  function buildViewerNav() {
    const nav = document.getElementById("app-nav");
    if (!nav) return;
    if (!VIEWER_SECTION_IDS.includes(activeViewerSection)) {
      activeViewerSection = "pgss";
    }
    nav.textContent = "";
    const items = [
      ["pgss", "pg_stat_statements"],
      ["ycql", "ycql_stat_statements"],
      ["ash", "Active Session History"],
      ["tablets", "Tablet Report"],
    ];
    if (docHasLatencyHistograms(lastDoc)) {
      items.push(["latency", "Latency modes"]);
    } else if (activeViewerSection === "latency") {
      activeViewerSection = "pgss";
    }
    items.forEach(([sid, label]) => {
      const btn = el("button", {
        type: "button",
        className: "app-tab",
        textContent: label,
        "data-viewer-section": sid,
        role: "tab",
        id: `tab-${sid}`,
        "aria-controls": `panel-${sid}`,
      });
      btn.addEventListener("click", () => setViewerSection(sid));
      nav.appendChild(btn);
    });
    setViewerSection(activeViewerSection);
  }

  function el(tag, attrs, children) {
    const n = document.createElement(tag);
    if (attrs) {
      Object.entries(attrs).forEach(([k, v]) => {
        if (k === "className") n.className = v;
        else if (k === "textContent") n.textContent = v;
        else if (k === "innerHTML") n.innerHTML = v;
        else n.setAttribute(k, v);
      });
    }
    (children || []).forEach((c) => n.appendChild(c));
    return n;
  }

  /** After "Grouped By:", render remainder in .section-title-groupby-highlight (accent). */
  function fillSectionTitleWithGroupedHighlight(titleEl, titleText) {
    titleEl.textContent = "";
    const marker = "Grouped By:";
    const idx = String(titleText || "").indexOf(marker);
    if (idx === -1) {
      titleEl.textContent = titleText == null ? "" : String(titleText);
      return;
    }
    const head = String(titleText).slice(0, idx + marker.length);
    const tail = String(titleText).slice(idx + marker.length).replace(/^\s+/, "");
    titleEl.appendChild(document.createTextNode(head));
    if (tail) {
      titleEl.appendChild(document.createTextNode(" "));
      titleEl.appendChild(
        el("span", { className: "section-title-groupby-highlight", textContent: tail })
      );
    }
  }

  function normQid(v) {
    if (v === null || v === undefined) return null;
    return String(v);
  }

  function mergeStatements(perNode) {
    let hasRowsInSource = false;
    let hasDbnameInSource = false;
    Object.keys(perNode || {}).forEach((nid) => {
      (perNode[nid] || []).forEach((r) => {
        if (Object.prototype.hasOwnProperty.call(r, "rows")) hasRowsInSource = true;
        const dbv = r.dbname;
        if (dbv != null && dbv !== undefined && String(dbv).trim() !== "") hasDbnameInSource = true;
      });
    });

    const seenDoc = new Set();
    Object.keys(perNode || {}).forEach((nid) => {
      (perNode[nid] || []).forEach((r) => {
        PG_STAT_DOCDB_KEYS.forEach((k) => {
          if (r[k] != null && r[k] !== undefined) seenDoc.add(k);
        });
      });
    });
    const docKeys = PG_STAT_DOCDB_KEYS.filter((k) => seenDoc.has(k));

    const acc = new Map();
    Object.keys(perNode || {}).forEach((nid) => {
      (perNode[nid] || []).forEach((r) => {
        const dn = r.dbname != null && r.dbname !== undefined ? String(r.dbname).trim() : "";
        const mk = `${String(r.queryid)}\0${dn}`;
        if (!acc.has(mk)) {
          const o = {
            queryid: String(r.queryid),
            dbname: dn || null,
            query: r.query || "",
            calls: 0,
            total_exec_time: 0,
          };
          if (hasRowsInSource) o.rows = 0;
          docKeys.forEach((k) => {
            o[k] = 0;
          });
          acc.set(mk, o);
        }
        const a = acc.get(mk);
        a.calls += Number(r.calls) || 0;
        a.total_exec_time += Number(r.total_exec_time) || 0;
        if (!a.dbname && r.dbname) a.dbname = String(r.dbname).trim() || null;
        if (hasRowsInSource) a.rows += Number(r.rows) || 0;
        docKeys.forEach((k) => {
          a[k] += Number(r[k]) || 0;
        });
        if (!a.query && r.query) a.query = r.query;
      });
    });
    const out = Array.from(acc.values()).map((a) => {
      const calls = a.calls;
      const mean = calls ? a.total_exec_time / calls : 0;
      const row = {
        calls: a.calls,
        total_ms: Math.round(a.total_exec_time * 100) / 100,
        mean_ms: Math.round(mean * 100) / 100,
        query: a.query,
      };
      if (hasDbnameInSource) {
        row.dbname = a.dbname != null ? a.dbname : null;
      }
      if (hasRowsInSource) {
        row.rows = Math.round(a.rows * 100) / 100;
        row.rows_per_call = calls ? Math.round((a.rows / calls) * 100) / 100 : 0;
      }
      docKeys.forEach((k) => {
        row[`${k}_per_call`] = calls ? Math.round((a[k] / calls) * 100) / 100 : 0;
      });
      row.queryid = a.queryid;
      const deltaSrc = {
        calls: a.calls,
        total_exec_time: a.total_exec_time,
        doc: {},
      };
      if (hasRowsInSource) deltaSrc.rows = a.rows;
      docKeys.forEach((k) => {
        deltaSrc.doc[k] = a[k];
      });
      row._deltaSrc = deltaSrc;
      return row;
    });
    out.sort((x, y) => y.total_ms - x.total_ms);
    return out;
  }

  function ycqlPreparedTruthy(v) {
    return v === true || v === "t" || v === "true" || v === 1 || v === "1";
  }

  function mergeYcqlStatements(perNode) {
    const acc = new Map();
    Object.keys(perNode || {}).forEach((nid) => {
      (perNode[nid] || []).forEach((r) => {
        const mk = String(r.queryid);
        if (!acc.has(mk)) {
          acc.set(mk, {
            queryid: mk,
            query: r.query || "",
            calls: 0,
            total_exec_time: 0,
            is_prepared: false,
          });
        }
        const a = acc.get(mk);
        a.calls += Number(r.calls) || 0;
        a.total_exec_time += Number(r.total_time) || 0;
        if (ycqlPreparedTruthy(r.is_prepared)) a.is_prepared = true;
        if (!a.query && r.query) a.query = r.query;
      });
    });
    const out = Array.from(acc.values()).map((a) => {
      const calls = a.calls;
      const mean = calls ? a.total_exec_time / calls : 0;
      const row = {
        calls: a.calls,
        total_ms: Math.round(a.total_exec_time * 100) / 100,
        mean_ms: Math.round(mean * 100) / 100,
        query: a.query,
        is_prepared: !!a.is_prepared,
        queryid: a.queryid,
        _deltaSrc: {
          calls: a.calls,
          total_exec_time: a.total_exec_time,
        },
      };
      return row;
    });
    out.sort((x, y) => y.total_ms - x.total_ms);
    return out;
  }

  function ycqlStatStatementColumns() {
    return [
      { key: "calls", label: "calls", type: "number", align: "right" },
      { key: "total_ms", label: "total time (ms)", type: "number", align: "right" },
      { key: "time_pct", label: "time %", type: "number", align: "right" },
      { key: "mean_ms", label: "mean time (ms)", type: "number", align: "right" },
      { key: "query", label: "query" },
      { key: "is_prepared", label: "is_prepared" },
      { key: "queryid", label: "queryid" },
    ];
  }

  function ycqlStatStatementColumnsDelta() {
    return [
      { key: "calls_per_sec", label: "calls/s", type: "number", align: "right" },
      { key: "total_ms", label: "total time (ms)", type: "number", align: "right" },
      { key: "time_pct", label: "time %", type: "number", align: "right" },
      { key: "mean_ms", label: "mean time (ms)", type: "number", align: "right" },
      { key: "query", label: "query" },
      { key: "is_prepared", label: "is_prepared" },
      { key: "queryid", label: "queryid" },
    ];
  }

  function formatYcqlPrepared(v) {
    if (v === null || v === undefined || v === "") return "";
    return ycqlPreparedTruthy(v) ? "true" : "false";
  }

  /**
   * When several merged rows share the same queryid (different dbname), prefer the highest total_ms
   * so ASH banner metrics align with the dominant statements row.
   */
  function pickMergedPgStatRowForQueryId(rows, qid) {
    const want = normQid(qid);
    if (want == null || !rows || !rows.length) return null;
    const matches = rows.filter((r) => normQid(r.queryid) === want);
    if (!matches.length) return null;
    matches.sort((a, b) => (Number(b.total_ms) || 0) - (Number(a.total_ms) || 0));
    return matches[0];
  }

  function pgStatPerNodeHasRowsColumn(perNode) {
    let has = false;
    Object.keys(perNode || {}).forEach((nid) => {
      (perNode[nid] || []).forEach((r) => {
        if (Object.prototype.hasOwnProperty.call(r, "rows")) has = true;
      });
    });
    return has;
  }

  /**
   * Sum `calls` on one node for a merged statement row.
   * YSQL: queryid + dbname; YCQL: queryid only.
   */
  function statementCallsOnNodeMatching(perNode, nodeId, stmtRow, matchDbname) {
    const wantQ = normQid(stmtRow.queryid);
    if (wantQ == null) return 0;
    const dn =
      matchDbname && stmtRow.dbname != null && stmtRow.dbname !== undefined
        ? String(stmtRow.dbname).trim()
        : "";
    const nid = nodeId != null && nodeId !== undefined ? String(nodeId) : "";
    const rows = (perNode || {})[nid] || [];
    let sum = 0;
    rows.forEach((r) => {
      if (normQid(r.queryid) !== wantQ) return;
      if (matchDbname) {
        const rdn = r.dbname != null && r.dbname !== undefined ? String(r.dbname).trim() : "";
        if (rdn !== dn) return;
      }
      sum += Number(r.calls) || 0;
    });
    return sum;
  }

  /**
   * Per-node positive contributions for the scoped statement: cumulative calls, or Δcalls vs prior when deltaMode.
   * Same “positive weights only” split as ASH load distribution (summarizeAshNodeLoadPct).
   */
  function statementCallsContributorsPerNodeMap(perNode, stmtRow, prevPerNode, deltaMode, matchDbname) {
    const keys = new Set(Object.keys(perNode || {}));
    if (deltaMode && prevPerNode) {
      Object.keys(prevPerNode).forEach((k) => keys.add(k));
    }
    const nm = new Map();
    keys.forEach((nid) => {
      const cur = statementCallsOnNodeMatching(perNode, nid, stmtRow, matchDbname);
      let metric = cur;
      if (deltaMode && prevPerNode) {
        const prev = statementCallsOnNodeMatching(prevPerNode, nid, stmtRow, matchDbname);
        metric = cur - prev;
      }
      if (metric > 0) nm.set(String(nid), metric);
    });
    return nm;
  }

  /** Same layout/tooltips as Load Distribution % table cells; skips row when cluster has ≤1 node. */
  function appendAshBannerCallsDistributionRow(noteEl, clusterNodeCount, dist) {
    if (clusterNodeCount <= 1) return;
    const row = el("div", { className: "ash-mode-banner-query-row" });
    row.appendChild(
      el("span", {
        className: "ash-mode-banner-query-k",
        textContent: `Calls Distribution % (across ${clusterNodeCount} nodes)`,
      })
    );
    const val = el("span", { className: "ash-mode-banner-query-highlight" });
    if (!dist || !dist.parts || !dist.parts.length) {
      val.classList.add("ash-mode-banner-query-highlight--empty");
      val.textContent = "—";
    } else {
      dist.parts.forEach((p, idx) => {
        if (idx > 0) val.appendChild(document.createTextNode(", "));
        const span = el("span", {
          className: "ash-node-dist-pct",
          textContent: `${Number(p.pct).toFixed(1)}%`,
        });
        wireQuickNodeIdTooltip(span, p.node_id);
        val.appendChild(span);
      });
      if (dist.ellipsis) {
        val.appendChild(document.createTextNode(", …"));
      }
    }
    row.appendChild(val);
    noteEl.appendChild(row);
  }

  /** Key/value row under ASH scoped banners; empty value shows em dash in muted style. */
  function ashBannerMetricRow(noteEl, keyLabel, valueText) {
    const row = el("div", { className: "ash-mode-banner-query-row" });
    row.appendChild(el("span", { className: "ash-mode-banner-query-k", textContent: keyLabel }));
    const disp = valueText == null || valueText === "" ? "" : String(valueText);
    const empty = disp === "";
    row.appendChild(
      el("span", {
        className: empty
          ? "ash-mode-banner-query-highlight ash-mode-banner-query-highlight--empty"
          : "ash-mode-banner-query-highlight",
        textContent: empty ? "—" : disp,
      })
    );
    noteEl.appendChild(row);
  }

  /**
   * Metrics from merged pg_stat_statements or ycql_stat_statements for the scoped query_id.
   * @returns {boolean} true when a matching row was found and metrics were appended
   */
  function appendAshScopedStatementSourceLines(
    noteEl,
    doc,
    prevDoc,
    qF,
    ashPerNode,
    sourceLabel,
    perNode,
    prevPerNode,
    mergeFn,
    opts
  ) {
    const want = normQid(qF);
    if (want == null || !perNode) return false;

    const matchDbname = !!(opts && opts.matchDbname);
    const hasRowsCol = matchDbname && pgStatPerNodeHasRowsColumn(perNode);

    const merged = mergeFn(perNode);
    let stmtRow = null;
    let deltaMode = false;
    if (prevDoc && prevPerNode) {
      deltaMode = true;
      const mergedPrev = mergeFn(prevPerNode);
      const deltaRows = deltaPgStatMergedRows(merged, mergedPrev);
      const derived = withPgStatDeltaDerivedRows(
        deltaRows,
        prevDoc.generated_at_utc,
        doc.generated_at_utc
      );
      stmtRow = pickMergedPgStatRowForQueryId(derived, qF);
    } else {
      stmtRow = pickMergedPgStatRowForQueryId(withPgStatTimePercent(merged), qF);
    }

    if (!stmtRow) {
      return false;
    }

    if (deltaMode) {
      const cps = stmtRow.calls_per_sec;
      ashBannerMetricRow(
        noteEl,
        "calls/s",
        cps != null && cps !== "" ? Number(cps).toFixed(2) : ""
      );
    } else {
      ashBannerMetricRow(
        noteEl,
        "calls",
        stmtRow.calls != null && stmtRow.calls !== "" ? String(stmtRow.calls) : ""
      );
    }

    const clusterNodes = ashSnapshotClusterNodeCount(doc, ashPerNode || {});
    const contribMap = statementCallsContributorsPerNodeMap(
      perNode,
      stmtRow,
      prevPerNode,
      deltaMode,
      matchDbname
    );
    const callsDist = summarizeAshNodeLoadPct(contribMap);
    appendAshBannerCallsDistributionRow(noteEl, clusterNodes, callsDist);

    const tms = formatPgStatMsTwoDecimals(stmtRow.total_ms);
    const pctBracket =
      stmtRow.time_pct != null && stmtRow.time_pct !== ""
        ? `[${Number(stmtRow.time_pct).toFixed(2)}%]`
        : "";
    let totalTimeVal = "";
    if (tms && pctBracket) totalTimeVal = `${tms} (ms) ${pctBracket}`;
    else if (tms) totalTimeVal = `${tms} (ms)`;
    else totalTimeVal = pctBracket;
    ashBannerMetricRow(noteEl, "total time", totalTimeVal);

    const meanMs = formatPgStatMsTwoDecimals(stmtRow.mean_ms);
    ashBannerMetricRow(noteEl, "mean time", meanMs ? `${meanMs} ms` : "");

    if (opts && opts.showIsPrepared) {
      ashBannerMetricRow(noteEl, "is_prepared", formatYcqlPrepared(stmtRow.is_prepared));
    }
    if (hasRowsCol) {
      const rawRpc =
        stmtRow.rows_per_call != null && stmtRow.rows_per_call !== ""
          ? stmtRow.rows_per_call
          : stmtRow.avg_rows_per_call;
      ashBannerMetricRow(noteEl, "rows/call", formatPgStatPerCallMetric(rawRpc));
    }
    return true;
  }

  /** Cumulative merged row for queryid (used to pick pg vs ycql statement source). */
  function mergedStatementRowForQuery(perNode, mergeFn, qid) {
    if (!perNode) return null;
    return pickMergedPgStatRowForQueryId(withPgStatTimePercent(mergeFn(perNode)), qid);
  }

  /**
   * Statement summary under the ASH query banner: pg_stat_statements when present, else ycql_stat_statements.
   * @param ashPerNode ASH per_node map (unfiltered) for cluster node count only.
   */
  function appendAshScopedQueryStatementLines(noteEl, doc, prevDoc, qF, ashPerNode) {
    const pgPer = doc && doc.pg_stat_statements && doc.pg_stat_statements.per_node;
    const ycqlPer = doc && doc.ycql_stat_statements && doc.ycql_stat_statements.per_node;
    const prevPg =
      prevDoc && prevDoc.pg_stat_statements && prevDoc.pg_stat_statements.per_node;
    const prevYcql =
      prevDoc && prevDoc.ycql_stat_statements && prevDoc.ycql_stat_statements.per_node;

    const inPg = mergedStatementRowForQuery(pgPer, mergeStatements, qF);
    const inYcql = mergedStatementRowForQuery(ycqlPer, mergeYcqlStatements, qF);

    if (inPg) {
      if (
        !appendAshScopedStatementSourceLines(
          noteEl,
          doc,
          prevDoc,
          qF,
          ashPerNode,
          "pg_stat_statements",
          pgPer,
          prevPg,
          mergeStatements,
          { matchDbname: true }
        )
      ) {
        ashBannerMetricRow(
          noteEl,
          "pg_stat_statements",
          "No Δ row for this query vs prior (zero change or not in merge)."
        );
      }
      return;
    }
    if (inYcql) {
      if (
        !appendAshScopedStatementSourceLines(
          noteEl,
          doc,
          prevDoc,
          qF,
          ashPerNode,
          "ycql_stat_statements",
          ycqlPer,
          prevYcql,
          mergeYcqlStatements,
          { matchDbname: false, showIsPrepared: true }
        )
      ) {
        ashBannerMetricRow(
          noteEl,
          "ycql_stat_statements",
          "No Δ row for this query vs prior (zero change or not in merge)."
        );
      }
      return;
    }
    if (pgPer || ycqlPer) {
      ashBannerMetricRow(
        noteEl,
        "statements",
        "No merged row for this query in pg_stat_statements or ycql_stat_statements."
      );
    }
  }

  function pgStatStatementColumns(merged, perNode) {
    let hasRowsInSource = false;
    let hasDbnameInSource = false;
    Object.keys(perNode || {}).forEach((nid) => {
      (perNode[nid] || []).forEach((r) => {
        if (Object.prototype.hasOwnProperty.call(r, "rows")) hasRowsInSource = true;
        if (Object.prototype.hasOwnProperty.call(r, "dbname")) hasDbnameInSource = true;
      });
    });
    const cols = [
      { key: "calls", label: "calls", type: "number", align: "right" },
      { key: "total_ms", label: "total time (ms)", type: "number", align: "right" },
      { key: "time_pct", label: "time %", type: "number", align: "right" },
      { key: "mean_ms", label: "mean time (ms)", type: "number", align: "right" },
      { key: "query", label: "query" },
    ];
    if (hasDbnameInSource) {
      cols.push({ key: "dbname", label: "dbname" });
    }
    if (hasRowsInSource) {
      cols.push({
        key: "rows_per_call",
        label: "rows per call",
        type: "number",
        align: "right",
        headerPerCall: true,
        headerBase: "rows",
        sortValue: (r) => {
          const x =
            r.rows_per_call != null && r.rows_per_call !== ""
              ? r.rows_per_call
              : r.avg_rows_per_call;
          return Number(x) || 0;
        },
      });
    }
    PG_STAT_DOCDB_KEYS.forEach((k) => {
      const kk = `${k}_per_call`;
      if (merged.some((r) => Object.prototype.hasOwnProperty.call(r, kk))) {
        cols.push({ key: kk, type: "number", align: "right", headerPerCall: true, headerBase: k });
      }
    });
    cols.push({ key: "queryid", label: "queryid" });
    return cols;
  }

  function statementMergeKey(r) {
    const dn = r.dbname != null && r.dbname !== undefined ? String(r.dbname).trim() : "";
    return `${String(r.queryid)}\0${dn}`;
  }

  /** Reconstruct approximate raw totals when _deltaSrc is missing (older snapshots). */
  function deltaSrcFromRowFallback(r) {
    if (!r) return { calls: 0, total_exec_time: 0, rows: 0, doc: {} };
    if (r._deltaSrc) return r._deltaSrc;
    const calls = Number(r.calls) || 0;
    const doc = {};
    PG_STAT_DOCDB_KEYS.forEach((k) => {
      const pk = `${k}_per_call`;
      if (!Object.prototype.hasOwnProperty.call(r, pk)) return;
      const pc = Number(r[pk]) || 0;
      doc[k] = calls * pc;
    });
    return {
      calls,
      total_exec_time: Number(r.total_ms) || 0,
      rows: r.rows != null ? Number(r.rows) : undefined,
      doc,
    };
  }

  /**
   * Per-statement deltas: new snapshot merged row minus previous (same queryid+dbname).
   * mean_ms = (Δ total_exec_time) / (Δ calls); DocDB and rows per-call use Δtotals / Δcalls.
   */
  function deltaPgStatMergedRows(curRows, prevRows) {
    const prevMap = new Map();
    (prevRows || []).forEach((r) => {
      prevMap.set(statementMergeKey(r), r);
    });
    const raw = [];
    (curRows || []).forEach((cur) => {
      const p = prevMap.get(statementMergeKey(cur)) || null;
      const sc = deltaSrcFromRowFallback(cur);
      const sp = p ? deltaSrcFromRowFallback(p) : { calls: 0, total_exec_time: 0, rows: 0, doc: {} };
      const dCalls = sc.calls - (sp.calls || 0);
      const dExec = sc.total_exec_time - (sp.total_exec_time || 0);
      const hasRows = Object.prototype.hasOwnProperty.call(cur, "rows");
      const dRows = hasRows ? (Number(sc.rows) || 0) - (sp.rows != null ? Number(sp.rows) || 0 : 0) : 0;
      const docKeySet = new Set();
      PG_STAT_DOCDB_KEYS.forEach((k) => {
        if ((sc.doc && k in sc.doc) || (sp.doc && k in sp.doc)) docKeySet.add(k);
        if (Object.prototype.hasOwnProperty.call(cur, `${k}_per_call`)) docKeySet.add(k);
        if (p && Object.prototype.hasOwnProperty.call(p, `${k}_per_call`)) docKeySet.add(k);
      });
      const row = {
        calls: Math.round(dCalls * 100) / 100,
        total_ms: Math.round(dExec * 100) / 100,
        mean_ms: dCalls > 0 ? Math.round((dExec / dCalls) * 100) / 100 : 0,
        query: cur.query,
        queryid: cur.queryid,
      };
      if (Object.prototype.hasOwnProperty.call(cur, "dbname")) {
        row.dbname = cur.dbname != null ? cur.dbname : null;
      }
      if (hasRows) {
        row.rows = Math.round(dRows * 100) / 100;
        row.rows_per_call = dCalls > 0 ? Math.round((dRows / dCalls) * 100) / 100 : 0;
      }
      docKeySet.forEach((dk) => {
        const ctot = sc.doc && sc.doc[dk] != null ? Number(sc.doc[dk]) : 0;
        const ptot = sp.doc && sp.doc[dk] != null ? Number(sp.doc[dk]) : 0;
        const dtot = ctot - ptot;
        row[`${dk}_per_call`] = dCalls > 0 ? Math.round((dtot / dCalls) * 100) / 100 : 0;
      });
      raw.push(row);
    });
    const filtered = raw.filter((r) => {
      if (r.calls !== 0 || r.total_ms !== 0) return true;
      if (r.rows != null && r.rows !== 0) return true;
      return PG_STAT_DOCDB_KEYS.some(
        (k) =>
          Object.prototype.hasOwnProperty.call(r, `${k}_per_call`) && Number(r[`${k}_per_call`]) !== 0
      );
    });
    filtered.sort((a, b) => b.total_ms - a.total_ms);
    return filtered;
  }

  function pgStatStatementColumnsDelta(merged, perNode) {
    let hasRowsInSource = false;
    let hasDbnameInSource = false;
    Object.keys(perNode || {}).forEach((nid) => {
      (perNode[nid] || []).forEach((r) => {
        if (Object.prototype.hasOwnProperty.call(r, "rows")) hasRowsInSource = true;
        if (Object.prototype.hasOwnProperty.call(r, "dbname")) hasDbnameInSource = true;
      });
    });
    const cols = [
      { key: "calls_per_sec", label: "calls/s", type: "number", align: "right" },
      { key: "total_ms", label: "total time (ms)", type: "number", align: "right" },
      { key: "time_pct", label: "time %", type: "number", align: "right" },
      { key: "mean_ms", label: "mean time (ms)", type: "number", align: "right" },
      { key: "query", label: "query" },
    ];
    if (hasDbnameInSource) {
      cols.push({ key: "dbname", label: "dbname" });
    }
    if (hasRowsInSource) {
      cols.push({
        key: "rows_per_call",
        label: "rows per call",
        type: "number",
        align: "right",
        headerPerCall: true,
        headerBase: "rows",
        sortValue: (r) => {
          const x =
            r.rows_per_call != null && r.rows_per_call !== ""
              ? r.rows_per_call
              : r.avg_rows_per_call;
          return Number(x) || 0;
        },
      });
    }
    PG_STAT_DOCDB_KEYS.forEach((k) => {
      const kk = `${k}_per_call`;
      if (merged.some((r) => Object.prototype.hasOwnProperty.call(r, kk))) {
        cols.push({ key: kk, type: "number", align: "right", headerPerCall: true, headerBase: k });
      }
    });
    cols.push({ key: "queryid", label: "queryid" });
    return cols;
  }

  /** Fixed-width UTC timestamp for activity headers (23 chars). */
  function formatSnapshotTsFixed(iso) {
    if (iso == null || iso === "") return "????-??-?? ??:??:?? UTC";
    try {
      const d = new Date(String(iso));
      if (Number.isNaN(d.getTime())) return String(iso).slice(0, 23).padEnd(23);
      const y = d.getUTCFullYear();
      const mo = String(d.getUTCMonth() + 1).padStart(2, "0");
      const da = String(d.getUTCDate()).padStart(2, "0");
      const h = String(d.getUTCHours()).padStart(2, "0");
      const mi = String(d.getUTCMinutes()).padStart(2, "0");
      const s = String(d.getUTCSeconds()).padStart(2, "0");
      return `${y}-${mo}-${da} ${h}:${mi}:${s} UTC`;
    } catch {
      return "????-??-?? ??:??:?? UTC";
    }
  }

  /** Positive span in seconds between older and newer snapshot timestamps (for ratio math). */
  function snapshotIntervalSeconds(olderIso, newerIso) {
    const t1 = new Date(String(olderIso)).getTime();
    const t2 = new Date(String(newerIso)).getTime();
    if (Number.isNaN(t1) || Number.isNaN(t2) || t2 <= t1) return 0;
    return (t2 - t1) / 1000;
  }

  /** Cumulative mode: time % = row total_ms / sum(total_ms) over displayed rows. */
  function withPgStatTimePercent(rows) {
    const arr = rows || [];
    const totalMs = arr.reduce((s, r) => s + (Number(r.total_ms) || 0), 0);
    return arr.map((r) => {
      const ms = Number(r.total_ms) || 0;
      return {
        ...r,
        time_pct: totalMs > 0 ? Math.round(10000 * (ms / totalMs)) / 100 : 0,
      };
    });
  }

  /**
   * Delta-mode derived fields: calls/s = Δcalls / interval, time % = row Δ total_ms / sum(Δ total_ms).
   */
  function withPgStatDeltaDerivedRows(rows, olderIso, newerIso) {
    const sec = snapshotIntervalSeconds(olderIso, newerIso);
    const arr = rows || [];
    const totalMs = arr.reduce((s, r) => s + (Number(r.total_ms) || 0), 0);
    return arr.map((r) => {
      const calls = Number(r.calls) || 0;
      const ms = Number(r.total_ms) || 0;
      return {
        ...r,
        calls_per_sec: sec > 0 ? Math.round((calls / sec) * 100) / 100 : 0,
        time_pct: totalMs > 0 ? Math.round(10000 * (ms / totalMs)) / 100 : 0,
      };
    });
  }

  /**
   * Terse human-readable span between two snapshot timestamps, e.g. "14s", "1min", "2h 15min", "1d 3h".
   */
  function formatDurationHuman(iso1, iso2) {
    const t1 = new Date(String(iso1)).getTime();
    const t2 = new Date(String(iso2)).getTime();
    if (Number.isNaN(t1) || Number.isNaN(t2)) return "—";
    let ms = t2 - t1;
    if (ms < 0) ms = 0;
    let sec = Math.floor(ms / 1000);
    if (sec === 0) return "0s";

    const days = Math.floor(sec / 86400);
    sec -= days * 86400;
    const hours = Math.floor(sec / 3600);
    sec -= hours * 3600;
    const mins = Math.floor(sec / 60);
    const secs = sec - mins * 60;

    const parts = [];
    if (days) parts.push(`${days}d`);
    if (hours) parts.push(`${hours}h`);
    if (mins) parts.push(`${mins}m`);
    if (secs) parts.push(`${secs}s`);
    return parts.join(" ");
  }

  /** "2026-06-03 19:01:08" / "19:03:25" from ISO; returns "" on parse failure. */
  function formatSnapshotDatePart(iso) {
    const d = new Date(String(iso));
    if (Number.isNaN(d.getTime())) return "";
    const y = d.getUTCFullYear();
    const mo = String(d.getUTCMonth() + 1).padStart(2, "0");
    const da = String(d.getUTCDate()).padStart(2, "0");
    return `${y}-${mo}-${da}`;
  }
  function formatSnapshotTimePart(iso) {
    const d = new Date(String(iso));
    if (Number.isNaN(d.getTime())) return "";
    const h = String(d.getUTCHours()).padStart(2, "0");
    const mi = String(d.getUTCMinutes()).padStart(2, "0");
    const s = String(d.getUTCSeconds()).padStart(2, "0");
    return `${h}:${mi}:${s}`;
  }

  function pgStatActivityBannerAt(tsIso, file) {
    const wrap = el("div", { className: "pgss-activity-banner" });
    const strong = el("strong", { className: "pgss-activity-title" });
    strong.appendChild(document.createTextNode("Activity @ "));
    const ts = el("span", { className: "yb-mono pgss-activity-mono" });
    ts.textContent = formatSnapshotTsFixed(tsIso);
    strong.appendChild(ts);
    if (file) {
      strong.appendChild(document.createTextNode(" — "));
      const fn = el("span", { className: "yb-mono pgss-activity-mono" });
      fn.textContent = String(file);
      strong.appendChild(fn);
    }
    wrap.appendChild(strong);
    return wrap;
  }

  function pgStatActivityBannerDelta(iso1, iso2, file) {
    const wrap = el("div", { className: "pgss-activity-banner" });
    const strong = el("strong", { className: "pgss-activity-title" });
    strong.appendChild(document.createTextNode("Activity "));
    const range = el("span", { className: "yb-mono pgss-activity-mono" });
    const d1 = formatSnapshotDatePart(iso1);
    const d2 = formatSnapshotDatePart(iso2);
    const t1 = formatSnapshotTimePart(iso1);
    const t2 = formatSnapshotTimePart(iso2);
    if (d1 && d2 && t1 && t2) {
      range.textContent =
        d1 === d2
          ? `${d1} ${t1} → ${t2} UTC`
          : `${d1} ${t1} → ${d2} ${t2} UTC`;
    } else {
      range.textContent = `${formatSnapshotTsFixed(iso1)} → ${formatSnapshotTsFixed(iso2)}`;
    }
    strong.appendChild(range);
    strong.appendChild(document.createTextNode(" ("));
    const dur = el("span", { className: "yb-mono pgss-activity-mono" });
    dur.textContent = formatDurationHuman(iso1, iso2);
    strong.appendChild(dur);
    strong.appendChild(document.createTextNode(")"));
    if (file) {
      strong.appendChild(document.createTextNode(" — "));
      const fn = el("span", { className: "yb-mono pgss-activity-mono" });
      fn.textContent = String(file);
      strong.appendChild(fn);
    }
    wrap.appendChild(strong);
    return wrap;
  }

  /** ASH: same banner layout as delta pg_stat, but the interval is the snapshot’s ash_window, not time between snapshots. */
  function ashWindowActivityBanner(doc, file) {
    const w = doc && doc.ash_window;
    if (
      w &&
      w.start_utc != null &&
      w.end_utc != null &&
      String(w.start_utc) !== "" &&
      String(w.end_utc) !== ""
    ) {
      return pgStatActivityBannerDelta(w.start_utc, w.end_utc, file);
    }
    const wrap = el("div", { className: "pgss-activity-banner" });
    wrap.appendChild(
      el("p", {
        className: "pgss-activity-note",
        textContent: "This snapshot has no ASH time window (ash_window); the ASH query interval is unknown.",
      })
    );
    return wrap;
  }

  /** YSQL + no wait_event_aux + no object_name → show object as [PGLayer]. */
  function ashDisplayObjectName(r) {
    const c = r.wait_event_component;
    const aux = r.wait_event_aux;
    const ob = r.object_name;
    const auxEmpty = aux == null || aux === "" || String(aux).trim() === "";
    const obEmpty = ob == null || ob === "" || String(ob).trim() === "";
    if (c != null && String(c).trim().toUpperCase() === "YSQL" && auxEmpty && obEmpty) {
      return "[PGLayer]";
    }
    return obEmpty ? null : String(ob);
  }

  /** Stable tablet/table identity for merging ASH rows: prefer catalog table_id when resolved from tablets. */
  function ashMergeTableKey(r) {
    const tid = r.table_id;
    if (tid != null && String(tid).trim() !== "") return String(tid).trim();
    const disp = ashDisplayObjectName(r);
    return disp != null ? String(disp) : "";
  }

  /** Group ASH rows by displayed object identity + resolved table_id (many aux values share one tablet/table). */
  function ashMergeKey(r) {
    return [
      normQid(r.query_id),
      r.wait_event_component,
      r.wait_event,
      r.wait_event_type,
      ashMergeTableKey(r),
      r.ysql_dbid == null ? "" : String(r.ysql_dbid),
    ].join("\0");
  }

  function mergeAsh(perNode) {
    const merged = new Map();
    Object.keys(perNode || {}).forEach((nid) => {
      (perNode[nid] || []).forEach((r) => {
        const k = ashMergeKey(r);
        if (!merged.has(k)) {
          merged.set(k, {
            ash_merge_key: k,
            query_id: r.query_id,
            wait_event_component: r.wait_event_component,
            wait_event: r.wait_event,
            wait_event_type: r.wait_event_type,
            wait_event_aux: r.wait_event_aux,
            ysql_dbid: r.ysql_dbid != null && r.ysql_dbid !== undefined ? r.ysql_dbid : null,
            namespace_name: r.namespace_name != null ? r.namespace_name : null,
            object_name: r.object_name != null ? r.object_name : null,
            table_id: r.table_id != null && r.table_id !== undefined ? r.table_id : null,
            samples: 0,
            query: r.query || "",
          });
        }
        const m = merged.get(k);
        m.samples += Number(r.samples) || 0;
        if (!m.query && r.query) m.query = r.query;
        m.namespace_name = m.namespace_name || r.namespace_name || null;
        m.object_name = m.object_name || r.object_name || null;
        if ((m.table_id == null || m.table_id === "") && r.table_id != null && String(r.table_id).trim() !== "") {
          m.table_id = r.table_id;
        }
        if (m.ysql_dbid == null && r.ysql_dbid != null && r.ysql_dbid !== undefined) {
          m.ysql_dbid = r.ysql_dbid;
        }
      });
    });
    const rows = Array.from(merged.values()).map((m) =>
      Object.assign({}, m, { object_name: ashDisplayObjectName(m) })
    );
    rows.sort((a, b) => b.samples - a.samples);
    return rows;
  }

  /**
   * Match ASH row to filter id. New snapshots use query_id as text (same as pg_stat queryid) so JS does not
   * lose 64-bit precision. For legacy JSON with query_id as a number, BigInt() compares the true integer.
   */
  function rowMatchesAshQueryIdFilter(r, wantRaw) {
    const want = String(wantRaw).trim();
    if (want === "") return false;
    const a = r.query_id != null && r.query_id !== undefined ? r.query_id : r.queryid;
    if (a == null) return false;
    if (String(a) === want) return true;
    if (typeof BigInt === "function" && /^-?\d+$/.test(want)) {
      try {
        if (BigInt(String(a)) === BigInt(want)) return true;
      } catch (e) {
        return false;
      }
    }
    return false;
  }

  function filterAshPerNodeByQueryId(perNode, qidStr) {
    const want = String(qidStr).trim();
    if (want === "") return perNode;
    const out = {};
    Object.keys(perNode || {}).forEach((nid) => {
      const rows = (perNode[nid] || []).filter((r) => rowMatchesAshQueryIdFilter(r, want));
      if (rows.length) out[nid] = rows;
    });
    return out;
  }

  function filterAshPerNodeByNodeId(perNode, nodeIdStr) {
    const want = String(nodeIdStr || "").trim();
    if (want === "") return perNode;
    const rows = (perNode && perNode[want]) || [];
    return rows.length ? { [want]: rows.slice() } : {};
  }

  function rowMatchesAshTableIdFilter(r, wantRaw) {
    const want = String(wantRaw).trim();
    if (want === "") return false;
    const t = r.table_id;
    return t != null && String(t).trim() === want;
  }

  function filterAshPerNodeByTableId(perNode, tableIdStr) {
    const want = String(tableIdStr || "").trim();
    if (want === "") return perNode;
    const out = {};
    Object.keys(perNode || {}).forEach((nid) => {
      const rows = (perNode[nid] || []).filter((r) => rowMatchesAshTableIdFilter(r, want));
      if (rows.length) out[nid] = rows;
    });
    return out;
  }

  /**
   * Resolve namespace.table_name from yb_local_tablets when ASH has no rows for this table_id.
   * table_id match is case-insensitive trimmed string equality.
   */
  function qualifiedNameFromLocalTablets(doc, tableId) {
    const want = tableId != null ? String(tableId).trim().toLowerCase() : "";
    if (!want || !doc) return "";
    const raw = doc.yb_local_tablets && doc.yb_local_tablets.per_node;
    if (!raw) return "";
    const nodeIds = Object.keys(raw);
    for (let i = 0; i < nodeIds.length; i += 1) {
      const rows = raw[nodeIds[i]] || [];
      for (let j = 0; j < rows.length; j += 1) {
        const r = rows[j];
        if (!r) continue;
        const tid = r.table_id;
        if (tid == null || tid === undefined) continue;
        if (String(tid).trim().toLowerCase() !== want) continue;
        const ns =
          r.namespace_name != null && r.namespace_name !== undefined
            ? String(r.namespace_name).trim()
            : "";
        const tn =
          r.table_name != null && r.table_name !== undefined ? String(r.table_name).trim() : "";
        if (ns && tn) return `${ns}.${tn}`;
        if (tn) return tn;
        if (ns) return ns;
      }
    }
    return "";
  }

  function ashSubtitleNsObjectForTableId(doc, tableId) {
    if (!doc || !tableId) return "";
    const raw = doc.yb_active_session_history && doc.yb_active_session_history.per_node;
    if (raw) {
      const f = filterAshPerNodeByTableId(raw, tableId);
      const rows = mergeAsh(f);
      const pick = rows.find((r) => r.namespace_name || r.object_name) || rows[0];
      if (pick) {
        const ns = pick.namespace_name != null ? String(pick.namespace_name) : "";
        const ob = pick.object_name != null ? String(pick.object_name) : "";
        if (ns && ob) return `${ns}.${ob}`;
        const partial = (ns || ob || "").trim();
        if (partial) return partial;
      }
    }
    return qualifiedNameFromLocalTablets(doc, tableId);
  }

  /** DDL/schema for a table_id from snapshot table_schemas.by_table_id (exact id match). */
  function tableSchemaForTableId(doc, tableId) {
    if (!doc || tableId == null || String(tableId).trim() === "") return null;
    const root = doc.table_schemas && doc.table_schemas.by_table_id;
    if (!root || typeof root !== "object") return null;
    const want = String(tableId).trim();
    if (root[want]) return root[want];
    const lower = want.toLowerCase();
    const keys = Object.keys(root);
    for (let i = 0; i < keys.length; i += 1) {
      if (String(keys[i]).trim().toLowerCase() === lower) return root[keys[i]];
    }
    return null;
  }

  /** Cloud · region · zone from node_topology for banner subtitle. */
  function ashNodePlacementLine(topo, nodeId) {
    const nid = nodeId != null ? String(nodeId) : "";
    if (!nid) return "";
    const t = (topo && topo[nid]) || {};
    const cloud = t.cloud != null && t.cloud !== undefined ? String(t.cloud).trim() : "";
    const region = t.region != null && t.region !== undefined ? String(t.region).trim() : "";
    const zone = t.zone != null && t.zone !== undefined ? String(t.zone).trim() : "";
    const parts = [cloud, region, zone].filter((x) => x !== "");
    return parts.join(" · ");
  }

  function getFirstQueryTextForFilter(doc, qid) {
    if (!doc) return null;
    const raw = doc.yb_active_session_history && doc.yb_active_session_history.per_node;
    if (!raw) return null;
    const f = filterAshPerNodeByQueryId(raw, qid);
    const keys = Object.keys(f);
    for (let i = 0; i < keys.length; i += 1) {
      const rows = f[keys[i]] || [];
      for (let j = 0; j < rows.length; j += 1) {
        if (rows[j].query) return String(rows[j].query);
      }
    }
    return null;
  }

  /** First matching query text from pg_stat_statements or ycql_stat_statements per_node. */
  function lookupStatementQueryText(doc, qid) {
    const want = normQid(qid);
    if (want == null) return null;
    const sections = [
      doc && doc.pg_stat_statements && doc.pg_stat_statements.per_node,
      doc && doc.ycql_stat_statements && doc.ycql_stat_statements.per_node,
    ];
    for (let s = 0; s < sections.length; s += 1) {
      const perNode = sections[s];
      if (!perNode) continue;
      const stmtKeys = Object.keys(perNode);
      for (let i = 0; i < stmtKeys.length; i += 1) {
        const rows = perNode[stmtKeys[i]] || [];
        for (let j = 0; j < rows.length; j += 1) {
          const r = rows[j];
          if (normQid(r && r.queryid) === want && r.query) return String(r.query);
        }
      }
    }
    return null;
  }

  function getQueryTextForToolbar(doc, qid) {
    const want = normQid(qid);
    const bg = backgroundAshQueryLabel(want);
    if (bg) return bg;
    const fromAsh = getFirstQueryTextForFilter(doc, qid);
    if (fromAsh) return fromAsh;
    return lookupStatementQueryText(doc, qid);
  }

  /** queryid string → query text from pg_stat_statements and ycql_stat_statements (first hit per id). */
  function buildPgStatQueryTextByQueryId(doc) {
    const map = new Map();
    const sections = [
      doc && doc.pg_stat_statements && doc.pg_stat_statements.per_node,
      doc && doc.ycql_stat_statements && doc.ycql_stat_statements.per_node,
    ];
    sections.forEach((st) => {
      if (!st) return;
      Object.keys(st).forEach((nid) => {
        (st[nid] || []).forEach((r) => {
          const id =
            r && r.queryid != null && r.queryid !== undefined ? String(r.queryid).trim() : "";
          if (!id || map.has(id)) return;
          const q = r.query != null && r.query !== undefined ? String(r.query).trim() : "";
          if (q) map.set(id, q);
        });
      });
    });
    return map;
  }

  /**
   * Fill ASH row.query: reserved background query_ids (1–13) get fixed labels; else pg_stat when absent.
   * Legacy snapshots that still embed query on ASH rows keep it unless the row is a reserved background id.
   */
  function enrichAshRowsQueryFromPgStat(doc, rows) {
    if (!rows || !rows.length) return rows;
    const map = buildPgStatQueryTextByQueryId(doc);
    return rows.map((r) => {
      const qidRaw = r.query_id != null && r.query_id !== undefined ? r.query_id : r.queryid;
      const qid = qidRaw != null && qidRaw !== undefined ? String(qidRaw).trim() : "";
      const bg = backgroundAshQueryLabel(qid);
      if (bg) return Object.assign({}, r, { query: bg });
      const existing = r.query != null && String(r.query).trim() !== "" ? String(r.query) : "";
      if (existing) return r;
      const fromStmt = qid ? map.get(qid) : "";
      if (!fromStmt) return r;
      return Object.assign({}, r, { query: fromStmt });
    });
  }

  function updateAshFilterToolbar() {
    /* Reserved: header no longer shows ASH filter context (details are in the ASH panel). */
  }

  /** When ASH is scoped to one query_id, table columns for query / query_id are redundant. */
  function ashColumnsWithoutQueryIdAndQuery(cols) {
    return cols.filter((c) => c.key !== "query_id" && c.key !== "query");
  }

  function buildAshQueryHref(qid) {
    const p = new URLSearchParams();
    p.set("view", "ash");
    p.set("query", String(qid));
    return `${window.location.pathname}?${p.toString()}`;
  }

  function buildAshNodeHref(nodeId) {
    const p = new URLSearchParams();
    p.set("view", "ash");
    p.set("node", String(nodeId));
    return `${window.location.pathname}?${p.toString()}`;
  }

  function buildAshTableIdHref(tableId) {
    const p = new URLSearchParams();
    p.set("view", "ash");
    p.set("table_id", String(tableId));
    return `${window.location.pathname}?${p.toString()}`;
  }

  function navigateToAshForQueryId(qid) {
    const s = String(qid).trim();
    if (!s) return;
    ashQueryIdFilter = s;
    ashNodeIdFilter = null;
    ashTableIdFilter = null;
    activeViewerSection = "ash";
    /* pushState so the browser Back button returns to the prior tab (e.g. statements). */
    writeViewerStateToUrl({ push: true });
    if (lastDoc) {
      renderDoc(lastDoc, lastPrevDoc);
    }
  }

  function navigateToAshForNodeId(nodeId) {
    const s = String(nodeId).trim();
    if (!s) return;
    ashNodeIdFilter = s;
    ashQueryIdFilter = null;
    ashTableIdFilter = null;
    activeViewerSection = "ash";
    writeViewerStateToUrl({ push: true });
    if (lastDoc) {
      renderDoc(lastDoc, lastPrevDoc);
    }
  }

  function navigateToAshForTableId(tableId) {
    const s = String(tableId).trim();
    if (!s) return;
    ashTableIdFilter = s;
    ashQueryIdFilter = null;
    ashNodeIdFilter = null;
    activeViewerSection = "ash";
    writeViewerStateToUrl({ push: true });
    if (lastDoc) {
      renderDoc(lastDoc, lastPrevDoc);
    }
  }

  /**
   * Per-node ASH rows with topology fields and display object_name.
   * `ash_flat_bucket_key` is ashMergeKey(snapshot row) before object_name display normalization so per-node
   * accumulation matches mergeAsh buckets (normalize-only differs from merge key).
   */
  function flattenAsh(perNode, topo) {
    const out = [];
    Object.keys(perNode || {}).forEach((nid) => {
      const t = (topo && topo[nid]) || {};
      (perNode[nid] || []).forEach((r) => {
        const ash_flat_bucket_key = ashMergeKey(r);
        const row = Object.assign({}, r, {
          node_id: nid,
          cloud: t.cloud || "",
          region: t.region || "",
          zone: t.zone || "",
          ash_flat_bucket_key,
        });
        row.object_name = ashDisplayObjectName(row);
        out.push(row);
      });
    });
    return out;
  }

  function groupSum(rows, keyFn) {
    const m = new Map();
    rows.forEach((r) => {
      const k = keyFn(r);
      const prev = m.get(k) || { key: k, samples: 0 };
      prev.samples += Number(r.samples) || 0;
      m.set(k, prev);
    });
    return Array.from(m.values()).sort((a, b) => b.samples - a.samples);
  }

  /** Sum samples by node_id; attach cloud/region/zone from first seen row per node (topology is per-node). */
  function sumAshByNode(rows) {
    const m = new Map();
    rows.forEach((r) => {
      const nid = r.node_id;
      const add = Number(r.samples) || 0;
      if (!m.has(nid)) {
        m.set(nid, {
          node_id: nid,
          cloud: r.cloud != null && r.cloud !== undefined ? String(r.cloud) : "",
          region: r.region != null && r.region !== undefined ? String(r.region) : "",
          zone: r.zone != null && r.zone !== undefined ? String(r.zone) : "",
          samples: 0,
        });
      }
      const ent = m.get(nid);
      ent.samples += add;
    });
    return Array.from(m.values()).sort((a, b) => b.samples - a.samples);
  }

  /** Nodes in cluster from topology when present, else ASH per_node keys. */
  function ashSnapshotClusterNodeCount(doc, ashPerNode) {
    const topo = doc && doc.node_topology;
    if (topo && typeof topo === "object") {
      const k = Object.keys(topo);
      if (k.length > 0) return k.length;
    }
    return Object.keys(ashPerNode || {}).length;
  }

  /**
   * Per-node sample sums for one merge bucket: flat rows whose `ash_flat_bucket_key` (or ashMergeKey fallback)
   * equals `wantKey`. Scans flatRows so lookup does not depend on Map key identity.
   */
  function buildNodeSampleMapForMergeKey(flatRows, wantKey) {
    if (wantKey == null || String(wantKey) === "") return null;
    const want = String(wantKey);
    const nm = new Map();
    (flatRows || []).forEach((r) => {
      const fk =
        r.ash_flat_bucket_key != null && String(r.ash_flat_bucket_key) !== ""
          ? String(r.ash_flat_bucket_key)
          : ashMergeKey(r);
      if (fk !== want) return;
      const nid = r.node_id != null && r.node_id !== undefined ? String(r.node_id).trim() : "";
      if (!nid) return;
      const add = Number(r.samples) || 0;
      nm.set(nid, (nm.get(nid) || 0) + add);
    });
    return nm;
  }

  /** Flat ASH rows grouped by bucketKeyFn → node_id → sample sum. */
  function accumulateAshBucketNodeSamples(flatRows, bucketKeyFn) {
    const out = new Map();
    (flatRows || []).forEach((r) => {
      const bk = bucketKeyFn(r);
      const nid = r.node_id != null && r.node_id !== undefined ? String(r.node_id) : "";
      if (!nid) return;
      const add = Number(r.samples) || 0;
      if (!out.has(bk)) out.set(bk, new Map());
      const nm = out.get(bk);
      nm.set(nid, (nm.get(nid) || 0) + add);
    });
    return out;
  }

  function summarizeAshNodeLoadPct(nodeMap) {
    if (!nodeMap || nodeMap.size === 0) return null;
    const pairs = Array.from(nodeMap.entries())
      .map(([nid, s]) => [String(nid), Number(s) || 0])
      .filter(([, s]) => s > 0);
    if (!pairs.length) return null;
    const M = pairs.reduce((acc, [, s]) => acc + s, 0);
    if (M <= 0) return null;
    pairs.sort((a, b) => b[1] - a[1]);
    const slice = pairs.slice(0, 5);
    const parts = slice.map(([nid, s]) => ({
      node_id: nid,
      pct: (100 * s) / M,
    }));
    return {
      parts,
      ellipsis: pairs.length > 5,
    };
  }

  /**
   * @param {boolean} [useMergeBucketScan] — Top-level merged ASH rows: scan flat rows by `ash_merge_key` ==
   *   `ash_flat_bucket_key` (avoids Map lookup / key recomputation mismatches). Other rollups keep false.
   */
  function attachAshNodeLoadDistribution(rows, flatRows, bucketKeyFn, enabled, useMergeBucketScan) {
    if (!enabled || !rows || !flatRows || typeof bucketKeyFn !== "function") return rows || [];
    if (useMergeBucketScan) {
      return rows.map((r) => {
        const bk =
          r.ash_merge_key != null && String(r.ash_merge_key) !== ""
            ? String(r.ash_merge_key)
            : bucketKeyFn(r);
        const nodeMap = buildNodeSampleMapForMergeKey(flatRows, bk);
        return {
          ...r,
          ash_node_load_distribution: summarizeAshNodeLoadPct(nodeMap),
        };
      });
    }
    const acc = accumulateAshBucketNodeSamples(flatRows, bucketKeyFn);
    return rows.map((r) => {
      const bk =
        r.ash_merge_key != null && String(r.ash_merge_key) !== ""
          ? String(r.ash_merge_key)
          : bucketKeyFn(r);
      return {
        ...r,
        ash_node_load_distribution: summarizeAshNodeLoadPct(acc.get(bk)),
      };
    });
  }

  function bucketKeyAshQueryIdFlat(r) {
    const raw = r.query_id != null && r.query_id !== undefined ? r.query_id : null;
    return raw != null && String(raw).trim() !== "" ? String(raw).trim() : "\0__no_query_id__";
  }

  function bucketKeyAshNamespaceQueryFlat(r) {
    const nn =
      r.namespace_name != null && r.namespace_name !== undefined ? String(r.namespace_name) : "";
    const q = r.query != null && r.query !== undefined ? String(r.query) : "";
    return JSON.stringify([nn, q]);
  }

  function bucketKeyAshNsObjBucketFlat(r) {
    const nn =
      r.namespace_name != null && r.namespace_name !== undefined ? String(r.namespace_name) : "";
    const tid =
      r.table_id != null && r.table_id !== undefined && String(r.table_id).trim() !== ""
        ? String(r.table_id).trim()
        : "";
    const on = r.object_name != null && r.object_name !== undefined ? String(r.object_name) : "";
    return tid ? `${nn}\0tid:${tid}` : `${nn}\0${on}`;
  }

  function bucketKeyAshNsObjQueryFlatFactory(ignoreQueryInKey) {
    return function bucketKeyAshNsObjQueryFlat(r) {
      const nn =
        r.namespace_name != null && r.namespace_name !== undefined ? String(r.namespace_name) : "";
      const on = r.object_name != null && r.object_name !== undefined ? String(r.object_name) : "";
      const tid =
        r.table_id != null && r.table_id !== undefined && String(r.table_id).trim() !== ""
          ? String(r.table_id).trim()
          : "";
      const q = r.query != null && r.query !== undefined ? String(r.query) : "";
      const dim = tid ? `tid:${tid}` : on;
      return ignoreQueryInKey ? JSON.stringify([nn, dim]) : JSON.stringify([nn, dim, q]);
    };
  }

  function bucketKeyAshCrzFlat(r) {
    const c = r.cloud != null && r.cloud !== undefined ? String(r.cloud) : "";
    const reg = r.region != null && r.region !== undefined ? String(r.region) : "";
    const z = r.zone != null && r.zone !== undefined ? String(r.zone) : "";
    return `${c}\t${reg}\t${z}`;
  }

  function bucketKeyAshDbFlat(r) {
    return String(r.namespace_name || "(none)");
  }

  function spliceAshNodeLoadDistributionColumn(baseCols, clusterNodeCount, enabled) {
    if (!enabled || clusterNodeCount <= 1) return baseCols;
    const col = {
      key: "ash_node_load_distribution",
      label: `Load Distribution % (across ${clusterNodeCount} nodes)`,
      sortable: false,
      type: "number",
    };
    const out = baseCols.slice();
    const qidIdx = out.findIndex((c) => c.key === "query_id");
    if (qidIdx >= 0) {
      out.splice(qidIdx, 0, col);
      return out;
    }
    out.push(col);
    return out;
  }

  /** Group merged ASH rows by namespace + query; sum samples. */
  function groupAshByNamespaceQuery(rows) {
    const m = new Map();
    (rows || []).forEach((r) => {
      const nn = r.namespace_name != null && r.namespace_name !== undefined ? String(r.namespace_name) : "";
      const q = r.query != null && r.query !== undefined ? String(r.query) : "";
      const k = JSON.stringify([nn, q]);
      if (!m.has(k)) {
        m.set(k, {
          namespace_name: nn,
          query_id: r.query_id != null && r.query_id !== undefined ? r.query_id : null,
          query: q,
          samples: 0,
        });
      }
      const ent = m.get(k);
      ent.samples += Number(r.samples) || 0;
      if ((ent.query_id == null || ent.query_id === "") && r.query_id != null && r.query_id !== undefined) {
        ent.query_id = r.query_id;
      }
    });
    return Array.from(m.values()).sort((a, b) => b.samples - a.samples);
  }

  /** Group merged ASH by pg_stat query id (query_id); sum samples. Rows without query_id bucket together. */
  function groupAshByQueryId(rows) {
    const m = new Map();
    (rows || []).forEach((r) => {
      const raw = r.query_id != null && r.query_id !== undefined ? r.query_id : null;
      const k =
        raw != null && String(raw).trim() !== "" ? String(raw).trim() : "\0__no_query_id__";
      if (!m.has(k)) {
        m.set(k, {
          query_id: k === "\0__no_query_id__" ? null : raw,
          namespace_name:
            r.namespace_name != null && r.namespace_name !== undefined ? String(r.namespace_name) : "",
          query: r.query != null && r.query !== undefined ? String(r.query) : "",
          samples: 0,
        });
      }
      const ent = m.get(k);
      ent.samples += Number(r.samples) || 0;
      if ((!ent.query || ent.query === "") && r.query) ent.query = String(r.query);
      if ((!ent.namespace_name || ent.namespace_name === "") && r.namespace_name) {
        ent.namespace_name = String(r.namespace_name);
      }
    });
    return Array.from(m.values()).sort((a, b) => b.samples - a.samples);
  }

  /**
   * Group merged ASH rows by namespace + tablet/table identity + optional query text.
   * When `ignoreQueryInKey` is true (query_id–scoped UI), omit `query` from the key so DocDB rows
   * (`query: null`) and YSQL rows with the statement text do not split into duplicate-looking groups.
   */
  function groupAshByNamespaceObjectQuery(rows, options) {
    const ignoreQueryInKey = options && options.ignoreQueryInKey;
    const m = new Map();
    (rows || []).forEach((r) => {
      const nn = r.namespace_name != null && r.namespace_name !== undefined ? String(r.namespace_name) : "";
      const on = r.object_name != null && r.object_name !== undefined ? String(r.object_name) : "";
      const tid =
        r.table_id != null && r.table_id !== undefined && String(r.table_id).trim() !== ""
          ? String(r.table_id).trim()
          : "";
      const q = r.query != null && r.query !== undefined ? String(r.query) : "";
      const dim = tid ? `tid:${tid}` : on;
      const k = ignoreQueryInKey ? JSON.stringify([nn, dim]) : JSON.stringify([nn, dim, q]);
      if (!m.has(k)) {
        m.set(k, {
          namespace_name: nn,
          object_name: on,
          table_id: tid || null,
          query: q,
          query_id: r.query_id != null && r.query_id !== undefined ? r.query_id : null,
          samples: 0,
        });
      }
      const ent = m.get(k);
      ent.samples += Number(r.samples) || 0;
      if ((!ent.object_name || ent.object_name === "") && on) ent.object_name = on;
      if (!ent.table_id && tid) ent.table_id = tid;
      if ((!ent.query || ent.query === "") && q) ent.query = q;
      if ((ent.query_id == null || ent.query_id === "") && r.query_id != null && r.query_id !== undefined) {
        ent.query_id = r.query_id;
      }
    });
    return Array.from(m.values()).sort((a, b) => b.samples - a.samples);
  }

  /** Namespace × object/table buckets for Top-50 charts (groups by table_id when known). */
  function ashAggregateNsObjectBuckets(rows) {
    const m = new Map();
    (rows || []).forEach((r) => {
      const nn = r.namespace_name != null && r.namespace_name !== undefined ? String(r.namespace_name) : "";
      const tid =
        r.table_id != null && r.table_id !== undefined && String(r.table_id).trim() !== ""
          ? String(r.table_id).trim()
          : "";
      const on = r.object_name != null && r.object_name !== undefined ? String(r.object_name) : "";
      const k = tid ? `${nn}\0tid:${tid}` : `${nn}\0${on}`;
      if (!m.has(k)) {
        m.set(k, {
          namespace_name: nn,
          object_name: on,
          table_id: tid || null,
          samples: 0,
        });
      }
      const ent = m.get(k);
      ent.samples += Number(r.samples) || 0;
      if ((!ent.object_name || ent.object_name === "") && on) ent.object_name = on;
      if (!ent.table_id && tid) ent.table_id = tid;
    });
    return Array.from(m.values()).sort((a, b) => b.samples - a.samples);
  }

  /**
   * ASH: add load_pct = 100 * row.samples / sum(samples over totalRows).
   * When `totalRows` is set (e.g. full set before a Top-50 slice), the denominator uses that;
   * otherwise the sum is over `pageRows` only.
   */
  function withAshLoadPercent(pageRows, totalRows) {
    const forTotal = totalRows != null && totalRows !== undefined ? totalRows : pageRows;
    const total = (forTotal || []).reduce((s, r) => s + (Number(r.samples) || 0), 0);
    return (pageRows || []).map((r) => ({
      ...r,
      load_pct: total > 0 ? Math.round(10000 * ((Number(r.samples) || 0) / total)) / 100 : 0,
    }));
  }

  /** Half-open [ash_window.start_utc, ash_window.end_utc) length in seconds; min ~1e-9 to avoid div-by-zero. */
  function ashWindowIntervalSeconds(snap) {
    const w = snap && snap.ash_window;
    if (!w) return 1;
    const t1 = new Date(String(w.start_utc || "")).getTime();
    const t2 = new Date(String(w.end_utc || "")).getTime();
    if (Number.isNaN(t1) || Number.isNaN(t2) || t2 <= t1) return 1;
    return Math.max(1e-9, (t2 - t1) / 1000);
  }

  /**
   * ASH: sessions_per_sec = samples / window_seconds (per snapshot ash_window in JSON).
   * Raw `samples` is kept for load %.
   */
  function withAshSessionsPerSec(rows, intervalSec) {
    const d = Math.max(1e-9, Number(intervalSec) || 0);
    return (rows || []).map((r) => ({
      ...r,
      sessions_per_sec: (Number(r.samples) || 0) / d,
    }));
  }

  function formatAshSessionsPerSec(n) {
    if (n == null || n === "") return "";
    const x = Number(n);
    if (Number.isNaN(x)) return String(n);
    if (x === 0) return "0";
    if (x >= 100) return x.toFixed(2);
    return x.toFixed(3);
  }

  /** pg_stat rows/call and DocDB per-call metrics: one decimal for aligned columns */
  function formatPgStatPerCallMetric(raw) {
    if (raw === null || raw === undefined || raw === "") return "";
    const x = Number(raw);
    if (Number.isNaN(x)) return "";
    return x.toFixed(1);
  }

  /** pg_stat time (ms) and mean_ms: two fractional digits for alignment */
  function formatPgStatMsTwoDecimals(raw) {
    if (raw === null || raw === undefined || raw === "") return "";
    const x = Number(raw);
    if (Number.isNaN(x)) return "";
    return x.toFixed(2);
  }

  function tabletTableKey(namespaceName, tableName) {
    const ns = namespaceName != null && namespaceName !== undefined ? String(namespaceName).trim() : "";
    const tn = tableName != null && tableName !== undefined ? String(tableName).trim() : "";
    return `${ns}\0${tn}`;
  }

  /** Distribution reports count only tablets whose `state` is TABLET_DATA_READY (case-insensitive). */
  function filterLocalTabletsDataReady(perNode) {
    const want = "TABLET_DATA_READY";
    const out = {};
    Object.keys(perNode || {}).forEach((nid) => {
      out[nid] = (perNode[nid] || []).filter((r) => {
        const s = r && r.state != null ? String(r.state).trim().toUpperCase() : "";
        return s === want;
      });
    });
    return out;
  }

  function flattenLocalTablets(perNode, topo) {
    const out = [];
    Object.keys(perNode || {}).forEach((nid) => {
      const t = (topo && topo[nid]) || {};
      (perNode[nid] || []).forEach((r) => {
        out.push(
          Object.assign({}, r, {
            node_id: nid,
            cloud: t.cloud != null && t.cloud !== undefined ? String(t.cloud) : "",
            region: t.region != null && t.region !== undefined ? String(t.region) : "",
            zone: t.zone != null && t.zone !== undefined ? String(t.zone) : "",
          })
        );
      });
    });
    return out;
  }

  /** Per logical table: total tablets and per-node counts (desc); node id only in tooltips. */
  function tabletsPerTableReport(perNode) {
    const byTable = new Map();
    const tableIdByKey = new Map();
    Object.keys(perNode || {}).forEach((nid) => {
      (perNode[nid] || []).forEach((r) => {
        const k = tabletTableKey(r.namespace_name, r.table_name);
        if (!byTable.has(k)) byTable.set(k, new Map());
        const byNode = byTable.get(k);
        byNode.set(nid, (byNode.get(nid) || 0) + 1);
        const tid = r.table_id;
        if (
          !tableIdByKey.has(k) &&
          tid != null &&
          tid !== undefined &&
          String(tid).trim() !== ""
        ) {
          tableIdByKey.set(k, String(tid).trim());
        }
      });
    });
    const rows = [];
    byTable.forEach((byNode, k) => {
      const parts = String(k).split("\0");
      const ns = parts[0] || "";
      const tbl = parts.length > 1 ? parts.slice(1).join("\0") : "";
      let total = 0;
      byNode.forEach((c) => {
        total += c;
      });
      const perNodeCounts = Array.from(byNode.entries())
        .map(([node_id, count]) => ({ node_id, count }))
        .sort((a, b) => b.count - a.count);
      rows.push({
        namespace_name: ns,
        table_name: tbl || "(unknown)",
        table_id: tableIdByKey.has(k) ? tableIdByKey.get(k) : null,
        tablets: total,
        per_node_counts: perNodeCounts,
      });
    });
    rows.sort((a, b) => b.tablets - a.tablets);
    return rows;
  }

  function tabletsPerNodeReport(perNode, topo) {
    const rows = Object.keys(perNode || {}).map((nid) => {
      const t = (topo && topo[nid]) || {};
      return {
        node_id: nid,
        tablets: (perNode[nid] || []).length,
        cloud: t.cloud != null && t.cloud !== undefined ? String(t.cloud) : "",
        region: t.region != null && t.region !== undefined ? String(t.region) : "",
        zone: t.zone != null && t.zone !== undefined ? String(t.zone) : "",
      };
    });
    rows.sort((a, b) => b.tablets - a.tablets);
    return rows;
  }

  /** Tablet counts grouped by placement triple from node topology. */
  function tabletsPerCloudRegionZoneReport(perNode, topo) {
    const flat = flattenLocalTablets(perNode, topo);
    const m = new Map();
    flat.forEach((r) => {
      const c = String(r.cloud || "").trim();
      const reg = String(r.region || "").trim();
      const z = String(r.zone || "").trim();
      const k = `${c}\t${reg}\t${z}`;
      m.set(k, (m.get(k) || 0) + 1);
    });
    const rows = Array.from(m.entries()).map(([key, tablets]) => {
      const p = String(key).split("\t");
      return {
        cloud: p[0] != null ? p[0] : "",
        region: p[1] != null ? p[1] : "",
        zone: p[2] != null ? p[2] : "",
        tablets,
      };
    });
    rows.sort((a, b) => b.tablets - a.tablets);
    return rows;
  }

  async function fetchJson(url) {
    const res = await fetch(url, { cache: "no-store" });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.json();
  }

  async function loadManifest() {
    const raw = await fetchJson(MANIFEST);
    if (Array.isArray(raw)) return raw;
    if (raw && Array.isArray(raw.entries)) return raw.entries;
    return [];
  }

  function setStatus(msg, isErr) {
    const s = document.getElementById("status-msg");
    s.textContent = msg || "";
    s.style.color = isErr ? "var(--yb-danger)" : "var(--yb-muted)";
  }

  function copyText(text) {
    const t = String(text || "");
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(t).catch(() => fallbackCopy(t));
    }
    return fallbackCopy(t);
  }

  function fallbackCopy(t) {
    const ta = document.createElement("textarea");
    ta.value = t;
    ta.style.position = "fixed";
    ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand("copy");
    } finally {
      document.body.removeChild(ta);
    }
    return Promise.resolve();
  }

  /**
   * Table cell keys shown in a fixed-width font — SQL, names, ids, wait events.
   * Cells for columns with type "number" also use yb-mono (numeric alignment); headers stay proportional.
   */
  const MONO_TABLE_CELL_KEYS = new Set([
    "query",
    "queryid",
    "query_id",
    "namespace_name",
    "object_name",
    "namespace_objname",
    "db_name",
    "dbname",
    "relname",
    "tablet_id",
    "table_name",
    "node_id",
    "leader",
    "wait_event",
    "wait_event_component",
    "wait_event_type",
    "wait_event_aux",
    "ysql_dbid",
    "cloud",
    "region",
    "zone",
    "cloud_region_zone",
    "cloud_region",
  ]);

  function applyMonoTableCellClass(td, colOrKey) {
    const key = typeof colOrKey === "string" ? colOrKey : colOrKey && colOrKey.key;
    const type = typeof colOrKey === "object" && colOrKey ? colOrKey.type : undefined;
    if (key === "query" || MONO_TABLE_CELL_KEYS.has(key) || type === "number") {
      td.classList.add("yb-mono");
    }
  }

  let _queryTipEl = null;
  let _queryTipShowTimer = null;
  let _queryTipHideTimer = null;
  let _queryTipGlobalWired = false;

  const HOVER_NODE_TIP_SHOW_MS = 45;

  let _hoverTipEl = null;
  let _hoverTipShowTimer = null;
  let _hoverTipHideTimer = null;

  function hideHoverTooltipImmediate() {
    if (_hoverTipShowTimer) {
      clearTimeout(_hoverTipShowTimer);
      _hoverTipShowTimer = null;
    }
    if (_hoverTipHideTimer) {
      clearTimeout(_hoverTipHideTimer);
      _hoverTipHideTimer = null;
    }
    if (_hoverTipEl && _hoverTipEl.classList.contains("query-tooltip-popup-visible")) {
      resetTooltipPopupMotion(_hoverTipEl);
      _hoverTipEl.classList.remove("query-tooltip-popup-visible");
      _hoverTipEl.setAttribute("aria-hidden", "true");
    }
  }

  function scheduleHideHoverTooltip() {
    if (_hoverTipHideTimer) {
      clearTimeout(_hoverTipHideTimer);
    }
    _hoverTipHideTimer = setTimeout(() => {
      if (_hoverTipEl) {
        resetTooltipPopupMotion(_hoverTipEl);
        _hoverTipEl.classList.remove("query-tooltip-popup-visible");
        _hoverTipEl.setAttribute("aria-hidden", "true");
      }
      _hoverTipHideTimer = null;
    }, 100);
  }

  function getHoverTooltipEl() {
    if (_hoverTipEl) {
      return _hoverTipEl;
    }
    ensureQueryTipDismissOnScrollResize();
    _hoverTipEl = el("div", {
      className: "query-tooltip-popup yb-hover-tooltip-popup",
      "aria-hidden": "true",
      role: "tooltip",
    });
    document.body.appendChild(_hoverTipEl);
    return _hoverTipEl;
  }

  /** Clear inline placement between shows (both SQL and quick hovers). */
  function resetTooltipPopupMotion(tip) {
    if (!tip) return;
    tip.style.transform = "";
    tip.style.left = "";
    tip.style.top = "";
    tip.style.right = "";
    tip.style.bottom = "";
  }

  function positionAndShowTooltipPopup(tip, anchorRect, textContent) {
    const margin = 8;
    const maxW = Math.min(window.innerWidth * 0.92, 52 * 16);
    const ax = anchorRect != null ? Number(anchorRect.left) : NaN;
    const ayBottom = anchorRect != null ? Number(anchorRect.bottom) : NaN;
    const ayTop = anchorRect != null ? Number(anchorRect.top) : NaN;

    resetTooltipPopupMotion(tip);
    tip.textContent = textContent;
    tip.setAttribute("aria-hidden", "false");
    tip.style.position = "fixed";
    tip.style.right = "auto";
    tip.style.bottom = "auto";
    tip.style.transform = "none";
    tip.style.maxWidth = `${maxW}px`;

    let tx = Number.isFinite(ax) ? ax : margin;
    if (tx + maxW > window.innerWidth - margin) {
      tx = Math.max(margin, window.innerWidth - maxW - margin);
    }

    let ty = Number.isFinite(ayBottom) ? ayBottom - 1 : margin;

    tip.style.left = "-99999px";
    tip.style.top = "0px";

    tip.classList.add("query-tooltip-popup-visible");

    let th = tip.offsetHeight;
    const maxH = window.innerHeight - 2 * margin;
    if (th > maxH) {
      tip.style.maxHeight = `${maxH}px`;
      th = tip.offsetHeight;
    } else {
      tip.style.maxHeight = "";
    }

    if (Number.isFinite(ayTop) && ty + th > window.innerHeight - margin) {
      const up = ayTop - th + 1;
      if (up >= margin) {
        ty = up;
      } else {
        ty = margin;
        tip.style.maxHeight = `${window.innerHeight - 2 * margin}px`;
      }
    }

    tip.style.left = `${Math.round(tx)}px`;
    tip.style.top = `${Math.round(ty)}px`;
  }

  /**
   * Node-id / chip hovers: place by pointer using measured box size (not assumed max width).
   * Repositions on mousemove while visible so the label stays next to the cursor.
   */
  function positionAndShowHoverTooltipNearCursor(clientX, clientY, text) {
    const tip = getHoverTooltipEl();
    const margin = 8;
    const offset = 12;
    const txt = String(text || "");

    resetTooltipPopupMotion(tip);
    tip.textContent = txt;
    tip.setAttribute("aria-hidden", "false");
    tip.style.position = "fixed";
    tip.style.right = "auto";
    tip.style.bottom = "auto";
    tip.style.transform = "none";

    tip.style.left = "-99999px";
    tip.style.top = "0px";

    tip.classList.add("query-tooltip-popup-visible");

    let cx = Number(clientX);
    let cy = Number(clientY);
    if (!Number.isFinite(cx)) cx = margin + offset;
    if (!Number.isFinite(cy)) cy = margin + offset;

    const vw = window.innerWidth;
    const vh = window.innerHeight;

    const tw = tip.offsetWidth;
    const th = tip.offsetHeight;

    let left = cx + offset;
    let top = cy + offset;

    if (left + tw > vw - margin) {
      left = vw - tw - margin;
    }
    if (top + th > vh - margin) {
      top = cy - th - offset;
    }
    if (left < margin) left = margin;
    if (top < margin) top = margin;

    tip.style.left = `${Math.round(left)}px`;
    tip.style.top = `${Math.round(top)}px`;
  }

  /** Fast custom tooltip (node_id); avoids slow native title= delay. */
  function wireQuickNodeIdTooltip(anchorEl, nodeId) {
    const t = String(nodeId || "").trim();
    if (!t || !anchorEl) return;
    let tipTrackX = 0;
    let tipTrackY = 0;
    function onTipPointerMove(e) {
      tipTrackX = e.clientX;
      tipTrackY = e.clientY;
      if (_hoverTipEl && _hoverTipEl.classList.contains("query-tooltip-popup-visible")) {
        positionAndShowHoverTooltipNearCursor(tipTrackX, tipTrackY, t);
      }
    }
    anchorEl.addEventListener("mouseenter", (e) => {
      tipTrackX = e.clientX;
      tipTrackY = e.clientY;
      document.addEventListener("mousemove", onTipPointerMove, { passive: true });
      hideQueryTooltipImmediate();
      if (_hoverTipHideTimer) {
        clearTimeout(_hoverTipHideTimer);
        _hoverTipHideTimer = null;
      }
      if (_hoverTipShowTimer) {
        clearTimeout(_hoverTipShowTimer);
      }
      _hoverTipShowTimer = setTimeout(() => {
        requestAnimationFrame(() => {
          positionAndShowHoverTooltipNearCursor(tipTrackX, tipTrackY, t);
        });
        _hoverTipShowTimer = null;
      }, HOVER_NODE_TIP_SHOW_MS);
    });
    anchorEl.addEventListener("mouseleave", () => {
      document.removeEventListener("mousemove", onTipPointerMove);
      if (_hoverTipShowTimer) {
        clearTimeout(_hoverTipShowTimer);
        _hoverTipShowTimer = null;
      }
      scheduleHideHoverTooltip();
    });
  }

  function hideQueryTooltipImmediate() {
    if (_queryTipShowTimer) {
      clearTimeout(_queryTipShowTimer);
      _queryTipShowTimer = null;
    }
    if (_queryTipHideTimer) {
      clearTimeout(_queryTipHideTimer);
      _queryTipHideTimer = null;
    }
    if (_queryTipEl && _queryTipEl.classList.contains("query-tooltip-popup-visible")) {
      resetTooltipPopupMotion(_queryTipEl);
      _queryTipEl.classList.remove("query-tooltip-popup-visible");
      _queryTipEl.setAttribute("aria-hidden", "true");
    }
  }

  function ensureQueryTipDismissOnScrollResize() {
    if (_queryTipGlobalWired) {
      return;
    }
    _queryTipGlobalWired = true;
    window.addEventListener(
      "scroll",
      () => {
        hideQueryTooltipImmediate();
        hideHoverTooltipImmediate();
      },
      true
    );
    window.addEventListener("resize", () => {
      hideQueryTooltipImmediate();
      hideHoverTooltipImmediate();
    });
  }

  function getQueryTooltipEl() {
    if (_queryTipEl) {
      return _queryTipEl;
    }
    ensureQueryTipDismissOnScrollResize();
    _queryTipEl = el("div", {
      className: "query-tooltip-popup",
      "aria-hidden": "true",
      role: "tooltip",
    });
    _queryTipEl.addEventListener("mouseenter", () => {
      if (_queryTipHideTimer) {
        clearTimeout(_queryTipHideTimer);
        _queryTipHideTimer = null;
      }
    });
    _queryTipEl.addEventListener("mouseleave", scheduleHideQueryTooltip);
    document.body.appendChild(_queryTipEl);
    return _queryTipEl;
  }

  function scheduleHideQueryTooltip() {
    if (_queryTipHideTimer) {
      clearTimeout(_queryTipHideTimer);
    }
    _queryTipHideTimer = setTimeout(() => {
      const tip = _queryTipEl;
      if (tip) {
        resetTooltipPopupMotion(tip);
        tip.classList.remove("query-tooltip-popup-visible");
        tip.setAttribute("aria-hidden", "true");
      }
      _queryTipHideTimer = null;
    }, 200);
  }

  function positionAndShowQueryTooltip(anchorRect, fullText) {
    positionAndShowTooltipPopup(getQueryTooltipEl(), anchorRect, fullText);
  }

  /**
   * Show SQL hover tooltip only when the preview is truncated (ellipsis).
   * Toggles .query-preview--truncated for cursor help (no dotted underline).
   */
  function wireQueryPreviewHoverTooltip(wrap, textEl, fullText) {
    let listenersAttached = false;

    function onEnter() {
      hideHoverTooltipImmediate();
      if (_queryTipHideTimer) {
        clearTimeout(_queryTipHideTimer);
        _queryTipHideTimer = null;
      }
      if (_queryTipShowTimer) {
        clearTimeout(_queryTipShowTimer);
      }
      _queryTipShowTimer = setTimeout(() => {
        requestAnimationFrame(() => {
          positionAndShowQueryTooltip(wrap.getBoundingClientRect(), fullText);
        });
        _queryTipShowTimer = null;
      }, 80);
    }

    function onLeave() {
      if (_queryTipShowTimer) {
        clearTimeout(_queryTipShowTimer);
        _queryTipShowTimer = null;
      }
      scheduleHideQueryTooltip();
    }

    function isTruncated() {
      return textEl.scrollWidth - textEl.clientWidth > 1;
    }

    function sync() {
      const truncated = isTruncated();
      textEl.classList.toggle("query-preview--truncated", truncated);
      if (truncated && !listenersAttached) {
        wrap.addEventListener("mouseenter", onEnter);
        wrap.addEventListener("mouseleave", onLeave);
        listenersAttached = true;
      } else if (!truncated && listenersAttached) {
        wrap.removeEventListener("mouseenter", onEnter);
        wrap.removeEventListener("mouseleave", onLeave);
        listenersAttached = false;
        hideQueryTooltipImmediate();
      }
    }

    requestAnimationFrame(() => {
      requestAnimationFrame(sync);
    });

    if (typeof ResizeObserver !== "undefined") {
      const ro = new ResizeObserver(() => sync());
      ro.observe(wrap);
    } else {
      window.addEventListener("resize", sync);
    }
  }

  function appendQueryCell(td, queryVal) {
    const full = String(queryVal || "");
    const wrap = el("div", { className: "query-cell" });
    const span = el("span", { className: "query-preview" });
    span.textContent = full;
    wrap.appendChild(span);
    const btn = el("button", {
      type: "button",
      className: "icon-copy-btn",
      "aria-label": "Copy query",
      title: "Copy query",
      innerHTML: CLIPBOARD_SVG,
    });
    btn.addEventListener("click", () => {
      copyText(queryVal).then(() => {
        btn.classList.add("icon-copy-done");
        setTimeout(() => btn.classList.remove("icon-copy-done"), 1200);
      });
    });
    wrap.appendChild(btn);
    wireQueryPreviewHoverTooltip(wrap, span, full);
    td.appendChild(wrap);
  }

  function appendQueryCellWithAshLinks(td, queryVal, qid) {
    const full = String(queryVal || "");
    const wrap = el("div", { className: "query-cell" });
    const a = el("a", {
      className: "query-preview query-ash-deeplink",
      href: buildAshQueryHref(qid),
      textContent: full,
    });
    a.addEventListener("click", (e) => {
      e.preventDefault();
      navigateToAshForQueryId(qid);
    });
    wrap.appendChild(a);
    const btn = el("button", {
      type: "button",
      className: "icon-copy-btn",
      "aria-label": "Copy query",
      title: "Copy query",
      innerHTML: CLIPBOARD_SVG,
    });
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      copyText(queryVal).then(() => {
        btn.classList.add("icon-copy-done");
        setTimeout(() => btn.classList.remove("icon-copy-done"), 1200);
      });
    });
    wrap.appendChild(btn);
    wireQueryPreviewHoverTooltip(wrap, a, full);
    td.appendChild(wrap);
  }

  /** Descending tablet counts only; hover shows node_id (fast tooltip). */
  function appendTabletCountStripCell(td, pairs) {
    td.classList.add("yb-mono", "yb-wrap-cell", "yb-count-strip");
    if (!pairs || !pairs.length) {
      td.textContent = "";
      return;
    }
    pairs.forEach((p, i) => {
      if (i > 0) td.appendChild(document.createTextNode(", "));
      const span = el("span", {
        className: "yb-count-chip",
      });
      span.textContent = String(p.count);
      wireQuickNodeIdTooltip(span, p.node_id);
      td.appendChild(span);
    });
  }

  /** Comma-separated Xi/M % for up to 5 nodes (desc); hover shows node_id (fast tooltip). */
  function appendAshNodeLoadDistributionCell(td, dist) {
    applyMonoTableCellClass(td, { key: "ash_node_load_distribution", type: "number" });
    if (!dist || !dist.parts || !dist.parts.length) {
      td.textContent = "";
      return;
    }
    dist.parts.forEach((p, idx) => {
      if (idx > 0) td.appendChild(document.createTextNode(", "));
      const span = el("span", {
        className: "ash-node-dist-pct",
        textContent: `${Number(p.pct).toFixed(1)}%`,
      });
      wireQuickNodeIdTooltip(span, p.node_id);
      td.appendChild(span);
    });
    if (dist.ellipsis) {
      td.appendChild(document.createTextNode(", …"));
    }
  }

  function appendColumnHeader(th, col, options) {
    const unify = options && options.unifyStatementHeaders;
    th.dataset.sortKey = col.key;
    th.dataset.sortType = col.type || "string";
    if (col.headerPerCall && col.headerBase) {
      th.classList.add("th-per-call-metric");
      const m = String(col.headerBase).trim();
      th.appendChild(document.createTextNode(m ? `${m} / call` : "/ call"));
    } else {
      if (unify) {
        th.classList.add("th-per-call-metric");
      }
      th.textContent = col.label != null ? String(col.label) : String(col.key);
    }
    if (col.sortable === false) {
      th.classList.add("th-no-sort");
    }
    if (col.align === "right") {
      th.classList.add("yb-cell-right");
    }
  }

  function buildSortableTable(title, rows, columns, subsectionId, ashCellOpts) {
    const section = el("section", { className: "ybtop-section" });
    const body = el("div", { className: "section-body" });
    if (subsectionId) {
      const header = el("div", { className: "section-header" });
      const toggle = el("button", { type: "button", className: "section-toggle" });
      const h2 = el("h2", { className: "section-title" });
      fillSectionTitleWithGroupedHighlight(h2, title);
      header.appendChild(toggle);
      header.appendChild(h2);
      section.appendChild(header);
      wireSubsectionCollapse(section, subsectionId, body, toggle);
    } else {
      const h2plain = el("h2", { className: "section-title" });
      fillSectionTitleWithGroupedHighlight(h2plain, title);
      section.appendChild(h2plain);
    }
    section.appendChild(body);

    if (!rows.length) {
      body.appendChild(el("p", { textContent: "(no rows)" }));
      return section;
    }
    const state = { key: columns[0].key, dir: "desc" };

    const table = el("table");
    const thead = el("thead");
    const trh = el("tr");
    columns.forEach((col) => {
      const th = el("th");
      appendColumnHeader(th, col);
      if (col.sortable !== false) {
        th.addEventListener("click", () => {
          if (state.key === col.key) state.dir = state.dir === "asc" ? "desc" : "asc";
          else {
            state.key = col.key;
            state.dir = col.type === "number" ? "desc" : "asc";
          }
          trh.querySelectorAll("th").forEach((x) => {
            x.classList.remove("sort-asc", "sort-desc");
          });
          th.classList.add(state.dir === "asc" ? "sort-asc" : "sort-desc");
          renderBody();
        });
      }
      trh.appendChild(th);
    });
    thead.appendChild(trh);
    table.appendChild(thead);
    const tbody = el("tbody");
    table.appendChild(tbody);

    function cmp(a, b) {
      const col = columns.find((c) => c.key === state.key) || columns[0];
      let va;
      let vb;
      if (typeof col.sortValue === "function") {
        va = col.sortValue(a);
        vb = col.sortValue(b);
      } else {
        va = a[state.key];
        vb = b[state.key];
      }
      if (col.type === "number") {
        va = Number(va) || 0;
        vb = Number(vb) || 0;
      } else {
        va = String(va || "").toLowerCase();
        vb = String(vb || "").toLowerCase();
      }
      if (va < vb) return state.dir === "asc" ? -1 : 1;
      if (va > vb) return state.dir === "asc" ? 1 : -1;
      return 0;
    }

    function renderBody() {
      tbody.textContent = "";
      const sorted = rows.slice().sort(cmp);
      sorted.forEach((row) => {
        const tr = el("tr");
        columns.forEach((col) => {
          const td = el("td");
          const v = row[col.key];
          if (col.key === "query") {
            applyMonoTableCellClass(td, col);
            const qid = row.query_id != null && row.query_id !== undefined ? row.query_id : row.queryid;
            if (
              ashCellOpts &&
              ashCellOpts.ashQueryTextLinks &&
              qid != null &&
              String(qid).trim() !== ""
            ) {
              appendQueryCellWithAshLinks(td, v, qid);
            } else {
              appendQueryCell(td, v);
            }
          } else if (col.key === "per_node_counts") {
            appendTabletCountStripCell(td, v);
          } else if (col.key === "load_pct" || col.key === "time_pct") {
            applyMonoTableCellClass(td, col);
            td.textContent =
              v === null || v === undefined || v === "" ? "" : `${Number(v).toFixed(2)}%`;
          } else if (col.key === "calls_per_sec") {
            applyMonoTableCellClass(td, col);
            td.textContent =
              v === null || v === undefined || v === "" ? "" : Number(v).toFixed(2);
          } else if (col.key === "total_ms" || col.key === "mean_ms") {
            applyMonoTableCellClass(td, col);
            td.textContent = formatPgStatMsTwoDecimals(v);
          } else if (col.key === "is_prepared") {
            applyMonoTableCellClass(td, col);
            td.textContent = formatYcqlPrepared(v);
          } else if (col.key === "sessions_per_sec") {
            applyMonoTableCellClass(td, col);
            td.textContent = formatAshSessionsPerSec(v);
          } else if (col.key === "ash_node_load_distribution") {
            appendAshNodeLoadDistributionCell(td, row.ash_node_load_distribution);
          } else if (col.headerPerCall && col.type === "number") {
            applyMonoTableCellClass(td, col);
            let raw = v;
            if (col.key === "rows_per_call") {
              raw =
                row.rows_per_call != null && row.rows_per_call !== ""
                  ? row.rows_per_call
                  : row.avg_rows_per_call;
            }
            td.textContent = formatPgStatPerCallMetric(raw);
          } else if (
            ashCellOpts &&
            ashCellOpts.tabletTableNameLinks &&
            col.key === "table_name" &&
            row.table_id != null &&
            String(row.table_id).trim() !== ""
          ) {
            applyMonoTableCellClass(td, col);
            const disp = v === null || v === undefined ? "" : String(v);
            const tid = row.table_id;
            const a = el("a", {
              className: "ash-queryid-deeplink",
              href: buildAshTableIdHref(tid),
              textContent: disp,
            });
            a.addEventListener("click", (e) => {
              e.preventDefault();
              navigateToAshForTableId(tid);
            });
            td.appendChild(a);
          } else if (
            ashCellOpts &&
            ashCellOpts.ashQueryIdLinks &&
            col.key === "query_id" &&
            row.query_id != null &&
            String(row.query_id) !== ""
          ) {
            applyMonoTableCellClass(td, col);
            const a = el("a", {
              className: "ash-queryid-deeplink",
              href: buildAshQueryHref(row.query_id),
              textContent: v === null || v === undefined ? "" : String(v),
            });
            a.addEventListener("click", (e) => {
              e.preventDefault();
              navigateToAshForQueryId(row.query_id);
            });
            td.appendChild(a);
          } else if (
            ashCellOpts &&
            ashCellOpts.ashNodeLinks &&
            col.key === "node_id" &&
            v != null &&
            String(v) !== ""
          ) {
            applyMonoTableCellClass(td, col);
            const a = el("a", {
              className: "ash-queryid-deeplink",
              href: buildAshNodeHref(v),
              textContent: String(v),
            });
            a.addEventListener("click", (e) => {
              e.preventDefault();
              navigateToAshForNodeId(v);
            });
            td.appendChild(a);
          } else if (
            ashCellOpts &&
            ashCellOpts.ashObjectLinks &&
            col.key === "object_name" &&
            row.table_id != null &&
            String(row.table_id).trim() !== ""
          ) {
            applyMonoTableCellClass(td, col);
            const disp = v === null || v === undefined ? "" : String(v);
            const tid = row.table_id;
            const a = el("a", {
              className: "ash-queryid-deeplink",
              href: buildAshTableIdHref(tid),
              textContent: disp,
            });
            a.addEventListener("click", (e) => {
              e.preventDefault();
              navigateToAshForTableId(tid);
            });
            td.appendChild(a);
          } else {
            applyMonoTableCellClass(td, col);
            td.textContent = v === null || v === undefined ? "" : String(v);
          }
          if (col.align === "right") {
            td.classList.add("yb-cell-right");
          }
          tr.appendChild(td);
        });
        tbody.appendChild(tr);
      });
    }

    const firstTh = trh.querySelector(`th[data-sort-key="${state.key}"]`);
    if (firstTh) firstTh.classList.add(state.dir === "asc" ? "sort-asc" : "sort-desc");
    renderBody();
    body.appendChild(table);
    return section;
  }

  function buildSortablePaginatedTable(
    titleBase,
    rows,
    columns,
    pageSize,
    subsectionId,
    initialSort,
    tableOptions
  ) {
    const opt = tableOptions || {};
    const unifyStatementHeaders = !!opt.unifyStatementHeaders;
    const pgssAshLinks = !!opt.pgssAshLinks;
    const ashCellOpts = opt.ashCellOpts;
    const section = el("section", { className: "ybtop-section" });
    const h2 = el("h2", { className: "section-title" });
    const body = el("div", { className: "section-body" });
    if (subsectionId) {
      const header = el("div", { className: "section-header" });
      const toggle = el("button", { type: "button", className: "section-toggle" });
      header.appendChild(toggle);
      header.appendChild(h2);
      section.appendChild(header);
      wireSubsectionCollapse(section, subsectionId, body, toggle);
    } else {
      section.appendChild(h2);
    }
    section.appendChild(body);

    const pager = el("div", { className: "pager" });
    if (!rows.length) {
      fillSectionTitleWithGroupedHighlight(h2, titleBase);
      body.appendChild(el("p", { textContent: "(no rows)" }));
      return section;
    }

    const state = {
      key: (initialSort && initialSort.key) || columns[0].key,
      dir: (initialSort && initialSort.dir) || "desc",
      page: 1,
    };

    function totalPages() {
      return Math.max(1, Math.ceil(rows.length / pageSize));
    }

    function cmp(a, b) {
      const col = columns.find((c) => c.key === state.key) || columns[0];
      let va;
      let vb;
      if (typeof col.sortValue === "function") {
        va = col.sortValue(a);
        vb = col.sortValue(b);
      } else {
        va = a[state.key];
        vb = b[state.key];
      }
      if (col.type === "number") {
        va = Number(va) || 0;
        vb = Number(vb) || 0;
      } else {
        va = String(va || "").toLowerCase();
        vb = String(vb || "").toLowerCase();
      }
      if (va < vb) return state.dir === "asc" ? -1 : 1;
      if (va > vb) return state.dir === "asc" ? 1 : -1;
      return 0;
    }

    function sortedRows() {
      return rows.slice().sort(cmp);
    }

    function updateHeading() {
      const tp = totalPages();
      fillSectionTitleWithGroupedHighlight(h2, titleBase);
      h2.appendChild(
        document.createTextNode(` — page ${state.page} of ${tp} (${rows.length} rows)`)
      );
    }

    const table = el("table");
    const thead = el("thead");
    const trh = el("tr");
    columns.forEach((col) => {
      const th = el("th");
      appendColumnHeader(th, col, { unifyStatementHeaders });
      if (col.sortable !== false) {
        th.addEventListener("click", () => {
          if (state.key === col.key) state.dir = state.dir === "asc" ? "desc" : "asc";
          else {
            state.key = col.key;
            state.dir = col.type === "number" ? "desc" : "asc";
          }
          state.page = 1;
          trh.querySelectorAll("th").forEach((x) => {
            x.classList.remove("sort-asc", "sort-desc");
          });
          th.classList.add(state.dir === "asc" ? "sort-asc" : "sort-desc");
          renderAll();
        });
      }
      trh.appendChild(th);
    });
    thead.appendChild(trh);
    table.appendChild(thead);
    const tbody = el("tbody");
    table.appendChild(tbody);

    function renderBody() {
      tbody.textContent = "";
      const sorted = sortedRows();
      const tp = totalPages();
      const p = Math.min(Math.max(1, state.page), tp);
      state.page = p;
      const start = (p - 1) * pageSize;
      const slice = sorted.slice(start, start + pageSize);
      slice.forEach((row) => {
        const tr = el("tr");
        columns.forEach((col) => {
          const td = el("td");
          const v = row[col.key];
          if (col.key === "query") {
            applyMonoTableCellClass(td, col);
            const qid = row.query_id != null && row.query_id !== undefined ? row.query_id : row.queryid;
            if (pgssAshLinks && row.queryid != null && String(row.queryid) !== "") {
              appendQueryCellWithAshLinks(td, v, row.queryid);
            } else if (
              ashCellOpts &&
              ashCellOpts.ashQueryTextLinks &&
              qid != null &&
              String(qid).trim() !== ""
            ) {
              appendQueryCellWithAshLinks(td, v, qid);
            } else {
              appendQueryCell(td, v);
            }
          } else if (col.key === "queryid" && pgssAshLinks && row.queryid != null && String(row.queryid) !== "") {
            applyMonoTableCellClass(td, col);
            const a = el("a", {
              className: "ash-queryid-deeplink",
              href: buildAshQueryHref(row.queryid),
              textContent: v === null || v === undefined ? "" : String(v),
            });
            a.addEventListener("click", (e) => {
              e.preventDefault();
              navigateToAshForQueryId(row.queryid);
            });
            td.appendChild(a);
          } else if (col.key === "per_node_counts") {
            appendTabletCountStripCell(td, v);
          } else if (col.key === "load_pct" || col.key === "time_pct") {
            applyMonoTableCellClass(td, col);
            td.textContent =
              v === null || v === undefined || v === "" ? "" : `${Number(v).toFixed(2)}%`;
          } else if (col.key === "calls_per_sec") {
            applyMonoTableCellClass(td, col);
            td.textContent =
              v === null || v === undefined || v === "" ? "" : Number(v).toFixed(2);
          } else if (col.key === "total_ms" || col.key === "mean_ms") {
            applyMonoTableCellClass(td, col);
            td.textContent = formatPgStatMsTwoDecimals(v);
          } else if (col.key === "is_prepared") {
            applyMonoTableCellClass(td, col);
            td.textContent = formatYcqlPrepared(v);
          } else if (col.key === "sessions_per_sec") {
            applyMonoTableCellClass(td, col);
            td.textContent = formatAshSessionsPerSec(v);
          } else if (col.key === "ash_node_load_distribution") {
            appendAshNodeLoadDistributionCell(td, row.ash_node_load_distribution);
          } else if (col.headerPerCall && col.type === "number") {
            applyMonoTableCellClass(td, col);
            let raw = v;
            if (col.key === "rows_per_call") {
              raw =
                row.rows_per_call != null && row.rows_per_call !== ""
                  ? row.rows_per_call
                  : row.avg_rows_per_call;
            }
            td.textContent = formatPgStatPerCallMetric(raw);
          } else if (
            ashCellOpts &&
            ashCellOpts.tabletTableNameLinks &&
            col.key === "table_name" &&
            row.table_id != null &&
            String(row.table_id).trim() !== ""
          ) {
            applyMonoTableCellClass(td, col);
            const disp = v === null || v === undefined ? "" : String(v);
            const tid = row.table_id;
            const a = el("a", {
              className: "ash-queryid-deeplink",
              href: buildAshTableIdHref(tid),
              textContent: disp,
            });
            a.addEventListener("click", (e) => {
              e.preventDefault();
              navigateToAshForTableId(tid);
            });
            td.appendChild(a);
          } else if (
            ashCellOpts &&
            ashCellOpts.ashQueryIdLinks &&
            col.key === "query_id" &&
            row.query_id != null &&
            String(row.query_id) !== ""
          ) {
            applyMonoTableCellClass(td, col);
            const a = el("a", {
              className: "ash-queryid-deeplink",
              href: buildAshQueryHref(row.query_id),
              textContent: v === null || v === undefined ? "" : String(v),
            });
            a.addEventListener("click", (e) => {
              e.preventDefault();
              navigateToAshForQueryId(row.query_id);
            });
            td.appendChild(a);
          } else if (
            ashCellOpts &&
            ashCellOpts.ashNodeLinks &&
            col.key === "node_id" &&
            v != null &&
            String(v) !== ""
          ) {
            applyMonoTableCellClass(td, col);
            const a = el("a", {
              className: "ash-queryid-deeplink",
              href: buildAshNodeHref(v),
              textContent: String(v),
            });
            a.addEventListener("click", (e) => {
              e.preventDefault();
              navigateToAshForNodeId(v);
            });
            td.appendChild(a);
          } else if (
            ashCellOpts &&
            ashCellOpts.ashObjectLinks &&
            col.key === "object_name" &&
            row.table_id != null &&
            String(row.table_id).trim() !== ""
          ) {
            applyMonoTableCellClass(td, col);
            const disp = v === null || v === undefined ? "" : String(v);
            const tid = row.table_id;
            const a = el("a", {
              className: "ash-queryid-deeplink",
              href: buildAshTableIdHref(tid),
              textContent: disp,
            });
            a.addEventListener("click", (e) => {
              e.preventDefault();
              navigateToAshForTableId(tid);
            });
            td.appendChild(a);
          } else {
            applyMonoTableCellClass(td, col);
            td.textContent = v === null || v === undefined ? "" : String(v);
          }
          if (col.align === "right") {
            td.classList.add("yb-cell-right");
          }
          tr.appendChild(td);
        });
        tbody.appendChild(tr);
      });
    }

    function renderPager() {
      pager.textContent = "";
      const tp = totalPages();

      const prev = el("button", { type: "button", className: "pager-btn", textContent: "‹ Prev" });
      prev.disabled = state.page <= 1;
      prev.addEventListener("click", () => {
        if (state.page > 1) {
          state.page -= 1;
          renderAll();
        }
      });
      pager.appendChild(prev);

      if (tp <= 1) {
        const b = el("button", {
          type: "button",
          className: "pager-btn pager-btn-current",
          textContent: "1",
          "aria-label": "Page 1 of 1",
        });
        b.disabled = true;
        pager.appendChild(b);
      } else {
        const pages = new Set([1, tp, state.page]);
        for (let d = -3; d <= 3; d += 1) {
          const x = state.page + d;
          if (x >= 1 && x <= tp) pages.add(x);
        }
        const sortedPages = Array.from(pages).sort((a, b) => a - b);
        let last = 0;
        sortedPages.forEach((pnum) => {
          if (last && pnum > last + 1) {
            pager.appendChild(el("span", { className: "pager-ellipsis", textContent: "…" }));
          }
          const b = el("button", {
            type: "button",
            className: "pager-btn" + (pnum === state.page ? " pager-btn-current" : ""),
            textContent: String(pnum),
          });
          b.addEventListener("click", () => {
            state.page = pnum;
            renderAll();
          });
          pager.appendChild(b);
          last = pnum;
        });
      }

      const next = el("button", { type: "button", className: "pager-btn", textContent: "Next ›" });
      next.disabled = state.page >= tp;
      next.addEventListener("click", () => {
        if (state.page < tp) {
          state.page += 1;
          renderAll();
        }
      });
      pager.appendChild(next);
    }

    function renderAll() {
      updateHeading();
      renderBody();
      renderPager();
    }

    body.appendChild(table);
    body.appendChild(pager);

    const firstTh = trh.querySelector(`th[data-sort-key="${state.key}"]`);
    if (firstTh) firstTh.classList.add(state.dir === "asc" ? "sort-asc" : "sort-desc");
    renderAll();
    return section;
  }

  // ------------------------------------------------------------------------------------
  // Latency-histogram multimodality (browser port of histogram.py / histogram_detect.py).
  // Stages 0-2 + template grouping run here; the Hartigan dip test (Stage 3) needs the
  // native diptest package and is skipped in the browser, so shape-flagged rows land in the
  // "unconfirmed" tier. Capture with watch --snapshot-latency-analysis for dip-confirmed sidecars.
  // Thresholds are kept identical to the Python detector.
  // ------------------------------------------------------------------------------------
  const HIST_TIER_RANK = {
    not_flagged: 0,
    unconfirmed: 1,
    moderate: 2,
    high: 3,
    very_high: 4,
  };

  function docHasLatencyHistograms(doc) {
    const pn = doc && doc.latency_histograms && doc.latency_histograms.per_node;
    if (!pn || typeof pn !== "object") return false;
    return Object.keys(pn).some((nid) => Array.isArray(pn[nid]) && pn[nid].length > 0);
  }

  function histCoerceBuckets(raw) {
    const out = {};
    if (!raw) return out;
    const add = (k, v) => {
      const c = Number(v);
      if (Number.isFinite(c)) out[k] = (out[k] || 0) + c;
    };
    if (Array.isArray(raw)) {
      raw.forEach((o) => {
        if (o && typeof o === "object") Object.entries(o).forEach(([k, v]) => add(k, v));
      });
    } else if (typeof raw === "object") {
      Object.entries(raw).forEach(([k, v]) => add(k, v));
    }
    return out;
  }

  function histMergeKey(r) {
    const db = r.dbname == null ? "" : String(r.dbname).trim();
    return `${r.queryid == null ? "" : String(r.queryid)}\0${db}`;
  }

  function mergeLatencyHistograms(perNode) {
    const acc = new Map();
    Object.keys(perNode || {}).forEach((nid) => {
      (perNode[nid] || []).forEach((r) => {
        const mk = histMergeKey(r);
        if (!acc.has(mk)) {
          acc.set(mk, {
            queryid: String(r.queryid),
            dbname:
              r.dbname != null && String(r.dbname).trim() !== ""
                ? String(r.dbname).trim()
                : null,
            query: r.query || "",
            calls: 0,
            buckets: {},
          });
        }
        const a = acc.get(mk);
        a.calls += Number(r.calls) || 0;
        const b = histCoerceBuckets(r.yb_latency_histogram);
        Object.entries(b).forEach(([k, v]) => {
          a.buckets[k] = (a.buckets[k] || 0) + v;
        });
        if (!a.query && r.query) a.query = r.query;
      });
    });
    const out = Array.from(acc.values());
    out.sort((x, y) => y.calls - x.calls);
    return out;
  }

  function deltaLatencyHistograms(cur, prev) {
    const pm = new Map((prev || []).map((r) => [histMergeKey(r), r]));
    const out = [];
    (cur || []).forEach((c) => {
      const p = pm.get(histMergeKey(c));
      const db = {};
      let has = false;
      Object.entries(c.buckets).forEach(([k, v]) => {
        const d = v - ((p && p.buckets[k]) || 0);
        if (d > 0) {
          db[k] = d;
          has = true;
        }
      });
      if (!has) return;
      const dcalls = c.calls - ((p && p.calls) || 0);
      out.push({
        queryid: c.queryid,
        dbname: c.dbname,
        query: c.query,
        calls: Math.max(0, dcalls),
        buckets: db,
      });
    });
    out.sort((x, y) => y.calls - x.calls);
    return out;
  }

  const HIST_BUCKET_RE = /[[(]\s*([+-]?[0-9]*\.?[0-9]+(?:[eE][+-]?[0-9]+)?)\s*,\s*([+-]?[0-9]*\.?[0-9]+(?:[eE][+-]?[0-9]+)?)?\s*[)\]]/;

  // Parse a flat {bucket_label: count} map into sorted {low, high, count} rows plus the
  // open-ended "[max,)" overflow count (no finite width -> excluded from peak finding),
  // mirroring the Python parse_histogram_buckets().
  function parseHistogramBuckets(buckets) {
    const rows = [];
    let overflow = 0;
    Object.entries(buckets || {}).forEach(([k, v]) => {
      const c = Number(v);
      if (!Number.isFinite(c) || c === 0) return;
      const m = String(k).match(HIST_BUCKET_RE);
      if (!m) return;
      const low = parseFloat(m[1]);
      if (!Number.isFinite(low)) return;
      if (m[2] == null || m[2] === "") {
        overflow += c;
        return;
      }
      const high = parseFloat(m[2]);
      if (!Number.isFinite(high)) return;
      rows.push({ low: low, high: high, count: c });
    });
    rows.sort((a, b) => a.low - b.low);
    return { buckets: rows, overflow: overflow };
  }

  // Arithmetic bucket midpoint taken in log2(ms) space (matches the Python _log2_mid).
  function log2Mid(low, high) {
    return Math.log2(Math.max((low + high) / 2, 1e-3));
  }

  // Sarle's (uncorrected) BC over weighted log2(ms) midpoints. Returns null when fewer than
  // 30 weighted samples back the estimate, matching the Python _bimodality_coefficient.
  function bimodalityCoefficient(xs, ws) {
    let n = 0;
    for (let i = 0; i < ws.length; i++) n += ws[i];
    if (n < 30) return null;
    let mean = 0;
    for (let i = 0; i < xs.length; i++) mean += ws[i] * xs[i];
    mean /= n;
    let m2 = 0;
    let m3 = 0;
    let m4 = 0;
    for (let i = 0; i < xs.length; i++) {
      const d = xs[i] - mean;
      const w = ws[i];
      const d2 = d * d;
      m2 += w * d2;
      m3 += w * d2 * d;
      m4 += w * d2 * d2;
    }
    m2 /= n;
    m3 /= n;
    m4 /= n;
    const std = Math.sqrt(m2);
    if (std === 0) return 0;
    const skew = m3 / Math.pow(m2, 1.5);
    const kurt = m4 / (m2 * m2);
    if (kurt <= 0) return null;
    return (skew * skew + 1) / kurt;
  }

  function gaussianKernel1d(sigma, radius) {
    const k = [];
    let s = 0;
    for (let i = -radius; i <= radius; i++) {
      const v = Math.exp(-(i * i) / (2 * sigma * sigma));
      k.push(v);
      s += v;
    }
    return k.map((v) => v / s);
  }

  function gaussianFilter1d(arr, sigma) {
    const radius = Math.max(1, Math.round(4 * sigma)); // scipy truncate=4.0, mode="reflect"
    const k = gaussianKernel1d(sigma, radius);
    const n = arr.length;
    const out = new Array(n).fill(0);
    for (let i = 0; i < n; i++) {
      let acc = 0;
      for (let j = -radius; j <= radius; j++) {
        let idx = i + j;
        // scipy "reflect" (half-sample symmetric): (d c b a | a b c d | d c b a).
        if (n > 0) {
          while (idx < 0 || idx >= n) {
            if (idx < 0) idx = -idx - 1;
            else if (idx >= n) idx = 2 * n - idx - 1;
          }
        } else {
          idx = 0;
        }
        acc += arr[idx] * k[j + radius];
      }
      out[i] = acc;
    }
    return out;
  }

  function peakProminence(y, p) {
    const h = y[p];
    const n = y.length;
    let leftMin = Infinity;
    for (let l = p - 1; l >= 0 && y[l] < h; l--) leftMin = Math.min(leftMin, y[l]);
    let rightMin = Infinity;
    for (let r = p + 1; r < n && y[r] < h; r++) rightMin = Math.min(rightMin, y[r]);
    if (leftMin === Infinity) leftMin = h;
    if (rightMin === Infinity) rightMin = h;
    return h - Math.max(leftMin, rightMin);
  }

  function findPeaks(y, prominence, distance) {
    const n = y.length;
    const raw = [];
    for (let i = 1; i < n - 1; i++) {
      if (y[i] > y[i - 1] && y[i] >= y[i + 1]) raw.push(i);
    }
    let cand = raw.map((idx) => ({ idx, prom: peakProminence(y, idx) }));
    if (prominence != null) cand = cand.filter((o) => o.prom >= prominence);
    // Enforce min distance: keep taller peaks first, drop close-by lower peaks.
    cand.sort((a, b) => y[b.idx] - y[a.idx]);
    const removed = new Set();
    const kept = [];
    cand.forEach((o) => {
      if (removed.has(o.idx)) return;
      kept.push(o.idx);
      cand.forEach((o2) => {
        if (o2.idx !== o.idx && Math.abs(o2.idx - o.idx) < distance) removed.add(o2.idx);
      });
    });
    kept.sort((a, b) => a - b);
    return kept;
  }

  function histTierOf(r) {
    if (!r.flag) return "not_flagged";
    if (r.dip_p == null) return "unconfirmed";
    if (r.dip_p <= 0.001) return "very_high";
    if (r.dip_p <= 0.01) return "high";
    return "moderate";
  }

  const round4 = (v) => Math.round(v * 10000) / 10000;

  function detectModesJS(bucketMap, opts) {
    const o = opts || {};
    const minCalls = o.minCalls == null ? 30 : o.minCalls;
    const bcThreshold = o.bcThreshold == null ? 0.555 : o.bcThreshold;
    const minOctaveSeparation = o.minOctaveSeparation == null ? 0.5 : o.minOctaveSeparation;
    const minValleyRatio = o.minValleyRatio == null ? 0.75 : o.minValleyRatio;
    const overflowRatioThreshold = o.overflowRatioThreshold == null ? 0.02 : o.overflowRatioThreshold;

    const parsed = parseHistogramBuckets(bucketMap);
    const buckets = parsed.buckets;
    const overflow = parsed.overflow;
    const total = buckets.reduce((s, r) => s + r.count, 0) + overflow;
    const res = {
      calls: buckets.reduce((s, r) => s + r.count, 0),
      flag: false,
      reason: "",
      bc: null,
      dip_p: null,
      n_raw_peaks: 0,
      n_modes_estimate: null,
      latency_min_ms: null,
      latency_max_ms: null,
      latency_spread_ratio: null,
      peak_pairs: [],
      overflow_count: overflow,
      overflow_ratio: total ? overflow / total : 0,
      overflow_flag: total ? overflow / total > overflowRatioThreshold && total >= minCalls : false,
      confidence_tier: "not_flagged",
    };

    if (buckets.length) {
      const latMin = Math.min(...buckets.map((r) => r.low));
      const latMax = Math.max(...buckets.map((r) => r.high));
      res.latency_min_ms = round4(latMin);
      res.latency_max_ms = round4(latMax);
      res.latency_spread_ratio = latMin > 0 ? round4(latMax / latMin) : null;
    }

    if (res.calls < minCalls) {
      res.reason = "insufficient_calls";
      res.confidence_tier = histTierOf(res);
      return res;
    }

    const mids = buckets.map((r) => log2Mid(r.low, r.high));
    const counts = buckets.map((r) => r.count);

    const bc = bimodalityCoefficient(mids, counts);
    res.bc = bc;
    if (bc == null || bc < bcThreshold) {
      res.reason = "bc_below_threshold";
      res.confidence_tier = histTierOf(res);
      return res;
    }

    // Stage 1 - width-normalized density in log2(ms) space.
    const rawWidths = buckets.map((r) => (r.low > 0 ? Math.log2(r.high) - Math.log2(r.low) : Math.log2(r.high)));
    const positive = rawWidths.filter((w) => w > 0);
    const fallbackWidth = positive.length ? Math.min(...positive) : 1;
    const widths = rawWidths.map((w) => (w > 0 ? w : fallbackWidth));
    let density = counts.map((c, i) => c / widths[i]);
    const dsum = density.reduce((s, d) => s + d, 0);
    if (dsum > 0) density = density.map((d) => d / dsum);

    const smoothed = gaussianFilter1d(density, 1.0);
    const diffs = [];
    for (let i = 1; i < mids.length; i++) diffs.push(mids[i] - mids[i - 1]);
    const avgSpacing = diffs.length ? diffs.reduce((s, d) => s + d, 0) / diffs.length : 1;
    const distance = Math.max(1, Math.round(minOctaveSeparation / Math.max(avgSpacing, 1e-6)));
    const maxS = Math.max(...smoothed);
    const prominence = maxS > 0 ? 0.03 * maxS : null;
    const peaks = findPeaks(smoothed, prominence, distance);
    res.n_raw_peaks = peaks.length;
    if (peaks.length < 2) {
      res.reason = "single_peak_after_smoothing";
      res.confidence_tier = histTierOf(res);
      return res;
    }

    // Stage 2 - keep every valid adjacent peak pair (raw-count valley check).
    const validPairs = [];
    for (let i = 0; i < peaks.length - 1; i++) {
      const p1 = peaks[i];
      const p2 = peaks[i + 1];
      if (mids[p2] - mids[p1] < minOctaveSeparation) continue;
      let valley = Infinity;
      for (let k = p1; k <= p2; k++) valley = Math.min(valley, counts[k]);
      const smaller = Math.min(counts[p1], counts[p2]);
      if (smaller <= 0) continue;
      if (valley / smaller > minValleyRatio) continue;
      const peak1ms = Math.pow(2, mids[p1]);
      const peak2ms = Math.pow(2, mids[p2]);
      validPairs.push({
        peak1_ms: round4(peak1ms),
        peak2_ms: round4(peak2ms),
        valley_ratio: round4(valley / smaller),
        gap_ms: round4(peak2ms - peak1ms),
        gap_ratio: peak1ms > 0 ? round4(peak2ms / peak1ms) : null,
      });
    }
    if (!validPairs.length) {
      res.reason = "no_significant_valley";
      res.confidence_tier = histTierOf(res);
      return res;
    }
    res.flag = true;
    res.peak_pairs = validPairs;
    res.n_modes_estimate = peaks.length;
    res.dip_p = null; // Stage 3 dip test not available in the browser
    res.confidence_tier = histTierOf(res);
    return res;
  }

  // Primary signal dip_p ascending (rows without a dip_p sort to the back at 1.0);
  // tiebreaker bc descending. Matches the Python rank_by_confidence.
  function rankByConfidence(results) {
    const ordered = results.slice().sort((a, b) => {
      const da = a.dip_p == null ? 1 : a.dip_p;
      const db = b.dip_p == null ? 1 : b.dip_p;
      if (da !== db) return da - db;
      return (b.bc || 0) - (a.bc || 0);
    });
    ordered.forEach((r, i) => {
      r.confidence_rank = i + 1;
    });
    return ordered;
  }

  const HIST_REWRITE_COMMENT_RE = /\/\*[\s\S]*?\*\//g;
  const HIST_IN_LIST_RE = /\bIN\s*\([^)]*\)/gi;
  const HIST_PLACEHOLDER_RE = /\$\d+/g;

  function normalizeQueryTemplate(query) {
    if (!query) return "";
    let q = String(query).replace(HIST_REWRITE_COMMENT_RE, "");
    q = q.replace(HIST_IN_LIST_RE, "IN (...)");
    q = q.replace(HIST_PLACEHOLDER_RE, "$N");
    q = q.replace(/\s+/g, " ").trim();
    return q;
  }

  function groupByTemplate(results) {
    const groups = new Map();
    results.forEach((r) => {
      const key = normalizeQueryTemplate(r.query);
      r.query_template = key;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(r);
    });
    const summaries = [];
    groups.forEach((members, key) => {
      members.sort((a, b) => (a.confidence_rank || 1e9) - (b.confidence_rank || 1e9));
      members.forEach((r, i) => {
        r.template_rank = i + 1;
        r.template_member_count = members.length;
      });
      const best = members[0];
      const peakSet = new Set();
      members.forEach((r) => {
        if (r.n_raw_peaks) peakSet.add(r.n_raw_peaks);
      });
      // Adjacent peak-pair gaps (ms / ratio) across all pairs of every member.
      const gapMs = [];
      const gapRatio = [];
      members.forEach((r) => {
        (r.peak_pairs || []).forEach((pp) => {
          if (pp.gap_ms != null) gapMs.push(pp.gap_ms);
          if (pp.gap_ratio != null) gapRatio.push(pp.gap_ratio);
        });
      });
      summaries.push({
        template: key,
        member_count: members.length,
        best_confidence_rank: best.confidence_rank || 0,
        best_confidence_tier: best.confidence_tier || "not_flagged",
        queryids: members.map((r) => r.queryid),
        peak_counts: Array.from(peakSet).sort((a, b) => a - b),
        // Best member's full adjacent-pair list (display); ranges span every pair.
        peak_pairs: (best.peak_pairs || []).slice(),
        gap_ms_range: gapMs.length ? [Math.min(...gapMs), Math.max(...gapMs)] : null,
        gap_ratio_range: gapRatio.length ? [Math.min(...gapRatio), Math.max(...gapRatio)] : null,
      });
    });
    summaries.sort(
      (a, b) => (a.best_confidence_rank || 1e9) - (b.best_confidence_rank || 1e9)
    );
    return summaries;
  }

  function analyzeLatencyDoc(doc, prevDoc) {
    // Prefer a precomputed (dip-confirmed) sidecar when watch --snapshot-latency-analysis produced one:
    // it already carries real dip_p / FDR-corrected tiers, so the browser needs no statistics.
    const pre = doc && doc._latencyAnalysis;
    if (pre && (pre.cumulative || pre.delta)) {
      const prevHas = docHasLatencyHistograms(prevDoc);
      const useDelta = prevHas && pre.delta && Array.isArray(pre.delta.results);
      const block = useDelta ? pre.delta : pre.cumulative;
      const results = block && Array.isArray(block.results) ? block.results.slice() : [];
      return {
        mode: useDelta ? "delta" : "cumulative",
        results: results,
        source: "offline",
        diptest: !!pre.diptest_available,
      };
    }
    const cur = mergeLatencyHistograms(doc.latency_histograms.per_node);
    const prevHas = docHasLatencyHistograms(prevDoc);
    const mode = prevHas ? "delta" : "cumulative";
    let rows;
    if (mode === "delta") {
      const prev = mergeLatencyHistograms(prevDoc.latency_histograms.per_node);
      rows = deltaLatencyHistograms(cur, prev);
    } else {
      rows = cur;
    }
    const results = rows.map((r) => {
      const res = detectModesJS(r.buckets, { minCalls: 30 });
      res.queryid = r.queryid;
      res.query = r.query;
      res.dbname = r.dbname;
      if (!res.calls) res.calls = r.calls;
      return res;
    });
    // Ranking and template grouping are deferred to the panel so they run over the
    // currently-displayed (filtered) rows, matching the Python report ordering.
    return { mode, results, source: "browser" };
  }

  function histFmt(v, digits) {
    if (v == null) return "";
    const n = Number(v);
    if (!Number.isFinite(n)) return String(v);
    return n.toFixed(digits == null ? 2 : digits);
  }

  function histSpreadStr(r) {
    if (r.latency_min_ms == null || r.latency_max_ms == null) return "";
    const rt = r.latency_spread_ratio != null ? `(x${histFmt(r.latency_spread_ratio, 1)})` : "";
    return `${histFmt(r.latency_min_ms)}-${histFmt(r.latency_max_ms)}ms${rt}`;
  }

  function histFormatPeakPair(pp) {
    if (!pp) return "";
    const rt = pp.gap_ratio != null ? `(x${histFmt(pp.gap_ratio, 1)})` : "";
    return `${histFmt(pp.peak1_ms)}->${histFmt(pp.peak2_ms)}ms${rt}`;
  }

  /** All valid adjacent mode splits, comma-separated (low→high latency). */
  function histGapStr(r) {
    const pairs = (r && r.peak_pairs) || [];
    if (!pairs.length) return "";
    return pairs.map(histFormatPeakPair).join(", ");
  }

  function renderLatencyPanel(panel, doc, prevDoc) {
    const analysis = analyzeLatencyDoc(doc, prevDoc);
    const state = { minTier: "high", flaggedOnly: false };

    const offline = analysis.source === "offline";
    const banner = el("div", { className: "pgss-activity-banner latency-banner" });
    const modeLabel = analysis.mode === "delta" ? "Δ vs prior snapshot" : "cumulative totals";
    const titleText = offline
      ? `Latency multimodality — ${modeLabel} · dip-confirmed`
      : `Latency multimodality — ${modeLabel}`;
    const titleEl = el("div", {
      className: "latency-banner-title",
      textContent: titleText,
    });
    if (offline) {
      titleEl.appendChild(
        el("span", {
          className: "latency-confirmed-badge",
          textContent: analysis.diptest ? "dip-confirmed" : "precomputed",
        })
      );
    }
    banner.appendChild(titleEl);
    banner.appendChild(
      el("div", {
        className: "pgss-activity-note",
        textContent: offline
          ? "Showing results precomputed by ybtop from this snapshot, including the " +
            "confirmatory Hartigan dip test and Benjamini-Hochberg FDR correction, so tiers " +
            "match watch --snapshot-latency-analysis. No statistics run in the browser." +
            (analysis.diptest
              ? ""
              : " (This sidecar was written without the diptest package, so shape-flagged " +
                "rows remain 'unconfirmed'.)")
          : "Browser detection runs Stages 0-2 (bimodality coefficient, peak finding, valley " +
            "check) + query-template grouping. The confirmatory Hartigan dip test is not " +
            "available in the browser, so shape-flagged queries are reported as 'unconfirmed'. " +
            "Capture with watch --snapshot-latency-analysis (and pip install 'ybtop[histogram]') " +
            "for dip-test confirmation and FDR-corrected tiers.",
      })
    );
    panel.appendChild(banner);

    const controls = el("div", { className: "latency-controls" });
    const tierWrap = el("label", { className: "latency-control" });
    tierWrap.appendChild(el("span", { textContent: "min tier:" }));
    const tierSel = el("select");
    [
      ["very_high", "very_high"],
      ["high", "high"],
      ["moderate", "moderate"],
      ["unconfirmed", "unconfirmed"],
      ["all", "all"],
    ].forEach(([val, label]) => {
      const opt = el("option", { value: val, textContent: label });
      if (val === state.minTier) opt.setAttribute("selected", "selected");
      tierSel.appendChild(opt);
    });
    tierWrap.appendChild(tierSel);
    controls.appendChild(tierWrap);

    const flagWrap = el("label", { className: "latency-control" });
    const flagChk = el("input", { type: "checkbox" });
    flagWrap.appendChild(flagChk);
    flagWrap.appendChild(el("span", { textContent: "flagged only" }));
    controls.appendChild(flagWrap);

    const dipWrap = el("label", { className: "latency-control" });
    const dipChk = el("input", { type: "checkbox" });
    dipChk.checked = !!latencyShowDipP;
    dipWrap.appendChild(dipChk);
    dipWrap.appendChild(el("span", { textContent: "show dip_p" }));
    controls.appendChild(dipWrap);
    panel.appendChild(controls);

    const legend = el("div", { className: "latency-legend" });
    legend.appendChild(
      el("div", {
        className: "latency-legend-title",
        textContent: "Reading the columns",
      })
    );
    [
      [
        "bc",
        "Bimodality coefficient (Sarle): (skew\u00b2 + 1) / kurtosis over log\u2082 latency midpoints. " +
          "A uniform distribution is ~0.556; values above that threshold (~0.555) pass the " +
          "prescreen as possibly multimodal. Higher bc means a more two-humped shape, but bc " +
          "alone is not the confirmation \u2014 peaks + valley (and dip_p when available) decide.",
      ],
      [
        "dip_p",
        "Hartigan dip-test p-value against unimodality (lower = stronger evidence of multiple " +
          "modes). Used for confidence tiers when a precomputed sidecar is present " +
          "(very_high \u2264 0.001, high \u2264 0.01). In pure browser detection this column is empty " +
          "(\u2014) because the dip test does not run client-side \u2014 use the show dip_p toggle to hide it.",
      ],
      [
        "peaks",
        "How many latency modes (speed classes) survived smoothing. 2+ is what makes a " +
          "statement multimodal.",
      ],
      [
        "spread",
        "Overall range, shown as lowest\u2013highest bucket with calls and (\u00d7 max\u00f7min). " +
          "It measures total variability, not multimodality: a query can have a wide spread and " +
          "still be a single mode (e.g. one broad hump), so a big spread alone is not a red flag.",
      ],
      [
        "gap",
        "Each adjacent mode split, shown as fast peak \u2192 slow peak with (\u00d7 slow\u00f7fast), " +
          "comma-separated when there are 3+ modes (low\u2192high latency). Example: " +
          "5\u219280ms (\u00d716), 80\u2192400ms (\u00d75) means three speed classes with those two " +
          "separations. The ratio hints at the cause (a ~2\u00d7 gap looks like cache hit/miss; a " +
          "10\u2013100\u00d7 gap looks more like retries, leader/follower reads, or cross-AZ hops). " +
          "This is distinct from spread (overall min\u2013max range).",
      ],
    ].forEach(([k, v]) => {
      const row = el("div", { className: "latency-legend-row" });
      row.appendChild(el("code", { className: "latency-legend-key", textContent: k }));
      row.appendChild(el("span", { className: "latency-legend-val", textContent: v }));
      legend.appendChild(row);
    });
    panel.appendChild(legend);

    const groupHolder = el("div");
    const tableHolder = el("div");
    panel.appendChild(groupHolder);
    panel.appendChild(tableHolder);

    function rerender() {
      let rows = analysis.results.slice();
      if (state.flaggedOnly) rows = rows.filter((r) => r.flag);
      if (state.minTier !== "all") {
        const th = HIST_TIER_RANK[state.minTier] || 0;
        rows = rows.filter((r) => (HIST_TIER_RANK[r.confidence_tier] || 0) >= th);
      }
      // Rank + group over the filtered rows so ranks and template rollups describe
      // exactly what's shown (matches the Python report ordering).
      rows = rankByConfidence(rows);
      const groups = groupByTemplate(rows);

      const displayRows = rows.map((r) => ({
        tier: r.confidence_tier,
        rank: r.confidence_rank,
        calls: r.calls,
        bc: r.bc != null ? round4(r.bc) : "",
        dip_p: r.dip_p == null ? "—" : histFmt(r.dip_p, 4),
        peaks: r.n_raw_peaks,
        spread: histSpreadStr(r),
        gap: histGapStr(r),
        tmpl: r.template_member_count > 1 ? `${r.template_rank}/${r.template_member_count}` : "",
        queryid: r.queryid,
        query: r.query,
      }));
      const cols = [
        { key: "tier", label: "tier", type: "number", sortValue: (r) => HIST_TIER_RANK[r.tier] || 0 },
        { key: "rank", label: "rank", type: "number", align: "right" },
        { key: "calls", label: "calls", type: "number", align: "right" },
        { key: "bc", label: "bc", type: "number", align: "right" },
      ];
      if (latencyShowDipP) {
        cols.push({ key: "dip_p", label: "dip_p", align: "right" });
      }
      cols.push(
        { key: "peaks", label: "peaks", type: "number", align: "right" },
        { key: "spread", label: "spread" },
        { key: "gap", label: "gap" },
        { key: "tmpl", label: "tmpl" },
        { key: "queryid", label: "queryid" },
        { key: "query", label: "query" }
      );
      tableHolder.textContent = "";
      const total = analysis.results.length;
      const flaggedN = analysis.results.filter((r) => r.flag).length;
      tableHolder.appendChild(
        buildSortableTable(
          `Latency modes — ${displayRows.length} shown (${flaggedN} flagged / ${total} analyzed)`,
          displayRows,
          cols,
          "sec-latency-main"
        )
      );

      const recurring = groups.filter((g) => g.member_count > 1);
      groupHolder.textContent = "";
      if (recurring.length) {
        const grpRows = recurring.map((g) => ({
          best_tier: g.best_confidence_tier,
          members: g.member_count,
          peaks: (g.peak_counts || []).join(",") || "",
          gap: histGapStr(g),
          queryids: (g.queryids || []).join(", "),
          template: g.template,
        }));
        const grpCols = [
          {
            key: "best_tier",
            label: "best tier",
            type: "number",
            sortValue: (r) => HIST_TIER_RANK[r.best_tier] || 0,
          },
          { key: "members", label: "members", type: "number", align: "right" },
          { key: "peaks", label: "peaks" },
          { key: "gap", label: "gap" },
          { key: "queryids", label: "queryids" },
          { key: "template", label: "template" },
        ];
        groupHolder.appendChild(
          buildSortableTable(
            `Recurring query templates (${recurring.length})`,
            grpRows,
            grpCols,
            "sec-latency-groups"
          )
        );
      }
    }

    tierSel.addEventListener("change", () => {
      state.minTier = tierSel.value;
      rerender();
    });
    flagChk.addEventListener("change", () => {
      state.flaggedOnly = flagChk.checked;
      rerender();
    });
    dipChk.addEventListener("change", () => {
      latencyShowDipP = dipChk.checked;
      rerender();
    });
    rerender();
  }

  function renderDoc(doc, prevDoc) {
    const app = document.getElementById("app");
    const nav = document.getElementById("app-nav");
    app.textContent = "";
    if (nav) nav.textContent = "";
    lastDoc = doc;
    lastPrevDoc = prevDoc;

    const curFile =
      currentIndex >= 0 && manifestEntries[currentIndex]
        ? manifestEntries[currentIndex].file
        : "";

    const st = doc.pg_stat_statements && doc.pg_stat_statements.per_node;
    const ycqlSt = doc.ycql_stat_statements && doc.ycql_stat_statements.per_node;
    const ash = doc.yb_active_session_history && doc.yb_active_session_history.per_node;
    const topo = doc.node_topology || {};

    const panelPgss = el("div", {
      className: "app-panel",
      "data-viewer-section": "pgss",
      role: "tabpanel",
      id: "panel-pgss",
      "aria-labelledby": "tab-pgss",
    });
    const panelYcql = el("div", {
      className: "app-panel",
      "data-viewer-section": "ycql",
      role: "tabpanel",
      id: "panel-ycql",
      "aria-labelledby": "tab-ycql",
    });
    const panelAsh = el("div", {
      className: "app-panel",
      "data-viewer-section": "ash",
      role: "tabpanel",
      id: "panel-ash",
      "aria-labelledby": "tab-ash",
    });
    const panelTablets = el("div", {
      className: "app-panel",
      "data-viewer-section": "tablets",
      role: "tabpanel",
      id: "panel-tablets",
      "aria-labelledby": "tab-tablets",
    });
    const panelLatency = el("div", {
      className: "app-panel",
      "data-viewer-section": "latency",
      role: "tabpanel",
      id: "panel-latency",
      "aria-labelledby": "tab-latency",
    });

    if (st) {
      const merged = mergeStatements(st);
      const prevSt = prevDoc && prevDoc.pg_stat_statements && prevDoc.pg_stat_statements.per_node;
      let pgTitle = "Top 25 — pg_stat_statements";
      let pgRows = withPgStatTimePercent(merged);
      let pgCols = pgStatStatementColumns(pgRows, st);
      const pgSort = { key: "total_ms", dir: "desc" };
      if (prevDoc && prevSt) {
        const mergedPrev = mergeStatements(prevSt);
        const deltaRows = deltaPgStatMergedRows(merged, mergedPrev);
        panelPgss.appendChild(
          pgStatActivityBannerDelta(prevDoc.generated_at_utc, doc.generated_at_utc, curFile)
        );
        pgTitle = "Top 25 — pg_stat_statements (Δ vs prior snapshot)";
        pgRows = withPgStatDeltaDerivedRows(
          deltaRows,
          prevDoc.generated_at_utc,
          doc.generated_at_utc
        );
        pgCols = pgStatStatementColumnsDelta(pgRows, st);
      } else {
        panelPgss.appendChild(pgStatActivityBannerAt(doc.generated_at_utc, curFile));
        if (prevDoc && !prevSt) {
          panelPgss.appendChild(
            el("div", {
              className: "pgss-activity-note",
              textContent:
                "Previous snapshot has no pg_stat_statements data; showing cumulative totals for this snapshot.",
            })
          );
        }
      }
      panelPgss.appendChild(
        buildSortablePaginatedTable(pgTitle, pgRows, pgCols, 25, "sec-pgss-main", pgSort, {
          unifyStatementHeaders: true,
          pgssAshLinks: true,
        })
      );
    } else {
      panelPgss.appendChild(
        el("p", {
          className: "app-panel-empty",
          textContent: "No pg_stat_statements.per_node in this snapshot.",
        })
      );
    }

    if (ycqlSt) {
      const mergedYcql = mergeYcqlStatements(ycqlSt);
      const prevYcqlSt =
        prevDoc && prevDoc.ycql_stat_statements && prevDoc.ycql_stat_statements.per_node;
      let ycqlTitle = "Top 25 — ycql_stat_statements";
      let ycqlRows = withPgStatTimePercent(mergedYcql);
      let ycqlCols = ycqlStatStatementColumns();
      const ycqlSort = { key: "total_ms", dir: "desc" };
      if (prevDoc && prevYcqlSt) {
        const mergedYcqlPrev = mergeYcqlStatements(prevYcqlSt);
        const prepByKey = new Map(
          mergedYcql.map((r) => [statementMergeKey(r), !!r.is_prepared])
        );
        const deltaYcqlRows = deltaPgStatMergedRows(mergedYcql, mergedYcqlPrev).map((r) => ({
          ...r,
          is_prepared: prepByKey.get(statementMergeKey(r)) || false,
        }));
        panelYcql.appendChild(
          pgStatActivityBannerDelta(prevDoc.generated_at_utc, doc.generated_at_utc, curFile)
        );
        ycqlTitle = "Top 25 — ycql_stat_statements (Δ vs prior snapshot)";
        ycqlRows = withPgStatDeltaDerivedRows(
          deltaYcqlRows,
          prevDoc.generated_at_utc,
          doc.generated_at_utc
        );
        ycqlCols = ycqlStatStatementColumnsDelta();
      } else {
        panelYcql.appendChild(pgStatActivityBannerAt(doc.generated_at_utc, curFile));
        if (prevDoc && !prevYcqlSt) {
          panelYcql.appendChild(
            el("div", {
              className: "pgss-activity-note",
              textContent:
                "Previous snapshot has no ycql_stat_statements data; showing cumulative totals for this snapshot.",
            })
          );
        }
      }
      panelYcql.appendChild(
        buildSortablePaginatedTable(
          ycqlTitle,
          ycqlRows,
          ycqlCols,
          25,
          "sec-ycql-main",
          ycqlSort,
          { unifyStatementHeaders: true, pgssAshLinks: true }
        )
      );
    } else {
      panelYcql.appendChild(
        el("p", {
          className: "app-panel-empty",
          textContent: "No ycql_stat_statements.per_node in this snapshot.",
        })
      );
    }

    if (ash) {
      panelAsh.appendChild(ashWindowActivityBanner(doc, curFile));
      const qF = ashQueryIdFilter;
      const nodeF = ashNodeIdFilter;
      const tableF = ashTableIdFilter;
      let ashData = ash;
      if (nodeF) ashData = filterAshPerNodeByNodeId(ashData, nodeF);
      if (qF) ashData = filterAshPerNodeByQueryId(ashData, qF);
      if (tableF) ashData = filterAshPerNodeByTableId(ashData, tableF);

      if (nodeF) {
        const place = ashNodePlacementLine(topo, nodeF);
        const bn = el("div", { className: "ash-mode-banner ash-mode-banner--scoped" });
        const nodeTitle = el("div", {
          className: "ash-mode-banner-title ash-mode-banner-title--kv",
        });
        nodeTitle.appendChild(el("span", { textContent: "node_id=" }));
        nodeTitle.appendChild(
          el("span", {
            className: "ash-mode-banner-query-highlight ash-mode-banner-query-highlight--inline",
            textContent: nodeF,
          })
        );
        bn.appendChild(nodeTitle);
        const row = el("div", { className: "ash-mode-banner-query-row" });
        row.appendChild(el("span", { className: "ash-mode-banner-query-k", textContent: "placement" }));
        row.appendChild(
          el("span", {
            className: place
              ? "ash-mode-banner-query-highlight"
              : "ash-mode-banner-query-highlight ash-mode-banner-query-highlight--empty",
            textContent: place || "(no topology in snapshot)",
          })
        );
        bn.appendChild(row);
        panelAsh.appendChild(bn);
      }
      if (tableF) {
        const subRaw = ashSubtitleNsObjectForTableId(doc, tableF);
        const sub = subRaw != null ? String(subRaw).trim() : "";
        const schemaEnt = tableSchemaForTableId(doc, tableF);
        const bn = el("div", { className: "ash-mode-banner ash-mode-banner--scoped" });
        bn.appendChild(
          el("div", {
            className: "ash-mode-banner-title",
            textContent: `table_id=${tableF}`,
          })
        );
        const row = el("div", { className: "ash-mode-banner-query-row" });
        row.appendChild(el("span", { className: "ash-mode-banner-query-k", textContent: "table/index" }));
        row.appendChild(
          el("span", {
            className: sub
              ? "ash-mode-banner-query-highlight"
              : "ash-mode-banner-query-highlight ash-mode-banner-query-highlight--empty",
            textContent: sub || "(qualified name not in snapshot)",
          })
        );
        bn.appendChild(row);
        if (schemaEnt && schemaEnt.engine === "YSQL" && schemaEnt.ddl != null && String(schemaEnt.ddl).trim() !== "") {
          const ddlRow = el("div", { className: "ash-mode-banner-query-row" });
          ddlRow.appendChild(el("span", { className: "ash-mode-banner-query-k", textContent: "schema" }));
          ddlRow.appendChild(
            el("span", {
              className: "ash-mode-banner-query-highlight ash-mode-banner-sql",
              textContent: String(schemaEnt.ddl).trim(),
            })
          );
          bn.appendChild(ddlRow);
        } else if (schemaEnt && schemaEnt.engine === "YSQL" && schemaEnt.error) {
          const errRow = el("div", { className: "ash-mode-banner-query-row" });
          errRow.appendChild(el("span", { className: "ash-mode-banner-query-k", textContent: "schema" }));
          errRow.appendChild(
            el("span", {
              className: "ash-mode-banner-query-highlight ash-mode-banner-query-highlight--empty",
              textContent: String(schemaEnt.error),
            })
          );
          bn.appendChild(errRow);
        }
        panelAsh.appendChild(bn);
      }
      if (qF) {
        const qRaw = getQueryTextForToolbar(doc, qF);
        const qText =
          qRaw != null && String(qRaw).trim() !== "" ? String(qRaw).trim() : "";
        const note = el("div", { className: "ash-mode-banner ash-mode-banner--scoped" });
        let ashQueryTitle = `query_id=${qF}`;
        if (st) {
          const rowDb = mergedStatementRowForQuery(st, mergeStatements, qF);
          if (rowDb && rowDb.dbname) ashQueryTitle += `; dbname=${rowDb.dbname}`;
        }
        note.appendChild(
          el("div", {
            className: "ash-mode-banner-title",
            textContent: ashQueryTitle,
          })
        );
        const row = el("div", { className: "ash-mode-banner-query-row" });
        row.appendChild(el("span", { className: "ash-mode-banner-query-k", textContent: "query" }));
        row.appendChild(
          el("span", {
            className: qText
              ? "ash-mode-banner-query-highlight"
              : "ash-mode-banner-query-highlight ash-mode-banner-query-highlight--empty",
            textContent: qText || "(no text in snapshot)",
          })
        );
        note.appendChild(row);
        appendAshScopedQueryStatementLines(note, doc, prevDoc, qF, ash);
        panelAsh.appendChild(note);
      }
      const ashClusterNodes = ashSnapshotClusterNodeCount(doc, ash);
      const ashShowNodeLoadDist = ashClusterNodes > 1 && !nodeF;
      /* Same pg_stat query text as merged rows so namespace+query / ns+object+query load-dist keys match. */
      const flatAsh = enrichAshRowsQueryFromPgStat(doc, flattenAsh(ashData, topo));

      let mergedAsh = enrichAshRowsQueryFromPgStat(doc, mergeAsh(ashData));
      mergedAsh = attachAshNodeLoadDistribution(
        mergedAsh,
        flatAsh,
        ashMergeKey,
        ashShowNodeLoadDist,
        true
      );

      const ashIntervalSec = ashWindowIntervalSeconds(doc);
      const ashEnriched = (rows) => withAshLoadPercent(withAshSessionsPerSec(rows, ashIntervalSec));
      const ASH_SPS_COL = {
        key: "sessions_per_sec",
        type: "number",
        label: "Active Sessions / sec",
        align: "right",
      };
      const ASH_LOAD_COL = {
        key: "load_pct",
        label: "Load %",
        type: "number",
        align: "right",
      };
      const mergedAshL = ashEnriched(mergedAsh);
      const ashMainColsBase = [
        ASH_SPS_COL,
        ASH_LOAD_COL,
        { key: "namespace_name", label: "namespace" },
        { key: "object_name", label: "object_name" },
        { key: "wait_event_component", label: "component" },
        { key: "wait_event_type", label: "wait_event_type" },
        { key: "wait_event", label: "wait_event" },
        { key: "query", label: "query" },
        { key: "query_id", label: "query_id" },
      ];
      const ashMainColsAll = tableF
        ? ashMainColsBase.filter((c) => c.key !== "namespace_name" && c.key !== "object_name")
        : ashMainColsBase;
      const ashMainColsStripped = qF ? ashColumnsWithoutQueryIdAndQuery(ashMainColsAll) : ashMainColsAll;
      const ashMainCols = spliceAshNodeLoadDistributionColumn(
        ashMainColsStripped,
        ashClusterNodes,
        ashShowNodeLoadDist
      );
      const ashReportCellOpts = {
        ashObjectLinks: true,
        ashQueryIdLinks: true,
        ashQueryTextLinks: true,
      };
      const ashPaginatedOpts = { ashCellOpts: ashReportCellOpts };
      const ashMainTop50GroupLabel = qF
        ? "Table/Index + Wait_Event"
        : tableF
        ? "Query + Wait_Event"
        : "Table/Index + Query + Wait_Event";
      panelAsh.appendChild(
        buildSortablePaginatedTable(
          `Top 50 Active Sessions/sec Grouped By: ${ashMainTop50GroupLabel}`,
          mergedAshL,
          ashMainCols,
          50,
          "sec-ash-main",
          undefined,
          ashPaginatedOpts
        )
      );

      if (tableF) {
        let byQueryId = groupAshByQueryId(mergedAsh);
        byQueryId = attachAshNodeLoadDistribution(
          byQueryId,
          flatAsh,
          bucketKeyAshQueryIdFlat,
          ashShowNodeLoadDist
        );
        const byQueryIdL = ashEnriched(byQueryId);
        panelAsh.appendChild(
          buildSortableTable(
            `Active Sessions/Sec Grouped By: Query (${byQueryIdL.length} groups)`,
            byQueryIdL,
            spliceAshNodeLoadDistributionColumn(
              [
                ASH_SPS_COL,
                ASH_LOAD_COL,
                { key: "query", label: "query" },
                { key: "query_id", label: "query_id" },
              ],
              ashClusterNodes,
              ashShowNodeLoadDist
            ),
            "sec-ash-by-query-id",
            ashReportCellOpts
          )
        );
      }

      if (!qF && !tableF) {
        let byNsQuery = groupAshByNamespaceQuery(mergedAsh);
        byNsQuery = attachAshNodeLoadDistribution(
          byNsQuery,
          flatAsh,
          bucketKeyAshNamespaceQueryFlat,
          ashShowNodeLoadDist
        );
        const byNsQueryL = ashEnriched(byNsQuery);
        const byNsQueryColsAll = spliceAshNodeLoadDistributionColumn(
          [
            ASH_SPS_COL,
            ASH_LOAD_COL,
            { key: "namespace_name", label: "namespace" },
            { key: "query", label: "query" },
            { key: "query_id", label: "query_id" },
          ],
          ashClusterNodes,
          ashShowNodeLoadDist
        );
        const byNsQueryTitle = `Active Sessions/Sec Grouped By: Database & Query (${byNsQueryL.length} groups)`;
        panelAsh.appendChild(
          buildSortableTable(byNsQueryTitle, byNsQueryL, byNsQueryColsAll, "sec-ash-ns-q", ashReportCellOpts)
        );

        let byNsObjBuckets = ashAggregateNsObjectBuckets(mergedAsh);
        byNsObjBuckets = attachAshNodeLoadDistribution(
          byNsObjBuckets,
          flatAsh,
          bucketKeyAshNsObjBucketFlat,
          ashShowNodeLoadDist
        );
        const byNsObjTop = withAshLoadPercent(
          withAshSessionsPerSec(byNsObjBuckets.slice(0, 50), ashIntervalSec),
          byNsObjBuckets
        );
        panelAsh.appendChild(
          buildSortableTable(
            "Top 50 Active Sessions/Sec Grouped By: Database & Table/Index",
            byNsObjTop,
            spliceAshNodeLoadDistributionColumn(
              [
                ASH_SPS_COL,
                ASH_LOAD_COL,
                { key: "namespace_name", label: "namespace" },
                { key: "object_name", label: "object_name" },
              ],
              ashClusterNodes,
              ashShowNodeLoadDist
            ),
            "sec-ash-ns-obj",
            ashReportCellOpts
          )
        );
      }

      if (!tableF) {
        let byNsObjQuery = groupAshByNamespaceObjectQuery(mergedAsh, { ignoreQueryInKey: !!qF });
        byNsObjQuery = attachAshNodeLoadDistribution(
          byNsObjQuery,
          flatAsh,
          bucketKeyAshNsObjQueryFlatFactory(!!qF),
          ashShowNodeLoadDist
        );
        const byNsObjQueryL = ashEnriched(byNsObjQuery);
        const byNsObjQueryTitle = qF
          ? `Active Sessions/sec Grouped By: Database + Table/Index (${byNsObjQueryL.length} groups)`
          : `Active Sessions/Sec Grouped By: Table/Index & Query (${byNsObjQueryL.length} groups)`;
        const byNsObjQueryColsAll = spliceAshNodeLoadDistributionColumn(
          [
            ASH_SPS_COL,
            ASH_LOAD_COL,
            { key: "namespace_name", label: "namespace" },
            { key: "object_name", label: "object_name" },
            { key: "query", label: "query" },
            { key: "query_id", label: "query_id" },
          ],
          ashClusterNodes,
          ashShowNodeLoadDist
        );
        const byNsObjQueryCols = qF
          ? ashColumnsWithoutQueryIdAndQuery(byNsObjQueryColsAll)
          : byNsObjQueryColsAll;
        panelAsh.appendChild(
          buildSortableTable(
            byNsObjQueryTitle,
            byNsObjQueryL,
            byNsObjQueryCols,
            "sec-ash-ns-obj-q",
            ashReportCellOpts
          )
        );
      }

      if (!nodeF) {
        const byNode = ashEnriched(sumAshByNode(flatAsh));
        panelAsh.appendChild(
          buildSortableTable(
            "Active Sessions/Sec Grouped By: Node",
            byNode,
            [
              ASH_SPS_COL,
              ASH_LOAD_COL,
              { key: "node_id", label: "node_id" },
              { key: "cloud", label: "cloud" },
              { key: "region", label: "region" },
              { key: "zone", label: "zone" },
            ],
            "sec-ash-node",
            { ashNodeLinks: true }
          )
        );

        let byCrzRows = groupSum(flatAsh, bucketKeyAshCrzFlat).map((x) => {
            const parts = String(x.key).split("\t");
            return {
              cloud: parts[0] != null ? parts[0] : "",
              region: parts[1] != null ? parts[1] : "",
              zone: parts[2] != null ? parts[2] : "",
              samples: x.samples,
            };
          });
        const byCrz = ashEnriched(byCrzRows);
        panelAsh.appendChild(
          buildSortableTable(
            "Active Sessions/Sec Grouped By: Cloud, Region & Zone",
            byCrz,
            [
              ASH_SPS_COL,
              ASH_LOAD_COL,
              { key: "cloud", label: "cloud" },
              { key: "region", label: "region" },
              { key: "zone", label: "zone" },
            ],
            "sec-ash-crz"
          )
        );
      }

      if (!tableF) {
        let byDbRows = groupSum(mergedAsh, (r) => String(r.namespace_name || "(none)")).map((x) => ({
          namespace_name: x.key,
          samples: x.samples,
        }));
        byDbRows = attachAshNodeLoadDistribution(
          byDbRows,
          flatAsh,
          bucketKeyAshDbFlat,
          ashShowNodeLoadDist
        );
        const byDb = ashEnriched(byDbRows);
        panelAsh.appendChild(
          buildSortableTable(
            "Active Sessions/Sec Grouped By: Database",
            byDb,
            spliceAshNodeLoadDistributionColumn(
              [ASH_SPS_COL, ASH_LOAD_COL, { key: "namespace_name", label: "namespace" }],
              ashClusterNodes,
              ashShowNodeLoadDist
            ),
            "sec-ash-db"
          )
        );
      }
    } else {
      panelAsh.appendChild(
        el("p", {
          className: "app-panel-empty",
          textContent: "No yb_active_session_history.per_node in this snapshot.",
        })
      );
    }

    const ltRaw = doc.yb_local_tablets && doc.yb_local_tablets.per_node;
    const ltHasAnyRows =
      ltRaw &&
      Object.keys(ltRaw).some((nid) => Array.isArray(ltRaw[nid]) && ltRaw[nid].length > 0);
    const lt = ltRaw ? filterLocalTabletsDataReady(ltRaw) : null;
    const hasLocalTablets =
      lt &&
      Object.keys(lt).some((nid) => Array.isArray(lt[nid]) && lt[nid].length > 0);
    if (hasLocalTablets) {
      const perTable = tabletsPerTableReport(lt);
      panelTablets.appendChild(
        buildSortableTable(
          "Tablet Distribution - By Table",
          perTable,
          [
            { key: "tablets", label: "tablets", type: "number" },
            { key: "namespace_name", label: "namespace" },
            { key: "table_name", label: "table_name" },
            {
              key: "per_node_counts",
              label: "per-node counts",
              type: "number",
              sortValue: (r) => {
                const a = r.per_node_counts;
                if (!a || !a.length) return 0;
                return Math.max(...a.map((x) => x.count));
              },
            },
          ],
          "sec-lt-per-table",
          { tabletTableNameLinks: true }
        )
      );
      panelTablets.appendChild(
        buildSortableTable(
          "Tablet Distribution - By Node",
          tabletsPerNodeReport(lt, topo),
          [
            { key: "tablets", label: "tablets", type: "number" },
            { key: "node_id", label: "node_id" },
            { key: "cloud", label: "cloud" },
            { key: "region", label: "region" },
            { key: "zone", label: "zone" },
          ],
          "sec-lt-per-node",
          { ashNodeLinks: true }
        )
      );
      panelTablets.appendChild(
        buildSortableTable(
          "Tablet Distribution - By Cloud:Region:Zone",
          tabletsPerCloudRegionZoneReport(lt, topo),
          [
            { key: "tablets", label: "tablets", type: "number" },
            { key: "cloud", label: "cloud" },
            { key: "region", label: "region" },
            { key: "zone", label: "zone" },
          ],
          "sec-lt-per-crz"
        )
      );
    } else {
      panelTablets.appendChild(
        el("p", {
          className: "app-panel-empty",
          textContent:
            ltHasAnyRows && !hasLocalTablets
              ? "No tablets in TABLET_DATA_READY state in this snapshot's yb_local_tablets data."
              : "No yb_local_tablets.per_node data in this snapshot.",
        })
      );
    }

    if (docHasLatencyHistograms(doc)) {
      try {
        renderLatencyPanel(panelLatency, doc, prevDoc);
      } catch (e) {
        panelLatency.appendChild(
          el("p", {
            className: "app-panel-empty",
            textContent: `Latency analysis failed: ${(e && e.message) || e}`,
          })
        );
      }
    }

    app.appendChild(panelPgss);
    app.appendChild(panelYcql);
    app.appendChild(panelAsh);
    app.appendChild(panelTablets);
    if (docHasLatencyHistograms(doc)) app.appendChild(panelLatency);
    buildViewerNav();
    updateAshFilterToolbar();
  }

  /** Manifest carries cumulative per-snapshot call totals. The bar at index i shows the call *rate*
   * — (calls(i) − calls(i−1)) / window-seconds, clamped ≥ 0 (pg_stat resets/prunes happen and would
   * otherwise plot huge negative spikes). Snapshot intervals vary, so plotting calls/s rather than the
   * raw delta keeps bar heights comparable. A bar is "pending" (dim, no height) when its rate can't be
   * computed: i = 0 has no prior, and entries written by ybtop < 0.1.11 carry no `ysql_calls`/
   * `ycql_calls` at all — those windows (and the first window after them) are shown pending, not zero. */
  let windowChartCollapsed = false;
  const WINDOW_CHART_LS_KEY = "ybtop.window-chart.collapsed";

  function manifestEntryTotalCalls(ent) {
    if (!ent) return null;
    const y = Number(ent.ysql_calls);
    const c = Number(ent.ycql_calls);
    const haveAny = Number.isFinite(y) || Number.isFinite(c);
    if (!haveAny) return null;
    return (Number.isFinite(y) ? y : 0) + (Number.isFinite(c) ? c : 0);
  }

  function deltaCallsForIndex(i) {
    if (i <= 0) return null;
    const cur = manifestEntryTotalCalls(manifestEntries[i]);
    const prev = manifestEntryTotalCalls(manifestEntries[i - 1]);
    if (cur == null || prev == null) return null;
    const d = cur - prev;
    return d > 0 ? d : 0;
  }

  /** Seconds spanned by window i (prior snapshot → this one), from manifest `utc` timestamps.
   * null when either timestamp is missing/unparseable or the span is non-positive. */
  function windowSecondsForIndex(i) {
    if (i <= 0) return null;
    const cur = manifestEntries[i] && manifestEntries[i].utc;
    const prev = manifestEntries[i - 1] && manifestEntries[i - 1].utc;
    if (!cur || !prev) return null;
    const tc = new Date(String(cur)).getTime();
    const tp = new Date(String(prev)).getTime();
    if (Number.isNaN(tc) || Number.isNaN(tp)) return null;
    const sec = (tc - tp) / 1000;
    return sec > 0 ? sec : null;
  }

  /** Δcalls per second for window i. Snapshot intervals vary, so the chart plots this rate (not the
   * raw Δ) to keep bar heights comparable across windows. null when the delta or span is unavailable. */
  function callRateForIndex(i) {
    const d = deltaCallsForIndex(i);
    if (d === null) return null;
    const sec = windowSecondsForIndex(i);
    if (sec == null) return null;
    return d / sec;
  }

  function formatCount(n) {
    if (n == null) return "—";
    const x = Number(n);
    if (!Number.isFinite(x)) return "—";
    return x.toLocaleString();
  }

  /** Compact calls/s: more precision at low rates, rounded thousands-separated at high rates. */
  function formatRate(n) {
    if (n == null) return "—";
    const x = Number(n);
    if (!Number.isFinite(x)) return "—";
    if (x === 0) return "0";
    if (x >= 100) return Math.round(x).toLocaleString();
    if (x >= 10) return x.toFixed(1);
    return x.toFixed(2);
  }

  function loadWindowChartCollapsedFromStorage() {
    try {
      const v = window.localStorage && window.localStorage.getItem(WINDOW_CHART_LS_KEY);
      windowChartCollapsed = v === "1";
    } catch (_e) {
      windowChartCollapsed = false;
    }
  }

  function saveWindowChartCollapsedToStorage() {
    try {
      if (window.localStorage) {
        window.localStorage.setItem(WINDOW_CHART_LS_KEY, windowChartCollapsed ? "1" : "0");
      }
    } catch (_e) {
      /* ignore */
    }
  }

  function applyWindowChartCollapsedClass() {
    const wrap = document.getElementById("window-chart");
    if (!wrap) return;
    wrap.classList.toggle("window-chart--collapsed", windowChartCollapsed);
    const btn = document.getElementById("window-chart-toggle");
    if (btn) {
      btn.textContent = windowChartCollapsed ? "▶" : "▼";
      btn.setAttribute("aria-expanded", windowChartCollapsed ? "false" : "true");
    }
  }

  function renderWindowChart() {
    const bars = document.getElementById("window-chart-bars");
    const status = document.getElementById("window-chart-status");
    if (!bars) return;
    bars.textContent = "";
    const n = manifestEntries.length;
    if (!n) {
      if (status) status.textContent = "";
      return;
    }
    let resolved = 0;
    let maxRate = 0;
    const rates = new Array(n);
    for (let i = 0; i < n; i += 1) {
      const r = callRateForIndex(i);
      rates[i] = r;
      if (r !== null) {
        resolved += 1;
        if (r > maxRate) maxRate = r;
      }
    }
    if (status) {
      status.textContent = resolved < n - 1 ? `${resolved}/${n - 1}` : "";
    }
    const denom = maxRate > 0 ? maxRate : 1;
    for (let i = 0; i < n; i += 1) {
      const ent = manifestEntries[i];
      const rate = rates[i];
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "window-chart-bar";
      btn.setAttribute("role", "listitem");
      if (i === currentIndex) btn.classList.add("window-chart-bar--current");
      const fill = document.createElement("span");
      fill.className = "window-chart-bar-fill";
      if (rate === null) {
        btn.classList.add("window-chart-bar--pending");
      } else if (rate === 0) {
        btn.classList.add("window-chart-bar--zero");
      } else {
        const pct = Math.max(2, Math.round((rate / denom) * 100));
        fill.style.height = `${pct}%`;
      }
      btn.appendChild(fill);
      const isoEnd = (ent && ent.utc) || "";
      const endHuman =
        formatSnapshotDatePart(isoEnd) && formatSnapshotTimePart(isoEnd)
          ? `${formatSnapshotDatePart(isoEnd)} ${formatSnapshotTimePart(isoEnd)}`
          : snapshotHumanFromFilename(ent.file) || ent.file;
      const prevEnt = i > 0 ? manifestEntries[i - 1] : null;
      const isoStart = (prevEnt && prevEnt.utc) || "";
      const startHuman =
        prevEnt && formatSnapshotDatePart(isoStart) && formatSnapshotTimePart(isoStart)
          ? `${formatSnapshotDatePart(isoStart)} ${formatSnapshotTimePart(isoStart)}`
          : prevEnt
            ? snapshotHumanFromFilename(prevEnt.file)
            : "";
      let tip;
      if (rate === null) {
        const cum = manifestEntryTotalCalls(ent);
        tip =
          cum != null
            ? `${endHuman} UTC — ${formatCount(cum)} cumulative calls`
            : `${endHuman} UTC — no call data (snapshot predates call tracking)`;
      } else {
        tip = `${startHuman} → ${endHuman} UTC — ${formatRate(rate)} calls/s`;
      }
      wireQuickNodeIdTooltip(btn, tip);
      btn.addEventListener("click", () => {
        if (i !== currentIndex) showSnapshotAt(i);
      });
      bars.appendChild(btn);
    }
  }

  function wireWindowChart() {
    loadWindowChartCollapsedFromStorage();
    applyWindowChartCollapsedClass();
    const btn = document.getElementById("window-chart-toggle");
    if (btn) {
      btn.addEventListener("click", () => {
        windowChartCollapsed = !windowChartCollapsed;
        saveWindowChartCollapsedToStorage();
        applyWindowChartCollapsedClass();
      });
    }
  }

  const MANIFEST_POLL_INTERVAL_MS = 30_000;
  let manifestRefreshInFlight = false;
  let manifestPollTimer = null;

  /**
   * Re-fetch the manifest, splice new entries in, drop GC'd ones, keep the user pinned to the same
   * snapshot file when possible (so a new arrival doesn't yank them off the window they're reading).
   * Chart + nav controls are re-rendered after; the active doc itself is not refetched.
   */
  async function refreshManifest() {
    if (manifestRefreshInFlight) return;
    manifestRefreshInFlight = true;
    try {
      const fresh = await loadManifest();
      if (!Array.isArray(fresh) || !fresh.length) return;
      const prevFile =
        currentIndex >= 0 && manifestEntries[currentIndex]
          ? manifestEntries[currentIndex].file
          : null;
      manifestEntries = fresh;
      currentIndex = prevFile
        ? manifestEntries.findIndex((e) => e && e.file === prevFile)
        : manifestEntries.length - 1;
      const btnPrev = document.getElementById("btn-prev");
      const btnNext = document.getElementById("btn-next");
      const btnFirst = document.getElementById("btn-first");
      const btnLast = document.getElementById("btn-last");
      if (btnPrev) btnPrev.disabled = currentIndex <= 0;
      if (btnNext) btnNext.disabled = currentIndex < 0 || currentIndex >= manifestEntries.length - 1;
      if (btnFirst) btnFirst.disabled = currentIndex <= 0;
      if (btnLast) btnLast.disabled = currentIndex < 0 || currentIndex >= manifestEntries.length - 1;
      const ent = currentIndex >= 0 ? manifestEntries[currentIndex] : null;
      updateNavDisplay(currentIndex >= 0 ? currentIndex : 0, manifestEntries.length, ent, lastDoc);
      renderWindowChart();
    } catch (_e) {
      /* ignore transient manifest fetch failures; retry on the next tick. */
    } finally {
      manifestRefreshInFlight = false;
    }
  }

  function startManifestPolling() {
    if (manifestPollTimer != null) return;
    manifestPollTimer = setInterval(refreshManifest, MANIFEST_POLL_INTERVAL_MS);
  }

  async function showSnapshotAt(index) {
    const app = document.getElementById("app");
    if (!manifestEntries.length) {
      const nav0 = document.getElementById("app-nav");
      if (nav0) nav0.textContent = "";
      app.textContent = "No entries in ybtop.manifest.json";
      return;
    }
    if (index < 0 || index >= manifestEntries.length) return;
    currentIndex = index;
    const ent = manifestEntries[index];
    updateNavDisplay(index, manifestEntries.length, ent, null);
    // Persist the window in the URL (replace, not push) so reloads land here.
    writeViewerStateToUrl();

    document.getElementById("btn-prev").disabled = index <= 0;
    document.getElementById("btn-next").disabled = index >= manifestEntries.length - 1;
    document.getElementById("btn-first").disabled = index <= 0;
    document.getElementById("btn-last").disabled = index >= manifestEntries.length - 1;
    renderWindowChart();

    app.textContent = "Loading…";
    const navEl = document.getElementById("app-nav");
    if (navEl) navEl.textContent = "";
    setStatus("", false);
    const name = ent.file;
    try {
      const prevName = index > 0 ? manifestEntries[index - 1].file : null;
      const analysisName = ent && ent.latency_analysis ? ent.latency_analysis : null;
      const [doc, prevDoc, analysis] = await Promise.all([
        fetchJson(name),
        prevName ? fetchJson(prevName).catch(() => null) : Promise.resolve(null),
        analysisName ? fetchJson(analysisName).catch(() => null) : Promise.resolve(null),
      ]);
      // Precomputed (dip-confirmed) latency report, when watch --snapshot-latency-analysis produced one.
      if (doc && analysis) doc._latencyAnalysis = analysis;
      app.textContent = "";
      renderDoc(doc, prevDoc);
      updateNavDisplay(index, manifestEntries.length, ent, doc);
      renderWindowChart();
      setStatus("", false);
    } catch (e) {
      const navErr = document.getElementById("app-nav");
      if (navErr) navErr.textContent = "";
      app.textContent = "";
      const banner = el("div", { className: "err-banner" });
      banner.textContent = `Could not load ${name}: ${e.message || e}. Use First, Last, Prev, or Next to try another snapshot.`;
      app.appendChild(banner);
      setStatus("Load failed", true);
    }
  }

  // Shown when an explicit ?t=<time> in the URL matches no manifest entry
  // (typo, or the snapshot was rotated out). We surface it rather than
  // silently opening the newest, while leaving the nav controls usable.
  function showWindowNotFoundError(key) {
    const app = document.getElementById("app");
    const navEl = document.getElementById("app-nav");
    if (navEl) navEl.textContent = "";
    const len = manifestEntries.length;
    const jump = document.getElementById("nav-jump");
    if (jump) jump.max = String(len);
    const total = document.getElementById("nav-total");
    if (total) total.textContent = ` / ${len}`;
    const fileEl = document.getElementById("nav-file");
    if (fileEl) fileEl.textContent = "";
    app.textContent = "";
    const banner = el("div", { className: "err-banner" });
    banner.textContent =
      `No snapshot matches ?t=${key} — the time is invalid or that snapshot has been rotated ` +
      `out of ybtop.manifest.json. Use First, Last, Prev, Next, the call-frequency chart, ` +
      `or the window number box to pick a window.`;
    app.appendChild(banner);
    setStatus("Snapshot not found", true);
  }

  function navPrev() {
    if (currentIndex > 0) showSnapshotAt(currentIndex - 1);
  }
  function navNext() {
    if (currentIndex < manifestEntries.length - 1) showSnapshotAt(currentIndex + 1);
  }
  function navFirst() {
    if (currentIndex > 0) showSnapshotAt(0);
  }
  function navLast() {
    if (manifestEntries.length > 0 && currentIndex < manifestEntries.length - 1) {
      showSnapshotAt(manifestEntries.length - 1);
    }
  }

  // Parse a 1-based window number, clamp to [1, len], and load that window.
  function jumpTo1Based(value) {
    const len = manifestEntries.length;
    if (!len) return;
    let n = parseInt(value, 10);
    if (!Number.isFinite(n)) return;
    n = Math.max(1, Math.min(len, n));
    showSnapshotAt(n - 1);
  }

  function wireNav() {
    document.getElementById("btn-prev").addEventListener("click", navPrev);
    document.getElementById("btn-next").addEventListener("click", navNext);
    document.getElementById("btn-first").addEventListener("click", navFirst);
    document.getElementById("btn-last").addEventListener("click", navLast);

    const jump = document.getElementById("nav-jump");
    if (jump) {
      jump.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          jumpTo1Based(jump.value);
          jump.blur();
        } else if (e.key === "Escape") {
          e.preventDefault();
          jump.value = String(currentIndex + 1);
          jump.blur();
        }
      });
      jump.addEventListener("change", () => jumpTo1Based(jump.value));
    }

    document.addEventListener("keydown", (e) => {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const t = e.target;
      const inInput =
        t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable);
      // Inside an input, let typing/arrows/Enter behave normally.
      if (inInput) return;
      switch (e.key) {
        case "ArrowLeft":
          e.preventDefault();
          navPrev();
          break;
        case "ArrowRight":
          e.preventDefault();
          navNext();
          break;
        case "Home":
          e.preventDefault();
          navFirst();
          break;
        case "End":
          e.preventDefault();
          navLast();
          break;
        case "g":
        case "G": {
          e.preventDefault();
          const j = document.getElementById("nav-jump");
          if (j) {
            j.focus();
            j.select();
          }
          break;
        }
        default:
          break;
      }
    });
  }

  function clearYbtopVersionPlaceholder() {
    const vEl = document.getElementById("ybtop-version");
    if (vEl && vEl.textContent.indexOf("__YBTOP_VERSION__") !== -1) {
      vEl.textContent = "";
    }
  }

  async function boot() {
    clearYbtopVersionPlaceholder();
    wireNav();
    wireWindowChart();
    window.addEventListener("popstate", () => {
      readViewerStateFromUrl();
      // A history entry may point at a different window (e.g. ASH deep-link);
      // load it rather than just re-rendering the current snapshot.
      const target = indexForWindowKey(urlWindowKey);
      if (target >= 0 && target !== currentIndex) {
        showSnapshotAt(target);
        return;
      }
      if (lastDoc) {
        renderDoc(lastDoc, lastPrevDoc);
      } else {
        updateAshFilterToolbar();
      }
    });
    try {
      manifestEntries = await loadManifest();
    } catch (e) {
      const navM = document.getElementById("app-nav");
      if (navM) navM.textContent = "";
      document.getElementById("app").textContent = `Failed to load ${MANIFEST}: ${e.message || e}`;
      setStatus("Manifest error", true);
      return;
    }
    if (!manifestEntries.length) {
      const navE = document.getElementById("app-nav");
      if (navE) navE.textContent = "";
      document.getElementById("app").textContent = "Manifest has no entries.";
      return;
    }
    readViewerStateFromUrl();
    // Honor a pinned `t` window from the URL. If `t` is present but matches no
    // snapshot, surface an error rather than silently opening the newest. With
    // no `t`, open the newest.
    if (urlWindowKey) {
      const pinned = indexForWindowKey(urlWindowKey);
      if (pinned >= 0) {
        showSnapshotAt(pinned);
      } else {
        showWindowNotFoundError(urlWindowKey);
      }
    } else {
      showSnapshotAt(manifestEntries.length - 1);
    }
    renderWindowChart();
    startManifestPolling();
  }

  boot();
})();
