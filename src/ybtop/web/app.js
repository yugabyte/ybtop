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
  /** Expand ashQueryIdFilter to every statement in its canonical-query family. */
  let ashCanonicalizeFilter = false;
  /** YSQL canonical families are database-scoped; null lets direct URLs pick the dominant match. */
  let ashCanonicalDbnameFilter = null;
  let ashNodeIdFilter = null;
  let ashTableIdFilter = null;

  /** Recurring-template table; gated on Merge similar SQL. Shared across statement / ASH / Latency. */
  let showRecurringTemplates = false;
  /**
   * Viewer-wide: when true, query templates collapse IN-lists, VALUES row-lists, $N binds,
   * and per-call comments via normalizeQueryTemplate. When false, each distinct SQL string is its
   * own template key (whitespace trimmed only).
   */
  let mergeSimilarSql = false;
  /** Prior Merge value while a canonical-family ASH URL forces grouping on; null when not held. */
  let mergeSimilarSqlSavedForFamily = null;
  /** True only when this snapshot resolved a family for the canonicalize URL (not the URL flag alone). */
  let canonicalFamilyResolved = false;
  /** Latency modes tab: include the dip_p column when true (browser-only UI preference). */
  let latencyShowDipP = true;
  /** Survive Merge similar SQL / full renderDoc rebuilds (same idea as latencyShowDipP). */
  let latencyMinTier = "high";
  let latencyFlaggedOnly = false;

  const VIEWER_SECTION_IDS = ["pgss", "ycql", "ash", "tablets", "latency"];
  const LATENCY_MIN_TIERS = ["very_high", "high", "moderate", "unconfirmed", "all"];
  const LATENCY_MIN_TIER_DEFAULT = "high";

  function urlParamIsTrue(p, key) {
    const v = p.get(key);
    return v === "t" || v === "true" || v === "1";
  }

  function urlParamIsFalse(p, key) {
    const v = p.get(key);
    return v === "f" || v === "false" || v === "0";
  }

  /**
   * Merge similar SQL as a user preference. While a canonical-family ASH report
   * forces the live switch on, the held value is what belongs in the URL and in
   * copied links — `canonicalize=t` already means grouping is on for that view.
   */
  function mergeSimilarSqlForUrl() {
    return mergeSimilarSqlSavedForFamily !== null
      ? mergeSimilarSqlSavedForFamily
      : mergeSimilarSql;
  }

  /** Non-default UI toggles. Omitted when they match the viewer defaults. */
  function applyViewerToggleParams(p) {
    const mergePref = mergeSimilarSqlForUrl();
    if (mergePref) p.set("merge", "t");
    if (mergePref && showRecurringTemplates) p.set("templates", "t");
    if (latencyFlaggedOnly) p.set("flagged", "t");
    if (!latencyShowDipP) p.set("dip_p", "f");
    if (latencyMinTier && latencyMinTier !== LATENCY_MIN_TIER_DEFAULT) {
      p.set("min_tier", latencyMinTier);
    }
  }

  function readViewerToggleParams(p) {
    mergeSimilarSql = urlParamIsTrue(p, "merge");
    showRecurringTemplates = mergeSimilarSql && urlParamIsTrue(p, "templates");
    latencyFlaggedOnly = urlParamIsTrue(p, "flagged");
    latencyShowDipP = !urlParamIsFalse(p, "dip_p");
    const mt = p.get("min_tier");
    latencyMinTier =
      mt != null && LATENCY_MIN_TIERS.indexOf(mt) >= 0 ? mt : LATENCY_MIN_TIER_DEFAULT;
  }

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
    const canonicalize = p.get("canonicalize");
    ashCanonicalizeFilter =
      ashQueryIdFilter != null &&
      (canonicalize === "t" || canonicalize === "true" || canonicalize === "1");
    const dbname = p.get("dbname");
    ashCanonicalDbnameFilter =
      ashCanonicalizeFilter && dbname != null && String(dbname).trim() !== ""
        ? String(dbname).trim()
        : null;
    const n = p.get("node");
    ashNodeIdFilter = n != null && String(n).trim() !== "" ? String(n).trim() : null;
    const tb = p.get("table_id");
    ashTableIdFilter = tb != null && String(tb).trim() !== "" ? String(tb).trim() : null;
    readViewerToggleParams(p);
    // The URL is the source of truth on load and on Back/Forward.
    qpmPlanView = p.get("plan_view") === "explain" ? "explain" : "plans";
    qpmShowLiterals = urlParamIsTrue(p, "literals");
    qpmViewScope = null;
    if (activeViewerSection !== "ash") {
      ashQueryIdFilter = null;
      ashCanonicalizeFilter = false;
      ashCanonicalDbnameFilter = null;
      ashNodeIdFilter = null;
      ashTableIdFilter = null;
      canonicalFamilyResolved = false;
    }
    syncMergeSimilarSqlForFamilyScope();
  }

  /**
   * Pin Merge similar SQL only while a canonical family actually resolved in this snapshot.
   * `canonicalize=t` stays in the URL across Prev/Next so a later window can still resolve.
   * A miss (or leaving ASH) restores the held preference once; the next resolved family
   * captures Merge again.
   */
  function syncMergeSimilarSqlForFamilyScope() {
    const familyActive = !!(ashCanonicalizeFilter && canonicalFamilyResolved);
    if (familyActive) {
      if (mergeSimilarSqlSavedForFamily === null) {
        mergeSimilarSqlSavedForFamily = mergeSimilarSql;
      }
      mergeSimilarSql = true;
      return;
    }
    if (mergeSimilarSqlSavedForFamily !== null) {
      mergeSimilarSql = mergeSimilarSqlSavedForFamily;
      mergeSimilarSqlSavedForFamily = null;
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
      if (ashQueryIdFilter && ashCanonicalizeFilter) {
        p.set("canonicalize", "t");
        if (ashCanonicalDbnameFilter) p.set("dbname", ashCanonicalDbnameFilter);
      }
      if (ashNodeIdFilter) p.set("node", ashNodeIdFilter);
      if (ashTableIdFilter) p.set("table_id", ashTableIdFilter);
      if (ashQueryIdFilter && qpmPlanView === "explain") p.set("plan_view", "explain");
      if (ashQueryIdFilter && qpmShowLiterals) p.set("literals", "t");
    }
    applyViewerToggleParams(p);
    const qs = p.toString();
    const newUrl = `${window.location.pathname}${qs ? "?" + qs : ""}${window.location.hash || ""}`;
    const mergePref = mergeSimilarSqlForUrl();
    const st = {
      ybtop: true,
      view: activeViewerSection,
      t: windowKey,
      query: ashQueryIdFilter || null,
      canonicalize: ashCanonicalizeFilter || false,
      dbname: ashCanonicalDbnameFilter || null,
      node: ashNodeIdFilter || null,
      table_id: ashTableIdFilter || null,
      merge: mergePref,
      templates: !!(mergePref && showRecurringTemplates),
      flagged: latencyFlaggedOnly,
      dip_p: latencyShowDipP,
      min_tier: latencyMinTier,
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
      ashCanonicalizeFilter = false;
      ashCanonicalDbnameFilter = null;
      ashNodeIdFilter = null;
      ashTableIdFilter = null;
      canonicalFamilyResolved = false;
    }
    syncMergeSimilarSqlForFamilyScope();
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
  function statementCallsOnNodeMatching(
    perNode,
    nodeId,
    stmtRow,
    matchDbname,
    canonicalFamily
  ) {
    const wantQ = normQid(stmtRow.queryid);
    if (wantQ == null && !canonicalFamily) return 0;
    const dn =
      matchDbname && stmtRow.dbname != null && stmtRow.dbname !== undefined
        ? String(stmtRow.dbname).trim()
        : "";
    const nid = nodeId != null && nodeId !== undefined ? String(nodeId) : "";
    const rows = (perNode || {})[nid] || [];
    let sum = 0;
    rows.forEach((r) => {
      if (canonicalFamily) {
        if (!statementRowMatchesCanonicalFamily(r, canonicalFamily)) return;
      } else if (normQid(r.queryid) !== wantQ) {
        return;
      }
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
  function statementCallsContributorsPerNodeMap(
    perNode,
    stmtRow,
    prevPerNode,
    deltaMode,
    matchDbname,
    canonicalFamily
  ) {
    const keys = new Set(Object.keys(perNode || {}));
    if (deltaMode && prevPerNode) {
      Object.keys(prevPerNode).forEach((k) => keys.add(k));
    }
    const nm = new Map();
    keys.forEach((nid) => {
      const cur = statementCallsOnNodeMatching(
        perNode,
        nid,
        stmtRow,
        matchDbname,
        canonicalFamily
      );
      let metric = cur;
      if (deltaMode && prevPerNode) {
        const prev = statementCallsOnNodeMatching(
          prevPerNode,
          nid,
          stmtRow,
          matchDbname,
          canonicalFamily
        );
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
    const canonicalFamily = opts && opts.canonicalFamily;
    const hasRowsCol = matchDbname && pgStatPerNodeHasRowsColumn(perNode);

    const merged = mergeFn(perNode);
    let stmtRow = null;
    let deltaMode = false;
    if (prevDoc && prevPerNode) {
      deltaMode = true;
      const mergedPrev = mergeFn(prevPerNode);
      const currentRows = canonicalFamily ? collapseStatementsByTemplate(merged) : merged;
      const previousRows = canonicalFamily ? collapseStatementsByTemplate(mergedPrev) : mergedPrev;
      const deltaRows = deltaPgStatMergedRows(currentRows, previousRows);
      const derived = withPgStatDeltaDerivedRows(
        deltaRows,
        prevDoc.generated_at_utc,
        doc.generated_at_utc
      );
      stmtRow = canonicalFamily
        ? derived.find((row) => statementRowMatchesCanonicalFamily(row, canonicalFamily)) || null
        : pickMergedPgStatRowForQueryId(derived, qF);
    } else {
      const displayRows = withPgStatTimePercent(
        canonicalFamily ? collapseStatementsByTemplate(merged) : merged
      );
      stmtRow = canonicalFamily
        ? displayRows.find((row) => statementRowMatchesCanonicalFamily(row, canonicalFamily)) || null
        : pickMergedPgStatRowForQueryId(displayRows, qF);
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
      matchDbname,
      canonicalFamily
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
  function appendAshScopedQueryStatementLines(
    noteEl,
    doc,
    prevDoc,
    qF,
    ashPerNode,
    canonicalFamily
  ) {
    const pgPer = doc && doc.pg_stat_statements && doc.pg_stat_statements.per_node;
    const ycqlPer = doc && doc.ycql_stat_statements && doc.ycql_stat_statements.per_node;
    const prevPg =
      prevDoc && prevDoc.pg_stat_statements && prevDoc.pg_stat_statements.per_node;
    const prevYcql =
      prevDoc && prevDoc.ycql_stat_statements && prevDoc.ycql_stat_statements.per_node;

    const inPg = canonicalFamily
      ? canonicalFamily.source === "ysql"
        ? canonicalFamily
        : null
      : mergedStatementRowForQuery(pgPer, mergeStatements, qF);
    const inYcql = canonicalFamily
      ? canonicalFamily.source === "ycql"
        ? canonicalFamily
        : null
      : mergedStatementRowForQuery(ycqlPer, mergeYcqlStatements, qF);

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
          { matchDbname: true, canonicalFamily }
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
          { matchDbname: false, showIsPrepared: true, canonicalFamily }
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
  /* ------------------------------------------------------------------ *
   * QPM: Query Plan Management (yb_pg_stat_plans) panel
   * ------------------------------------------------------------------ */

  /** A plan still counts as in use if its last_used is within this of the newest. */
  const QPM_PLAN_ACTIVE_WINDOW_MS = 300000;
  /** Below both of these, "fastest" is more likely sampling noise than a real win. */
  const QPM_LOW_CONFIDENCE_CALLS = 50;
  const QPM_LOW_CONFIDENCE_SHARE = 0.05;

  /** Plan-node attributes worth printing under a node; anything else is noise here. */
  const QPM_PLAN_DETAIL_KEYS = [
    "Relation Name",
    "Index Name",
    "Index Cond",
    "Hash Cond",
    "Merge Cond",
    "Join Filter",
    "Storage Filter",
    "Filter",
    "Sort Key",
    "Cache Key",
  ];

  /**
   * Hints minus the Set(...) GUC boilerplate: a one-line shape fingerprint.
   *
   * QPM hints are ~19 clauses, of which ~18 are Set(...) planner settings identical
   * across every plan of a query. What distinguishes two plans is the join/scan
   * clauses, so those are all this keeps.
   */
  function qpmPlanShapeSignature(hints) {
    if (hints == null) return "";
    return String(hints)
      .replace(/^\s*\/\*\+/, "")
      .replace(/\*\/\s*$/, "")
      .replace(/Set\([^)]*\)/g, "")
      .replace(/\s+/g, " ")
      .trim();
  }

  // A type name as the deparser writes it after a constant: bigint, text[],
  // timestamp without time zone, character varying(20), sch.enum_t, "Mixed".
  const QPM_TYPE_NAME_SRC =
    '(?:"(?:[^"]|"")*"\\.)?(?:"(?:[^"]|"")*"|bit varying|character varying|double precision'
    + "|(?:time|timestamp)(?:\\(\\d+\\))? with(?:out)? time zone"
    + "|[A-Za-z_][A-Za-z_0-9$]*(?:\\.[A-Za-z_][A-Za-z_0-9$]*)?)(?:\\(\\d+(?:,\\d+)?\\))?(?:\\[\\])*";
  const QPM_MASK_CONST = new RegExp("'\\?'(?:::" + QPM_TYPE_NAME_SRC + ")?", "g");
  const QPM_MASK_ARRAY = /ARRAY\[\?(?:, (?:\?|\.\.\.))*\]/g;
  const QPM_MASK_CAST = new RegExp("\\(\\?\\)::" + QPM_TYPE_NAME_SRC, "g");

  /**
   * Plan text with every parameter and constant reduced to "?".
   *
   * QPM records a prepared statement's plan twice, under two planids: its first
   * executions are planned with their values (a custom plan, where QPM writes
   * '?'::bigint) and later ones generically ($1). An IN list is ARRAY[$1, $2]
   * in the generic plan and one '?'::text[] constant in the custom one.
   */
  function qpmMaskPlanParams(text) {
    return String(text == null ? "" : text)
      .replace(QPM_MASK_CONST, "?")
      .replace(/\$\d+/g, "?")
      .replace(QPM_MASK_ARRAY, "?")
      .replace(QPM_MASK_CAST, "?");
  }

  /**
   * What makes two recorded plans one plan, for the panel and for pinning: the same
   * hints, and the same text once parameters are masked. Null without the text.
   */
  function qpmPlanTwinKey(entry) {
    if (!entry || entry.plan == null) return null;
    return (entry.hints == null ? "" : String(entry.hints)) + "\u0000" + qpmMaskPlanParams(entry.plan);
  }

  /** qpmPlanTwinKey by plan_ref, each text masked once (they run to KBs). */
  function qpmTwinKeysFor(texts) {
    const memo = new Map();
    return (ref) => {
      const k = ref == null ? "" : String(ref);
      if (!memo.has(k)) memo.set(k, k === "" ? null : qpmPlanTwinKey((texts || {})[k]));
      return memo.get(k);
    };
  }

  /**
   * Why one card stands for several planids, naming each. The generic plan is the
   * one whose text keeps more $N parameters (a custom plan has '?' in their place;
   * batched nested loops add $N of their own to both).
   */
  function qpmVariantsNote(variants, texts) {
    const params = variants.map(
      (v) => (String(((texts || {})[v.plan_ref] || {}).plan || "").match(/\$\d+/g) || []).length
    );
    const most = Math.max.apply(null, params);
    const least = Math.min.apply(null, params);
    const listed = variants.map((v, i) => {
      const form = most > least ? (params[i] === most ? "generic plan, " : "custom plan, ") : "";
      return v.planid + " (" + form + qpmCallsText(v.calls) + ", " + qpmFmtMs(v.avg_exec_time) + ")";
    });
    const refs = new Set(variants.map((v) => v.plan_ref));
    let text = "One plan, recorded under " + variants.length + " planids: " + listed.join(" · ") + ".";
    if (refs.size > 1) {
      text +=
        most > least
          ? " They differ only in how the parameters appear: $1 in the generic plan, '?' in a custom"
            + " plan, which PostgreSQL makes with the values for a prepared statement's first executions."
          : " Their texts differ only in how parameter values are written.";
    }
    if (refs.size < variants.length) {
      text +=
        " Where the text is identical, the difference lives in cost and row estimates, which this"
        + " text omits — set yb_pg_stat_plans_verbose_plans = on to see it.";
    }
    return text + " The hints are the same, so one pin covers them all.";
  }

  /** Plans are keyed by queryid; a canonical family spans several, so union them. */
  function qpmScopeQueryIds(queryId, canonicalFamily) {
    const out = new Set();
    if (canonicalFamily && canonicalFamily.queryIds && canonicalFamily.queryIds.forEach) {
      canonicalFamily.queryIds.forEach((q) => out.add(String(q)));
    }
    if (queryId != null && String(queryId) !== "") out.add(String(queryId));
    return out;
  }

  /**
   * dbids carrying the drilldown's database name, from the snapshot's oid->name
   * map. Returns a Set (possibly empty) to filter by, or null for no filter when
   * the snapshot predates the map or the drilldown names no database.
   *
   * Without this the panel merged every database's plans for a queryid --
   * including databases dropped long ago, which QPM keeps -- so "fastest" and
   * "in use" could be judged against a database the query no longer runs in.
   */
  function qpmDbidsForName(qpmSection, dbname) {
    const map = qpmSection && qpmSection.databases;
    if (!map || dbname == null || String(dbname) === "") return null;
    const out = new Set();
    Object.keys(map).forEach((oid) => {
      if (String(map[oid]) === String(dbname)) out.add(String(oid));
    });
    return out;
  }

  /**
   * Databases the plan panel and a replay scope to. The drilldown's own, when it
   * names one; otherwise the live ones, if the statement has plans in any. The
   * busiest pg_stat_statements row can belong to a dropped database (pgss and QPM
   * both keep them), and mixing its plans in made "fastest plan no longer in use"
   * a dropped database's plan. No filter when only dropped databases have plans.
   */
  function qpmPanelDbids(qpmSection, dbname, queryIds) {
    const named = qpmDbidsForName(qpmSection, dbname);
    if (named) return named;
    const map = qpmSection && qpmSection.databases;
    if (!map) return null;
    const live = new Set(Object.keys(map));
    const perNode = (qpmSection && qpmSection.per_node) || {};
    const inScope = (r) => r && (!queryIds || !queryIds.size || queryIds.has(String(r.queryid)));
    const anyLive = Object.keys(perNode).some((nid) =>
      (perNode[nid] || []).some((r) => inScope(r) && live.has(String(r.dbid)))
    );
    return anyLive ? live : null;
  }

  /** The queryids a statement row stands for: itself, or every member of a template row. */
  function qpmRowQueryIds(r, templateMembers) {
    if (!r) return [];
    if (Array.isArray(r._tmpl_queryids) && r._tmpl_queryids.length) return r._tmpl_queryids.map(String);
    if (Array.isArray(r.query_members) && r.query_members.length) {
      return r.query_members.map((m) => String(m && m.query_id));
    }
    // A template row that a delta pass rebuilt without its members: find them by its
    // template key (see qpmTemplateMembers).
    const members = templateMembers ? templateMembers.get(statementMergeKey(r)) : null;
    if (members && members.length) return members.map(String);
    return r.queryid != null ? [String(r.queryid)] : [];
  }

  /**
   * Template key -> member queryids, from the snapshot's own statements. The plans
   * column counts a merged row's plans across its members, and must not depend on
   * how a delta pass rebuilds grouped rows: one that subtracts collapsed rows drops
   * their member list, one that collapses per-statement deltas keeps it.
   */
  function qpmTemplateMembers(mergedRows) {
    const out = new Map();
    collapseStatementsByTemplate(mergedRows).forEach((r) => out.set(statementMergeKey(r), r._tmpl_queryids || []));
    return out;
  }

  /**
   * "queryid|dbid" -> Set of plan keys, plus "queryid|*" across all databases.
   * Same plan identity the drilldown groups by (qpmPlanTwinKey, else planid +
   * plan_ref), so the PGSS count matches the number of plan cards the drilldown
   * shows for that statement.
   */
  function qpmPlanIndex(qpmSection) {
    const idx = new Map();
    const add = (k, v) => {
      let set = idx.get(k);
      if (!set) {
        set = new Set();
        idx.set(k, set);
      }
      set.add(v);
    };
    const perNode = (qpmSection && qpmSection.per_node) || {};
    const twinKey = qpmTwinKeysFor(qpmSection && qpmSection.plans);
    Object.keys(perNode).forEach((nid) => {
      (perNode[nid] || []).forEach((r) => {
        if (!r || r.queryid == null) return;
        const twin = twinKey(r.plan_ref);
        const plan =
          twin != null
            ? "t" + twin
            : "p" + String(r.planid == null ? "" : r.planid) + "|" + String(r.plan_ref == null ? "" : r.plan_ref);
        add(String(r.queryid) + "|" + String(r.dbid == null ? "" : r.dbid), plan);
        add(String(r.queryid) + "|*", plan);
      });
    });
    return idx;
  }

  /**
   * Pins and unpins made from this page: "dbid|queryid" -> { pinned, at }, with
   * `at` on the server's clock. The collector only learns about a pin at its next
   * checkpoint and the viewer keeps showing the snapshot you were reading, so
   * without this a fresh pin did not show as P until a later snapshot landed and
   * you moved to it.
   */
  const qpmPinEdits = new Map();

  function qpmRecordPinEdit(dbid, queryid, pinned, serverMs) {
    // The Date header has 1 s resolution; count the edit from the end of its
    // second so a snapshot stamped within it is never taken to already include it.
    const at = isFinite(serverMs) ? serverMs + 1000 : Date.now();
    qpmPinEdits.set(String(dbid) + "|" + String(queryid), { pinned: !!pinned, at });
  }

  /**
   * dbid -> Set(queryid): the snapshot's pinned map with this page's edits applied
   * over it -- but only edits newer than the snapshot. A snapshot taken after an
   * edit read the hint table itself, so it is already right.
   */
  function qpmEffectivePinned(qpmSection, snapshotUtcMs, edits) {
    const out = new Map();
    const raw = (qpmSection && qpmSection.pinned) || {};
    Object.keys(raw).forEach((d) => out.set(String(d), new Set((raw[d] || []).map(String))));
    (edits || qpmPinEdits).forEach((e, key) => {
      if (isFinite(snapshotUtcMs) && snapshotUtcMs >= e.at) return;
      const cut = key.indexOf("|");
      const d = key.slice(0, cut);
      const q = key.slice(cut + 1);
      if (!out.has(d)) out.set(d, new Set());
      if (e.pinned) out.get(d).add(q);
      else out.get(d).delete(q);
    });
    return out;
  }

  /**
   * Adds qpm_plans (distinct plans) and qpm_pinned to statement rows for the PGSS
   * "plans" column. Scoped exactly like the drilldown: the row's own database when
   * the snapshot can map its name, otherwise every database, and all queryids a
   * template row stands for.
   */
  function annotateRowsWithQpmPlans(rows, qpmSection, pinnedByDbOverride, templateMembers) {
    const idx = qpmPlanIndex(qpmSection);
    const pinnedByDb = pinnedByDbOverride || qpmEffectivePinned(qpmSection, NaN, new Map());
    return (rows || []).map((r) => {
      const dbids = qpmDbidsForName(qpmSection, r && r.dbname);
      const plans = new Set();
      let pinned = false;
      qpmRowQueryIds(r, templateMembers).forEach((q) => {
        const keys = dbids ? Array.from(dbids, (d) => q + "|" + d) : [q + "|*"];
        keys.forEach((k) => {
          const set = idx.get(k);
          if (set) set.forEach((pk) => plans.add(pk));
        });
        pinnedByDb.forEach((qs, d) => {
          if ((!dbids || dbids.has(d)) && qs.has(q)) pinned = true;
        });
      });
      return Object.assign({}, r, { qpm_plans: plans.size, qpm_pinned: pinned });
    });
  }

  const QPM_PLANS_COL = {
    key: "qpm_plans",
    label: "plans",
    type: "number",
    align: "right",
    title:
      "Distinct plans Query Plan Management recorded for this statement in its database. "
      + "P = a hint is pinned for it.",
    // At equal counts a pinned statement sorts above an unpinned one.
    sortValue: (r) => (Number(r && r.qpm_plans) || 0) + (r && r.qpm_pinned ? 0.5 : 0),
  };

  /** Statement columns with "plans" placed just before dbname (or last, if absent). */
  function withQpmPlansColumn(cols) {
    const out = (cols || []).slice();
    const at = out.findIndex((c) => c && c.key === "dbname");
    out.splice(at >= 0 ? at : out.length, 0, QPM_PLANS_COL);
    return out;
  }

  /** "2" or "2 P", deliberately quiet: one plan is muted, blank when QPM saw none. */
  function appendQpmPlansCell(td, row) {
    td.classList.add("qpm-plans-cell");
    const n = Number(row && row.qpm_plans) || 0;
    if (n > 0) {
      td.appendChild(
        el("span", {
          className: n > 1 ? "qpm-plans-n qpm-plans-n--multi" : "qpm-plans-n",
          textContent: String(n),
        })
      );
    }
    if (row && row.qpm_pinned) {
      td.appendChild(
        el("span", { className: "qpm-plans-pin", textContent: "P", title: "A hint is pinned for this statement" })
      );
    }
  }

  /** Human label for a plan's database; a dbid missing from the map was dropped. */
  function qpmDatabaseLabel(qpmSection, dbid) {
    if (dbid == null || String(dbid) === "") return null;
    const map = qpmSection && qpmSection.databases;
    if (!map) return "oid " + dbid;
    return map[String(dbid)] != null ? String(map[String(dbid)]) : "oid " + dbid + " (dropped)";
  }

  /**
   * Fold per-node QPM rows into one group per distinct plan, fastest first.
   *
   * One group per plan as a user would count them (qpmPlanTwinKey): same hints and
   * same text but for how parameters appear. QPM gives a prepared statement's
   * custom and generic plans separate planids, and on a JDBC workload that made
   * two cards -- and a "fastest plan" verdict -- out of nearly every statement.
   * The planids behind a group are its `variants`, most calls first; the first
   * stands for the group (planid, plan_ref: its text is shown and a pin names it).
   * Without the text, planid + plan_ref: planid ignores FROM-list and AND-clause
   * ordering, so one planid can carry two different rendered texts.
   *
   * avg_exec_time and avg_est_cost arrive as per-node averages and are recombined
   * call-weighted. A plain mean of means misreports any plan whose calls are
   * lopsided across nodes -- which is the normal case, since QPM is per-node.
   */
  function aggregateQpmPlans(qpmSection, queryIds, dbids) {
    const perNode = (qpmSection && qpmSection.per_node) || {};
    const twinKey = qpmTwinKeysFor(qpmSection && qpmSection.plans);
    const byKey = new Map();
    Object.keys(perNode).forEach((nid) => {
      (perNode[nid] || []).forEach((r) => {
        if (!r || r.queryid == null) return;
        if (queryIds && queryIds.size && !queryIds.has(String(r.queryid))) return;
        // A Set -- even an empty one -- is a strict database filter; null means none.
        if (dbids && !dbids.has(String(r.dbid))) return;
        const dbid = r.dbid == null ? "" : String(r.dbid);
        const planid = String(r.planid == null ? "" : r.planid);
        const ref = r.plan_ref == null ? "" : String(r.plan_ref);
        const twin = twinKey(ref);
        // Database is part of a plan's identity: stats, hint table and pin target
        // are all per database, and the same queryid often runs in several.
        const key = (twin != null ? "t" + twin : "p" + planid + "|" + ref) + "|" + dbid;
        let g = byKey.get(key);
        if (!g) {
          g = {
            planid,
            plan_ref: ref,
            dbid: dbid,
            queryIds: new Set(),
            nodes: [],
            calls: 0,
            _wsumAvg: 0,
            _wsumCost: 0,
            _weight: 0,
            _variants: new Map(),
            max_exec_time: null,
            first_used: null,
            last_used: null,
            max_exec_time_params: null,
          };
          byKey.set(key, g);
        }
        const calls = Number(r.calls) || 0;
        const w = calls > 0 ? calls : 1;
        g.calls += calls;
        g._weight += w;
        g.queryIds.add(String(r.queryid));
        if (g.nodes.indexOf(nid) === -1) g.nodes.push(nid);
        const avg = Number(r.avg_exec_time);
        if (isFinite(avg)) g._wsumAvg += avg * w;
        const vk = planid + "|" + ref;
        let v = g._variants.get(vk);
        if (!v) {
          v = { planid, plan_ref: ref, calls: 0, _wsumAvg: 0, _weight: 0 };
          g._variants.set(vk, v);
        }
        v.calls += calls;
        v._weight += w;
        if (isFinite(avg)) v._wsumAvg += avg * w;
        const cost = Number(r.avg_est_cost);
        if (isFinite(cost)) g._wsumCost += cost * w;
        const mx = Number(r.max_exec_time);
        // The parameters travel with the max: QPM records them per node, for that
        // node's slowest execution, so "slowest params" is the slowest node's.
        if (isFinite(mx) && (g.max_exec_time == null || mx > g.max_exec_time)) {
          g.max_exec_time = mx;
          g.max_exec_time_params = r.max_exec_time_params ? String(r.max_exec_time_params) : null;
        }
        const fu = r.first_used ? Date.parse(r.first_used) : NaN;
        if (isFinite(fu) && (g.first_used == null || fu < g.first_used)) g.first_used = fu;
        const lu = r.last_used ? Date.parse(r.last_used) : NaN;
        if (isFinite(lu) && (g.last_used == null || lu > g.last_used)) g.last_used = lu;
      });
    });
    const groups = Array.from(byKey.values()).map((g) => {
      const w = g._weight > 0 ? g._weight : 1;
      g.avg_exec_time = g._wsumAvg / w;
      g.avg_est_cost = g._wsumCost / w;
      g.variants = Array.from(g._variants.values())
        .map((v) => ({
          planid: v.planid,
          plan_ref: v.plan_ref,
          calls: v.calls,
          avg_exec_time: v._wsumAvg / (v._weight > 0 ? v._weight : 1),
        }))
        .sort((a, b) => b.calls - a.calls || (a.planid < b.planid ? -1 : a.planid > b.planid ? 1 : 0));
      g.planid = g.variants[0].planid;
      g.plan_ref = g.variants[0].plan_ref;
      delete g._wsumAvg;
      delete g._wsumCost;
      delete g._weight;
      delete g._variants;
      return g;
    });
    // Hints fix scans, joins and join order -- not aggregation, sorting or where a
    // filter runs -- so two different plans can share them, and a pin allows both.
    const texts = (qpmSection && qpmSection.plans) || {};
    const hintsKey = (g) => {
      const h = (texts[g.plan_ref] || {}).hints;
      return h == null ? null : g.dbid + "\u0000" + h;
    };
    const byHints = new Map();
    groups.forEach((g) => {
      const k = hintsKey(g);
      if (k == null) return;
      if (!byHints.has(k)) byHints.set(k, []);
      byHints.get(k).push(g);
    });
    groups.forEach((g) => {
      const k = hintsKey(g);
      g.sameHintsAs = k == null ? [] : byHints.get(k).filter((o) => o !== g).map((o) => o.planid);
    });
    groups.sort((a, b) => a.avg_exec_time - b.avg_exec_time || b.calls - a.calls);
    return groups;
  }

  /**
   * Whether the fastest plan is the one actually serving traffic, plus the numbers
   * the panel headline quotes. Mutates each group with `active`.
   *
   * "active" is judged against the newest last_used among this query's own plans,
   * never against wall clock: a query that simply stopped running must not be
   * reported as a plan regression.
   *
   *   single     -- only one plan on record, nothing to compare
   *   ok         -- fastest plan is also the one taking the most calls
   *   underused  -- fastest plan is live but a minority of calls run it
   *   abandoned  -- fastest plan is no longer being used at all
   */
  function qpmPlanVerdict(groups) {
    if (!groups || groups.length === 0) return null;
    let newest = null;
    groups.forEach((g) => {
      if (g.last_used != null && (newest == null || g.last_used > newest)) newest = g.last_used;
    });
    groups.forEach((g) => {
      g.active =
        newest == null || g.last_used == null
          ? false
          : newest - g.last_used <= QPM_PLAN_ACTIVE_WINDOW_MS;
    });
    // planid can differ while the rendered plan is byte-identical: with
    // yb_pg_stat_plans_verbose_plans off the text carries no costs or row
    // estimates, and that is exactly where two such plans differ. With the text
    // at hand they are one group (see aggregateQpmPlans); without it, flag them,
    // or the panel shows two indistinguishable rows and reads as a bug.
    const byRef = new Map();
    groups.forEach((g) => {
      if (!g.plan_ref) return;
      const k = g.plan_ref + "|" + g.dbid;
      if (!byRef.has(k)) byRef.set(k, []);
      byRef.get(k).push(g);
    });
    groups.forEach((g) => {
      g.sameTextAs = (byRef.get(g.plan_ref + "|" + g.dbid) || []).filter((o) => o !== g).map((o) => o.planid);
    });
    const totalCalls = groups.reduce((s, g) => s + (g.calls || 0), 0);
    const fastest = groups[0];
    const activeGroups = groups.filter((g) => g.active);
    const pool = activeGroups.length ? activeGroups : groups;
    let current = null;
    pool.forEach((g) => {
      if (current == null || (g.calls || 0) > (current.calls || 0)) current = g;
    });
    const fastestShare = totalCalls > 0 ? (fastest.calls || 0) / totalCalls : 0;
    const slowerRatio =
      current && fastest.avg_exec_time > 0 ? current.avg_exec_time / fastest.avg_exec_time : 1;
    let status;
    if (groups.length === 1) status = "single";
    else if (!fastest.active) status = "abandoned";
    else if (current === fastest) status = "ok";
    else status = "underused";
    return {
      status,
      fastest,
      current,
      totalCalls,
      fastestShare,
      slowerRatio,
      planCount: groups.length,
      activeCount: activeGroups.length,
      newestLastUsed: newest,
      // "Fastest" off a handful of calls is not a finding; say so rather than imply one.
      lowConfidence:
        (fastest.calls || 0) < QPM_LOW_CONFIDENCE_CALLS && fastestShare < QPM_LOW_CONFIDENCE_SHARE,
    };
  }

  /** One-line summary for the panel header. */
  function qpmVerdictHeadline(v) {
    if (!v) return "";
    const n = v.planCount;
    const plural = n === 1 ? "1 plan" : n + " plans";
    if (v.status === "single") return plural + " · no plan change recorded";
    const ratio = v.slowerRatio >= 1.005 ? v.slowerRatio.toFixed(2) + "×" : null;
    // A "fastest" measured on a handful of calls is what the ratio is against; say so here,
    // not only inside the card.
    const fastest =
      "fastest plan"
      + (v.lowConfidence ? " (" + qpmCallsText(v.fastest.calls || 0) + ", provisional)" : "");
    if (v.status === "abandoned") {
      return (
        plural +
        " · " + fastest + " no longer in use" +
        (ratio ? " · current plan " + ratio + " slower" : "")
      );
    }
    if (v.status === "underused") {
      return (
        plural +
        " · " + fastest + " serves " +
        Math.round(v.fastestShare * 100) +
        "% of calls" +
        (ratio ? " · current plan " + ratio + " slower" : "")
      );
    }
    return plural + " · " + v.activeCount + " active · fastest plan is the one in use";
  }

  function qpmCallsText(n) {
    return Number(n).toLocaleString() + (Number(n) === 1 ? " call" : " calls");
  }

  function qpmFmtMs(v) {
    if (v == null || !isFinite(v)) return "—";
    if (v >= 60000) return (v / 1000).toFixed(0) + " s";
    if (v >= 1000) return (v / 1000).toFixed(2) + " s";
    if (v >= 100) return v.toFixed(0) + " ms";
    if (v >= 10) return v.toFixed(2) + " ms";
    return v.toFixed(3) + " ms";
  }

  /** "2026-09-17 11:32" -- minute precision; seconds only cost column width here. */
  function qpmFmtStamp(ms) {
    return new Date(ms).toISOString().replace("T", " ").slice(0, 16);
  }

  /** Relative gap, rendered as a bare duration: call sites supply the "before X" framing. */
  function qpmFmtAge(ms) {
    if (ms == null || !isFinite(ms)) return "—";
    const s = Math.max(0, Math.round(ms / 1000));
    if (s < 90) return s + "s";
    const m = Math.round(s / 60);
    if (m < 90) return m + "m";
    const h = Math.round(m / 60);
    if (h < 48) return h + "h";
    return Math.round(h / 24) + "d";
  }

  /** EXPLAIN-style indented tree from a QPM json plan payload. */
  function qpmRenderPlanTree(planText) {
    const pre = el("pre", { className: "qpm-tree" });
    let root = null;
    try {
      root = JSON.parse(String(planText));
    } catch (e) {
      root = null;
    }
    if (root == null) {
      pre.textContent = planText == null || String(planText) === "" ? "(no plan text)" : String(planText);
      return pre;
    }
    const lines = [];
    function walk(node, depth) {
      if (!node || typeof node !== "object") return;
      const pad = "  ".repeat(depth);
      const type = node["Node Type"] == null ? "(node)" : String(node["Node Type"]);
      let head = pad + (depth > 0 ? "-> " : "") + type;
      if (node["Join Type"] && /Join|Nested Loop/.test(type)) head += " (" + node["Join Type"] + ")";
      lines.push(head);
      QPM_PLAN_DETAIL_KEYS.forEach((k) => {
        if (node[k] == null) return;
        const v = Array.isArray(node[k]) ? node[k].join(", ") : String(node[k]);
        lines.push(pad + "     " + k + ": " + v);
      });
      (node.Plans || []).forEach((c) => walk(c, depth + 1));
    }
    (Array.isArray(root) ? root : [root]).forEach((e) => walk(e && e.Plan ? e.Plan : e, 0));
    pre.textContent = lines.join("\n");
    return pre;
  }

  /**
   * True when max_exec_time_params carries at least one real value.
   *
   * With yb_pg_stat_plans_show_max_exec_params off (the default) every parameter
   * renders as '?', so the column exists but says nothing; showing it only adds a
   * row that looks like data.
   */
  function qpmParamsHaveValues(params) {
    if (params == null || String(params) === "") return false;
    return (
      String(params)
        .replace(/\$\d+\s*=\s*'\?'/g, "")
        .replace(/[\s,]+/g, "").length > 0
    );
  }

  function qpmBadge(text, kind) {
    return el("span", { className: "qpm-badge qpm-badge--" + kind, textContent: text });
  }

  function qpmMetric(parent, key, value, extraClass) {
    const row = el("div", { className: "qpm-metric" + (extraClass ? " " + extraClass : "") });
    row.appendChild(el("span", { className: "qpm-metric-k", textContent: key }));
    row.appendChild(el("span", { className: "qpm-metric-v", textContent: value }));
    parent.appendChild(row);
  }

  /** One plan: a clickable summary line plus a body that expands. */
  function qpmPlanCard(group, verdict, texts, index, clusterNodeCount, snapshotFile, queryId) {
    const isFastest = group === verdict.fastest;
    const isCurrent = verdict.current === group;
    const openByDefault = isFastest;
    const card = el("div", {
      className: "qpm-card" + (isFastest ? " qpm-card--best" : ""),
    });

    const head = el("div", { className: "qpm-card-head", role: "button", tabindex: "0" });
    const caret = el("span", { className: "qpm-caret", textContent: openByDefault ? "▾" : "▸" });
    head.appendChild(caret);
    if (isFastest) head.appendChild(qpmBadge("FASTEST", "best"));
    const share = verdict.totalCalls > 0 ? Math.round((group.calls / verdict.totalCalls) * 100) : 0;
    if (isCurrent) head.appendChild(qpmBadge("in use", "live"));
    else if (!group.active) head.appendChild(qpmBadge("not in use", "stale"));
    else head.appendChild(qpmBadge("minority use", "minor"));
    head.appendChild(el("span", { className: "qpm-card-ms", textContent: qpmFmtMs(group.avg_exec_time) }));
    head.appendChild(
      el("span", {
        className: "qpm-card-calls",
        textContent: qpmCallsText(group.calls) + " · " + share + "%",
      })
    );
    const sig = qpmPlanShapeSignature((texts[group.plan_ref] || {}).hints);
    head.appendChild(
      el("span", {
        className: "qpm-card-shape",
        textContent:
          (group.sameTextAs && group.sameTextAs.length
            ? "= same plan text · "
            : group.sameHintsAs && group.sameHintsAs.length
              ? "= same hints · "
              : "") + (sig || "planid " + group.planid),
        title: sig,
      })
    );
    card.appendChild(head);

    const body = el("div", { className: "qpm-card-body" });
    body.hidden = !openByDefault;

    if (isFastest && verdict.lowConfidence) {
      body.appendChild(
        el("div", {
          className: "qpm-note qpm-note--warn",
          textContent:
            "Fastest by average over only " +
            qpmCallsText(group.calls) +
            " (" +
            share +
            "% of this query) — treat as provisional.",
        })
      );
    }

    const metrics = el("div", { className: "qpm-metrics" });
    qpmMetric(metrics, "avg", qpmFmtMs(group.avg_exec_time));
    qpmMetric(metrics, "max", qpmFmtMs(group.max_exec_time));
    qpmMetric(metrics, "calls", group.calls.toLocaleString());
    qpmMetric(metrics, "est cost", isFinite(group.avg_est_cost) ? group.avg_est_cost.toFixed(2) : "—");
    qpmMetric(
      metrics,
      "nodes",
      group.nodes.length + (clusterNodeCount ? " of " + clusterNodeCount : "")
    );
    const variants = group.variants || [];
    qpmMetric(
      metrics,
      variants.length > 1 ? "planids" : "planid",
      group.planid + (variants.length > 1 ? " + " + (variants.length - 1) + " more" : "")
    );
    if (group.dbLabel) qpmMetric(metrics, "database", group.dbLabel);
    if (group.first_used != null) {
      qpmMetric(metrics, "first used", qpmFmtStamp(group.first_used));
    }
    if (group.last_used != null) {
      const rel = verdict.newestLastUsed != null ? verdict.newestLastUsed - group.last_used : null;
      qpmMetric(
        metrics,
        "last used",
        qpmFmtStamp(group.last_used) +
          (rel != null && rel > QPM_PLAN_ACTIVE_WINDOW_MS ? "  (" + qpmFmtAge(rel) + " before newest)" : "")
      );
    }
    if (qpmParamsHaveValues(group.max_exec_time_params)) {
      qpmMetric(metrics, "slowest params", group.max_exec_time_params, "qpm-metric--full");
    }
    body.appendChild(metrics);

    if (variants.length > 1) {
      body.appendChild(el("div", { className: "qpm-note", textContent: qpmVariantsNote(variants, texts) }));
    }
    if (group.sameHintsAs && group.sameHintsAs.length > 0) {
      body.appendChild(
        el("div", {
          className: "qpm-note",
          textContent:
            "Same hints as planid " + group.sameHintsAs.join(", ") + ", with a different plan: hints"
            + " fix scans, joins and join order, not aggregation, sorting or where a filter runs."
            + " Pinning either one allows both.",
        })
      );
    }
    if (group.sameTextAs && group.sameTextAs.length > 0) {
      body.appendChild(
        el("div", {
          className: "qpm-note",
          textContent:
            "Identical plan text and hints to planid " +
            group.sameTextAs.join(", ") +
            ", yet QPM recorded a separate planid. The difference lives in cost and " +
            "row estimates, which this text omits — set " +
            "yb_pg_stat_plans_verbose_plans = on to see it.",
        })
      );
    }

    // Plans exist per node; say so when this one is not on all of them.
    if (clusterNodeCount && group.nodes.length < clusterNodeCount) {
      body.appendChild(
        el("div", {
          className: "qpm-note",
          textContent:
            "Recorded on " +
            group.nodes.length +
            " of " +
            clusterNodeCount +
            " nodes: " +
            group.nodes.join(", "),
        })
      );
    }

    const entry = texts[group.plan_ref] || {};
    body.appendChild(el("div", { className: "qpm-sub-k", textContent: "PLAN" }));
    body.appendChild(qpmRenderPlanTree(entry.plan));

    if (entry.hints) {
      const hintWrap = el("details", { className: "qpm-hints" });
      hintWrap.appendChild(el("summary", { textContent: "hints (pin this plan)" }));
      hintWrap.appendChild(el("pre", { className: "qpm-tree", textContent: String(entry.hints) }));
      // A hint is keyed by queryid, so a canonical family spanning several has no
      // single pin target; offer pinning only when the plan maps to exactly one.
      const pinTargets = Array.from(group.queryIds || []);
      if (group.dbDropped) {
        hintWrap.appendChild(
          el("div", {
            className: "qpm-note",
            textContent:
              "Recorded in a database that has since been dropped (" + group.dbLabel
              + "), so there is nothing to pin into.",
          })
        );
      } else if (pinTargets.length === 1) {
        hintWrap.appendChild(qpmPinControls(group, pinTargets[0], snapshotFile));
      } else if (pinTargets.length > 1) {
        hintWrap.appendChild(
          el("div", {
            className: "qpm-note",
            textContent:
              "This plan is recorded under " + pinTargets.length + " query_ids, and a hint "
              + "is pinned per query_id -- open a single query_id to pin it.",
          })
        );
      }
      body.appendChild(hintWrap);
    }

    card.appendChild(body);

    function toggle() {
      body.hidden = !body.hidden;
      caret.textContent = body.hidden ? "▸" : "▾";
      head.setAttribute("aria-expanded", body.hidden ? "false" : "true");
    }
    head.setAttribute("aria-expanded", openByDefault ? "true" : "false");
    head.addEventListener("click", toggle);
    head.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        toggle();
      }
    });
    return card;
  }

  /* ---- collection toggle: see ybtop/control.py ---- */

  const QPM_ENABLE_POLL_MS = 5000;
  /* ~3 checkpoints at the default 60 s interval before we stop waiting. */
  const QPM_ENABLE_GIVE_UP_MS = 180000;

  /** Toggle state for this data directory, or null when the endpoint is unavailable. */
  let qpmCollectionState = null;

  async function qpmFetchCollectionState() {
    try {
      const res = await fetch("api/control", { cache: "no-store" });
      if (!res.ok) return null;
      return await res.json();
    } catch (e) {
      return null;
    }
  }

  async function qpmSetCollection(on) {
    const res = await fetch("api/control", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query_plans: !!on }),
    });
    let body = {};
    try {
      body = await res.json();
    } catch (e) {
      body = {};
    }
    if (!res.ok) throw new Error(body.error || "HTTP " + res.status);
    qpmCollectionState = body;
    return body;
  }

  /**
   * What to show when the snapshot on screen carries no plans. Pure, so the
   * state model is testable without a DOM.
   *
   * The snapshot alone cannot answer this: it only records whether plans were
   * collected when IT was taken. After someone enables collection, every older
   * snapshot still says "not collected", and the viewer stays pinned to the one
   * you were reading -- so deciding from the snapshot alone offers "Enable" again
   * on every navigation. The live toggle is the other half of the answer.
   *
   *   "unavailable" -- no control endpoint (files served without watch)
   *   "readonly"    -- toggle off and this data directory cannot be written
   *   "off"         -- toggle off: offer Enable
   *   "enabling"    -- toggle on, on the newest snapshot, first plans not in yet
   *   "predates"    -- toggle on, but this is an older snapshot from before it
   */
  function qpmCollectionOffState(state, viewingNewest) {
    if (state == null) return "unavailable";
    if (!state.query_plans) return state.writable === false ? "readonly" : "off";
    return viewingNewest ? "enabling" : "predates";
  }

  /** Header label for a panel that has plans: reflects the live toggle, not the click. */
  function qpmCollectionHeaderLabel(state) {
    return state && state.query_plans ? "disable collection" : "collection off \u00b7 enable";
  }

  function qpmViewingNewestSnapshot() {
    return currentIndex >= 0 && currentIndex >= manifestEntries.length - 1;
  }

  /** Move to the newest snapshot -- what Last does -- after refreshing the manifest. */
  async function qpmShowLatestSnapshot() {
    try {
      const fresh = await loadManifest();
      if (Array.isArray(fresh) && fresh.length) manifestEntries = fresh;
    } catch (e) {
      /* use the manifest we already have */
    }
    if (manifestEntries.length) showSnapshotAt(manifestEntries.length - 1);
  }

  /**
   * One wait-for-the-first-collected-snapshot for the whole page.
   *
   * Panels are rebuilt on every navigation, so a timer per panel multiplied and
   * each one eventually reloaded the page, long after the user had moved on. Now
   * an "enabling" panel just attaches here, and when the snapshot lands the page
   * advances to it only if a waiting panel is still on screen.
   *
   * Advancing (not reloading) matters: once newer snapshots exist the URL pins
   * the old window with t=, so a reload would land on the pre-enable snapshot.
   */
  let qpmEnableWait = null;

  function qpmWaitForCollectedSnapshot() {
    if (qpmEnableWait) return qpmEnableWait;
    const wait = { startCount: manifestEntries.length, t0: Date.now(), gaveUp: false };
    qpmEnableWait = wait;
    const tick = async () => {
      let fresh = null;
      try {
        fresh = await loadManifest();
      } catch (e) {
        fresh = null;
      }
      if (Array.isArray(fresh) && fresh.length > wait.startCount) {
        qpmEnableWait = null;
        manifestEntries = fresh;
        if (document.querySelector(".qpm-panel--enabling")) {
          showSnapshotAt(manifestEntries.length - 1);
        }
        return;
      }
      if (Date.now() - wait.t0 > QPM_ENABLE_GIVE_UP_MS) {
        wait.gaveUp = true;
        document.querySelectorAll(".qpm-panel--enabling").forEach(qpmMarkEnablingStalled);
        return;
      }
      setTimeout(tick, QPM_ENABLE_POLL_MS);
    };
    setTimeout(tick, QPM_ENABLE_POLL_MS);
    return wait;
  }

  function qpmMarkEnablingStalled(section) {
    const note = section.querySelector(".qpm-enable-row .qpm-note");
    if (note) {
      note.className = "qpm-note qpm-note--warn";
      note.textContent =
        "Collection is on, but no new snapshot has arrived. Is `ybtop watch` running "
        + "against this directory?";
    }
  }

  /**
   * Stands in for the plan panel when this snapshot has no plans, in whichever of
   * the qpmCollectionOffState states applies. Rebuilt from state on every render,
   * so it shows the same thing however the user got here.
   */
  function qpmCollectionOffPanel(state) {
    const mode = qpmCollectionOffState(state, qpmViewingNewestSnapshot());
    const section = el("section", {
      className:
        "ybtop-section qpm-panel qpm-panel--off" + (mode === "enabling" ? " qpm-panel--enabling" : ""),
    });
    const head = el("div", { className: "qpm-panel-head" });
    head.appendChild(el("span", { className: "qpm-panel-title", textContent: "QUERY PLANS" }));
    const verdict = el("span", { className: "qpm-panel-verdict" });
    head.appendChild(verdict);
    section.appendChild(head);
    const body = el("div", { className: "qpm-panel-body" });
    const row = el("div", { className: "qpm-enable-row" });
    const note = el("span", { className: "qpm-note" });
    body.appendChild(row);
    section.appendChild(body);

    if (mode === "enabling") {
      verdict.className = "qpm-panel-verdict qpm-panel-verdict--enabling";
      verdict.textContent = "enabling \u2014 plans start with the next checkpoint";
      row.appendChild(el("span", { className: "qpm-spinner", "aria-hidden": "true" }));
      note.textContent =
        "Collection is on. The collector writes plans on its next checkpoint, and this "
        + "view moves to that snapshot when it lands.";
      row.appendChild(note);
      if (qpmWaitForCollectedSnapshot().gaveUp) qpmMarkEnablingStalled(section);
      return section;
    }

    if (mode === "predates") {
      verdict.className = "qpm-panel-verdict qpm-panel-verdict--ok";
      verdict.textContent = "collection on";
      const latest = el("button", {
        className: "qpm-enable-btn",
        type: "button",
        textContent: "Show latest snapshot",
      });
      latest.addEventListener("click", () => {
        latest.disabled = true;
        qpmShowLatestSnapshot();
      });
      note.textContent = "This snapshot was taken before plan collection was enabled.";
      row.appendChild(latest);
      row.appendChild(note);
      return section;
    }

    verdict.textContent = "collection off for this data directory";
    const btn = el("button", {
      className: "qpm-enable-btn",
      type: "button",
      textContent: "Enable plan collection",
    });
    row.appendChild(btn);
    row.appendChild(note);

    if (mode === "unavailable" || mode === "readonly") {
      btn.disabled = true;
      note.className = "qpm-note qpm-note--warn";
      note.textContent =
        mode === "unavailable"
          ? "This viewer is serving files without the control endpoint, so collection "
            + "cannot be enabled from here. Restart the collector with --snapshot-query-plans."
          : "This data directory is not writable, so collection cannot be enabled from "
            + "here. Restart the collector with --snapshot-query-plans.";
      return section;
    }

    note.textContent =
      "Adds yb_pg_stat_plans to each snapshot (about +30% size). Takes effect on the "
      + "collector's next checkpoint.";
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      btn.textContent = "Enabling\u2026";
      try {
        await qpmSetCollection(true);
      } catch (e) {
        btn.textContent = "Enable plan collection";
        btn.disabled = false;
        note.className = "qpm-note qpm-note--warn";
        note.textContent = "Could not enable: " + (e.message || e);
        return;
      }
      // Rebuild from the new state rather than patching this one: same code path
      // as every later render, so what you see now is what you see after navigating.
      section.replaceWith(qpmCollectionOffPanel(qpmCollectionState));
    });
    return section;
  }

  /** Tracking modes in which QPM actually records plans. */
  const QPM_TRACK_MODES_ON = ["all", "top"];

  /**
   * Which QUERY PLANS block a snapshot calls for. Pure, so the guardrail is
   * testable without a DOM.
   *
   *   "legacy"       -- snapshot predates the QPM section; nothing to reason about
   *   "unsupported"  -- cluster has no yb_pg_stat_plans view
   *   "tracking-off" -- view exists but yb_pg_stat_plans_track records nothing, so
   *                     offering to enable collection would only write empty sets
   *   "collection-off" -- QPM works and is tracking; ybtop just is not collecting
   *   "plans"        -- section has data
   */
  function qpmPanelMode(qpm) {
    if (!qpm) return "legacy";
    if (qpm.supported === false) return "unsupported";
    if (qpm.track != null && QPM_TRACK_MODES_ON.indexOf(String(qpm.track).toLowerCase()) < 0) {
      return "tracking-off";
    }
    if (qpm.collected === false) return "collection-off";
    return "plans";
  }

  /** Bare panel carrying one explanatory line and no controls. */
  function qpmNoticePanel(statusText, detailText, warn) {
    const section = el("section", { className: "ybtop-section qpm-panel qpm-panel--off" });
    const head = el("div", { className: "qpm-panel-head" });
    head.appendChild(el("span", { className: "qpm-panel-title", textContent: "QUERY PLANS" }));
    head.appendChild(el("span", { className: "qpm-panel-verdict", textContent: statusText }));
    section.appendChild(head);
    if (detailText) {
      const body = el("div", { className: "qpm-panel-body" });
      body.appendChild(
        el("div", {
          className: warn ? "qpm-note qpm-note--warn" : "qpm-note",
          textContent: detailText,
        })
      );
      section.appendChild(body);
    }
    return section;
  }

  /** Query string naming one recorded plan -- the server resolves the database from it. */
  function qpmPinQuery(target) {
    return ["queryid", "planid", "plan_ref", "dbid", "file"]
      .map((k) => k + "=" + encodeURIComponent(String(target[k] == null ? "" : target[k])))
      .join("&");
  }

  async function qpmFetchPinState(target) {
    try {
      const res = await fetch("api/pin?" + qpmPinQuery(target), { cache: "no-store" });
      if (!res.ok) return null;
      return await res.json();
    } catch (e) {
      return null;
    }
  }

  async function qpmSetHinting(enable, target) {
    const res = await fetch("api/hinting", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.assign({ enable: !!enable }, target)),
    });
    let body = {};
    try {
      body = await res.json();
    } catch (e) {
      body = {};
    }
    if (!res.ok) throw new Error(body.error || "HTTP " + res.status);
    return body;
  }

  async function qpmPostPin(route, payload) {
    const res = await fetch("api/" + route, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    // Server clock, so edits compare against snapshot times without browser skew.
    const serverMs = Date.parse(res.headers.get("Date") || "");
    let body = {};
    try {
      body = await res.json();
    } catch (e) {
      body = {};
    }
    if (!res.ok) throw new Error(body.error || "HTTP " + res.status);
    body._serverMs = serverMs;
    return body;
  }

  /**
   * Why a pinned hint would not change any plans yet.
   *
   * Inserting the row is not enough: pg_hint_plan only consults the hint table
   * when enable_hint_table is on, and only matches ybtop's queryid keys when
   * yb_use_query_id_for_hinting is on. Both are read here from the seed node, and
   * both must hold for the sessions whose plans you want changed -- setting them
   * in ybtop's own session does nothing for the application.
   */
  function qpmPinEffectWarnings(prereq) {
    const out = [];
    if (!prereq) return out;
    if (prereq.enable_hint_table === false) out.push("pg_hint_plan.enable_hint_table is off");
    if (prereq.use_query_id_for_hinting === false) {
      out.push("pg_hint_plan.yb_use_query_id_for_hinting is off");
    }
    return out;
  }

  /**
   * What the pin row offers, from /api/pin state. Pure, so it is testable.
   *   "ready"        -- hint table exists: Pin / Remove / hint-table toggle
   *   "installable"  -- pg_hint_plan ships but is not created here: Install
   *   "unavailable"  -- pinning is off, or cannot work on this cluster: say why
   */
  function qpmPinRowMode(st) {
    if (!st) return "unavailable";
    if (st.available) return "ready";
    if (st.installable) return "installable";
    return "unavailable";
  }

  /**
   * Every pin-row button's state, from /api/pin state alone. Pure, and applied in
   * full on every redraw: Remove disables itself when clicked, and a redraw that
   * only un-hid it left it dead after Pin -> Remove -> Pin on the same page.
   */
  function qpmPinButtonStates(st) {
    const mode = qpmPinRowMode(st);
    const ready = mode === "ready";
    return {
      install: { hidden: mode !== "installable", disabled: false },
      pin: { hidden: false, disabled: !ready },
      remove: { hidden: !(ready && st.pinned), disabled: false },
      guc: { hidden: !ready, disabled: false, enable: ready && qpmPinEffectWarnings(st.prereq).length > 0 },
    };
  }

  /**
   * Pin / remove controls under a plan's hints.
   *
   * The request names which recorded plan to pin (snapshot file + queryid + planid
   * + plan_ref); the hint text itself is read server-side from that snapshot, so
   * the browser never supplies the SQL payload.
   */
  function qpmPinControls(group, queryIdForPin, snapshotFile) {
    // Everything the server needs to find this exact recorded plan, database included.
    const target = {
      queryid: String(queryIdForPin),
      planid: String(group.planid),
      plan_ref: String(group.plan_ref),
      dbid: String(group.dbid || ""),
      file: String(snapshotFile || ""),
    };
    const wrap = el("div", { className: "qpm-pin-row" });
    const status = el("span", { className: "qpm-note" });
    // A query_id covers the statement exactly as the application sends it: the same
    // SQL with literal values (typed into ysqlsh, say) or other parameter types is a
    // different query_id, which the pin does not touch.
    const pinBtn = el("button", {
      className: "qpm-enable-btn",
      type: "button",
      textContent: "Pin this plan",
      title:
        "Pins this plan for query_id " + queryIdForPin + ": the statement as the application "
        + "sends it. The same SQL with literal values, or with other parameter types, is a "
        + "different query_id and keeps its own plan.",
    });
    const rmBtn = el("button", {
      className: "qpm-enable-btn qpm-enable-btn--danger",
      type: "button",
      textContent: "Remove pinned hint",
    });
    rmBtn.hidden = true;
    // Turning hint-table lookup on changes plan selection for every new session in
    // the database, so it is its own control rather than a side effect of pinning.
    const gucBtn = el("button", {
      className: "qpm-enable-btn",
      type: "button",
      textContent: "Enable hint table",
      title:
        "ALTER DATABASE ... SET pg_hint_plan.enable_hint_table = on and "
        + "yb_use_query_id_for_hinting = on. Applies to new sessions.",
    });
    gucBtn.hidden = true;
    // Replaces what used to be an instruction to go run CREATE EXTENSION by hand.
    const installBtn = el("button", {
      className: "qpm-enable-btn",
      type: "button",
      textContent: "Install pg_hint_plan",
      title: "CREATE EXTENSION pg_hint_plan in this database",
    });
    installBtn.hidden = true;
    wrap.appendChild(installBtn);
    wrap.appendChild(pinBtn);
    wrap.appendChild(rmBtn);
    wrap.appendChild(gucBtn);
    wrap.appendChild(status);

    function applyState(st) {
      const mode = qpmPinRowMode(st);
      const b = qpmPinButtonStates(st);
      [[installBtn, b.install], [pinBtn, b.pin], [rmBtn, b.remove], [gucBtn, b.guc]].forEach(([btn, v]) => {
        btn.hidden = v.hidden;
        btn.disabled = v.disabled;
      });
      if (mode !== "ready") {
        status.className = "qpm-note";
        status.textContent =
          mode === "installable"
            ? "pg_hint_plan isn't installed in " + ((st && st.database) || "this database")
              + " yet. Installing it creates an empty hint table; no plan changes until "
              + "you pin one."
            : (st && st.reason) || "Pinning is not available from this viewer.";
        return;
      }
      const warnings = qpmPinEffectWarnings(st.prereq);
      // Offer the fix only while it is needed; offer the undo once it is not.
      if (b.guc.enable) {
        gucBtn.textContent = "Enable hint table";
        gucBtn.className = "qpm-enable-btn";
        gucBtn.dataset.enable = "1";
      } else {
        gucBtn.textContent = "Disable hint table";
        gucBtn.className = "qpm-enable-btn qpm-enable-btn--danger";
        gucBtn.dataset.enable = "0";
      }
      if (st.pinned) {
        status.className = warnings.length ? "qpm-note qpm-note--warn" : "qpm-note";
        status.textContent =
          "A hint is pinned for query_id " + queryIdForPin + " in " + (st.database || "this database") + "."
          + (warnings.length
            ? " It will not change any plans while " + warnings.join(" and ")
              + " for your application's sessions."
            : "");
      } else {
        status.className = warnings.length ? "qpm-note qpm-note--warn" : "qpm-note";
        status.textContent = warnings.length
          ? "Pinning will insert the hint, but it cannot take effect while "
            + warnings.join(" and ") + " for your application's sessions."
          : "Pins this plan for query_id " + queryIdForPin + " in "
            + (st.database || "this database") + ".";
      }
    }

    async function act(route, btn, label) {
      const was = btn.textContent;
      btn.disabled = true;
      btn.textContent = label;
      try {
        const st = await qpmPostPin(route, target);
        qpmRecordPinEdit(target.dbid, target.queryid, st.pinned, st._serverMs);
        btn.textContent = was;
        applyState(st);
      } catch (e) {
        btn.textContent = was;
        btn.disabled = false;
        status.className = "qpm-note qpm-note--warn";
        status.textContent = String(e.message || e);
      }
    }

    installBtn.addEventListener("click", async () => {
      installBtn.disabled = true;
      installBtn.textContent = "Installing\u2026";
      try {
        const st = await qpmPostPin("hint-plan/install", target);
        installBtn.textContent = "Install pg_hint_plan";
        applyState(st);
      } catch (e) {
        installBtn.textContent = "Install pg_hint_plan";
        installBtn.disabled = false;
        status.className = "qpm-note qpm-note--warn";
        status.textContent = String(e.message || e);
      }
    });
    pinBtn.addEventListener("click", () => act("pin", pinBtn, "Pinning\u2026"));
    rmBtn.addEventListener("click", () => act("unpin", rmBtn, "Removing\u2026"));
    gucBtn.addEventListener("click", async () => {
      const turningOn = gucBtn.dataset.enable === "1";
      const was = gucBtn.textContent;
      gucBtn.disabled = true;
      gucBtn.textContent = turningOn ? "Enabling\u2026" : "Disabling\u2026";
      try {
        const res = await qpmSetHinting(turningOn, target);
        const db = (res.applied && res.applied.database) || "this database";
        const fresh = await qpmFetchPinState(target);
        applyState(fresh);
        status.className = "qpm-note";
        status.textContent =
          (turningOn
            ? "Hint table enabled on " + db + "."
            : "Hint table disabled on " + db + ".")
          + " This applies to sessions started from now on -- existing connections, "
          + "and backends held by a pooler such as YSQL Connection Manager, keep the "
          + "old setting until they are recycled.";
      } catch (e) {
        gucBtn.disabled = false;
        gucBtn.textContent = was;
        status.className = "qpm-note qpm-note--warn";
        status.textContent = String(e.message || e);
      }
    });

    pinBtn.disabled = true;
    gucBtn.hidden = true;
    status.textContent = "Checking hint table\u2026";
    qpmFetchPinState(target).then(applyState);
    return wrap;
  }

  /**
   * Collection on/off control in the header of a panel that has plans. Its label
   * is derived from the live toggle every time it is drawn, so after "disable" it
   * still says "off" when the user navigates away and back.
   */
  function qpmCollectionHeaderControl() {
    const btn = el("button", { className: "qpm-panel-off-btn", type: "button" });
    function paint() {
      const on = !!(qpmCollectionState && qpmCollectionState.query_plans);
      btn.textContent = qpmCollectionHeaderLabel(qpmCollectionState);
      btn.title = on
        ? "Stop writing yb_pg_stat_plans into new snapshots"
        : "Collect yb_pg_stat_plans again from the next checkpoint";
      btn.classList.toggle("qpm-panel-off-btn--off", !on);
    }
    paint();
    btn.addEventListener("click", async () => {
      const on = !!(qpmCollectionState && qpmCollectionState.query_plans);
      btn.disabled = true;
      try {
        await qpmSetCollection(!on);
        btn.title = on
          ? "Off from the next checkpoint; snapshots already taken keep their plans"
          : "On from the next checkpoint";
      } catch (e) {
        btn.title = "Could not change collection: " + String(e.message || e);
      }
      btn.disabled = false;
      const title = btn.title;
      paint();
      btn.title = title;
    });
    return btn;
  }

  /* ---- EXPLAIN ANALYZE with the slowest recorded parameters: see ybtop/explain.py ---- */

  /** QPM keeps max_exec_time_params in 256 bytes; text cut to fit is exactly this long. */
  const QPM_PARAM_TEXT_SLOT_BYTES = 255;
  const QPM_EXPLAIN_DEFAULT_TIMEOUT_S = 30;
  const QPM_EXPLAIN_MAX_TIMEOUT_S = 600;
  const QPM_EXPLAIN_POLL_MS = 1000;
  const QPM_READ_VERBS = ["SELECT", "WITH", "VALUES", "TABLE"];
  const QPM_WRITE_VERBS = ["INSERT", "UPDATE", "DELETE", "MERGE"];

  /**
   * Inverse of PostgreSQL's BuildParamLogString, the format QPM stores:
   * "$1 = 'it''s', $2 = NULL" -> {1: "it's", 2: null}. Throws on anything else.
   * Mirrors explain.parse_param_text; a parity test holds the two together.
   */
  function qpmParseParamText(text) {
    const out = {};
    if (text == null || text === "") return out;
    const s = String(text);
    const head = /\$(\d+) = /y;
    let i = 0;
    for (;;) {
      head.lastIndex = i;
      const m = head.exec(s);
      if (!m) throw new Error("expected $N = at offset " + i);
      const num = Number(m[1]);
      i = head.lastIndex;
      let val;
      if (s.startsWith("NULL", i)) {
        val = null;
        i += 4;
      } else if (s[i] === "'") {
        i += 1;
        let buf = "";
        for (;;) {
          const j = s.indexOf("'", i);
          if (j < 0) throw new Error("unterminated value for $" + num);
          buf += s.slice(i, j);
          if (s[j + 1] === "'") {
            buf += "'";
            i = j + 2;
            continue;
          }
          i = j + 1;
          break;
        }
        val = buf;
      } else {
        throw new Error("expected a quoted value or NULL for $" + num);
      }
      if (Object.prototype.hasOwnProperty.call(out, num)) throw new Error("$" + num + " appears twice");
      out[num] = val;
      if (i === s.length) break;
      if (!s.startsWith(", ", i) || i + 2 === s.length) throw new Error("expected ', ' after $" + num);
      i += 2;
    }
    const keys = Object.keys(out).map(Number).sort((a, b) => a - b);
    keys.forEach((k, idx) => {
      if (k !== idx + 1) throw new Error("parameters are not numbered $1..$" + keys.length);
    });
    return out;
  }

  /**
   * PostgreSQL's identifier bytes: ASCII letters, digits, _ and $, and anything
   * non-ASCII (the lexer takes every byte from 0x80 up as part of a name). Works on
   * UTF-16 units: both halves of an astral character count, as the whole one does.
   */
  function qpmIdentChar(ch) {
    return ch != null && ch !== "" && (ch === "_" || ch === "$" || /[A-Za-z0-9]/.test(ch) || ch.charCodeAt(0) >= 0x80);
  }

  /**
   * Walk SQL outside quotes, comments and dollar-quoted bodies, calling
   * onPlaceholder(start, end, n) for each $n. A $n glued to an identifier
   * (foo$1 is a legal name) is not one. Mirrors explain._scan.
   */
  function qpmScanSql(sql, onPlaceholder, onLiteral) {
    const s = String(sql == null ? "" : sql);
    const n = s.length;
    let i = 0;
    while (i < n) {
      const c = s[i];
      const start = i;
      if (c === "'") {
        // E'...' honours backslash escapes; plain strings only ''.
        const esc =
          i > 0 && (s[i - 1] === "e" || s[i - 1] === "E") && (i < 2 || !qpmIdentChar(s[i - 2]));
        i += 1;
        while (i < n) {
          if (esc && s[i] === "\\") {
            i += 2;
            continue;
          }
          if (s[i] === "'") {
            if (s[i + 1] === "'") {
              i += 2;
              continue;
            }
            break;
          }
          i += 1;
        }
        i += 1;
        if (onLiteral) onLiteral(start, Math.min(i, n));
      } else if (c === '"') {
        i += 1;
        while (i < n) {
          if (s[i] === '"') {
            if (s[i + 1] === '"') {
              i += 2;
              continue;
            }
            break;
          }
          i += 1;
        }
        i += 1;
      } else if (c === "-" && s.startsWith("--", i)) {
        const j = s.indexOf("\n", i);
        i = j < 0 ? n : j + 1;
        if (onLiteral) onLiteral(start, i);
      } else if (c === "/" && s.startsWith("/*", i)) {
        let depth = 1;
        i += 2;
        while (i < n && depth) {
          if (s.startsWith("/*", i)) {
            depth += 1;
            i += 2;
          } else if (s.startsWith("*/", i)) {
            depth -= 1;
            i += 2;
          } else {
            i += 1;
          }
        }
        if (onLiteral) onLiteral(start, i);
      } else if (c === "$") {
        if (i > 0 && qpmIdentChar(s[i - 1])) {
          i += 1;
          continue;
        }
        const ph = /\$(\d+)/y;
        ph.lastIndex = i;
        const m = ph.exec(s);
        if (m) {
          onPlaceholder(i, ph.lastIndex, Number(m[1]));
          i = ph.lastIndex;
          continue;
        }
        const dq = /\$([A-Za-z_][A-Za-z_0-9]*)?\$/y;
        dq.lastIndex = i;
        const d = dq.exec(s);
        if (d) {
          const close = s.indexOf(d[0], dq.lastIndex);
          i = close < 0 ? n : close + d[0].length;
          if (onLiteral) onLiteral(start, i);
          continue;
        }
        i += 1;
      } else {
        i += 1;
      }
    }
  }

  function qpmPlaceholders(sql) {
    const out = new Set();
    qpmScanSql(sql, (a, b, num) => out.add(num));
    return out;
  }

  /** A standard-conforming SQL literal, for display. Nothing built here is executed. */
  function qpmSqlLiteral(v) {
    return v == null ? "NULL" : "'" + String(v).replace(/'/g, "''") + "'";
  }

  /** The statement with each $n shown as its recorded value -- display only. */
  function qpmInlineLiterals(sql, values) {
    const s = String(sql == null ? "" : sql);
    const vals = values || {};
    let out = "";
    let last = 0;
    qpmScanSql(s, (a, b, num) => {
      if (Object.prototype.hasOwnProperty.call(vals, num)) {
        out += s.slice(last, a) + qpmSqlLiteral(vals[num]);
        last = b;
      }
    });
    return out + s.slice(last);
  }

  function qpmUtf8Length(s) {
    return new TextEncoder().encode(String(s)).length;
  }

  /** Why these recorded parameters cannot be replayed, or null. Mirrors explain.param_text_problem. */
  function qpmParamTextProblem(text) {
    if (text == null || text === "") return null;
    if (qpmUtf8Length(text) === QPM_PARAM_TEXT_SLOT_BYTES) {
      return (
        "QPM cut this statement's slowest parameters short (they did not fit its "
        + "255-byte slot), so they cannot be replayed."
      );
    }
    let values;
    try {
      values = qpmParseParamText(text);
    } catch (e) {
      return "QPM's record of the slowest parameters could not be read.";
    }
    const vs = Object.keys(values).map((k) => values[k]);
    if (vs.length && vs.every((v) => v === "?")) {
      return (
        "QPM shows the parameters as '?': yb_pg_stat_plans_show_max_exec_params is "
        + "off for ybtop's login."
      );
    }
    return null;
  }

  /** {values: [$1..$k], error: null} to bind, or {values: null, error}. Mirrors explain.bind_values. */
  function qpmBindValues(sql, text) {
    const problem = qpmParamTextProblem(text);
    if (problem) return { values: null, error: problem };
    const values = qpmParseParamText(text);
    const nums = Object.keys(values).map(Number);
    const refs = qpmPlaceholders(sql);
    if (!refs.size) {
      if (nums.length) return { values: null, error: "the recorded parameters do not match the statement text" };
      return { values: [], error: null };
    }
    // A loop, not Math.max(...refs): a bulk INSERT can carry 100k+ placeholders,
    // past what a spread call can take.
    let top = 0;
    refs.forEach((k) => {
      if (k > top) top = k;
    });
    for (let k = 1; k <= top; k++) {
      if (!Object.prototype.hasOwnProperty.call(values, k)) {
        if (!nums.length) {
          return {
            values: null,
            error:
              "pg_stat_statements replaced this statement's constants with $1.. and QPM "
              + "recorded no parameter values for it, so there is nothing to replay.",
          };
        }
        // Values are always $1..$k, so what is missing is past them: constants that
        // pg_stat_statements numbered after the statement's bind parameters.
        return {
          values: null,
          error:
            "pg_stat_statements replaced a constant in this statement with $" + k + ", and QPM "
            + "records bind parameters only, so its value is unknown.",
        };
      }
    }
    if (nums.some((k) => k > top)) {
      return { values: null, error: "the recorded parameters do not match the statement text" };
    }
    const out = [];
    for (let k = 1; k <= top; k++) out.push(values[k]);
    return { values: out, error: null };
  }

  /** First keyword, past comments, whitespace and opening parentheses. */
  function qpmFirstVerb(sql) {
    const s = String(sql == null ? "" : sql);
    const n = s.length;
    let i = 0;
    while (i < n) {
      if (" \t\n\r\f\v".indexOf(s[i]) >= 0 || s[i] === "(") {
        i += 1;
      } else if (s.startsWith("--", i)) {
        const j = s.indexOf("\n", i);
        i = j < 0 ? n : j + 1;
      } else if (s.startsWith("/*", i)) {
        let depth = 1;
        i += 2;
        while (i < n && depth) {
          if (s.startsWith("/*", i)) {
            depth += 1;
            i += 2;
          } else if (s.startsWith("*/", i)) {
            depth -= 1;
            i += 2;
          } else {
            i += 1;
          }
        }
      } else {
        const m = /^[A-Za-z]+/.exec(s.slice(i));
        return m ? m[0].toUpperCase() : "";
      }
    }
    return "";
  }

  /**
   * {kind: "read" | "write" | null, label}. Mirrors explain.statement_kind: writes run
   * in a transaction that is rolled back, everything else READ ONLY; null is refused.
   */
  function qpmStatementKind(sql, planText) {
    const verb = qpmFirstVerb(sql);
    const isWriteVerb = QPM_WRITE_VERBS.indexOf(verb) >= 0;
    if (QPM_READ_VERBS.indexOf(verb) < 0 && !isWriteVerb) return { kind: null, label: verb || "?" };
    const plan = String(planText == null ? "" : planText);
    const op = /"Operation":\s*"(Insert|Update|Delete|Merge)"/.exec(plan);
    const writes =
      isWriteVerb
      || /"Node Type":\s*"(?:ModifyTable|LockRows)"/.test(plan)
      || /^\s*(?:->\s+)?(?:(?:Insert|Update|Delete|Merge) on |LockRows)/m.test(plan);
    let label;
    if (isWriteVerb) label = verb;
    else if (op) label = verb + " … " + op[1].toUpperCase();
    else if (writes) label = verb === "SELECT" ? "SELECT … FOR UPDATE" : verb + " (locks rows)";
    else label = verb;
    return { kind: writes ? "write" : "read", label };
  }

  /** The statement with strings, comments and dollar-quoted bodies blanked. Mirrors explain._code_only. */
  function qpmCodeOnly(sql) {
    const s = String(sql == null ? "" : sql);
    const chars = s.split("");
    qpmScanSql(s, () => {}, (a, b) => {
      for (let k = a; k < b; k++) chars[k] = " ";
    });
    return chars.join("");
  }

  /**
   * Calls whose effect a ROLLBACK does not undo (other sessions, the server, files,
   * other databases, sequences, statistics). Mirrors explain._SIDE_EFFECT_CALL.
   *
   * A leading group, not a lookbehind: this is built while app.js loads, and a
   * browser without lookbehind (Safari before 16.4) would throw there and take
   * the whole viewer down with it.
   */
  const QPM_SIDE_EFFECT_CALL = new RegExp(
    '(?:^|[^A-Za-z0-9_$\\u{80}-\\u{10FFFF}])"?('
      + "pg_terminate_backend|pg_cancel_backend|pg_reload_conf|pg_rotate_logfile|pg_promote"
      + "|pg_create_restore_point|pg_switch_wal|pg_(?:start|stop)_backup|pg_backup_(?:start|stop)"
      + "|pg_stat_statements_reset|pg_stat_reset[a-z_]*|pg_stat_clear_snapshot"
      + "|pg_(?:create|drop)_[a-z_]*replication_slot|pg_replication_origin_[a-z_]+"
      + "|pg_file_[a-z_]+|lo_export|lo_import"
      + "|dblink[a-z_]*|setval"
      + "|yb_pg_stat_plans_(?:reset|insert)[a-z_]*|yb_reset_analyze_statistics"
      + "|yb_cancel_transaction|yb_query_diagnostics|yb_increment_[a-z_]+"
      + ')"?[ \\t\\n\\r\\f\\v]*\\(',
    "iu"
  );

  function qpmSideEffectCall(sql) {
    const m = QPM_SIDE_EFFECT_CALL.exec(qpmCodeOnly(sql));
    return m ? m[1].toLowerCase() : null;
  }

  function qpmSideEffectReason(fn) {
    return "This statement calls " + fn + "(), whose effect a rollback does not undo, so it is not replayed.";
  }

  /** Why qpmStatementKind refused a statement. Mirrors explain.not_replayable_reason. */
  function qpmNotReplayableReason(sql, label) {
    if (String(sql == null ? "" : sql).replace(/^[ \t\n\r\f\v]+/, "").startsWith("<")) {
      return "pg_stat_statements does not show this statement's text to ybtop's login.";
    }
    return (
      "Only SELECT, WITH, VALUES, TABLE, INSERT, UPDATE, DELETE and MERGE statements are "
      + "replayed; this one starts with " + (label || "?") + "."
    );
  }

  /** YugabyteDB ignores DEBUG without DIST, so DEBUG implies it. */
  function qpmExplainOptions(dist, debug) {
    const out = ["ANALYZE"];
    if (dist || debug) out.push("DIST");
    if (debug) out.push("DEBUG");
    return out;
  }

  /** pg_stat_statements text for a queryid: this database on this node first. Mirrors explain.statement_text. */
  function qpmStatementText(doc, queryid, datname, preferNode) {
    const per = (doc && doc.pg_stat_statements && doc.pg_stat_statements.per_node) || {};
    const nodes = Object.keys(per).sort();
    const at = nodes.indexOf(preferNode);
    if (at >= 0) {
      nodes.splice(at, 1);
      nodes.unshift(preferNode);
    }
    let fallback = null;
    for (const nid of nodes) {
      for (const r of per[nid] || []) {
        if (!r || String(r.queryid) !== String(queryid) || !r.query) continue;
        if (datname == null || String(r.dbname || "") === String(datname)) return String(r.query);
        if (fallback == null) fallback = String(r.query);
      }
    }
    return fallback;
  }

  /**
   * The recorded execution EXPLAIN ANALYZE replays: the slowest one in scope that can
   * be replayed. {ok: true, ...target} or {ok: false, reason}; null when nothing in
   * scope was recorded. The server re-derives the statement and values from the
   * snapshot for the row this names (explain.resolve_target), never from the page.
   */
  function qpmExplainTarget(doc, queryIds, dbids) {
    const qpm = doc && doc.yb_pg_stat_plans;
    const perNode = (qpm && qpm.per_node) || {};
    const cands = [];
    Object.keys(perNode)
      .sort()
      .forEach((nid) => {
        (perNode[nid] || []).forEach((r) => {
          if (!r || r.queryid == null) return;
          if (queryIds && queryIds.size && !queryIds.has(String(r.queryid))) return;
          if (dbids && !dbids.has(String(r.dbid))) return;
          cands.push({ nid, r });
        });
      });
    if (!cands.length) return null;
    // Stable: equal times keep node order, so the pick is deterministic.
    cands.sort((a, b) => (Number(b.r.max_exec_time) || 0) - (Number(a.r.max_exec_time) || 0));
    const databases = (qpm && qpm.databases) || {};
    const plans = (qpm && qpm.plans) || {};
    const roles = (qpm && qpm.roles) || {};
    const slowestMs = Number(cands[0].r.max_exec_time);
    let firstReason = null;
    let otherReason = null;
    for (const { nid, r } of cands) {
      const dbid = String(r.dbid);
      const datname = databases[dbid];
      let why = null;
      let sql = null;
      let kind = null;
      let bound = null;
      // Same order as explain.resolve_target, so page and server give one reason.
      if (r.plan_ref == null || String(r.plan_ref) === "") {
        why = "QPM kept no plan for this execution.";
      } else if (r.userid == null || String(r.userid) === "") {
        why = "QPM did not record which role ran this statement, so it is not replayed.";
      } else if (!datname) {
        why = "this plan was recorded in a database that no longer exists (oid " + dbid + ")";
      } else {
        sql = qpmStatementText(doc, r.queryid, datname, nid);
        if (!sql) why = "pg_stat_statements text for this query_id is not in the snapshot";
      }
      if (!why) {
        kind = qpmStatementKind(sql, (plans[r.plan_ref] || {}).plan);
        if (!kind.kind) why = qpmNotReplayableReason(sql, kind.label);
      }
      if (!why) {
        const fn = qpmSideEffectCall(sql);
        if (fn) why = qpmSideEffectReason(fn);
      }
      if (!why) {
        bound = qpmBindValues(sql, r.max_exec_time_params);
        if (bound.error) why = bound.error;
      }
      if (why) {
        if (firstReason == null) firstReason = why;
        else if (otherReason == null && why !== firstReason) otherReason = why;
        continue;
      }
      const shown = r.max_exec_time_params ? qpmParseParamText(r.max_exec_time_params) : {};
      const userid = r.userid == null ? null : String(r.userid);
      return {
        ok: true,
        queryid: String(r.queryid),
        planid: String(r.planid),
        plan_ref: String(r.plan_ref),
        dbid,
        datname: String(datname),
        node: nid,
        userid,
        role: userid != null && roles[userid] != null ? String(roles[userid]) : null,
        statement: sql,
        paramsText: r.max_exec_time_params == null ? null : String(r.max_exec_time_params),
        sqlDisplay: qpmInlineLiterals(sql, shown),
        hasParams: qpmPlaceholders(sql).size > 0,
        kind: kind.kind,
        label: kind.label,
        recordedMaxMs: Number(r.max_exec_time),
        slowestMs,
        // The very slowest could not be replayed; say why this one was picked instead.
        skippedReason: Number(r.max_exec_time) < slowestMs ? firstReason : null,
      };
    }
    // The slowest execution's reason first; when the rest fail for another one
    // (a family of truncated and normalised variants), that one matters too.
    return { ok: false, reason: firstReason, otherReason, slowestMs };
  }

  /** Node types with their join type, relation and index, depth-first: a plan's shape. */
  function qpmPlanSignature(planText) {
    let root = null;
    try {
      root = JSON.parse(String(planText));
    } catch (e) {
      return null;
    }
    const parts = [];
    function walk(node, depth) {
      if (!node || typeof node !== "object") return;
      parts.push(
        depth + ":" + ["Node Type", "Join Type", "Relation Name", "Index Name"]
          .map((k) => (node[k] == null ? "" : String(node[k])))
          .join("/")
      );
      (node.Plans || []).forEach((c) => walk(c, depth + 1));
    }
    (Array.isArray(root) ? root : [root]).forEach((e) => walk(e && e.Plan ? e.Plan : e, 0));
    return parts.length ? parts.join(" ") : null;
  }

  /**
   * Which recorded plan the planner chose for the replay: {group, index}, with
   * group null for a plan QPM has not recorded; null when there is no plan to compare.
   */
  function qpmMatchRecordedPlan(planJson, groups, texts) {
    const sig = qpmPlanSignature(planJson);
    if (!sig) return null;
    let comparable = 0;
    for (let i = 0; i < (groups || []).length; i++) {
      const t = (texts || {})[groups[i].plan_ref];
      const other = t ? qpmPlanSignature(t.plan) : null;
      if (other == null) continue;
      comparable += 1;
      if (other === sig) return { group: groups[i], index: i };
    }
    // Plans recorded as text, or not at all: nothing to say "not recorded" against.
    return comparable ? { group: null, index: -1 } : null;
  }

  /**
   * How the replay's plan relates to the recorded ones, as {text, warn}, or null.
   *
   * A run replays one recorded execution (run.planid / run.plan_ref), so the note
   * says whether the planner chose that same plan again. If it did, a replay far
   * faster than the recorded slowest was not slow because of its plan.
   */
  function qpmReplayPlanNote(match, run, groups) {
    if (!match) return null;
    const ran = (groups || []).find((g) =>
      (g.variants || [{ planid: g.planid, plan_ref: g.plan_ref }]).some(
        (v) => String(v.plan_ref) === String(run && run.plan_ref) && String(v.planid) === String(run && run.planid)
      )
    );
    const ranText = ran ? "the slowest execution ran planid " + ran.planid : null;
    if (!match.group) {
      return {
        warn: true,
        text:
          "The planner chose a plan QPM has not recorded for this statement"
          + (ranText ? "; " + ranText : "")
          + ". The replay is planned for these values: the application may have been on a generic"
          + " prepared plan, other settings, or older statistics.",
      };
    }
    const g = match.group;
    const tags = [];
    if (match.index === 0) tags.push("the fastest recorded plan");
    if (g.active === false) tags.push("not in use lately");
    let text = "Same plan shape as recorded planid " + g.planid + (tags.length ? " — " + tags.join(", ") : "") + ".";
    if (ran === g) text += " It is the plan the slowest execution ran.";
    else if (ran) text += " Not the plan the slowest execution ran (planid " + ran.planid + ").";
    return { warn: false, text };
  }

  function qpmQuoteIdent(name) {
    return '"' + String(name).replace(/"/g, '""') + '"';
  }

  /** Exactly what a run sends, for the confirmation dialog. Mirrors explain.run_explain. */
  function qpmExplainSequence(target, options, timeoutS) {
    const role = target.role
      ? qpmQuoteIdent(target.role)
      : target.userid != null
        ? "<role oid " + target.userid + ">"
        : null;
    return [
      "SET statement_timeout = '" + timeoutS + "s';",
      target.kind === "write" ? "BEGIN;" : "BEGIN READ ONLY;",
      role ? "SET LOCAL ROLE " + role + ";  -- with that role's own settings" : null,
      "SET LOCAL statement_timeout = '" + timeoutS + "s';",
      "SET LOCAL yb_disable_transactional_writes = off;  -- so ROLLBACK undoes every write",
      "EXPLAIN (FORMAT JSON) …;  -- plan only, to match against the recorded plans",
      "EXPLAIN (" + options.join(", ") + ") …;",
      "ROLLBACK;",
    ].filter(Boolean);
  }

  /** The unindented summary lines EXPLAIN ANALYZE ends with, as [label, value]. */
  function qpmExplainSummary(planText) {
    const want = [
      "Execution Time",
      "Planning Time",
      "Storage Read Requests",
      "Storage Rows Scanned",
      "Storage Write Requests",
      "Catalog Read Requests",
      "Peak Memory Usage",
    ];
    const got = {};
    String(planText == null ? "" : planText)
      .split("\n")
      .forEach((line) => {
        const m = /^([A-Z][A-Za-z ]+):\s*(.+)$/.exec(line);
        if (m && want.indexOf(m[1]) >= 0 && got[m[1]] == null) got[m[1]] = m[2].trim();
      });
    return want.filter((k) => got[k] != null).map((k) => [k, got[k]]);
  }

  /** Seconds, 1..600; blank or not a plain number -> the default. Mirrors explain.clamp_timeout. */
  function qpmClampTimeout(v) {
    const text = v == null || typeof v === "boolean" ? "" : String(v).replace(/^[ \t\n\r\f\v]+|[ \t\n\r\f\v]+$/g, "");
    if (!/^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$/.test(text)) {
      return QPM_EXPLAIN_DEFAULT_TIMEOUT_S;
    }
    const n = Number(text);
    if (!isFinite(n)) return QPM_EXPLAIN_DEFAULT_TIMEOUT_S;
    return Math.max(1, Math.min(QPM_EXPLAIN_MAX_TIMEOUT_S, Math.floor(n + 0.5)));
  }

  /**
   * Fold a GET /api/explain answer into the page's run cache. The server is the
   * source of truth for the statements asked about: what it no longer has (the
   * collector restarted) is dropped, or the page would show "running" for ever.
   */
  function qpmApplyExplainResponse(cache, dbid, queryIds, body) {
    (queryIds || []).forEach((q) => cache.delete(String(dbid) + "|" + String(q)));
    [body && body.run, body && body.active].forEach((run) => {
      if (run && run.dbid != null && run.queryid != null) {
        cache.set(String(run.dbid) + "|" + String(run.queryid), run);
      }
    });
    return cache;
  }

  /** The drilldown a plan view belongs to, from the URL alone -- stable across Prev/Next. */
  function qpmViewScopeKey(queryId, canonicalize, dbnameParam) {
    return String(queryId) + "|" + (canonicalize ? "family|" + (dbnameParam || "") : "query");
  }

  /** The statements to ask the collector about: the one a run would replay first. */
  function qpmRunQueryIds(scopeIds, target) {
    const out = [];
    if (target && target.ok) out.push(String(target.queryid));
    (scopeIds || []).forEach((q) => {
      if (out.indexOf(String(q)) < 0) out.push(String(q));
    });
    return out.slice(0, 200);
  }

  /* ---- EXPLAIN ANALYZE: view state, runs from the server, polling ---- */

  /** Drilldown view state, mirrored in the URL as plan_view=explain and literals=t. */
  let qpmPlanView = "plans";
  let qpmShowLiterals = false;
  /** Which drilldown that state belongs to; a different query starts on its plans. */
  let qpmViewScope = null;
  /** "dbid|queryid" -> the latest run the server reported for that statement. */
  const qpmExplainRuns = new Map();
  /** {available, reason} from the collector, once asked. */
  let qpmExplainAvail = null;
  /** The run in flight anywhere on this collector: one at a time. */
  let qpmExplainActive = null;
  /** What the poller asks about: the statements of the panel on screen. */
  let qpmExplainWatch = null;
  /** True for the whole of a poll, await included: a redraw then must not start a second loop. */
  let qpmExplainPolling = false;
  /** Run ids the user asked to cancel, so a redraw keeps saying "Cancelling". */
  const qpmExplainCancelling = new Set();
  /**
   * The plan panel on screen, for the query banner's EXPLAIN button: open its
   * dialog, or bring its result into view. Set on every render; the banner is
   * drawn just before the panel, so it looks this up when clicked.
   */
  let qpmPanelActions = null;
  /** Redraws for on-screen explain surfaces (panel, query row); pruned when detached. */
  const qpmExplainListeners = new Set();

  function qpmExplainPrefs() {
    let p = {};
    try {
      p = JSON.parse(window.localStorage.getItem("ybtop.explainPrefs") || "{}") || {};
    } catch (e) {
      p = {};
    }
    return {
      dist: !!p.dist,
      debug: !!p.debug,
      timeout_s: qpmClampTimeout(p.timeout_s == null ? QPM_EXPLAIN_DEFAULT_TIMEOUT_S : p.timeout_s),
    };
  }

  function qpmSaveExplainPrefs(p) {
    try {
      window.localStorage.setItem("ybtop.explainPrefs", JSON.stringify(p));
    } catch (e) {
      /* a convenience only */
    }
  }

  function qpmNotifyExplain() {
    qpmExplainListeners.forEach((fn) => {
      if (!fn.isLive()) qpmExplainListeners.delete(fn);
      else fn();
    });
  }

  function qpmListen(fn, isLive) {
    fn.isLive = isLive;
    qpmExplainListeners.add(fn);
  }

  /** Newest run among these statements, from what the server last reported. */
  function qpmLatestRun(dbid, queryIds) {
    let best = null;
    (queryIds || []).forEach((q) => {
      const run = qpmExplainRuns.get(String(dbid) + "|" + String(q));
      if (run && (best == null || String(run.started_utc) > String(best.started_utc))) best = run;
    });
    return best;
  }

  function qpmRememberRun(run) {
    if (run && run.dbid != null && run.queryid != null) {
      qpmExplainRuns.set(String(run.dbid) + "|" + String(run.queryid), run);
    }
  }

  async function qpmFetchExplain(dbid, queryIds) {
    try {
      const q =
        "api/explain?dbid=" + encodeURIComponent(String(dbid))
        + "&queryids=" + encodeURIComponent(Array.from(queryIds || []).join(","));
      const res = await fetch(q, { cache: "no-store" });
      if (!res.ok) {
        if (res.status === 404) qpmExplainAvail = { available: false, reason: null, missing: true };
        return null;
      }
      const body = await res.json();
      qpmExplainAvail = { available: !!body.available, reason: body.reason || null };
      qpmExplainActive = body.active || null;
      qpmApplyExplainResponse(qpmExplainRuns, dbid, queryIds, body);
      return body;
    } catch (e) {
      return null;
    }
  }

  async function qpmPostExplain(route, payload) {
    const res = await fetch("api/" + route, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    let body = {};
    try {
      body = await res.json();
    } catch (e) {
      body = {};
    }
    if (!res.ok) {
      const err = new Error(body.error || "HTTP " + res.status);
      err.active = body.active || null;
      throw err;
    }
    return body;
  }

  /** Poll while the watched statement has a run in flight; one poller for the page. */
  function qpmEnsureExplainPoll() {
    if (qpmExplainPolling) return;
    qpmExplainPolling = true;
    const tick = async () => {
      const w = qpmExplainWatch;
      if (w) {
        await qpmFetchExplain(w.dbid, w.queryIds);
        qpmNotifyExplain();
      }
      // A failed poll keeps the last state, so a run in flight keeps being polled.
      const run = w ? qpmLatestRun(w.dbid, w.queryIds) : null;
      if (run && run.state === "running") setTimeout(tick, QPM_EXPLAIN_POLL_MS);
      else qpmExplainPolling = false;
    };
    setTimeout(tick, QPM_EXPLAIN_POLL_MS);
  }

  function qpmSetPlanView(view) {
    qpmPlanView = view === "explain" ? "explain" : "plans";
    writeViewerStateToUrl();
    qpmNotifyExplain();
  }

  function qpmFmtClock(iso) {
    const ms = Date.parse(String(iso || ""));
    return isFinite(ms) ? new Date(ms).toISOString().slice(11, 19) + " UTC" : "—";
  }

  /* ---- EXPLAIN ANALYZE: confirmation dialog ---- */

  /**
   * The "this really runs it" dialog. Shows the statement with its values inlined,
   * the options, the timeout, and the exact sequence that will be sent.
   */
  function qpmOpenExplainDialog(target, snapshotFile, onStarted) {
    const dlg = el("dialog", { className: "qpm-dialog", "aria-labelledby": "qpm-dialog-title" });
    const form = el("form", { method: "dialog", className: "qpm-dialog-form" });
    dlg.appendChild(form);
    const title = el("h3", { id: "qpm-dialog-title", className: "qpm-dialog-title", textContent: "Run EXPLAIN ANALYZE?" });
    form.appendChild(title);
    const actions = el("div", { className: "qpm-dialog-actions" });
    const closeBtn = el("button", { type: "button", className: "qpm-enable-btn qpm-dialog-cancel", textContent: "Cancel" });
    // Once Run is clicked the collector may already be starting the statement, so
    // the dialog stays until the answer is in; cancel from the result view.
    let starting = false;
    closeBtn.addEventListener("click", () => {
      if (!starting) dlg.close();
    });
    dlg.addEventListener("cancel", (ev) => {
      if (starting) ev.preventDefault();
    });
    dlg.addEventListener("close", () => dlg.remove());

    const avail = qpmExplainAvail;
    const blocker =
      avail && avail.missing
        ? "This viewer has no EXPLAIN endpoint -- it is an older ybtop, or files served without ybtop watch."
        : avail && !avail.available
          ? avail.reason || "EXPLAIN ANALYZE is not available from this viewer."
          : !target || !target.ok
            ? "This statement cannot be replayed: " + ((target && target.reason) || "nothing recorded for it.")
              + (target && target.otherReason ? " Its other recorded executions: " + target.otherReason : "")
            : null;
    if (blocker) {
      // Nothing can run, so don't ask as if it could.
      title.textContent = "EXPLAIN ANALYZE";
      form.appendChild(el("p", { className: "qpm-dialog-note", textContent: blocker }));
      closeBtn.textContent = "Close";
      actions.appendChild(closeBtn);
      form.appendChild(actions);
      document.body.appendChild(dlg);
      dlg.showModal();
      closeBtn.focus();
      return;
    }

    form.appendChild(
      el("p", {
        className: "qpm-dialog-warn",
        textContent:
          "This executes the statement on the cluster, once, with the parameters of its slowest "
          + "recorded execution -- including any functions it calls.",
      })
    );
    if (target.kind === "write") {
      form.appendChild(
        el("p", {
          className: "qpm-dialog-warn qpm-dialog-warn--write",
          textContent:
            target.label + ": its changes are made inside a transaction that is always rolled "
            + "back, but it holds their row locks while it runs, and sequence values it draws "
            + "are used up.",
        })
      );
    }
    const meta = el("div", { className: "qpm-dialog-meta" });
    [
      ["query_id", target.queryid],
      ["slowest", qpmFmtMs(target.recordedMaxMs) + " on " + target.node],
      ["database", target.datname],
      ["as", target.role || (target.userid != null ? "role oid " + target.userid : "ybtop's login")],
    ].forEach(([k, v]) => qpmMetric(meta, k, v));
    form.appendChild(meta);
    if (target.skippedReason) {
      form.appendChild(
        el("p", {
          className: "qpm-dialog-note",
          textContent:
            "The slowest execution (" + qpmFmtMs(target.slowestMs) + ") cannot be replayed -- "
            + target.skippedReason + " This is the slowest one that can.",
        })
      );
    }
    form.appendChild(el("pre", { className: "qpm-dialog-sql", textContent: target.sqlDisplay }));

    const prefs = qpmExplainPrefs();
    const opts = el("div", { className: "qpm-dialog-opts" });
    function check(label, checked, disabled, title) {
      const lab = el("label", { className: "qpm-dialog-check", title: title || "" });
      const box = el("input", { type: "checkbox" });
      box.checked = !!checked;
      box.disabled = !!disabled;
      lab.appendChild(box);
      lab.appendChild(document.createTextNode(label));
      opts.appendChild(lab);
      return box;
    }
    check("ANALYZE", true, true, "Always on: the point is to run it");
    const distBox = check("DIST", prefs.dist, false, "Storage requests, rows scanned and RPC timing per node");
    const debugBox = check("DEBUG", prefs.debug, false, "DocDB and RocksDB metrics per node; needs DIST");
    const tlab = el("label", { className: "qpm-dialog-timeout" });
    tlab.appendChild(document.createTextNode("Statement timeout"));
    const tin = el("input", {
      type: "number",
      min: "1",
      max: String(QPM_EXPLAIN_MAX_TIMEOUT_S),
      step: "1",
      value: String(prefs.timeout_s),
      "aria-label": "Statement timeout in seconds",
    });
    tlab.appendChild(tin);
    tlab.appendChild(document.createTextNode("s"));
    opts.appendChild(tlab);
    form.appendChild(opts);

    const seq = el("pre", { className: "qpm-dialog-seq" });
    form.appendChild(seq);
    const msg = el("p", { className: "qpm-dialog-note qpm-dialog-note--err" });
    msg.hidden = true;
    form.appendChild(msg);
    const runBtn = el("button", {
      type: "button",
      className: "qpm-enable-btn qpm-dialog-run" + (target.kind === "write" ? " qpm-enable-btn--danger" : ""),
      textContent: target.kind === "write" ? "Run, then roll back" : "Run",
    });
    // One run at a time per collector: say so now rather than after the click.
    const busy = qpmExplainActive && qpmExplainActive.state === "running" ? qpmExplainActive : null;
    if (busy) {
      runBtn.disabled = true;
      msg.hidden = false;
      msg.className = "qpm-dialog-note";
      msg.textContent =
        String(busy.queryid) === String(target.queryid)
          ? "This statement is already running (started " + qpmFmtClock(busy.started_utc) + ")."
          : "Another EXPLAIN ANALYZE is running (query_id " + busy.queryid + ", started "
            + qpmFmtClock(busy.started_utc) + "); one runs at a time.";
    }

    function sync() {
      // DEBUG needs DIST: tick it and hold it while DEBUG is on.
      if (debugBox.checked) distBox.checked = true;
      distBox.disabled = debugBox.checked;
      seq.textContent = qpmExplainSequence(
        target,
        qpmExplainOptions(distBox.checked, debugBox.checked),
        qpmClampTimeout(tin.value)
      ).join("\n");
    }
    distBox.addEventListener("change", sync);
    debugBox.addEventListener("change", sync);
    tin.addEventListener("input", sync);
    sync();

    // Enter in the timeout box would submit the form: running must be a click.
    form.addEventListener("submit", (ev) => ev.preventDefault());
    runBtn.addEventListener("click", async () => {
      const chosen = {
        dist: distBox.checked,
        debug: debugBox.checked,
        timeout_s: qpmClampTimeout(tin.value),
      };
      tin.value = String(chosen.timeout_s);
      qpmSaveExplainPrefs(chosen);
      runBtn.disabled = true;
      runBtn.textContent = "Starting…";
      starting = true;
      closeBtn.disabled = true;
      msg.hidden = true;
      msg.className = "qpm-dialog-note qpm-dialog-note--err";
      try {
        const body = await qpmPostExplain("explain", {
          queryid: target.queryid,
          planid: target.planid,
          plan_ref: target.plan_ref,
          dbid: target.dbid,
          userid: target.userid,
          node: target.node,
          file: snapshotFile || "",
          dist: chosen.dist,
          debug: chosen.debug,
          timeout_s: chosen.timeout_s,
        });
        qpmRememberRun(body.run);
        qpmExplainActive = body.run;
        starting = false;
        dlg.close();
        onStarted(body.run);
      } catch (e) {
        starting = false;
        closeBtn.disabled = false;
        // A 409 names the run in flight; the page shows it (and can cancel it) if it is ours.
        if (e.active) {
          qpmRememberRun(e.active);
          qpmExplainActive = e.active;
          qpmNotifyExplain();
        }
        runBtn.disabled = !!e.active;
        runBtn.textContent = target.kind === "write" ? "Run, then roll back" : "Run";
        msg.hidden = false;
        msg.setAttribute("role", "alert");
        msg.textContent = String(e.message || e);
      }
    });

    actions.appendChild(closeBtn);
    actions.appendChild(runBtn);
    form.appendChild(actions);
    document.body.appendChild(dlg);
    dlg.showModal();
    // Cancel has focus: running is a deliberate second step, never an Enter away.
    closeBtn.focus();
  }

  /* ---- EXPLAIN ANALYZE: result view ---- */

  /**
   * One line at the top of the plan panel: the Run button with what it would
   * replay, or the last run's result with a way to it -- visible without first
   * switching to the EXPLAIN tab. Muted, with the reason, when nothing can run.
   */
  /** "6.2× faster than the recorded 106 ms", or null when there is nothing to compare. */
  function qpmRatioText(ms, recordedMs) {
    const ratio = recordedMs > 0 ? ms / recordedMs : null;
    if (ratio == null || !isFinite(ratio) || ratio <= 0) return null;
    return (ratio >= 1 ? ratio.toFixed(ratio >= 10 ? 0 : 1) + "× slower" : (1 / ratio).toFixed(1 / ratio >= 10 ? 0 : 1) + "× faster")
      + " than the recorded " + qpmFmtMs(recordedMs);
  }

  /**
   * What the plan panel's EXPLAIN strip shows, from the run, the target and whether
   * the collector allows it. Pure, so every state is testable:
   *   button {label, action: "open" | "show", muted, running}
   *   parts  [[kind, text]] with kind k (key), v, quiet, mono
   *   link   "Show result →" / "Show progress →" or null
   */
  function qpmExplainStripModel(run, target, avail) {
    const off = !!(avail && !avail.available);
    const ready = !off && !!(target && target.ok);
    const running = !!(run && run.state === "running");
    const parts = [];
    let button;
    if (running) {
      button = { label: "Running…", action: "show", muted: false, running: true };
      parts.push(["k", "on"], ["v", run.node + (run.role ? " as " + run.role : "")]);
    } else {
      button = { label: run ? "Run again" : "Explain analyze", action: "open", muted: !ready, running: false };
      if (run) {
        parts.push(["k", "last run"]);
        if (run.state === "done" && run.execution_ms != null) {
          parts.push(["v", qpmFmtMs(run.execution_ms)]);
          const ratio = qpmRatioText(run.execution_ms, run.recorded_max_ms);
          if (ratio) parts.push(["quiet", ratio]);
        } else {
          parts.push(["v", run.state === "timeout" ? "timed out" : run.state === "cancelled" ? "cancelled" : "failed"]);
        }
        parts.push(["quiet", qpmFmtClock(run.finished_utc || run.started_utc)]);
      } else if (ready) {
        parts.push(
          ["k", "replays"],
          ["v", "the slowest execution, " + qpmFmtMs(target.recordedMaxMs) + " on " + target.node],
          ["quiet", "as " + (target.role || "role oid " + target.userid)]
        );
        if (target.paramsText) parts.push(["mono", target.paramsText]);
      } else {
        parts.push([
          "quiet",
          off
            ? (avail && avail.reason) || "EXPLAIN ANALYZE is not available from this viewer."
            : "Cannot be replayed: " + ((target && target.reason) || "nothing recorded for it."),
        ]);
      }
    }
    return {
      muted: !ready && !run,
      button,
      parts,
      link: run ? (running ? "Show progress →" : "Show result →") : null,
    };
  }

  /**
   * One line at the top of the plan panel: the Run button with what it would
   * replay, or the last run's result with a way to it -- visible without first
   * switching to the EXPLAIN tab. Muted, with the reason, when nothing can run.
   */
  function qpmExplainStrip(run, target, openDialog, showResult) {
    const m = qpmExplainStripModel(run, target, qpmExplainAvail);
    const strip = el("div", { className: "qpm-explain-strip" + (m.muted ? " qpm-explain-strip--muted" : "") });
    const btn = el("button", {
      type: "button",
      className:
        "qpm-explain-primary qpm-explain-primary--sm"
        + (m.button.running ? " qpm-explain-primary--running" : "")
        + (m.button.muted ? " qpm-explain-primary--muted" : ""),
    });
    btn.appendChild(
      m.button.running
        ? el("span", { className: "qpm-seg-dot", "aria-hidden": "true" })
        : el("span", { className: "qpm-explain-glyph", "aria-hidden": "true", textContent: "▶" })
    );
    btn.appendChild(document.createTextNode(m.button.label));
    btn.addEventListener("click", () => (m.button.action === "show" ? showResult && showResult() : openDialog()));
    strip.appendChild(btn);
    const text = el("span", { className: "qpm-explain-strip-text" });
    const CLS = {
      k: "qpm-explain-strip-k",
      v: "qpm-explain-strip-v",
      quiet: "qpm-explain-strip-v qpm-explain-strip-v--quiet",
      mono: "qpm-explain-strip-v qpm-explain-strip-v--mono",
    };
    m.parts.forEach(([kind, t]) => {
      const span = el("span", { className: CLS[kind], textContent: t });
      // Values are cut to one line; the card below and the dialog show them in full.
      if (kind === "mono") span.title = t;
      text.appendChild(span);
    });
    strip.appendChild(text);
    if (m.link && showResult) {
      const link = el("button", { type: "button", className: "qpm-explain-strip-link", textContent: m.link });
      link.addEventListener("click", showResult);
      strip.appendChild(link);
    }
    return strip;
  }

  /**
   * The EXPLAIN side of the plan panel: an invitation before any run, progress while
   * one runs, then its output -- with which recorded plan it matches and how its time
   * compares to the slowest recorded execution it replayed.
   */
  function qpmExplainView(run, target, groups, texts, openDialog, drillQueryId) {
    const wrap = el("div", { className: "qpm-explain" });
    const again = el("button", {
      type: "button",
      className: "qpm-enable-btn",
      textContent: run ? "Run again…" : "Run EXPLAIN ANALYZE…",
    });
    again.addEventListener("click", openDialog);

    if (!run) {
      wrap.appendChild(qpmExplainStrip(null, target, openDialog, null));
      return wrap;
    }

    const head = el("div", { className: "qpm-explain-head" });
    head.appendChild(
      el("span", { className: "qpm-explain-title", textContent: "EXPLAIN (" + (run.options || ["ANALYZE"]).join(", ") + ")" })
    );
    const sub = el("span", { className: "qpm-explain-sub" });
    head.appendChild(sub);
    wrap.appendChild(head);

    // Which statement ran matters in a canonical family, where it may not be the one
    // the drilldown was opened on.
    const which = String(run.queryid) !== String(drillQueryId) ? " · query_id " + run.queryid : "";
    if (run.state === "running") {
      // Elapsed on the collector's clock, not this browser's. Updated in place by
      // tick(), so a redraw each second does not steal keyboard focus.
      sub.setAttribute("aria-live", "polite");
      const tick = (r) => {
        const elapsed = Math.max(0, Math.round(Number(r.run_s) || 0));
        sub.textContent =
          "running on " + r.node + (r.role ? " as " + r.role : "") + which + " · "
          + elapsed + " s of " + r.timeout_s + " s";
      };
      tick(run);
      wrap._qpmTick = tick;
      const row = el("div", { className: "qpm-enable-row" });
      row.appendChild(el("span", { className: "qpm-spinner", "aria-hidden": "true" }));
      const stop = el("button", { type: "button", className: "qpm-enable-btn qpm-enable-btn--danger", textContent: "Cancel" });
      if (qpmExplainCancelling.has(run.id)) {
        stop.disabled = true;
        stop.textContent = "Cancelling…";
      }
      stop.addEventListener("click", async () => {
        stop.disabled = true;
        stop.textContent = "Cancelling…";
        qpmExplainCancelling.add(run.id);
        try {
          const body = await qpmPostExplain("explain/cancel", { dbid: run.dbid, queryid: run.queryid });
          qpmRememberRun(body.run);
        } catch (e) {
          qpmExplainCancelling.delete(run.id);
          stop.disabled = false;
          stop.textContent = "Cancel";
        }
      });
      row.appendChild(stop);
      row.appendChild(
        el("span", {
          className: "qpm-note",
          textContent:
            (run.kind === "write" ? "Runs in a transaction that is rolled back. " : "Read-only transaction. ")
            + "Stops at " + run.timeout_s + " s: statement_timeout on the server, and a cancel from the collector.",
        })
      );
      wrap.appendChild(row);
      return wrap;
    }

    sub.textContent =
      qpmFmtClock(run.finished_utc || run.started_utc)
      + " · " + run.node + (run.role ? " · as " + run.role : "")
      + (run.datname ? " · " + run.datname : "") + which;
    head.appendChild(again);

    if (run.state !== "done") {
      wrap.appendChild(
        el("div", {
          className: "qpm-note qpm-note--warn",
          role: "alert",
          textContent: (run.state === "error" ? "Failed: " : "") + String(run.error || "no output"),
        })
      );
    }
    // The slowest case can move on after a run: say so, rather than let an old
    // result stand for the current worst execution.
    if (
      target && target.ok && String(target.queryid) === String(run.queryid)
      && isFinite(target.recordedMaxMs) && isFinite(Number(run.recorded_max_ms))
      && target.recordedMaxMs > Number(run.recorded_max_ms) * 1.001
      && String(target.paramsText) !== String(run.params_text)
    ) {
      wrap.appendChild(
        el("div", {
          className: "qpm-note qpm-note--warn",
          textContent:
            "A slower execution has been recorded since this run: " + qpmFmtMs(target.recordedMaxMs)
            + " (this run replayed " + qpmFmtMs(Number(run.recorded_max_ms)) + "). Run again to replay it.",
        })
      );
    }

    const metrics = el("div", { className: "qpm-metrics" });
    if (run.execution_ms != null) {
      const ratio = run.recorded_max_ms > 0 ? run.execution_ms / run.recorded_max_ms : null;
      qpmMetric(metrics, "this run", qpmFmtMs(run.execution_ms));
      qpmMetric(
        metrics,
        "recorded slowest",
        qpmFmtMs(run.recorded_max_ms)
          + (ratio != null && isFinite(ratio) && ratio > 0
            ? ratio >= 1
              ? "  (" + ratio.toFixed(ratio >= 10 ? 0 : 1) + "× slower now)"
              : "  (" + (1 / ratio).toFixed(1 / ratio >= 10 ? 0 : 1) + "× faster now)"
            : ""),
        "qpm-metric--wide"
      );
    } else if (run.recorded_max_ms != null) {
      qpmMetric(metrics, "recorded slowest", qpmFmtMs(run.recorded_max_ms));
    }
    qpmExplainSummary(run.plan_text)
      .filter(([k]) => k !== "Execution Time")
      .forEach(([k, v]) => qpmMetric(metrics, k.replace(/^Storage /, "").toLowerCase(), v));
    if (run.params_text) qpmMetric(metrics, "params", run.params_text, "qpm-metric--full");
    if (run.role_settings && run.role_settings.length) {
      qpmMetric(metrics, "role settings", run.role_settings.join(", "), "qpm-metric--full");
    }
    wrap.appendChild(metrics);

    const note = qpmReplayPlanNote(qpmMatchRecordedPlan(run.plan_json, groups, texts), run, groups);
    if (note) {
      wrap.appendChild(el("div", { className: note.warn ? "qpm-note qpm-note--warn" : "qpm-note", textContent: note.text }));
    }
    const notices = run.notices || [];
    notices.slice(0, 20).forEach((n) => wrap.appendChild(el("div", { className: "qpm-note", textContent: n })));
    if (notices.length > 20) {
      wrap.appendChild(el("div", { className: "qpm-note", textContent: "… and " + (notices.length - 20) + " more notices" }));
    }
    if (run.plan_text) {
      wrap.appendChild(el("pre", { className: "qpm-tree qpm-explain-out", textContent: String(run.plan_text) }));
    }
    return wrap;
  }

  /**
   * The query banner's EXPLAIN control: a primary button while nothing runs, live
   * progress while something does, and a chip for the last result. It drives the
   * plan panel below (qpmPanelActions), so there is one dialog and one result view.
   */
  /**
   * What the query banner's EXPLAIN control shows. Pure:
   *   {label, running, muted, title, last: chip text or null}
   */
  function qpmExplainBannerModel(run, target, avail) {
    const off = !!(avail && !avail.available);
    const ready = !off && !!(target && target.ok);
    const running = !!(run && run.state === "running");
    let label;
    let title;
    if (running) {
      label = "Running · " + Math.max(0, Math.round(Number(run.run_s) || 0)) + " s";
      title = "Show the EXPLAIN ANALYZE that is running";
    } else {
      label = "Explain analyze";
      title = off
        ? (avail && avail.reason) || "EXPLAIN ANALYZE is off for this collector"
        : ready
          ? "EXPLAIN ANALYZE this statement with the values of its slowest recorded execution ("
            + qpmFmtMs(target.recordedMaxMs) + ")"
          : "Cannot be replayed: " + ((target && target.reason) || "nothing recorded");
    }
    let last = null;
    if (run && !running) {
      last =
        (run.state === "done" && run.execution_ms != null
          ? "Last run " + qpmFmtMs(run.execution_ms)
          : run.state === "timeout" ? "Last run timed out" : run.state === "cancelled" ? "Last run cancelled" : "Last run failed")
        + " · " + qpmFmtClock(run.finished_utc || run.started_utc) + " →";
    }
    return { label, running, muted: !ready && !running, title, last };
  }

  /**
   * The query banner's EXPLAIN control: a primary button while nothing runs, live
   * progress while something does, and a chip for the last result. It drives the
   * plan panel below (qpmPanelActions), so there is one dialog and one result view.
   */
  function qpmExplainBannerControl(target, runScope) {
    const wrap = el("span", { className: "qpm-explain-banner" });
    const btn = el("button", { type: "button", className: "qpm-explain-primary" });
    const last = el("button", { type: "button", className: "qpm-explain-last", title: "Show the result" });
    wrap.appendChild(last);
    wrap.appendChild(btn);
    const act = (name) => () => {
      const p = qpmPanelActions;
      if (p && p.isLive()) p[name]();
    };
    btn.addEventListener("click", () => {
      const run = runScope ? qpmLatestRun(runScope.dbid, runScope.queryIds) : null;
      (run && run.state === "running" ? act("reveal") : act("open"))();
    });
    last.addEventListener("click", act("reveal"));
    let painted = null;
    function paint() {
      const run = runScope ? qpmLatestRun(runScope.dbid, runScope.queryIds) : null;
      const m = qpmExplainBannerModel(run, target, qpmExplainAvail);
      const key = JSON.stringify(m);
      if (key === painted) return;
      painted = key;
      btn.textContent = "";
      btn.classList.toggle("qpm-explain-primary--muted", m.muted);
      btn.classList.toggle("qpm-explain-primary--running", m.running);
      btn.appendChild(
        m.running
          ? el("span", { className: "qpm-seg-dot", "aria-hidden": "true" })
          : el("span", { className: "qpm-explain-glyph", "aria-hidden": "true", textContent: "▶" })
      );
      btn.appendChild(document.createTextNode(m.label));
      btn.title = m.title;
      last.hidden = !m.last;
      if (m.last) last.textContent = m.last;
    }
    paint();
    qpmListen(paint, () => wrap.isConnected);
    return wrap;
  }

  /** "literals" toggle for the drilldown's query row; forced on while the EXPLAIN view is open. */
  function qpmLiteralsControl(queryEl, labelEl, noteEl, originalText, originalLabel, target, drillQueryId, runScope) {
    const btn = el("button", { type: "button", className: "qpm-literals-btn", textContent: "inline literals" });
    function paint() {
      // Same lookup as the panel, so the row and the output above/below it are one run.
      const run = runScope ? qpmLatestRun(runScope.dbid, runScope.queryIds) : null;
      const forced = qpmPlanView === "explain";
      const on = !!(target && target.ok && (qpmShowLiterals || forced));
      // In the EXPLAIN view the row shows what actually ran; otherwise what would.
      const shownRun = forced && run && run.sql_display ? run : null;
      const text = on ? (shownRun ? shownRun.sql_display : target.sqlDisplay) : originalText;
      queryEl.textContent = text || "(no text in snapshot)";
      labelEl.textContent = on ? "query · literals" : originalLabel;
      btn.setAttribute("aria-pressed", on ? "true" : "false");
      btn.classList.toggle("qpm-literals-btn--on", on);
      btn.disabled = !(target && target.ok) || forced;
      btn.title = !(target && target.ok)
        ? "No values to inline: " + ((target && target.reason) || "nothing recorded")
        : forced
          ? "Shown with values while the EXPLAIN ANALYZE view is open"
          : on
            ? "Show the statement with its $n placeholders"
            : "Show the statement with the values of its slowest recorded execution";
      noteEl.hidden = !on;
      if (on) {
        const src = shownRun || target;
        // A run's values were the slowest when it ran; a slower one may have come since.
        noteEl.textContent =
          (shownRun ? "values this run replayed, the slowest when it ran: " : "values of the slowest recorded execution: ")
          + qpmFmtMs(shownRun ? shownRun.recorded_max_ms : target.recordedMaxMs)
          + " on " + src.node
          + (String(src.queryid) !== String(drillQueryId) ? " · query_id " + src.queryid : "");
      }
    }
    btn.addEventListener("click", () => {
      qpmShowLiterals = !qpmShowLiterals;
      writeViewerStateToUrl();
      paint();
    });
    paint();
    qpmListen(paint, () => btn.isConnected);
    return btn;
  }

  /**
   * The QUERY PLANS block for a scoped ASH drilldown. Returns null when the
   * snapshot has no QPM section, the cluster does not support QPM, or this query
   * has no plans on record -- the panel should be absent, not empty.
   */
  function qpmPlansPanel(doc, queryId, canonicalFamily, clusterNodeCount, snapshotFile, dbname, explainTarget) {
    const qpm = doc && doc.yb_pg_stat_plans;
    const mode = qpmPanelMode(qpm);
    if (mode === "legacy" || mode === "collection-off") {
      return qpmCollectionOffPanel(qpmCollectionState);
    }
    if (mode === "unsupported") {
      return qpmNoticePanel(
        "this cluster has no Query Plan Management",
        "yb_pg_stat_plans is not present. QPM needs YugabyteDB 2025.2.3 or later.",
        false
      );
    }
    if (mode === "tracking-off") {
      // Offer the fix, not a button that would only ever collect empty plan sets.
      return qpmNoticePanel(
        "plan tracking is disabled on this cluster"
          + (qpm.track ? " (yb_pg_stat_plans_track = " + qpm.track + ")" : ""),
        "QPM records nothing until yb_pg_stat_plans_track is 'all' (every statement) or "
          + "'top' (top-level statements only). Set it in the cluster's YSQL "
          + "configuration -- for example ysql_pg_conf_csv=yb_pg_stat_plans_track=all -- "
          + "then plan collection can be enabled here.",
        true
      );
    }
    const scopeQids = qpmScopeQueryIds(queryId, canonicalFamily);
    const groups = aggregateQpmPlans(qpm, scopeQids, qpmPanelDbids(qpm, dbname, scopeQids));
    groups.forEach((g) => {
      g.dbLabel = qpmDatabaseLabel(qpm, g.dbid);
      g.dbDropped = /\(dropped\)$/.test(String(g.dbLabel || ""));
    });
    if (groups.length === 0) return null;
    const verdict = qpmPlanVerdict(groups);
    const texts = qpm.plans || {};

    const section = el("section", { className: "ybtop-section qpm-panel" });
    const head = el("div", { className: "qpm-panel-head" });
    const toggleBtn = el("button", {
      className: "qpm-panel-toggle",
      type: "button",
      textContent: "▾",
    });
    head.appendChild(toggleBtn);
    head.appendChild(el("span", { className: "qpm-panel-title", textContent: "QUERY PLANS" }));
    head.appendChild(
      el("span", {
        className: "qpm-panel-verdict qpm-panel-verdict--" + verdict.status,
        textContent: qpmVerdictHeadline(verdict),
      })
    );
    // Recorded plans <-> EXPLAIN ANALYZE; a plain button until a run exists.
    const explainCtl = el("span", { className: "qpm-explain-ctl" });
    head.appendChild(explainCtl);
    if (qpmCollectionState && qpmCollectionState.writable !== false) {
      head.appendChild(qpmCollectionHeaderControl());
    }
    section.appendChild(head);

    const body = el("div", { className: "qpm-panel-body" });
    const plansView = el("div", { className: "qpm-plans-view" });
    // EXPLAIN ANALYZE, up front: no tab switch needed to see or start it.
    const stripHost = el("div", { className: "qpm-explain-strip-host" });
    plansView.appendChild(stripHost);
    const explainView = el("div", { className: "qpm-explain-view" });
    if (qpm.truncated) {
      plansView.appendChild(
        el("div", {
          className: "qpm-note qpm-note--warn",
          textContent:
            "A node hit the per-node QPM row cap (" +
            qpm.limit +
            "), so this plan set may be incomplete.",
        })
      );
    }
    groups.forEach((g, i) =>
      plansView.appendChild(
        qpmPlanCard(g, verdict, texts, i, clusterNodeCount, snapshotFile, queryId)
      )
    );
    body.appendChild(plansView);
    body.appendChild(explainView);
    section.appendChild(body);

    // Runs are keyed per statement and database on the collector, which holds them,
    // so a run survives navigating away, a reload, and newer snapshots.
    const runDbid = explainTarget && explainTarget.ok ? explainTarget.dbid : groups[0].dbid;
    const scopeIds = qpmScopeQueryIds(queryId, canonicalFamily);
    const runQueryIds = qpmRunQueryIds(
      scopeIds.size ? Array.from(scopeIds) : [].concat(...groups.map((g) => Array.from(g.queryIds || []))),
      explainTarget
    );
    // Show the EXPLAIN side and bring it on screen: a run started from the query
    // banner is otherwise running out of sight below it.
    function reveal() {
      if (body.hidden) toggleBtn.click();
      qpmSetPlanView("explain");
      // After the dialog's close has handed focus back (and scrolled to) the button
      // that opened it; and only when the panel's top is not already well in view.
      setTimeout(() => {
        const r = section.getBoundingClientRect();
        if (r.top < 0 || r.top > window.innerHeight * 0.55) {
          section.scrollIntoView({ behavior: "smooth", block: "start" });
        }
      }, 80);
    }
    function openDialog() {
      qpmOpenExplainDialog(explainTarget, snapshotFile, (run) => {
        qpmExplainWatch = { dbid: run.dbid, queryIds: runQueryIds };
        reveal();
        qpmEnsureExplainPoll();
      });
    }
    qpmPanelActions = { open: openDialog, reveal, isLive: () => section.isConnected };
    let paintedCtl = null;
    let paintedView = null;
    let paintedStrip = null;
    function paint() {
      const run = qpmLatestRun(runDbid, runQueryIds);
      const view = qpmPlanView === "explain" ? "explain" : "plans";
      // Structure only: the elapsed seconds of a running run are updated in place.
      const runKey = run ? run.id + "|" + run.state + "|" + (qpmExplainCancelling.has(run.id) ? "c" : "") : "none";
      const off = !!(qpmExplainAvail && !qpmExplainAvail.available);
      const ctlKey = view + "|" + runKey + "|" + (off ? "off" : "on");
      if (ctlKey !== paintedCtl) {
        paintedCtl = ctlKey;
        explainCtl.textContent = "";
        {
          // Always shown: the EXPLAIN side is a first-class view, not something to
          // discover after a first run.
          const seg = el("span", { className: "qpm-seg", role: "group", "aria-label": "Plan view" });
          [
            ["plans", "Recorded plans"],
            ["explain", "Explain analyze"],
          ].forEach(([v, label]) => {
            const b = el("button", {
              type: "button",
              className: "qpm-seg-btn" + (v === view ? " qpm-seg-btn--on" : ""),
              textContent: label,
              "aria-pressed": v === view ? "true" : "false",
            });
            if (v === "explain" && run && run.state === "running") {
              b.appendChild(el("span", { className: "qpm-seg-dot", "aria-hidden": "true" }));
            }
            b.addEventListener("click", () => qpmSetPlanView(v));
            seg.appendChild(b);
          });
          explainCtl.appendChild(seg);
        }
      }
      if (view === "plans" && runKey + "|" + off !== paintedStrip) {
        paintedStrip = runKey + "|" + off;
        stripHost.textContent = "";
        stripHost.appendChild(qpmExplainStrip(run, explainTarget, openDialog, reveal));
      }
      plansView.hidden = view !== "plans";
      explainView.hidden = view !== "explain";
      if (view === "explain" && runKey + "|" + off !== paintedView) {
        paintedView = runKey + "|" + off;
        explainView.textContent = "";
        explainView.appendChild(qpmExplainView(run, explainTarget, groups, texts, openDialog, queryId));
      } else if (view === "explain" && run && run.state === "running") {
        const shown = explainView.firstChild;
        if (shown && shown._qpmTick) shown._qpmTick(run);
      }
    }
    paint();
    qpmListen(paint, () => section.isConnected);
    qpmExplainWatch = { dbid: runDbid, queryIds: runQueryIds };
    qpmFetchExplain(runDbid, runQueryIds).then(() => {
      qpmNotifyExplain();
      const run = qpmLatestRun(runDbid, runQueryIds);
      if (run && run.state === "running") qpmEnsureExplainPoll();
    });

    toggleBtn.addEventListener("click", () => {
      body.hidden = !body.hidden;
      toggleBtn.textContent = body.hidden ? "▸" : "▾";
      toggleBtn.setAttribute("aria-expanded", body.hidden ? "false" : "true");
    });
    toggleBtn.setAttribute("aria-expanded", "true");
    toggleBtn.setAttribute("aria-label", "Toggle query plans");
    return section;
  }

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

  /**
   * Query text used as an ASH grouping dimension. With Merge similar SQL on this is the
   * normalized template so IN/VALUES/bind variants collapse; otherwise the raw snapshot text.
   */
  function ashQueryGroupText(query) {
    if (!mergeSimilarSql) {
      return query != null && query !== undefined ? String(query) : "";
    }
    const key = queryTemplateKey(query);
    return key || (query != null && query !== undefined ? String(query) : "");
  }

  function ashQueryColumnLabel() {
    return mergeSimilarSql ? "canonical query" : "query";
  }

  /**
   * Grouping identity for query text. Resolved SQL uses ashQueryGroupText (canonical when Merge
   * is on, raw otherwise). With Merge on, unresolved rows fall back to query_id so distinct
   * DocDB/YCQL/evicted ids do not collapse into one bucket; with Merge off they stay one
   * empty-text bucket, matching the pre-merge rollups.
   */
  function ashCanonicalGroupKey(r) {
    const q = ashQueryGroupText(r.query);
    if (q) return q;
    if (mergeSimilarSql) {
      const qid = normQid(r.query_id);
      if (qid != null && String(qid).trim() !== "") return `\0qid:${String(qid)}`;
    }
    return "\0__no_query__";
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

  /** Top-50 merge key when Merge similar SQL is on: table/index + canonical query + wait event. */
  function ashMergeKeyCanonical(r) {
    return [
      ashCanonicalGroupKey(r),
      r.wait_event_component,
      r.wait_event,
      r.wait_event_type,
      ashMergeTableKey(r),
      r.ysql_dbid == null ? "" : String(r.ysql_dbid),
    ].join("\0");
  }

  /** Collapse query_id-merged ASH rows onto the canonical-query Top 50 identity. */
  function collapseAshMergedByCanonicalQuery(rows) {
    const m = new Map();
    (rows || []).forEach((r) => {
      const k = ashMergeKeyCanonical(r);
      const q = ashQueryGroupText(r.query);
      if (!m.has(k)) {
        m.set(k, {
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
          query: q,
          _best_samples: 0,
        });
      }
      const ent = m.get(k);
      const add = Number(r.samples) || 0;
      ent.samples += add;
      if (
        q &&
        add > ent._best_samples &&
        r.query_id != null &&
        String(r.query_id).trim() !== ""
      ) {
        ent.query_id = r.query_id;
        ent._best_samples = add;
      }
      if (!ent.query && q) ent.query = q;
      ent.namespace_name = ent.namespace_name || r.namespace_name || null;
      ent.object_name = ent.object_name || r.object_name || null;
      if (
        (ent.table_id == null || ent.table_id === "") &&
        r.table_id != null &&
        String(r.table_id).trim() !== ""
      ) {
        ent.table_id = r.table_id;
      }
      if (ent.ysql_dbid == null && r.ysql_dbid != null && r.ysql_dbid !== undefined) {
        ent.ysql_dbid = r.ysql_dbid;
      }
    });
    const out = Array.from(m.values()).map((ent) => {
      const rest = Object.assign({}, ent);
      delete rest._best_samples;
      return Object.assign({}, rest, { object_name: ashDisplayObjectName(rest) });
    });
    out.sort((a, b) => b.samples - a.samples);
    return out;
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

  function filterAshPerNodeByCanonicalFamily(perNode, family) {
    if (!family || !family.queryIds || !family.queryIds.size) return perNode;
    const wanted = Array.from(family.queryIds);
    const out = {};
    Object.keys(perNode || {}).forEach((nid) => {
      const rows = (perNode[nid] || []).filter((r) =>
        wanted.some((qid) => rowMatchesAshQueryIdFilter(r, qid))
      );
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

  /** Canonical-family scope keeps the representative query_id but hides repeated canonical SQL. */
  function ashColumnsWithoutQuery(cols) {
    return cols
      .filter((c) => c.key !== "query")
      .map((c) =>
        c.key === "query_id" ? Object.assign({}, c, { label: "representative query_id" }) : c
      );
  }

  function buildAshQueryHref(qid, options) {
    const opts = options || {};
    const p = new URLSearchParams();
    p.set("view", "ash");
    p.set("query", String(qid));
    if (opts.canonicalize) {
      p.set("canonicalize", "t");
      if (opts.dbname != null && String(opts.dbname).trim() !== "") {
        p.set("dbname", String(opts.dbname).trim());
      }
    }
    applyViewerToggleParams(p);
    return `${window.location.pathname}?${p.toString()}`;
  }

  /** When a table opts into family drilldowns, query-text links expand the canonical family. */
  function ashFamilyLinkOptions(row, cellOpts) {
    if (!cellOpts || !cellOpts.canonicalizeFamily) return {};
    const dbname = row && row.dbname != null ? row.dbname : null;
    return { canonicalize: true, dbname: dbname };
  }

  function buildAshNodeHref(nodeId) {
    const p = new URLSearchParams();
    p.set("view", "ash");
    p.set("node", String(nodeId));
    applyViewerToggleParams(p);
    return `${window.location.pathname}?${p.toString()}`;
  }

  function buildAshTableIdHref(tableId) {
    const p = new URLSearchParams();
    p.set("view", "ash");
    p.set("table_id", String(tableId));
    applyViewerToggleParams(p);
    return `${window.location.pathname}?${p.toString()}`;
  }

  function navigateToAshForQueryId(qid, options) {
    const opts = options || {};
    const s = String(qid).trim();
    if (!s) return;
    ashQueryIdFilter = s;
    ashCanonicalizeFilter = !!opts.canonicalize;
    ashCanonicalDbnameFilter =
      ashCanonicalizeFilter && opts.dbname != null && String(opts.dbname).trim() !== ""
        ? String(opts.dbname).trim()
        : null;
    ashNodeIdFilter = null;
    ashTableIdFilter = null;
    syncMergeSimilarSqlForFamilyScope();
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
    ashCanonicalizeFilter = false;
    ashCanonicalDbnameFilter = null;
    ashTableIdFilter = null;
    canonicalFamilyResolved = false;
    syncMergeSimilarSqlForFamilyScope();
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
    ashCanonicalizeFilter = false;
    ashCanonicalDbnameFilter = null;
    ashNodeIdFilter = null;
    canonicalFamilyResolved = false;
    syncMergeSimilarSqlForFamilyScope();
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
   * @param {boolean} [useMergeBucketScan] — Scan flat rows by `ash_merge_key` == `ash_flat_bucket_key`.
   *   Only pass true when the merged rows are keyed by `ashMergeKey` (Merge similar SQL off).
   *   Canonical Top 50 keys (`ashMergeKeyCanonical`) never equal `ash_flat_bucket_key`; other
   *   rollups keep false.
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
    if (mergeSimilarSql) return ashCanonicalGroupKey(r);
    const raw = r.query_id != null && r.query_id !== undefined ? r.query_id : null;
    return raw != null && String(raw).trim() !== "" ? String(raw).trim() : "\0__no_query_id__";
  }

  function bucketKeyAshNamespaceQueryFlat(r) {
    const nn =
      r.namespace_name != null && r.namespace_name !== undefined ? String(r.namespace_name) : "";
    return JSON.stringify([nn, ashCanonicalGroupKey(r)]);
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
      const q = ashCanonicalGroupKey(r);
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
      const q = ashQueryGroupText(r.query);
      const k = JSON.stringify([nn, ashCanonicalGroupKey(r)]);
      if (!m.has(k)) {
        m.set(k, {
          namespace_name: nn,
          query_id: null,
          query: q,
          samples: 0,
          _best_samples: 0,
        });
      }
      const ent = m.get(k);
      const add = Number(r.samples) || 0;
      const raw = r.query_id != null && r.query_id !== undefined ? r.query_id : null;
      ent.samples += add;
      if ((!ent.query || ent.query === "") && q) ent.query = q;
      // Heaviest member: resolvable SQL always; unresolved SQL only when Merge already
      // keyed the bucket by query_id (so Load Distribution can recompute the same key).
      // The shared empty-text bucket (Merge off) stays unstamped.
      if (
        (q || mergeSimilarSql) &&
        add > (ent._best_samples || 0) &&
        raw != null &&
        String(raw).trim() !== ""
      ) {
        ent.query_id = raw;
        ent._best_samples = add;
      }
    });
    return Array.from(m.values())
      .map((ent) => {
        const row = Object.assign({}, ent);
        delete row._best_samples;
        return row;
      })
      .sort((a, b) => b.samples - a.samples);
  }

  /** Group merged ASH by pg_stat query id (query_id); sum samples. Rows without query_id bucket together. */
  function groupAshByQueryId(rows) {
    const m = new Map();
    (rows || []).forEach((r) => {
      const raw = r.query_id != null && r.query_id !== undefined ? r.query_id : null;
      const q = ashQueryGroupText(r.query);
      const k = mergeSimilarSql
        ? ashCanonicalGroupKey(r)
        : raw != null && String(raw).trim() !== ""
          ? String(raw).trim()
          : "\0__no_query_id__";
      if (!m.has(k)) {
        m.set(k, {
          query_id: k === "\0__no_query_id__" || k === "\0__no_query__" ? null : raw,
          namespace_name:
            r.namespace_name != null && r.namespace_name !== undefined ? String(r.namespace_name) : "",
          query: q,
          samples: 0,
        });
      }
      const ent = m.get(k);
      const add = Number(r.samples) || 0;
      ent.samples += add;
      if ((!ent.query || ent.query === "") && q) ent.query = q;
      if ((!ent.namespace_name || ent.namespace_name === "") && r.namespace_name) {
        ent.namespace_name = String(r.namespace_name);
      }
      if (
        mergeSimilarSql &&
        k !== "\0__no_query__" &&
        add > (ent._best_samples || 0) &&
        raw != null &&
        String(raw).trim() !== ""
      ) {
        ent.query_id = raw;
        ent._best_samples = add;
      }
    });
    return Array.from(m.values())
      .map((ent) => {
        const row = Object.assign({}, ent);
        delete row._best_samples;
        return row;
      })
      .sort((a, b) => b.samples - a.samples);
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
      const q = ashQueryGroupText(r.query);
      const dim = tid ? `tid:${tid}` : on;
      const k = ignoreQueryInKey
        ? JSON.stringify([nn, dim])
        : JSON.stringify([nn, dim, ashCanonicalGroupKey(r)]);
      if (!m.has(k)) {
        m.set(k, {
          namespace_name: nn,
          object_name: on,
          table_id: tid || null,
          query: q,
          query_id: null,
          samples: 0,
          _best_samples: 0,
        });
      }
      const ent = m.get(k);
      const add = Number(r.samples) || 0;
      const raw = r.query_id != null && r.query_id !== undefined ? r.query_id : null;
      ent.samples += add;
      if ((!ent.object_name || ent.object_name === "") && on) ent.object_name = on;
      if (!ent.table_id && tid) ent.table_id = tid;
      if ((!ent.query || ent.query === "") && q) ent.query = q;
      if (
        (q || mergeSimilarSql) &&
        add > (ent._best_samples || 0) &&
        raw != null &&
        String(raw).trim() !== ""
      ) {
        ent.query_id = raw;
        if (q) ent.query = q;
        ent._best_samples = add;
      }
    });
    return Array.from(m.values())
      .map((ent) => {
        const row = Object.assign({}, ent);
        delete row._best_samples;
        return row;
      })
      .sort((a, b) => b.samples - a.samples);
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

  /**
   * Give each recurring-template member its share of the template's rates. Both metrics are linear
   * in samples, so a proportional split is exact and lets the member list rank by whichever of
   * Active Sessions/sec or Load % the table is sorted on.
   */
  function withAshTemplateMemberRates(rows) {
    return (rows || []).map((r) => {
      const members = r.query_members;
      if (!members || !members.length) return r;
      const total = Number(r.samples) || 0;
      return {
        ...r,
        query_members: members.map((m) => {
          const share = total > 0 ? (Number(m.samples) || 0) / total : 0;
          return {
            ...m,
            sessions_per_sec: (Number(r.sessions_per_sec) || 0) * share,
            load_pct: Math.round(10000 * ((Number(r.load_pct) || 0) * share)) / 10000,
          };
        }),
      };
    });
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
    "template",
    "queryids",
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

  /**
   * Table sort key -> the member field that ranks the "member queryids (ranked)" list, so the
   * ranking follows whichever metric column the user is sorting the template table by.
   */
  const TEMPLATE_MEMBER_RANK_FIELDS = {
    calls: "calls",
    calls_per_sec: "calls_per_sec",
    total_ms: "total_ms",
    time_pct: "total_ms",
    samples: "samples",
    sessions_per_sec: "sessions_per_sec",
    load_pct: "load_pct",
    best_tier: "tier",
  };

  /** Numeric value used to order members by `field` (tiers rank by severity, not alphabetically). */
  function templateMemberRankValue(member, field) {
    if (field === "tier") return HIST_TIER_RANK[member.tier] || 0;
    return Number(member[field]) || 0;
  }

  /** Re-rank template members by the active sort column; falls back to the prebuilt order. */
  function memberRankField(sortKey) {
    return TEMPLATE_MEMBER_RANK_FIELDS[sortKey];
  }

  function orderTemplateMembers(members, sortKey, dir) {
    const list = members || [];
    const field = memberRankField(sortKey);
    if (!field || !list.some((m) => m && m[field] != null && m[field] !== "")) return list;
    const sign = dir === "asc" ? 1 : -1;
    return list
      .slice()
      .sort((a, b) => {
        const av = templateMemberRankValue(a, field);
        const bv = templateMemberRankValue(b, field);
        if (av !== bv) return (av - bv) * sign;
        // Ties keep the list's build-time order (total time / samples / confidence) so equal
        // members never shuffle between renders.
        const ra = a.rank == null ? Infinity : Number(a.rank);
        const rb = b.rank == null ? Infinity : Number(b.rank);
        if (ra !== rb) return ra - rb;
        return String(a.query_id).localeCompare(String(b.query_id));
      })
      .map((m, i) => Object.assign({}, m, { rank: i + 1 }));
  }

  function appendAshTemplateMembersCell(td, members, metricField) {
    applyMonoTableCellClass(td, "queryids");
    const list = el("div", { className: "tmpl-member-list" });
    (members || []).forEach((member, i) => {
      const row = el("div", { className: "tmpl-member-row" });
      row.appendChild(
        el("span", {
          className: "tmpl-member-rank",
          textContent: `${member.rank != null ? member.rank : i + 1}.`,
        })
      );
      const qid = member.query_id;
      const a = el("a", {
        className: "ash-queryid-deeplink",
        href: buildAshQueryHref(qid),
        textContent: String(qid),
        title: member.query ? String(member.query) : "Open query-scoped ASH",
      });
      a.addEventListener("click", (e) => {
        e.preventDefault();
        navigateToAshForQueryId(qid);
      });
      row.appendChild(a);
      // Show the metric the list is ranked by, so the parenthetical explains the ordering.
      const order = metricField && member[metricField] != null && member[metricField] !== ""
        ? [metricField]
        : ["samples", "total_ms", "calls"];
      let metric = "";
      for (let k = 0; k < order.length && !metric; k += 1) {
        const f = order[k];
        const val = member[f];
        if (val == null || val === "") continue;
        if (f === "samples") metric = `(${Number(val) || 0} samples)`;
        else if (f === "total_ms") metric = `(${formatPgStatMsTwoDecimals(val)} ms)`;
        else if (f === "calls") metric = `(${Number(val) || 0} calls)`;
        else if (f === "calls_per_sec") metric = `(${(Number(val) || 0).toFixed(2)} calls/s)`;
        else if (f === "sessions_per_sec") metric = `(${formatAshSessionsPerSec(val)} sessions/s)`;
        else if (f === "load_pct") metric = `(${(Number(val) || 0).toFixed(2)}% load)`;
        else if (f === "tier") metric = `(${String(val)})`;
      }
      row.appendChild(
        el("span", {
          className: "tmpl-member-samples",
          textContent: metric,
        })
      );
      list.appendChild(row);
    });
    td.appendChild(list);
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

  /** ASH link target for a statement row; collapsed rows open the whole canonical family. */
  function statementRowAshLink(row) {
    const primary = row && row._tmpl_primary_queryid;
    if (primary != null && String(primary).trim() !== "") {
      return {
        queryId: primary,
        canonicalize: true,
        dbname: row.dbname != null ? row.dbname : null,
      };
    }
    const qid = row && row.queryid;
    if (qid != null && String(qid).trim() !== "" && !row._tmpl_member_count) {
      return { queryId: qid, canonicalize: false, dbname: null };
    }
    return null;
  }

  function appendQueryCellWithAshLinks(td, queryVal, qid, options) {
    const opts = options || {};
    const full = String(queryVal || "");
    const wrap = el("div", { className: "query-cell" });
    const a = el("a", {
      className: "query-preview query-ash-deeplink",
      href: buildAshQueryHref(qid, opts),
      textContent: full,
    });
    a.addEventListener("click", (e) => {
      e.preventDefault();
      navigateToAshForQueryId(qid, opts);
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
    if (col.title) th.title = String(col.title);
    if (col.sortable === false) {
      th.classList.add("th-no-sort");
    }
    if (col.align === "right") {
      th.classList.add("yb-cell-right");
    }
  }

  function buildSortableTable(title, rows, columns, subsectionId, ashCellOpts, initialSort) {
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
    const hasInitial =
      initialSort &&
      initialSort.key &&
      columns.some((c) => c.key === initialSort.key && c.sortable !== false);
    const state = hasInitial
      ? { key: initialSort.key, dir: initialSort.dir || "desc" }
      : { key: columns[0].key, dir: "desc" };

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
              appendQueryCellWithAshLinks(td, v, qid, ashFamilyLinkOptions(row, ashCellOpts));
            } else {
              appendQueryCell(td, v);
            }
          } else if (col.key === "qpm_plans") {
            appendQpmPlansCell(td, row);
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
          } else if (col.key === "query_members") {
            appendAshTemplateMembersCell(
              td,
              orderTemplateMembers(v, state.key, state.dir),
              TEMPLATE_MEMBER_RANK_FIELDS[state.key]
            );
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
            (col.key === "query_id" || col.key === "queryid")
          ) {
            const qid = col.key === "queryid" ? row.queryid : row.query_id;
            if (qid != null && String(qid).trim() !== "") {
              applyMonoTableCellClass(td, col);
              const a = el("a", {
                className: "ash-queryid-deeplink",
                href: buildAshQueryHref(qid),
                textContent: v === null || v === undefined ? "" : String(v),
              });
              a.addEventListener("click", (e) => {
                e.preventDefault();
                navigateToAshForQueryId(qid);
              });
              td.appendChild(a);
            } else {
              applyMonoTableCellClass(td, col);
              td.textContent = v === null || v === undefined ? "" : String(v);
            }
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
            const stmtLink = statementRowAshLink(row);
            if (pgssAshLinks && stmtLink != null) {
              appendQueryCellWithAshLinks(td, v, stmtLink.queryId, {
                canonicalize: stmtLink.canonicalize,
                dbname: stmtLink.dbname,
              });
            } else if (
              ashCellOpts &&
              ashCellOpts.ashQueryTextLinks &&
              qid != null &&
              String(qid).trim() !== ""
            ) {
              appendQueryCellWithAshLinks(td, v, qid, ashFamilyLinkOptions(row, ashCellOpts));
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
          } else if (col.key === "qpm_plans") {
            appendQpmPlansCell(td, row);
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
          } else if (col.key === "query_members") {
            appendAshTemplateMembersCell(
              td,
              orderTemplateMembers(v, state.key, state.dir),
              TEMPLATE_MEMBER_RANK_FIELDS[state.key]
            );
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
            (col.key === "query_id" || col.key === "queryid")
          ) {
            const qid = col.key === "queryid" ? row.queryid : row.query_id;
            if (qid != null && String(qid).trim() !== "") {
              applyMonoTableCellClass(td, col);
              const a = el("a", {
                className: "ash-queryid-deeplink",
                href: buildAshQueryHref(qid),
                textContent: v === null || v === undefined ? "" : String(v),
              });
              a.addEventListener("click", (e) => {
                e.preventDefault();
                navigateToAshForQueryId(qid);
              });
              td.appendChild(a);
            } else {
              applyMonoTableCellClass(td, col);
              td.textContent = v === null || v === undefined ? "" : String(v);
            }
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

  // Strip /* ... */ comments except planner hints (pg_hint_plan / YSQL /*+ ... */), which can
  // change the chosen plan and must keep templates distinct. Kept identical to Python
  // _REWRITE_COMMENT_RE.
  const HIST_REWRITE_COMMENT_RE = /\/\*(?!\+)[\s\S]*?\*\//g;
  // Collapse only value-list `IN (...)` (literals / `$N`), never a subquery `IN (SELECT ...)`.
  const HIST_IN_LIST_RE = /\bIN\s*\((?!\s*SELECT\b)[^)]*\)/gi;
  // Bulk VALUES row-list — collapse `VALUES (...),(...),...` (any row count, one level of nested
  // parens allowed per row) to a canonical `VALUES (...)`. Kept identical to the Python
  // _VALUES_LIST_RE so grouping matches across the CLI and every panel.
  const HIST_VALUES_LIST_RE = /\bVALUES\s*\((?:[^()]|\([^()]*\))*\)(?:\s*,\s*\((?:[^()]|\([^()]*\))*\))*/gi;
  const HIST_PLACEHOLDER_RE = /\$\d+/g;

  function normalizeQueryTemplate(query) {
    if (!query) return "";
    let q = String(query).replace(HIST_REWRITE_COMMENT_RE, "");
    q = q.replace(HIST_IN_LIST_RE, "IN (...)");
    q = q.replace(HIST_VALUES_LIST_RE, "VALUES (...)");
    q = q.replace(HIST_PLACEHOLDER_RE, "$N");
    q = q.replace(/\s+/g, " ").trim();
    return q;
  }

  /** Template identity for viewer grouping — full normalize when Merge similar SQL is on. */
  function queryTemplateKey(query) {
    if (!mergeSimilarSql) {
      if (!query) return "";
      return String(query).replace(/\s+/g, " ").trim();
    }
    return normalizeQueryTemplate(query);
  }

  function canonicalStatementFamilyKey(source, template, dbname) {
    return `${source}\0${template}\0${source === "ysql" ? String(dbname || "") : ""}`;
  }

  /**
   * Snapshot-local query_id ↔ canonical-family index. YSQL families retain dbname because the
   * statement Top 25 intentionally does not merge the same query shape across databases.
   */
  function buildCanonicalStatementFamilyIndex(doc) {
    const families = new Map();
    const byQueryId = new Map();

    function addRows(source, rows) {
      (rows || []).forEach((row) => {
        const template = normalizeQueryTemplate(row.query);
        const qid =
          row.queryid != null && row.queryid !== undefined ? String(row.queryid).trim() : "";
        if (!template || !qid) return;
        const dbname =
          source === "ysql" && row.dbname != null && String(row.dbname).trim() !== ""
            ? String(row.dbname).trim()
            : "";
        const familyKey = canonicalStatementFamilyKey(source, template, dbname);
        if (!families.has(familyKey)) {
          families.set(familyKey, {
            key: familyKey,
            source,
            template,
            dbname: dbname || null,
            queryIds: new Set(),
            total_ms: 0,
          });
        }
        const family = families.get(familyKey);
        family.queryIds.add(qid);
        family.total_ms += Number(row.total_ms) || 0;
        if (!byQueryId.has(qid)) byQueryId.set(qid, []);
        byQueryId.get(qid).push(family);
      });
    }

    const pgPer = doc && doc.pg_stat_statements && doc.pg_stat_statements.per_node;
    const ycqlPer = doc && doc.ycql_stat_statements && doc.ycql_stat_statements.per_node;
    addRows("ysql", pgPer ? mergeStatements(pgPer) : []);
    addRows("ycql", ycqlPer ? mergeYcqlStatements(ycqlPer) : []);
    return { families, byQueryId };
  }

  function resolveCanonicalStatementFamily(index, queryId, dbname) {
    const qid = queryId != null ? String(queryId).trim() : "";
    if (!qid || !index || !index.byQueryId) return null;
    let candidates = (index.byQueryId.get(qid) || []).slice();
    const wantedDb = dbname != null ? String(dbname).trim() : "";
    if (wantedDb) {
      const sameDb = candidates.filter(
        (family) => family.source === "ysql" && String(family.dbname || "") === wantedDb
      );
      if (sameDb.length) candidates = sameDb;
    }
    candidates.sort((a, b) => (Number(b.total_ms) || 0) - (Number(a.total_ms) || 0));
    return candidates[0] || null;
  }

  function statementRowMatchesCanonicalFamily(row, family) {
    if (!row || !family) return false;
    if (normalizeQueryTemplate(row.query) !== family.template) return false;
    if (family.source !== "ysql") return true;
    const dbname =
      row.dbname != null && row.dbname !== undefined ? String(row.dbname).trim() : "";
    return dbname === String(family.dbname || "");
  }

  // --- Shared query-template grouping for the statement/ASH panels ------------------------------
  // These reuse queryTemplateKey (normalizeQueryTemplate when Merge similar SQL is on). The
  // browser and CLI use the same rules, so a template collapses the same way in every panel.

  /**
   * Collapse merged statement rows (pg_stat / ycql shape, carrying `_deltaSrc`) into one row per
   * query template (and dbname, when present). Aggregates the raw totals so the row keeps the exact
   * same shape as mergeStatements() output (including a fresh `_deltaSrc` and recomputed per-call
   * fields), which lets it flow through the existing delta / time-% pipeline unchanged. The stable
   * identity (`queryid` = the template text, `dbname` preserved) makes delta keying line up across
   * snapshots.
   */
  function collapseStatementsByTemplate(mergedRows) {
    const hasRows = (mergedRows || []).some((r) => Object.prototype.hasOwnProperty.call(r, "rows"));
    const hasDbname = (mergedRows || []).some((r) =>
      Object.prototype.hasOwnProperty.call(r, "dbname")
    );
    const hasPrepared = (mergedRows || []).some((r) =>
      Object.prototype.hasOwnProperty.call(r, "is_prepared")
    );
    const groups = new Map();
    (mergedRows || []).forEach((r) => {
      const key = queryTemplateKey(r.query);
      if (!key) return;
      const db =
        r.dbname != null && String(r.dbname).trim() !== "" ? String(r.dbname).trim() : "";
      const groupKey = hasDbname ? `${key}\0${db}` : key;
      if (!groups.has(groupKey)) groups.set(groupKey, { key: key, dbname: db, members: [] });
      groups.get(groupKey).members.push(r);
    });
    const out = [];
    groups.forEach((g) => {
      const key = g.key;
      const members = g.members;
      if (!key) return;
      let calls = 0;
      let exec = 0;
      let rows = 0;
      const doc = {};
      const dbs = new Set();
      const queryids = [];
      let anyPrepared = false;
      // Heaviest member with a real id: the collapsed row's `queryid` is the template text, so ASH
      // deep links need a statement that actually exists to scope to.
      let primaryQueryid = null;
      let primaryMs = -1;
      members.forEach((m) => {
        const s = deltaSrcFromRowFallback(m);
        calls += Number(s.calls) || 0;
        exec += Number(s.total_exec_time) || 0;
        const memberMs = Number(s.total_exec_time) || 0;
        if (memberMs > primaryMs && m.queryid != null && String(m.queryid).trim() !== "") {
          primaryQueryid = String(m.queryid);
          primaryMs = memberMs;
        }
        if (s.rows != null) rows += Number(s.rows) || 0;
        if (s.doc) {
          Object.keys(s.doc).forEach((k) => {
            doc[k] = (doc[k] || 0) + (Number(s.doc[k]) || 0);
          });
        }
        if (m.dbname != null && String(m.dbname).trim() !== "") dbs.add(String(m.dbname).trim());
        if (m.queryid != null) queryids.push(String(m.queryid));
        if (ycqlPreparedTruthy(m.is_prepared)) anyPrepared = true;
      });
      const row = {
        queryid: key,
        query: key,
        calls: calls,
        total_ms: Math.round(exec * 100) / 100,
        mean_ms: calls ? Math.round((exec / calls) * 100) / 100 : 0,
        _tmpl_member_count: members.length,
        _tmpl_queryids: queryids,
        _tmpl_primary_queryid: primaryQueryid,
      };
      if (hasDbname) row.dbname = g.dbname || null;
      if (hasPrepared) row.is_prepared = anyPrepared;
      if (hasRows) {
        row.rows = Math.round(rows * 100) / 100;
        row.rows_per_call = calls ? Math.round((rows / calls) * 100) / 100 : 0;
      }
      Object.keys(doc).forEach((k) => {
        row[`${k}_per_call`] = calls ? Math.round((doc[k] / calls) * 100) / 100 : 0;
      });
      const deltaSrc = { calls: calls, total_exec_time: exec, doc: doc };
      if (hasRows) deltaSrc.rows = rows;
      row._deltaSrc = deltaSrc;
      out.push(row);
    });
    out.sort((a, b) => b.total_ms - a.total_ms);
    return out;
  }

  /** Summary rows (member_count > 1) for the statement panels' "Recurring query templates" table. */
  function statementTemplateSummaryRows(rows) {
    const hasDbname = (rows || []).some((r) => Object.prototype.hasOwnProperty.call(r, "dbname"));
    const groups = new Map();
    (rows || []).forEach((r) => {
      const template = queryTemplateKey(r.query);
      if (!template) return;
      const dbname =
        hasDbname && r.dbname != null && String(r.dbname).trim() !== ""
          ? String(r.dbname).trim()
          : "";
      const key = hasDbname ? `${template}\0${dbname}` : template;
      if (!groups.has(key)) {
        groups.set(key, {
          query_template: template,
          template: template,
          dbname: hasDbname ? dbname || null : undefined,
          calls: 0,
          calls_per_sec: 0,
          total_ms: 0,
          _members: new Map(),
        });
      }
      const g = groups.get(key);
      g.calls += Number(r.calls) || 0;
      g.calls_per_sec += Number(r.calls_per_sec) || 0;
      g.total_ms += Number(r.total_ms) || 0;
      const qid = r.queryid != null && r.queryid !== undefined ? String(r.queryid).trim() : "";
      if (!qid) return;
      if (!g._members.has(qid)) {
        g._members.set(qid, {
          query_id: qid,
          query: r.query != null ? String(r.query) : "",
          total_ms: 0,
          calls: 0,
          calls_per_sec: 0,
        });
      }
      const member = g._members.get(qid);
      member.total_ms += Number(r.total_ms) || 0;
      member.calls += Number(r.calls) || 0;
      member.calls_per_sec += Number(r.calls_per_sec) || 0;
      if (!member.query && r.query) member.query = String(r.query);
    });
    const all = Array.from(groups.values());
    const totalMs = all.reduce((s, g) => s + g.total_ms, 0);
    const summary = all
      .map((g) => {
        const queryMembers = Array.from(g._members.values())
          .sort(
            (a, b) =>
              b.total_ms - a.total_ms || String(a.query_id).localeCompare(String(b.query_id))
          )
          .map((member, i) => ({
            ...member,
            total_ms: Math.round(member.total_ms * 100) / 100,
            calls_per_sec: Math.round(member.calls_per_sec * 100) / 100,
            rank: i + 1,
          }));
        const row = {
          query_template: g.query_template,
          template: g.template,
          query: g.template,
          queryid: queryMembers.length ? queryMembers[0].query_id : null,
          members: queryMembers.length,
          calls: g.calls,
          calls_per_sec: Math.round(g.calls_per_sec * 100) / 100,
          total_ms: Math.round(g.total_ms * 100) / 100,
          time_pct: totalMs > 0 ? Math.round(10000 * (g.total_ms / totalMs)) / 100 : 0,
          query_members: queryMembers,
          queryids: queryMembers.map((m) => m.query_id).join(", "),
        };
        if (hasDbname) row.dbname = g.dbname != null ? g.dbname : null;
        return row;
      })
      .filter((g) => g.members > 1)
      .sort((a, b) => b.total_ms - a.total_ms);
    return summary;
  }

  // Sort/metric columns stay on the left (matching the panel's original layout); canonical query
  // and the ranked member list follow.
  function statementTemplateSummaryColumns(opts) {
    const options = opts || {};
    const cols = [];
    if (options.callsPerSec) {
      cols.push({ key: "calls_per_sec", label: "calls/s", type: "number", align: "right" });
    } else {
      cols.push({ key: "calls", label: "calls", type: "number", align: "right" });
    }
    cols.push(
      { key: "total_ms", label: "total time (ms)", type: "number", align: "right" },
      { key: "time_pct", label: "time %", type: "number", align: "right" }
    );
    if (options.dbname) {
      cols.push({ key: "dbname", label: "dbname" });
    }
    cols.push(
      { key: "query", label: "canonical query" },
      { key: "query_members", label: "member queryids (ranked)", sortable: false }
    );
    return cols;
  }

  /** Sort of the "Recurring query templates" tables — mirrors the statement panel (total time desc). */
  const STATEMENT_TEMPLATE_SUMMARY_SORT = { key: "total_ms", dir: "desc" };

  function relabelQueryColumn(cols, label) {
    return (cols || []).map((c) => (c.key === "query" ? Object.assign({}, c, { label }) : c));
  }

  function applyCanonicalizedQueryText(rows) {
    (rows || []).forEach((r) => {
      const key = queryTemplateKey(r.query);
      if (key) r.query = key;
    });
    return rows;
  }

  /** Columns for a template-collapsed statement table: drop per-statement id, keep dbname. */
  function groupedStatementDisplayColumns(baseCols) {
    return relabelQueryColumn(
      (baseCols || []).filter((c) => c.key !== "queryid"),
      "canonical query"
    );
  }

  /** ASH sample grouping by normalized query template; sums samples, counts distinct query_ids. */
  function groupAshByTemplate(rows) {
    const m = new Map();
    (rows || []).forEach((r) => {
      const q = r.query != null && r.query !== undefined ? String(r.query) : "";
      const key = queryTemplateKey(q) || "\0__no_query__";
      if (!m.has(key)) {
        m.set(key, {
          query_template: key === "\0__no_query__" ? "" : key,
          query: key === "\0__no_query__" ? "" : key,
          query_id: r.query_id != null && r.query_id !== undefined ? r.query_id : null,
          samples: 0,
          _members: new Map(),
        });
      }
      const ent = m.get(key);
      ent.samples += Number(r.samples) || 0;
      if ((ent.query_id == null || ent.query_id === "") && r.query_id != null) ent.query_id = r.query_id;
      const qid =
        r.query_id != null && r.query_id !== undefined ? String(r.query_id).trim() : "";
      if (qid) {
        if (!ent._members.has(qid)) {
          ent._members.set(qid, { query_id: qid, query: q, samples: 0 });
        }
        const member = ent._members.get(qid);
        member.samples += Number(r.samples) || 0;
        if (!member.query && q) member.query = q;
      }
    });
    return Array.from(m.values())
      .map((e) => {
        const queryMembers = Array.from(e._members.values())
          .sort(
            (a, b) =>
              b.samples - a.samples || String(a.query_id).localeCompare(String(b.query_id))
          )
          .map((member, i) => ({ ...member, rank: i + 1 }));
        return {
          query_template: e.query_template,
          query: e.query,
          query_id: queryMembers.length ? queryMembers[0].query_id : e.query_id,
          samples: e.samples,
          members: queryMembers.length || 1,
          query_members: queryMembers,
        };
      })
      .sort((a, b) => b.samples - a.samples);
  }

  /** Small reusable labeled checkbox (recurring-templates visibility, etc.). */
  function buildGroupByTemplateControl(checked, onChange, opts) {
    const options = opts || {};
    const disabled = !!options.disabled;
    const wrap = el("label", {
      className: "tmpl-group-control" + (disabled ? " tmpl-group-control--disabled" : ""),
    });
    const chk = el("input", { type: "checkbox" });
    chk.checked = !!checked && !disabled;
    chk.disabled = disabled;
    if (disabled) {
      wrap.title = options.disabledTitle || "Turn on Merge similar SQL first.";
    }
    chk.addEventListener("change", () => onChange(chk.checked));
    wrap.appendChild(chk);
    wrap.appendChild(el("span", { textContent: options.label || "Toggle" }));
    return wrap;
  }

  function buildShowRecurringTemplatesControl(checked, onChange) {
    return buildGroupByTemplateControl(checked, onChange, {
      disabled: !mergeSimilarSql,
      label: "Show recurring query templates",
      disabledTitle: "Turn on Merge similar SQL to show recurring query templates.",
    });
  }

  /** Viewer-wide Merge similar SQL toggle. */
  function buildMergeSimilarSqlControl(title) {
    const wrap = el("label", { className: "tmpl-group-control" });
    wrap.title =
      title || "When on, IN-lists, VALUES lists, and bind position variables are collapsed";
    const chk = el("input", { type: "checkbox" });
    chk.checked = !!mergeSimilarSql;
    chk.addEventListener("change", () => {
      mergeSimilarSql = chk.checked;
      // Family ASH still needs Merge on; do not record this click as the value to restore.
      syncMergeSimilarSqlForFamilyScope();
      if (!mergeSimilarSql) {
        showRecurringTemplates = false;
      }
      writeViewerStateToUrl();
      if (lastDoc) renderDoc(lastDoc, lastPrevDoc);
    });
    wrap.appendChild(chk);
    wrap.appendChild(el("span", { textContent: "Merge similar SQL" }));
    return wrap;
  }

  function groupByTemplate(results) {
    const groups = new Map();
    results.forEach((r) => {
      const key = queryTemplateKey(r.query);
      r.query_template = key;
      if (!key) return;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(r);
    });
    const summaries = [];
    groups.forEach((members, key) => {
      // Same ordering as rankByConfidence, computed from the raw signals so member ranks stay
      // stable no matter which rows the tier / flagged-only filters are currently showing.
      members.sort((a, b) => {
        const da = a.dip_p == null ? 1 : a.dip_p;
        const db = b.dip_p == null ? 1 : b.dip_p;
        if (da !== db) return da - db;
        const bcDiff = (b.bc || 0) - (a.bc || 0);
        if (bcDiff) return bcDiff;
        return String(a.queryid).localeCompare(String(b.queryid));
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
      const queryMembers = members.map((r, i) => ({
        query_id: r.queryid != null && r.queryid !== undefined ? String(r.queryid) : "",
        query: r.query != null ? String(r.query) : "",
        dbname: r.dbname != null && String(r.dbname).trim() !== "" ? String(r.dbname).trim() : null,
        calls: r.calls,
        tier: r.confidence_tier || "not_flagged",
        rank: i + 1,
      })).filter((m) => m.query_id);
      // best_confidence_rank comes from rankByConfidence over the full analyzed set (caller
      // stamps it before grouping); do not fall back to 0 — that sorted as missing (1e9).
      const bestRank = best.confidence_rank;
      summaries.push({
        query_template: key,
        template: key,
        member_count: members.length,
        members: queryMembers.length || members.length,
        best_confidence_rank: bestRank != null && bestRank !== "" ? Number(bestRank) : 1e9,
        best_confidence_tier: best.confidence_tier || "not_flagged",
        queryids: queryMembers.map((m) => m.query_id),
        query_members: queryMembers,
        dbname: best.dbname != null && String(best.dbname).trim() !== "" ? String(best.dbname).trim() : null,
        peak_counts: Array.from(peakSet).sort((a, b) => a - b),
        // Best member's full adjacent-pair list (display); ranges span every pair.
        peak_pairs: (best.peak_pairs || []).slice(),
        gap_ms_range: gapMs.length ? [Math.min(...gapMs), Math.max(...gapMs)] : null,
        gap_ratio_range: gapRatio.length ? [Math.min(...gapRatio), Math.max(...gapRatio)] : null,
      });
    });
    summaries.sort((a, b) => a.best_confidence_rank - b.best_confidence_rank);
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
    // Ranking and template grouping happen in the panel: groups use the full analyzed
    // set so "best" ranks stay stable under min-tier / flagged-only; the table is then
    // re-ranked among the visible rows (that visible re-rank matches the Python report).
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
    const state = { minTier: latencyMinTier, flaggedOnly: latencyFlaggedOnly };

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
    flagChk.checked = !!state.flaggedOnly;
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
    panel.appendChild(buildMergeSimilarSqlControl());
    panel.appendChild(
      buildShowRecurringTemplatesControl(mergeSimilarSql && showRecurringTemplates, (v) => {
        showRecurringTemplates = v;
        writeViewerStateToUrl();
        if (lastDoc) renderDoc(lastDoc, lastPrevDoc);
      })
    );

    const legend = el("div", { className: "latency-legend" });
    legend.appendChild(
      el("div", {
        className: "latency-legend-title",
        textContent: "Reading the columns",
      })
    );
    const legendRows = [
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
    ];
    legendRows.forEach(([k, v]) => {
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
      // Stamp confidence_rank on every analyzed statement first so template "best" ranks stay
      // stable when tier / flagged-only filters hide some members. The filtered table below is
      // re-ranked among the visible rows only.
      rankByConfidence(analysis.results);
      const groups = groupByTemplate(analysis.results);

      let rows = analysis.results.slice();
      if (state.flaggedOnly) rows = rows.filter((r) => r.flag);
      if (state.minTier !== "all") {
        const th = HIST_TIER_RANK[state.minTier] || 0;
        rows = rows.filter((r) => (HIST_TIER_RANK[r.confidence_tier] || 0) >= th);
      }
      rows = rankByConfidence(rows);
      const shownTemplates = new Set(
        rows.map((r) => r.query_template || queryTemplateKey(r.query)).filter(Boolean)
      );
      // Without Merge similar SQL a "template" is just the exact SQL text, so the recurring
      // rollup is suppressed rather than shown as singletons.
      const recurring = mergeSimilarSql
        ? groups.filter(
            (g) =>
              (g.member_count || g.members || 0) > 1 &&
              g.query_template &&
              shownTemplates.has(g.query_template)
          )
        : [];

      const displayRows = rows.map((r) => ({
        tier: r.confidence_tier,
        rank: r.confidence_rank,
        calls: r.calls,
        bc: r.bc != null ? round4(r.bc) : "",
        dip_p: r.dip_p == null ? "—" : histFmt(r.dip_p, 4),
        peaks: r.n_raw_peaks,
        spread: histSpreadStr(r),
        gap: histGapStr(r),
        query: mergeSimilarSql ? queryTemplateKey(r.query) || r.query : r.query,
        queryid: r.queryid,
        dbname: r.dbname,
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
        { key: "query", label: mergeSimilarSql ? "canonical query" : "query" },
        { key: "queryid", label: "queryid" }
      );
      tableHolder.textContent = "";
      const total = analysis.results.length;
      const flaggedN = analysis.results.filter((r) => r.flag).length;
      tableHolder.appendChild(
        buildSortableTable(
          `Latency modes — ${displayRows.length} shown (${flaggedN} flagged / ${total} analyzed)`,
          displayRows,
          cols,
          "sec-latency-main",
          {
            ashQueryTextLinks: true,
            ashQueryIdLinks: true,
            canonicalizeFamily: !!mergeSimilarSql,
          }
        )
      );

      groupHolder.textContent = "";
      if (showRecurringTemplates && recurring.length) {
        const grpRows = recurring.map((g) => ({
          best_tier: g.best_confidence_tier,
          peaks: (g.peak_counts || []).join(",") || "",
          gap: histGapStr(g),
          query: g.template,
          queryid: (g.queryids && g.queryids[0]) || null,
          dbname: g.dbname || (g.query_members && g.query_members[0] && g.query_members[0].dbname) || null,
          query_members: g.query_members,
        }));
        const grpCols = [
          {
            key: "best_tier",
            label: "best tier",
            type: "number",
            sortValue: (r) => HIST_TIER_RANK[r.best_tier] || 0,
          },
          { key: "peaks", label: "peaks" },
          { key: "gap", label: "gap" },
          { key: "query", label: "canonical query" },
          { key: "query_members", label: "member queryids (ranked)", sortable: false },
        ];
        groupHolder.appendChild(
          buildSortableTable(
            `Recurring query templates (${recurring.length})`,
            grpRows,
            grpCols,
            "sec-latency-groups",
            { ashQueryTextLinks: true, canonicalizeFamily: true }
          )
        );
      }
    }

    tierSel.addEventListener("change", () => {
      latencyMinTier = tierSel.value;
      state.minTier = latencyMinTier;
      writeViewerStateToUrl();
      rerender();
    });
    flagChk.addEventListener("change", () => {
      latencyFlaggedOnly = flagChk.checked;
      state.flaggedOnly = latencyFlaggedOnly;
      writeViewerStateToUrl();
      rerender();
    });
    dipChk.addEventListener("change", () => {
      latencyShowDipP = dipChk.checked;
      writeViewerStateToUrl();
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
    const canonicalFamilyIndex = buildCanonicalStatementFamilyIndex(doc);
    const canonicalFamily =
      ashQueryIdFilter && ashCanonicalizeFilter
        ? resolveCanonicalStatementFamily(
            canonicalFamilyIndex,
            ashQueryIdFilter,
            ashCanonicalDbnameFilter
          )
        : null;
    canonicalFamilyResolved = !!canonicalFamily;
    syncMergeSimilarSqlForFamilyScope();

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
      const isDelta = !!(prevDoc && prevSt);
      const grouped = mergeSimilarSql;
      const pgSort = { key: "total_ms", dir: "desc" };

      if (isDelta) {
        panelPgss.appendChild(
          pgStatActivityBannerDelta(prevDoc.generated_at_utc, doc.generated_at_utc, curFile)
        );
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

      // Ungrouped display rows (used for the recurring-templates summary and the ungrouped table).
      let baseRows;
      if (isDelta) {
        baseRows = withPgStatDeltaDerivedRows(
          deltaPgStatMergedRows(merged, mergeStatements(prevSt)),
          prevDoc.generated_at_utc,
          doc.generated_at_utc
        );
      } else {
        baseRows = withPgStatTimePercent(merged);
      }

      panelPgss.appendChild(buildMergeSimilarSqlControl());
      panelPgss.appendChild(
        buildShowRecurringTemplatesControl(mergeSimilarSql && showRecurringTemplates, (v) => {
          showRecurringTemplates = v;
          writeViewerStateToUrl();
          if (lastDoc) renderDoc(lastDoc, lastPrevDoc);
        })
      );

      // "plans" column appears only once collection has produced plans for this snapshot.
      const pgssQpm = doc.yb_pg_stat_plans;
      const pgssShowPlans = qpmPanelMode(pgssQpm) === "plans";
      let pgSummary = mergeSimilarSql ? statementTemplateSummaryRows(baseRows) : [];
      const pgssPinned = pgssShowPlans
        ? qpmEffectivePinned(pgssQpm, Date.parse(doc.generated_at_utc || ""))
        : null;
      if (pgssShowPlans) pgSummary = annotateRowsWithQpmPlans(pgSummary, pgssQpm, pgssPinned);
      if (showRecurringTemplates && pgSummary.length) {
        const summaryCols = statementTemplateSummaryColumns({
          callsPerSec: isDelta,
          dbname: pgSummary.some((r) => Object.prototype.hasOwnProperty.call(r, "dbname")),
        });
        panelPgss.appendChild(
          buildSortableTable(
            `Recurring query templates (${pgSummary.length})`,
            pgSummary,
            pgssShowPlans ? withQpmPlansColumn(summaryCols) : summaryCols,
            "sec-pgss-templates",
            { ashQueryTextLinks: true, canonicalizeFamily: true },
            STATEMENT_TEMPLATE_SUMMARY_SORT
          )
        );
      }

      let pgTitle;
      let pgRows;
      let pgCols;
      if (grouped) {
        if (isDelta) {
          const collapsedCur = collapseStatementsByTemplate(merged);
          const collapsedPrev = collapseStatementsByTemplate(mergeStatements(prevSt));
          const memberByKey = new Map(
            collapsedCur.map((r) => [statementMergeKey(r), r._tmpl_member_count])
          );
          const primaryByKey = new Map(
            collapsedCur.map((r) => [statementMergeKey(r), r._tmpl_primary_queryid])
          );
          pgRows = withPgStatDeltaDerivedRows(
            deltaPgStatMergedRows(collapsedCur, collapsedPrev),
            prevDoc.generated_at_utc,
            doc.generated_at_utc
          );
          pgRows.forEach((r) => {
            r._tmpl_member_count = memberByKey.get(statementMergeKey(r)) || 1;
            r._tmpl_primary_queryid = primaryByKey.get(statementMergeKey(r)) || null;
          });
          applyCanonicalizedQueryText(pgRows);
          pgCols = groupedStatementDisplayColumns(pgStatStatementColumnsDelta(pgRows, st));
          pgTitle = "Top 25 — pg_stat_statements (Δ vs prior snapshot)";
        } else {
          pgRows = withPgStatTimePercent(collapseStatementsByTemplate(merged));
          applyCanonicalizedQueryText(pgRows);
          pgCols = groupedStatementDisplayColumns(pgStatStatementColumns(pgRows, st));
          pgTitle = "Top 25 — pg_stat_statements";
        }
      } else {
        pgRows = baseRows;
        const pgBaseCols = isDelta
          ? pgStatStatementColumnsDelta(pgRows, st)
          : pgStatStatementColumns(pgRows, st);
        if (mergeSimilarSql) {
          applyCanonicalizedQueryText(pgRows);
          pgCols = relabelQueryColumn(pgBaseCols, "canonical query");
        } else {
          pgCols = pgBaseCols;
        }
        pgTitle = isDelta
          ? "Top 25 — pg_stat_statements (Δ vs prior snapshot)"
          : "Top 25 — pg_stat_statements";
      }

      if (pgssShowPlans) {
        pgRows = annotateRowsWithQpmPlans(
          pgRows, pgssQpm, pgssPinned, mergeSimilarSql ? qpmTemplateMembers(merged) : null
        );
        pgCols = withQpmPlansColumn(pgCols);
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
      const isDelta = !!(prevDoc && prevYcqlSt);
      const grouped = mergeSimilarSql;
      const ycqlSort = { key: "total_ms", dir: "desc" };

      if (isDelta) {
        panelYcql.appendChild(
          pgStatActivityBannerDelta(prevDoc.generated_at_utc, doc.generated_at_utc, curFile)
        );
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

      let baseRows;
      if (isDelta) {
        const mergedYcqlPrev = mergeYcqlStatements(prevYcqlSt);
        const prepByKey = new Map(mergedYcql.map((r) => [statementMergeKey(r), !!r.is_prepared]));
        const deltaYcqlRows = deltaPgStatMergedRows(mergedYcql, mergedYcqlPrev).map((r) => ({
          ...r,
          is_prepared: prepByKey.get(statementMergeKey(r)) || false,
        }));
        baseRows = withPgStatDeltaDerivedRows(
          deltaYcqlRows,
          prevDoc.generated_at_utc,
          doc.generated_at_utc
        );
      } else {
        baseRows = withPgStatTimePercent(mergedYcql);
      }

      panelYcql.appendChild(
        buildMergeSimilarSqlControl(
          "YCQL uses ? bind markers, so $N collapsing does not apply, and CQL has no multi-row VALUES lists; IN (...) lists, single-row VALUES, and comments still collapse"
        )
      );
      panelYcql.appendChild(
        buildShowRecurringTemplatesControl(mergeSimilarSql && showRecurringTemplates, (v) => {
          showRecurringTemplates = v;
          writeViewerStateToUrl();
          if (lastDoc) renderDoc(lastDoc, lastPrevDoc);
        })
      );

      const ycqlSummary = mergeSimilarSql ? statementTemplateSummaryRows(baseRows) : [];
      if (showRecurringTemplates && ycqlSummary.length) {
        panelYcql.appendChild(
          buildSortableTable(
            `Recurring query templates (${ycqlSummary.length})`,
            ycqlSummary,
            statementTemplateSummaryColumns({ callsPerSec: isDelta }),
            "sec-ycql-templates",
            { ashQueryTextLinks: true, canonicalizeFamily: true },
            STATEMENT_TEMPLATE_SUMMARY_SORT
          )
        );
      }

      let ycqlTitle;
      let ycqlRows;
      let ycqlCols;
      if (grouped) {
        if (isDelta) {
          const collapsedCur = collapseStatementsByTemplate(mergedYcql);
          const collapsedPrev = collapseStatementsByTemplate(mergeYcqlStatements(prevYcqlSt));
          const prepByKey = new Map(
            collapsedCur.map((r) => [statementMergeKey(r), !!r.is_prepared])
          );
          const memberByKey = new Map(
            collapsedCur.map((r) => [statementMergeKey(r), r._tmpl_member_count])
          );
          const primaryByKey = new Map(
            collapsedCur.map((r) => [statementMergeKey(r), r._tmpl_primary_queryid])
          );
          ycqlRows = withPgStatDeltaDerivedRows(
            deltaPgStatMergedRows(collapsedCur, collapsedPrev).map((r) => ({
              ...r,
              is_prepared: prepByKey.get(statementMergeKey(r)) || false,
            })),
            prevDoc.generated_at_utc,
            doc.generated_at_utc
          );
          ycqlRows.forEach((r) => {
            r._tmpl_member_count = memberByKey.get(statementMergeKey(r)) || 1;
            r._tmpl_primary_queryid = primaryByKey.get(statementMergeKey(r)) || null;
          });
          applyCanonicalizedQueryText(ycqlRows);
          ycqlCols = groupedStatementDisplayColumns(ycqlStatStatementColumnsDelta());
          ycqlTitle = "Top 25 — ycql_stat_statements (Δ vs prior snapshot)";
        } else {
          ycqlRows = withPgStatTimePercent(collapseStatementsByTemplate(mergedYcql));
          applyCanonicalizedQueryText(ycqlRows);
          ycqlCols = groupedStatementDisplayColumns(ycqlStatStatementColumns());
          ycqlTitle = "Top 25 — ycql_stat_statements";
        }
      } else {
        ycqlRows = baseRows;
        const ycqlBaseCols = isDelta
          ? ycqlStatStatementColumnsDelta()
          : ycqlStatStatementColumns();
        if (mergeSimilarSql) {
          applyCanonicalizedQueryText(ycqlRows);
          ycqlCols = relabelQueryColumn(ycqlBaseCols, "canonical query");
        } else {
          ycqlCols = ycqlBaseCols;
        }
        ycqlTitle = isDelta
          ? "Top 25 — ycql_stat_statements (Δ vs prior snapshot)"
          : "Top 25 — ycql_stat_statements";
      }

      panelYcql.appendChild(
        buildSortablePaginatedTable(
          ycqlTitle,
          ycqlRows,
          ycqlCols,
          25,
          "sec-ycql-main",
          ycqlSort,
          {
            unifyStatementHeaders: true,
            pgssAshLinks: true,
          }
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
      if (qF) {
        ashData = canonicalFamily
          ? filterAshPerNodeByCanonicalFamily(ashData, canonicalFamily)
          : filterAshPerNodeByQueryId(ashData, qF);
      }
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
      if (!qF) {
        // Outside a query drilldown the plan view and literals start over.
        qpmViewScope = null;
        qpmPlanView = "plans";
        qpmShowLiterals = false;
      }
      if (qF) {
        const qpmRowDb = !canonicalFamily && st ? mergedStatementRowForQuery(st, mergeStatements, qF) : null;
        const qpmDbname =
          canonicalFamily && canonicalFamily.dbname
            ? canonicalFamily.dbname
            : qpmRowDb && qpmRowDb.dbname
              ? qpmRowDb.dbname
              : null;
        // A different drilldown starts on its recorded plans, with placeholders. Keyed
        // on the URL alone: stepping Prev/Next must not reset it when a window lacks
        // the query, or when a family resolves in one window and not the next.
        const qpmScope = qpmViewScopeKey(qF, ashCanonicalizeFilter, ashCanonicalDbnameFilter);
        if (qpmViewScope !== null && qpmViewScope !== qpmScope) {
          qpmPlanView = "plans";
          qpmShowLiterals = false;
          writeViewerStateToUrl();
        }
        qpmViewScope = qpmScope;
        const qpmSec = doc && doc.yb_pg_stat_plans;
        let explainTarget = null;
        try {
          explainTarget =
            qpmPanelMode(qpmSec) === "plans"
              ? qpmExplainTarget(
                doc,
                qpmScopeQueryIds(qF, canonicalFamily),
                qpmPanelDbids(qpmSec, qpmDbname, qpmScopeQueryIds(qF, canonicalFamily))
              )
              : null;
        } catch (e) {
          // An optional aid: whatever the recorded text holds, the page still renders.
          explainTarget = { ok: false, reason: "could not read the recorded statement (" + (e.message || e) + ")" };
        }
        const qRaw = canonicalFamily ? canonicalFamily.template : getQueryTextForToolbar(doc, qF);
        const qText =
          qRaw != null && String(qRaw).trim() !== "" ? String(qRaw).trim() : "";
        const note = el("div", { className: "ash-mode-banner ash-mode-banner--scoped" });
        let ashQueryTitle = canonicalFamily
          ? `canonical family (${canonicalFamily.queryIds.size} query_ids); representative query_id=${qF}`
          : `query_id=${qF}`;
        if (canonicalFamily && canonicalFamily.dbname) {
          ashQueryTitle += `; dbname=${canonicalFamily.dbname}`;
        } else if (st) {
          const rowDb = mergedStatementRowForQuery(st, mergeStatements, qF);
          if (rowDb && rowDb.dbname) ashQueryTitle += `; dbname=${rowDb.dbname}`;
        }
        const titleRow = el("div", { className: "ash-mode-banner-title-row" });
        titleRow.appendChild(
          el("div", {
            className: "ash-mode-banner-title",
            textContent: ashQueryTitle,
          })
        );
        note.appendChild(titleRow);
        const row = el("div", { className: "ash-mode-banner-query-row" });
        const queryLabel = canonicalFamily ? "canonical query" : "query";
        const queryK = el("span", { className: "ash-mode-banner-query-k", textContent: queryLabel });
        row.appendChild(queryK);
        const queryV = el("span", {
          className: qText
            ? "ash-mode-banner-query-highlight"
            : "ash-mode-banner-query-highlight ash-mode-banner-query-highlight--empty",
          textContent: qText || "(no text in snapshot)",
        });
        row.appendChild(queryV);
        note.appendChild(row);
        const litScope =
          explainTarget && explainTarget.ok
            ? { dbid: explainTarget.dbid, queryIds: qpmRunQueryIds(Array.from(qpmScopeQueryIds(qF, canonicalFamily)), explainTarget) }
            : null;
        // The drilldown's primary action, where the eye lands: top right of the query.
        if (explainTarget) titleRow.appendChild(qpmExplainBannerControl(explainTarget, litScope));
        // Only statements with placeholders have anything to inline. The chip sits
        // by the label, so switching the text under it never moves it.
        if (explainTarget && (!explainTarget.ok || explainTarget.hasParams)) {
          const litNote = el("div", { className: "qpm-literals-note" });
          row.insertBefore(
            qpmLiteralsControl(queryV, queryK, litNote, qText, queryLabel, explainTarget, qF, litScope),
            queryV
          );
          note.appendChild(litNote);
        }
        appendAshScopedQueryStatementLines(note, doc, prevDoc, qF, ash, canonicalFamily);
        panelAsh.appendChild(note);
        const qpmPanel = qpmPlansPanel(
          doc,
          qF,
          canonicalFamily,
          ashSnapshotClusterNodeCount(doc, ash),
          currentIndex >= 0 && manifestEntries[currentIndex]
            ? manifestEntries[currentIndex].file
            : null,
          qpmDbname,
          explainTarget
        );
        if (qpmPanel) panelAsh.appendChild(qpmPanel);
      }
      const ashClusterNodes = ashSnapshotClusterNodeCount(doc, ash);
      const ashShowNodeLoadDist = ashClusterNodes > 1 && !nodeF;
      /* Same pg_stat query text as merged rows so namespace+query / ns+object+query load-dist keys match. */
      const flatAsh = enrichAshRowsQueryFromPgStat(doc, flattenAsh(ashData, topo));

      let mergedAshByQueryId = enrichAshRowsQueryFromPgStat(doc, mergeAsh(ashData));
      let mergedAsh = mergeSimilarSql
        ? collapseAshMergedByCanonicalQuery(mergedAshByQueryId)
        : mergedAshByQueryId;
      mergedAsh = attachAshNodeLoadDistribution(
        mergedAsh,
        flatAsh,
        mergeSimilarSql ? ashMergeKeyCanonical : ashMergeKey,
        ashShowNodeLoadDist,
        !mergeSimilarSql
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
        { key: "query", label: ashQueryColumnLabel() },
        { key: "query_id", label: "query_id" },
      ];
      const ashMainColsAll = tableF
        ? ashMainColsBase.filter((c) => c.key !== "namespace_name" && c.key !== "object_name")
        : ashMainColsBase;
      const ashMainColsStripped = qF
        ? canonicalFamily
          ? ashColumnsWithoutQuery(ashMainColsAll)
          : ashColumnsWithoutQueryIdAndQuery(ashMainColsAll)
        : ashMainColsAll;
      const ashMainCols = spliceAshNodeLoadDistributionColumn(
        ashMainColsStripped,
        ashClusterNodes,
        ashShowNodeLoadDist
      );
      const ashReportCellOpts = {
        ashObjectLinks: true,
        ashQueryIdLinks: true,
        ashQueryTextLinks: true,
        canonicalizeFamily: !!mergeSimilarSql,
      };
      const ashPaginatedOpts = { ashCellOpts: ashReportCellOpts };
      // Sort columns (Active Sessions/sec, Load %) stay on the left; canonical query and ranked
      // member query_ids follow.
      const ashTemplateSummaryCols = [
        ASH_SPS_COL,
        ASH_LOAD_COL,
        { key: "query", label: "canonical query" },
        { key: "query_members", label: "member query_ids (ranked)", sortable: false },
      ];

      // Recurring query templates for ASH. The wait-event Top 50 stays the main table; Merge similar SQL
      // only changes how that table groups query text.
      const byTemplateL = !qF
        ? withAshTemplateMemberRates(ashEnriched(groupAshByTemplate(mergedAshByQueryId)))
        : [];
      if (!qF) {
        panelAsh.appendChild(buildMergeSimilarSqlControl());
        panelAsh.appendChild(
          buildShowRecurringTemplatesControl(mergeSimilarSql && showRecurringTemplates, (v) => {
            showRecurringTemplates = v;
            writeViewerStateToUrl();
            if (lastDoc) renderDoc(lastDoc, lastPrevDoc);
          })
        );
        const ashTemplateSummary = mergeSimilarSql
          ? byTemplateL.filter(
              (r) => String(r.query_template || "").trim() !== "" && (Number(r.members) || 1) > 1
            )
          : [];
        if (showRecurringTemplates && ashTemplateSummary.length) {
          panelAsh.appendChild(
            buildSortableTable(
              `Recurring query templates (${ashTemplateSummary.length})`,
              ashTemplateSummary,
              ashTemplateSummaryCols,
              "sec-ash-templates",
              Object.assign({}, ashReportCellOpts, { canonicalizeFamily: true })
            )
          );
        }
      }

      {
        const ashMainTop50GroupLabel = qF
          ? canonicalFamily
            ? "Table/Index + Canonical Query + Wait_Event"
            : "Table/Index + Wait_Event"
          : tableF
          ? mergeSimilarSql
            ? "Canonical Query + Wait_Event"
            : "Query + Wait_Event"
          : mergeSimilarSql
          ? "Table/Index + Canonical Query + Wait_Event"
          : "Table/Index + Query + Wait_Event";
        let ashMainRows = mergedAshL;
        let ashMainDisplayCols = ashMainCols;
        panelAsh.appendChild(
          buildSortablePaginatedTable(
            `Top 50 Active Sessions/sec Grouped By: ${ashMainTop50GroupLabel}`,
            ashMainRows,
            ashMainDisplayCols,
            50,
            "sec-ash-main",
            undefined,
            ashPaginatedOpts
          )
        );
      }

      if (tableF) {
        let byQueryId = groupAshByQueryId(mergedAshByQueryId);
        byQueryId = attachAshNodeLoadDistribution(
          byQueryId,
          flatAsh,
          bucketKeyAshQueryIdFlat,
          ashShowNodeLoadDist
        );
        const byQueryIdL = ashEnriched(byQueryId);
        panelAsh.appendChild(
          buildSortableTable(
            `Active Sessions/Sec Grouped By: ${mergeSimilarSql ? "Canonical Query" : "Query"} (${byQueryIdL.length} groups)`,
            byQueryIdL,
            spliceAshNodeLoadDistributionColumn(
              [
                ASH_SPS_COL,
                ASH_LOAD_COL,
                { key: "query", label: ashQueryColumnLabel() },
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
        let byNsQuery = groupAshByNamespaceQuery(mergedAshByQueryId);
        byNsQuery = attachAshNodeLoadDistribution(
          byNsQuery,
          flatAsh,
          bucketKeyAshNamespaceQueryFlat,
          ashShowNodeLoadDist
        );
        const byNsQueryL = ashEnriched(byNsQuery);
        const byNsQueryCols = spliceAshNodeLoadDistributionColumn(
          [
            ASH_SPS_COL,
            ASH_LOAD_COL,
            { key: "namespace_name", label: "namespace" },
            {
              key: "query",
              label: mergeSimilarSql ? "canonical query" : "query",
            },
          ].concat(mergeSimilarSql ? [] : [{ key: "query_id", label: "query_id" }]),
          ashClusterNodes,
          ashShowNodeLoadDist
        );
        const byNsQueryTitle = `Active Sessions/Sec Grouped By: Database & ${mergeSimilarSql ? "Canonical Query" : "Query"} (${byNsQueryL.length} groups)`;
        panelAsh.appendChild(
          buildSortableTable(byNsQueryTitle, byNsQueryL, byNsQueryCols, "sec-ash-ns-q", ashReportCellOpts)
        );

        let byNsObjBuckets = ashAggregateNsObjectBuckets(mergedAshByQueryId);
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
        let byNsObjQuery = groupAshByNamespaceObjectQuery(mergedAshByQueryId, { ignoreQueryInKey: !!qF });
        byNsObjQuery = attachAshNodeLoadDistribution(
          byNsObjQuery,
          flatAsh,
          bucketKeyAshNsObjQueryFlatFactory(!!qF),
          ashShowNodeLoadDist
        );
        const byNsObjQueryL = ashEnriched(byNsObjQuery);
        const byNsObjQueryTitle = qF
          ? `Active Sessions/sec Grouped By: Database + Table/Index (${byNsObjQueryL.length} groups)`
          : `Active Sessions/Sec Grouped By: Table/Index & ${mergeSimilarSql ? "Canonical Query" : "Query"} (${byNsObjQueryL.length} groups)`;
        const byNsObjQueryColsAll = spliceAshNodeLoadDistributionColumn(
          [
            ASH_SPS_COL,
            ASH_LOAD_COL,
            { key: "namespace_name", label: "namespace" },
            { key: "object_name", label: "object_name" },
            { key: "query", label: ashQueryColumnLabel() },
            { key: "query_id", label: "query_id" },
          ],
          ashClusterNodes,
          ashShowNodeLoadDist
        );
        let byNsObjQueryCols = qF
          ? canonicalFamily
            ? ashColumnsWithoutQuery(byNsObjQueryColsAll)
            : ashColumnsWithoutQueryIdAndQuery(byNsObjQueryColsAll)
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
    writeViewerStateToUrl();
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
      // Another tab (or person) may have flipped plan collection; re-learn it here.
      qpmFetchCollectionState().then((st) => {
        if (st) qpmCollectionState = st;
      });
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
      // A modal is open (the EXPLAIN dialog): the snapshot behind it must not move.
      if (document.querySelector("dialog[open]")) return;
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
      // No `t` means "follow the newest" — do not leave an older window on
      // screen and then let renderDoc pin that older time onto this entry.
      const newest = manifestEntries.length ? manifestEntries.length - 1 : -1;
      if (!urlWindowKey) {
        if (newest >= 0 && currentIndex !== newest) {
          showSnapshotAt(newest);
          return;
        }
      } else {
        const target = indexForWindowKey(urlWindowKey);
        if (target >= 0 && target !== currentIndex) {
          showSnapshotAt(target);
          return;
        }
      }
      if (lastDoc) {
        renderDoc(lastDoc, lastPrevDoc);
      } else {
        updateAshFilterToolbar();
      }
    });
    qpmCollectionState = await qpmFetchCollectionState();
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
