# Changelog

All notable functional changes to **ybtop** are listed here by release. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) (newest first).

## [0.1.10] — 2026-05-19

### Added

- **YCQL statement statistics:** Snapshots collect top **`ycql_stat_statements`** per node (via **`yb_ycql_utils`**, created on the first **`watch`** tick). The browser viewer has a dedicated **ycql** tab with the same merged/delta layout as YSQL **`pg_stat_statements`**. ASH rows can be enriched with YCQL query text from this data.
- **ASH top tables:** After per-node collection, snapshots rank **`table_id`** values cluster-wide by ASH **samples** (default top **25**, **`--snapshot-ash-top-tables`**, `0` disables). Stored under **`ash_top_tables`** in each snapshot JSON.
- **Optional YSQL DDL extraction:** **`--snapshot-table-ddl`** fetches **`CREATE TABLE`** / **`CREATE INDEX`** definitions (including Yugabyte-specific PK/index details where available via **`pg_catalog`**) for top ASH **YSQL** tables on the seed connection. Results are stored in **`table_schemas.by_table_id`**. Off by default; YCQL DDL is not collected (YSQL connection only).
- **Structured activity logging:** **`ybtop watch`** writes JSON-lines to **`OUTPUT_DIR/ybtop.log`** by default, with per-checkpoint **`checkpoint_summary`** events and nested stage timings (**`build_snapshot`**, per-node query stages, **`write_snapshot`**, **`gc_snapshots`**). Size-based rotation (**1 MiB**, five backups). Configurable via **`--log-file`**, **`--log-level`**, **`--log-max-bytes`**, **`--log-backup-count`**, or **`--no-log-file`**.

### Changed

- **Browser (table-scoped ASH):** When **`table_schemas`** is present for the filtered **`table_id`**, the scoped banner shows **YSQL schema (DDL)**.
- **Checkpoint log shape:** Summary timings nest parent stages correctly (**`build_snapshot.total_ms`** vs inner **`stages_ms`**; per-node **`total_ms`** vs query sub-stages) so totals are not double-counted with child stages.

### Fixed

- **Engine classification for `ash_top_tables`:** YSQL tables are identified from **`table_id`** hex layout (and tablet metadata), not from **`TServer`** ASH rows with **`ysql_dbid = 0`** (which incorrectly labeled many YSQL tables as YCQL).

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
