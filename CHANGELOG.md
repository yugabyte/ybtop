# Changelog

All notable functional changes to **ybtop** are listed here by release. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) (newest first).

## [0.1.14] — 2026-11-05

### Added

- **MCP server for in-flight queries (opt-in):** **`ybtop serve --mcp`** (and **`ybtop watch --mcp`**, which embeds the same viewer) starts a [Model Context Protocol](https://modelcontextprotocol.io/) endpoint over **streamable HTTP** at **`http://127.0.0.1:8766/mcp`**, on its own port next to the viewer. Off unless `--mcp` is given. Needs the new extra (`pip3 install 'ybtop[mcp]'`, Python ≥ 3.10) and a YSQL connection: `serve` gains the usual **`--dsn` / `--host` / `--user` / `--password` / `--dbname`** flags (YSQL port is **`--db-port`** there, since `--port` is the viewer's HTTP port), used only with `--mcp`. Plain `serve` still needs no database.
- **Tools (all read-only):** `list_nodes`, **`active_summary`** (start here: backends by state/node/database, current wait events, longest-running queries, oldest open transactions, busiest query templates), **`list_active_queries`** (filters: min duration, database, user, application, node, `query_id`, SQL substring; includes `idle in transaction`), **`backend_detail`** (one node+pid: state, ASH wait profile, lock relationships), **`query_wait_profile`** (ASH wait events with YugabyteDB's descriptions, tables/indexes and nodes over the last 5–600 s; per `query_id` or workload-wide), **`query_stats`** (cluster-merged `pg_stat_statements` + approximate p50/p90/p99 from `yb_latency_histogram`), **`blocking_locks`** (lock-wait chains from `yb_lock_status()` joined to backends via `yb_backend_xid`, with root blockers ranked), **`long_transactions`**, **`recently_terminated_queries`** (`yb_terminated_queries`). Tools whose catalog objects are missing on older YugabyteDB versions return a clear "not available" error. A node that is unreachable is reported under `errors` instead of failing the call.
- **Security options:** **`--mcp-bind`** (default loopback `127.0.0.1`; on loopback the MCP SDK also validates the `Host` header against DNS rebinding) and **`--mcp-auth-token`** / **`YBTOP_MCP_TOKEN`** (when set, every request needs `Authorization: Bearer <token>`, checked in constant time; prefer the env var, since command-line arguments show up in the process list). Binding to a non-loopback address without a token prints a warning, because tool output contains SQL text with literal values.
- **Safety:** every MCP database session is **read-only** (`default_transaction_read_only`) with a **10 s `statement_timeout`**; ybtop's own backends are excluded from results via the existing `/* service:ybtop */` tag.

- **Diagnosis tools (find bottlenecks, slow-query causes, missing/broken indexes):** `cluster_bottlenecks`, `top_queries`, `find_full_scans`, `diagnose_query`, `explain_query`, `index_health`, `verify_index` (see the README's *MCP server* section). They combine `pg_stat_statements` (cluster-merged), ASH, recorded plans from **`yb_pg_stat_plans`** (including the parameters of each plan's slowest call), `EXPLAIN` (generic plan via `PREPARE` + `force_generic_plan` for `$N` statements; `analyze=true` only for SELECT/WITH/VALUES in the read-only session), `pg_index` / `pg_stat_user_indexes` and `yb_index_check`. Findings: full table scans with a candidate `CREATE INDEX` (expression index when the column is wrapped in a function; explicit "cast defeats the index" when a type mismatch is the cause), indexes that don't cover the whole predicate, scan ratio, conflict/read-restart retries, catalog waits, lock contention, plan flips between parameter values, never-analyzed tables, node imbalance, single-tablet tables and hot tablets, and broken / duplicate / redundant / unused indexes.
- **Design notes:** scan ratio compares DocDB rows scanned with rows delivered to the *client* (DocDB's own "returned" count hides predicates that can't be pushed down); full scans are read from plans because YugabyteDB leaves `pg_stat_user_tables.seq_scan` at 0; index usage counters are node-local and are summed across nodes; `index_health` credits the scans of an index slated for dropping to its replacement so the advice never removes every access path.
- **Selectivity guard:** a full scan whose result is ≥ 10% of the scanned rows is reported as a low-severity *broad scan* (reduce the work instead) rather than a missing index. Verified on a live cluster: the suggested index for a selective filter cut a query from ~230 ms / 200,000 rows scanned to ~6 ms / 200, while the expression index proposed for a one-third-of-the-table filter was ignored by the planner.
- **MCP SDK 2.x:** the server uses the `mcp` 2.x SDK (`MCPServer`) and the extra requires `mcp>=2,<3`; 1.x is not supported. The test suite passes on every 2.x release from 2.0.0 to 2.3.0. Unexpected tool errors are not shown to callers by the 2.x SDK, so ybtop raises its own `ToolError` for expected failures (bad arguments, unsupported features) and for database errors (first line only).
- **Multi-node behaviour:** tools fan out to every node from `yb_servers()` in parallel and merge the results (calls and index scans are summed; verified against per-node counters on a 3-node universe). An unreachable node is reported under `errors` while the rest still answer, and `index_health` warns when not every node reported usage. If the seed node is down, discovery and the seed-only paths (`explain_query`, `verify_index`, the capability probe, index catalog) fall back to the other known nodes and remember whichever answered last. Connections have a 5 s `connect_timeout` (unless the DSN sets one) so a dead host cannot hang a call. The seed must be reachable when ybtop starts, since that is how the node list is first learned.
- **Workaround for a YugabyteDB bug:** on 2026.1, `pg_class.relname = ANY(text[])` is pushed down to DocDB and fails with "compressed lz4 data is corrupt"; the catalog lookups cast to `text` to keep the filter in YSQL.

- **Docker:** a `Dockerfile` (multi-stage, non-root, pip extras via `EXTRAS` build arg) and a `docker-compose.yaml` that runs a single-node YugabyteDB (pinned `2026.1.2.0-b137`, persistent volume, health check) with `ybtop watch` serving the viewer and MCP on `127.0.0.1`. See the README's *Docker* section.

### Changed

- `_connection_args` stores the YSQL port as `db_port` (CLI flag unchanged for `watch` / `reset_pg_stat_statements`).

## [0.1.13] — 2026-08-12

### Added

- **Browser (URL toggle state):** Viewer controls are stored in the query string so a reload or copied link keeps them. Non-defaults only: **`merge=t`**, **`templates=t`** (Show recurring query templates; requires merge), Latency **`flagged=t`**, **`dip_p=f`** (hidden; shown is the default), and **`min_tier=`** when it is not `high`. Same `t` / omit-default convention as **`canonicalize=t`**. While a family ASH report keeps Merge on for that view only, the URL still records whether Merge was already on — so leaving the report (or a snapshot that cannot resolve the family) does not leave Merge stuck on.
- **Browser (query-template grouping):** The statement, ASH, and Latency-modes tabs can treat bind-count / IN-list / VALUES-list variants as one shape. **Merge similar SQL** (off by default) is the viewer-wide switch — there is no separate group-by control. With it on, query columns become **canonical query**, YSQL/YCQL Top 25s fold to one row per canonical query (YSQL still split by **dbname**), and ASH groups the Top 50 by **Table/Index + Canonical Query + Wait_Event** (unresolved SQL stays split by `query_id` so distinct sessions do not collapse). Clicking canonical-query text in any table opens `?view=ash&query=…&canonicalize=t` for the whole snapshot-local family; clicking a concrete `queryid` or member id always opens the ordinary one-id report. Family reports aggregate statement metrics plus ASH breakdowns across every matching `query_id`; YSQL links also carry **dbname**, while unresolved ids fall back to the one-id report. Family rows label their heaviest member as **representative query_id**. With Merge off, blank-query ASH rows stay one bucket, as before. Latency-modes remains one row per statement. **Show recurring query templates** (gated on merging) optionally lists multi-member shapes with a ranked `queryid` list that follows the column you sort by (YSQL keeps the same **dbname** split as the Top 25). Membership for Latency-modes templates is taken from every analyzed statement so min-tier / flagged-only filters do not rewrite a template. YCQL uses `?` binds, so the `$N` step never fires, and CQL has no multi-row `VALUES` lists; it still gets `IN (...)` collapsing, comment stripping, and single-row `VALUES (...)` canonicalization (which merges literal inserts). Grouping and family indexes are browser-only; the CLI and viewer apply the same query-normalization rules, and the snapshot format is unchanged.

### Changed

- **Query-template normalization (CLI + viewer):** `normalize_query_template` (and the matching browser function) now also collapses a bulk **`VALUES (...),(...),…`** row-list — any number of rows, one level of nested parens allowed per row for casts/function calls — to a canonical **`VALUES (...)`**. This folds multi-row upserts/bulk updates such as `UPDATE t AS x SET … FROM (VALUES (...)) WHERE …` into a single template regardless of row count, affecting Latency-modes template grouping and the new statement/ASH panel grouping. Per-call `/* … */` comments are still stripped, but planner hints that start with **`/*+`** are preserved so distinct hint sets stay distinct templates. See **Query normalization** in the README for the full step list and before/after examples.
- **`IN (...)` normalization now skips subqueries:** only a **value-list** `IN (...)` (literals / `$N`) is collapsed; a subquery **`IN (SELECT …)`** (e.g. `DELETE FROM t WHERE c IN (SELECT c FROM t WHERE …)`) is left intact so semantically distinct subquery predicates no longer fold into the same template.

## [0.1.12] — 2026-08-11

### Added

- **Latency-histogram collection (opt-in):** **`ybtop watch --snapshot-latency-histograms`** stores each statement's YugabyteDB **`yb_latency_histogram`** from the existing top-N-by-total-time **`pg_stat_statements`** pull (**`--snapshot-statements-per-node`**) as a **`latency_histograms.per_node`** section in the snapshot JSON. Capability-gated on the `yb_latency_histogram` column; NULL histograms are coalesced to empty jsonb at query time and omitted from the section so later merge/delta/detection ignore them. Normalized to a flat `{bucket_label: count}` map. No database tables are created — cumulative counters live in the snapshot files, and deltas are computed by subtracting consecutive snapshots.
- **Browser (Latency modes tab):** When a snapshot carries `latency_histograms`, the viewer shows a **Latency modes** tab with the same cumulative/delta convention as the statement tabs (delta vs prior snapshot when available). Runs Stages 0-2 plus template grouping in the browser with min-tier / flagged-only filters. The Hartigan dip test is not available in the browser, so shape-flagged rows are reported as **`unconfirmed`** unless a precomputed sidecar is present (see below).
- **Precomputed analysis sidecars (offline dip-confirmed browser view):** **`ybtop watch --snapshot-latency-analysis`** (implies `--snapshot-latency-histograms`) writes an **`ybtop.latency.<ts>.json`** sidecar next to each snapshot — the full report (cumulative **and** delta vs prior) including the **dip test** and **FDR** — and records it on the manifest entry (`latency_analysis`). The **Latency modes** tab prefers this sidecar when present, showing real `dip_p` and confirmed tiers with a **dip-confirmed** badge (no statistics run in the browser); it falls back to the in-browser Stages 0-2 detector otherwise. Because the sidecar contains only precomputed numbers, no GPL-licensed detector code (numpy/scipy/**diptest**) ships with ybtop or the viewer — the optional `[histogram]` extra runs only at analysis time. Sidecars are pruned alongside their snapshots by retention GC. Collection/analysis is best-effort: if the extra is not installed, `watch` logs a one-time note and keeps writing snapshots.

### Changed

- **Removed the offline `ybtop histogram` CLI command:** multimodality detection for dip-confirmed tiers is via **`watch --snapshot-latency-analysis`** sidecars and the viewer's **Latency modes** tab (browser Stages 0-2 without a sidecar). The `[histogram]` optional extra remains for sidecar generation only.
- **Latency histogram collection shares the statements query:** `yb_latency_histogram` is selected on the existing top-N-by-total-time `pg_stat_statements` pull (no second round-trip). NULL values coalesce to empty jsonb; empty histograms are omitted from `latency_histograms.per_node`. Removed **`--snapshot-latency-histograms-per-node`** (limit follows **`--snapshot-statements-per-node`**).
- **Latency gap column lists every adjacent mode split:** when 3+ peaks are detected, **`gap`** (CLI + Latency modes tab) shows all valid peak pairs comma-separated (low→high latency), e.g. `2→20ms(×10), 20→200ms(×10)`, instead of only the lowest-latency pair. Template summaries use the best member's full pair list; cross-member `gap_*_range` aggregates every pair.
- **Latency modes legend + dip_p toggle (browser):** the Latency modes legend now also explains **`bc`** (bimodality coefficient) and **`dip_p`** (Hartigan dip-test p-value). A **show dip_p** control hides or shows the `dip_p` column without changing analysis.

## [0.1.11] — 2026-06-03

### Added

- **Browser (call-frequency bar chart):** A new bar chart above the tab nav shows the **call rate (calls/s) per window** across the full manifest — **Δcalls** (YSQL `calls` + YCQL `calls`, clamped at ≥ 0 to absorb resets/prunes) divided by the window's duration, so bars stay comparable even when snapshot intervals vary. Bars are clickable to jump to that window; the bar for the current window is highlighted. Hover tooltip shows **`start → end UTC — X calls/s`** (or **cumulative calls** for the first window, which has no prior). Chart is collapsible via a toggle; collapsed state persists in **`localStorage`**. No per-snapshot HTTP fetches: heights come straight from the manifest.
- **Manifest call totals + size:** `ybtop watch` records **`bytes`**, **`ysql_calls`**, and **`ycql_calls`** for each entry written to **`ybtop.manifest.json`**. Entries written by older versions lack these fields; the viewer treats such windows as having no call data (no bar) rather than charting them as zero.
- **Browser (manifest polling):** The viewer re-fetches **`ybtop.manifest.json`** every **30 s** and updates the bar chart and Prev/Next/Last/jump-box bounds in place. The user stays pinned to their current snapshot by filename (a new arrival does not yank them to the newest); entries that have been GC'd off disk disappear from the chart and nav.
- **Embedded HTTP server (HEAD):** **`/static/...`** and snapshot JSON paths now support **HEAD** requests, returning **`Content-Length`** without a body.
- **Browser (quick window navigation):** Jump to any snapshot without stepping through Prev/Next — **click a bar** in the call-frequency chart, or **click the window number**, type a value, and press **Enter**. Keyboard shortcuts (outside a text box): **←/→** for Prev/Next, **Home/End** for First/Last, **`g`** to focus the window-number box (also shown in a tip at the bottom of the page). Out-of-range numbers clamp to the first/last window. Stepping back from the newest window pins it in the URL by snapshot timestamp (**`?t=YYYYMMDD_HHMMSS`**, from the `ybtop.out.*.json` filename) so a **reload** returns to the same snapshot even as the manifest shifts (new snapshots added, old ones GC'd); on the newest window no `t` is written, so a plain reload always follows the latest. An explicit **`?t=…`** is honored on load; if it matches no snapshot (invalid time, or rotated out of the manifest) the viewer shows a **"snapshot not found"** error rather than silently opening the newest.

### Changed

- **Browser (top toolbar):** The per-snapshot file/time label is replaced by the manifest's **overall span** (e.g. `— 2026-06-03 18:22:59 → 2026-06-03 21:22:51 UTC`). The bar chart's highlighted bar and tooltip identify the current window instead.
- **Browser (activity banner):** Compact one-line format **`Activity <date> <start> → <end> UTC (Xm Ys) — <file>`** for delta panels, and **`Activity @ <ts> — <file>`** for cumulative.

## [0.1.10] — 2026-05-19

### Added

- **YCQL statement statistics:** Snapshots collect top **`ycql_stat_statements`** per node (via **`yb_ycql_utils`**, created on the first **`watch`** tick). The browser viewer has a dedicated **ycql** tab with the same merged/delta layout as YSQL **`pg_stat_statements`**. ASH rows can be enriched with YCQL query text from this data.
- **ASH top tables:** After per-node collection, snapshots rank **`table_id`** values cluster-wide by ASH **samples** (default top **25**, **`--snapshot-ash-top-tables`**, `0` disables). Stored under **`ash_top_tables`** in each snapshot JSON.
- **Optional YSQL DDL extraction:** **`--snapshot-table-ddl`** fetches **`CREATE TABLE`** / **`CREATE INDEX`** definitions (including Yugabyte-specific PK/index details where available via **`pg_catalog`**) for top ASH **YSQL** tables on the seed connection. Results are stored in **`table_schemas.by_table_id`**. Off by default; YCQL DDL is not collected (YSQL connection only).
- **Structured activity logging:** **`ybtop watch`** writes JSON-lines to **`OUTPUT_DIR/ybtop.log`** by default, with per-checkpoint **`checkpoint_summary`** events and nested stage timings (**`build_snapshot`**, per-node query stages, **`write_snapshot`**, **`gc_snapshots`**). Size-based rotation (**1 MiB**, five backups). Configurable via **`--log-file`**, **`--log-level`**, **`--log-max-bytes`**, **`--log-backup-count`**, or **`--no-log-file`**.
- **Parallel per-node collection:** **`--node-parallelism`** (default **8**) caps concurrent node connections when fanning out snapshot queries across the cluster.

### Changed

- **Browser (table-scoped ASH):** When **`table_schemas`** is present for the filtered **`table_id`**, the scoped banner shows **YSQL schema (DDL)**.
- **Checkpoint log shape:** Summary timings nest parent stages correctly (**`build_snapshot.total_ms`** vs inner **`stages_ms`**; per-node **`total_ms`** vs query sub-stages) so totals are not double-counted with child stages.

### Fixed

- **Engine classification for `ash_top_tables`:** YSQL tables are identified from **`table_id`** hex layout (and tablet metadata), not from **`TServer`** ASH rows with **`ysql_dbid = 0`** (which incorrectly labeled many YSQL tables as YCQL).
- **Parallel per-node collection:** Worker threads each copy **`contextvars`** before running snapshot queries so **`--node-parallelism` > 1** does not fail with **`cannot enter context: … is already entered`** when structured logging stages are active. **`CheckpointLog.record`** is synchronized for concurrent per-node timing updates.

## [0.1.9] — 2026-04-23

### Added

- **ASH query-scoped banner (browser):** When the viewer filters ASH by **`query`**, the panel shows statement context from **`pg_stat_statements`** when present: title line **`query_id=…`** plus **`dbname=…`** when the merged row has a database name; metrics aligned with the statements tab (**calls/s** in Δ mode vs cumulative **calls**, **total time** as `… (ms) [time %]`, **mean time … ms**, **rows/call** when the snapshot includes row counts), including **Δ vs prior snapshot** when the prior snapshot includes pg_stat data.
- **Calls distribution for that query (browser):** **Calls Distribution % (across N nodes)** summarizes each node’s share of **Δcalls** (delta mode) or **calls** (cumulative) for the filtered **`query_id`** + **`dbname`** identity, top entries with **`…`** beyond five and **node_id** hover tooltips (multi-node clusters only; same interaction pattern as ASH load distribution).
- **Reserved ASH `query_id` names (1–13):** Internal YugabyteDB background **`query_id`** values (e.g. **Flush**, **Compaction**, **RemoteBootstrap**, **Snapshot**, **XCluster**) display fixed labels in the **query** column and query banner instead of blank SQL; the mapping can be extended as new reserved ids appear in newer releases.

### Changed

- **Statements table (browser + `ybtop watch` Rich tables):** Numeric column headers are **total time (ms)** and **mean time (ms)** (replacing **time (ms)** and **mean_ms** in the UI copy).
- **ASH section titles (browser):** **Active Sessions / sec** grouping titles are easier to scan; **Grouped By:** suffix uses accent styling; **Top 50 — ASH** subtitles reflect whether the roll-up is query-focused, table-focused, or full dimension mix.
- **ASH scoped by `table_id`:** Omits redundant **query** / **query_id** columns and collapses sections that add no information in that mode.
- **Navigation:** Additional **`query_id`** and **`node_id`** links from ASH / tablet-report contexts filter the ASH tab consistently.

### Fixed

- **Load Distribution %** could show no data in ASH roll-ups grouped primarily by **query** (merged bucket ↔ flat per-node row alignment).
- **Query tooltip / underline:** Full-SQL hover applies only when the **query** cell is truncated; node-percent tooltips use improved positioning.

## [0.1.8]

### Added

- **ASH load distribution (browser):** On multi-node clusters (and when not node-scoped), several ASH tables include **Load Distribution % (across N nodes)**—per-node sample share (top entries, with fast hover tooltips). The column is omitted for **ASH by cloud + region + zone** where it is redundant.
- **Stable merge keys for ASH rollups:** Merged ASH rows carry an internal **`ash_merge_key`**; flat per-node rows carry **`ash_flat_bucket_key`** and (with **`pg_stat`** query text) align bucket keys so load distribution and namespace/query rollups resolve correctly.

### Changed

- **ASH snapshot SQL / JSON:** `ash_aggregated` no longer joins **`pg_stat_statements`** for SQL text; **`merge_ash_groups`** no longer emits a **`query`** field on merged ASH rows. The viewer resolves statement text from **`pg_stat_statements.per_node`** by **`query_id`** (including for **flat** per-node rows used in rollups), so **ASH by namespace + query** and **ASH by namespace + object_name + query** stay consistent.
- **`yb_local_tablets` collection:** Snapshot tablet rows exclude **`state = 'TABLET_DATA_TOMBSTONED'`** (tombstoned tablets are not pulled). The ASH lateral join to **`yb_local_tablets`** is unchanged.
- **Browser viewer (ASH):** Scoped filters (**`node`**, **`table_id`**, **`query`**) use clearer banners and typography; **`wait_event_component`** column label is **component**; **`table_id` subtitles** can fall back to **`yb_local_tablets`** when ASH lacks a matching row.
- **Browser viewer (tablet distribution):** **`table_name`** links open ASH scoped by **`table_id`**; tables use **`width: auto`** with horizontal scroll on narrow panels.
- **Browser viewer (statements):** Numeric columns (**calls**, **time (ms)**, **time %**, **mean_ms**, per-call metrics) are right-aligned and use a fixed-width font where appropriate; **time (ms)** and **mean_ms** always show two decimal places; **rows/call** and DocDB **\*/call** columns use one decimal for alignment.
- **Browser viewer (ASH metrics):** **Active Sessions / sec** uses consistent fractional formatting (including values below 1); **Active Sessions / sec** and **Load %** are right-aligned; **query** cells show the full-SQL tooltip only when the preview is ellipsized (dotted underline + **`cursor: help`** when truncated).
- **Load distribution UX:** Custom tooltip positioning (no laggy native **`title`**); bogus **0%** chips from topology/node-id mismatches are avoided; **Top 50 — ASH by samples** uses a direct per-bucket scan so node maps match merged buckets.

## [0.1.7]

### Added

- **`table_id` in snapshots:** `yb_local_tablets` rows include `table_id`; ASH rows resolved via `yb_local_tablets` include `table_id` so table vs index / duplicate names across schemas are distinguishable.
- **Browser ASH drill-down:** `node_id` links open ASH scoped by `node`; `object_name` links (when `table_id` exists) open ASH scoped by `table_id`. URL parameters `node` and `table_id` compose with other filters as documented in the viewer.
- **ASH by query (table-scoped):** When filtering by `table_id`, a roll-up by **`query_id`** shows which statements drive activity against that table/index.
- **`query_id` deeplinks** across major ASH tables in the viewer (consistent with pg_stat statement links).

### Changed

- **Python `merge_ash_groups`:** Merge key prefers **`table_id`** when present (aligned with browser grouping).
- **Viewer ASH layout:** Redundant sections are omitted under node / table / query scopes where appropriate; **“ASH samples by database”** is always the **last** ASH subsection.

## [0.1.6]

### Changed

- **Tablet distribution (browser):** Counts and breakdowns use only tablets in **`TABLET_DATA_READY`** state; clearer messaging when raw tablet rows exist but none qualify.

## [0.1.5]

### Added

- **SQL tagging:** Outgoing queries can be prefixed with **`/* service:ybtop */`** for identification in server logs (via shared DB tagging helpers).

### Changed

- **`ybtop watch`:** Long statement text in the live dashboard is **truncated** to a short preview (multi-line SQL summarized).
- **ASH rollups (browser + `merge_ash_groups`):** Grouping no longer splits solely on different **`wait_event_aux`** when rows share the same **object / tablet identity**, reducing duplicate “same object” lines.

## [0.1.4]

### Added

- **`ybtop watch` live dashboard:** Alternate-screen layout with merged **top pg_stat_statements**, **nodes ranked by ASH active sessions/sec**, and **ASH summarized by cloud / region / zone**.
- **Delta pg_stat in watch:** When an older snapshot exists in the manifest, the statements panel can show **Δ vs prior snapshot**.
- **Manifest / snapshot helpers** to load prior snapshots for delta and viewer-related flows.

### Changed

- **Embedded HTTP viewer:** Bind happens **before** watch starts; bind or output-directory failures **exit with status 1** instead of continuing without a working viewer.
- **Live layout:** Snapshot write errors surface inside the dashboard; **`Live`** does not redirect stdout/stderr (prints are not swallowed).
- **Terminal UX:** Viewer URL uses **OSC 8** without Rich-specific link IDs where relevant for broader terminal compatibility; a **first-checkpoint collecting** message appears before the initial snapshot completes.

## [0.1.3]

### Added

- Initial **ybtop** release: **pg_stat_statements**, **ASH**, and **tablet** collection into JSON snapshots, CLI **`watch`** / **`serve`**, and static **browser viewer**.
