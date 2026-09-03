# ybtop

Terminal and browser-based observability for [YugabyteDB](https://www.yugabyte.com/). Connect to a single node, **discover the rest of the universe** with `yb_servers()`, pull statement stats, **active session history (ASH)**, and **per-node tablet placement**, and write time-stamped **JSON snapshots** plus a `ybtop.manifest.json` index. A small **static web viewer** (served as static assets with JSON loaded over HTTP) lets you explore snapshots in a browser: statement rankings, ASH groupings, tablet distribution, and optional delta views when a prior snapshot is available. Slice and dice data by query, object (table/index), nodes etc. to detect outliers.

## Install

```bash
pip3 install .
```

Optional latency-multimodality detector (for `watch --snapshot-latency-analysis` sidecars; adds numpy, scipy, and diptest):

```bash
pip3 install '.[histogram]'
```

## Key commands

| Command | Purpose |
|--------|--------|
| **`ybtop watch`** | Live **terminal** status (iteration, last snapshot name, time, **placement/ASH summary** by cloud·region·zone) and, on every tick, a new **`ybtop.out.*.json`** in `--output-dir`, with **`ybtop.manifest.json`** updated. **By default also starts the HTTP viewer** on `127.0.0.1:8765` (same as `ybtop serve`); use **`--no-serve`** to only write files and use the TUI. Requires DB connectivity for the full run. |
| **`ybtop serve --data-dir DIR`** | **Read-only** HTTP server: serves the static viewer and JSON from a directory that already has **`ybtop.manifest.json`** and **`ybtop.out.*.json`**. **No database** is needed—ideal for **offline** review, archives, or sharing a folder of snapshots. |
| **`ybtop reset_pg_stat_statements`** | Runs `pg_stat_statements_reset()` on **each** YSQL node (via the same `yb_servers()`-based fan-out). Requires appropriate privileges. |

**Connection** (any subcommand that talks to the cluster): one of **`--dsn`**, **`--host`** (with `--port` default `5433`, etc.), or env **`YBTOP_DSN`** / **`DATABASE_URL`**. Optional **`YBTOP_PASSWORD`** for `--host`.

**Watch / viewer HTTP** (this is the **ybtop** HTTP port, not YSQL): **`--serve-bind`**, **`--serve-port`** (default `8765`); for **`serve`** the flags are **`--bind`** and **`--port`**.

**Snapshot tuning:** **`--interval`** (seconds between watch ticks, default `60`); **`--ash-window-minutes`** (rolling ASH window when `--ash-start`/`--ash-end` are not set, default `5`); or fixed **`--ash-start`** / **`--ash-end`** (ISO-8601). **`--snapshot-statements-per-node`** (default `200`) and **`--snapshot-ash-per-node`** (default `1000`) cap how many rows are stored per node in each file. **`--node-parallelism`** (default `8`) limits how many nodes are queried concurrently during each snapshot (useful on large clusters). **`--snapshot-ash-top-tables`** (default `25`, `0` disables) ranks **`table_id`** values cluster-wide by ASH samples after per-node collection. **`--snapshot-table-ddl`** (opt-in) fetches **YSQL** `CREATE TABLE` / `CREATE INDEX` DDL for those top tables (via the seed connection). **`--snapshot-latency-histograms`** (opt-in) collects each statement's **`yb_latency_histogram`** from the same top-N-by-total-time **`pg_stat_statements`** pull for the viewer's **Latency modes** tab. **`--snapshot-latency-analysis`** (opt-in; implies `--snapshot-latency-histograms`) additionally precomputes the **dip-confirmed** multimodality report into an **`ybtop.latency.*.json`** sidecar per snapshot so the browser can show confirmed tiers offline without shipping any statistics code (requires the `[histogram]` extra; best-effort — skipped with a log note if it is not installed). **`--snapshot-retention-hours`** (default `3`, `0` disables) prunes old snapshot files and the manifest. **`--output-dir`** (default current directory) is the same path you pass to **`serve --data-dir`**.

**What latency multimodality finds:** Some queries have a **split personality**; most calls are fast, but a meaningful chunk are much slower (or there are several distinct speed "clusters" rather than one). Averages, and even p99s, hide this: a single number can't tell you that a query is really behaving like *two or three different queries at once*. The viewer's **Latency modes** tab reads YugabyteDB's per-statement latency histogram and looks for exactly that shape — statements whose per-call latency splits into two or more separate **speed classes** (e.g. a fast ~2 ms mode and a slow ~50 ms mode). Those are the queries worth a closer look, because the split usually has a concrete, fixable cause: tablet **leader vs. follower/local reads**, buffer **cache hit vs. miss**, **read-restart** or **lock-conflict retries**, **cross-AZ vs. same-AZ** hops, a **leader move** during the window, or the optimizer **flipping query plans**. Each flagged statement is reported with a **confidence tier**, the number of **peaks** (speed classes found), the overall latency **spread**, and the **gap** between adjacent modes; near-identical statements are collapsed into recurring **query templates** so one shared root cause shows up once instead of many times. Capture with **`--snapshot-latency-analysis`** (and `pip install 'ybtop[histogram]'`) for dip-confirmed tiers in the browser; without a sidecar the tab still runs shape-based detection and reports shape-flagged rows as **`unconfirmed`**.

**Logging (`ybtop watch`):** By default writes **structured JSON lines** to **`OUTPUT_DIR/ybtop.log`** (one event per line, suitable for `jq` or log pipelines). Each checkpoint logs **`checkpoint_summary`** with nested timings: top-level stages (**`build_snapshot`**, **`write_snapshot`**, **`gc_snapshots`**), inner snapshot work under **`build_snapshot.stages_ms`**, and per-node query timings under **`build_snapshot.per_node_ms`**. Logs **rotate** at **1 MiB** (five backups: `ybtop.log.1`, …). Flags: **`--log-file`**, **`--log-level`** (`INFO` / `DEBUG`), **`--log-max-bytes`**, **`--log-backup-count`**, **`--no-log-file`**. Logging is file-only; the terminal dashboard is unchanged.

## Architecture: what we query and what goes in a snapshot

**Discovery and fan-out**  
`ybtop` uses a **seed** libpq DSN to any one node, then runs **`yb_servers()`** to list YSQL nodes (host, port, and placement: cloud, region, zone when available). Each snapshot query runs **per node** (per-node `pg_stat_statements` / ASH / tablets), then results are stored under **node id** keys in the JSON.

**Data sources (YSQL)**

| Source | Role |
|--------|------|
| **`pg_stat_statements`** (plus `pg_database` for `dbname`) | Top **N** statements by total execution time. Includes `queryid` (as text), `query`, `calls`, time metrics, and optionally **rows** and **Yugabyte DocDB** per-statement fields when supported. |
| **`ycql_stat_statements`** (via **`yb_ycql_utils`** extension) | Top **N** YCQL statements per node: `queryid`, `query`, `calls`, `total_time`, `is_prepared`. Extension is created once on the first **`watch`** snapshot (`CREATE EXTENSION IF NOT EXISTS yb_ycql_utils`). |
| **`yb_active_session_history`** | ASH rows in **[ash_window.start, ash_window.end)** (see below). Aggregated in SQL by `query_id`, wait-event dimensions, `ysql_dbid`, etc., with counts as **`samples`**, enriched with namespace / object / **`table_id`** via **`yb_local_tablets`** when **`wait_event_aux`** matches. Query text is resolved in the **viewer** from **`pg_stat_statements`** / **`ycql_stat_statements`**. `query_id` is stored as **text** in snapshots. **`wait_event_aux`** is a 15-character prefix: **`tablet_id`** for most components, **`table_id`** for **`YCQL`**. |
| **`yb_local_tablets`** | Per-node **tablet** rows (for tablet distribution UIs in the viewer). |
| **Capabilities** | At runtime, features such as the ASH time-range function vs time predicate, and optional DocDB columns on `pg_stat_statements`, are detected and queries adapt. |

**What each snapshot file contains (conceptually)**  
Each **`ybtop.out.YYYYMMDD_HHMMSS.json`** includes: **`generated_at_utc`**, **`ash_window`** (`start_utc` / `end_utc`—the ASH window used for that collection), **seed** info, **node ids**, **`node_topology`**, and four **`per_node`** maps:

- **`pg_stat_statements`** – list of top statements for that node.  
- **`ycql_stat_statements`** – list of top YCQL statements for that node.  
- **`yb_active_session_history`** – ASH aggregate rows (samples, wait events, resolved namespace / object / **`table_id`**, etc.). Query text is filled in the **viewer** from merged **`pg_stat_statements`** / **`ycql_stat_statements`** when available.  
- **`yb_local_tablets`** – tablet rows for that node.  

Optional sections (when enabled / applicable):

- **`ash_top_tables`** – top **`table_id`** values by total ASH **samples** across nodes (default top **25**).  
- **`table_schemas.by_table_id`** – **YSQL** DDL for those tables/indexes when **`--snapshot-table-ddl`** is set (YCQL schema is not collected via YSQL).  
- **`latency_histograms.per_node`** – per statement from the same top-N-by-total-time **`pg_stat_statements`** set (**`--snapshot-statements-per-node`**), the **`yb_latency_histogram`** normalized to a flat `{bucket_label: count}` map, plus `queryid`, `query`, `dbname`, and `calls`, when **`--snapshot-latency-histograms`** is set. NULL histograms coalesce to empty jsonb and are omitted. Counts are cumulative; the viewer computes deltas by subtracting consecutive snapshots.  

- **`ybtop.latency.<ts>.json`** (sidecar) – present when the snapshot was captured with **`--snapshot-latency-analysis`**: the precomputed dip-confirmed report (`cumulative` + `delta`, unfiltered) that the viewer's **Latency modes** tab loads for offline confirmed tiers. Referenced from the manifest entry's **`latency_analysis`** field and pruned alongside its snapshot by retention GC.  

**`ybtop.manifest.json`** lists snapshot files in order so the **Prev / Next** controls (and the **call-frequency chart** / window-number box) in the viewer can walk through time. Each entry may carry a **`latency_analysis`** pointer to its sidecar (above).

**Intervals (how often vs what window)**  
- **Snapshot interval** = **`ybtop watch --interval`**: time between *complete* collection passes and a new `ybtop.out.*.json` (default **60s**).  
- **ASH window** = either **`--ash-window-minutes`** of rolling history ending at **UTC now** each tick (default **5** minutes), or a fixed **`--ash-start`** / **`--ash-end`** range. The window is recorded in the snapshot as **`ash_window`**.

## Browser viewer: what you can do and what the columns mean

**Structure**  
The viewer has four main **tabs** (the URL can include **`?view=pgss`**, **`ycql`**, **`ash`**, or **`tablets`** so **reload** keeps the same section), plus a **Latency modes** tab (**`?view=latency`**) that appears only when the snapshot includes `latency_histograms`:

1. **pg_stat_statements** – Merged cluster view: **calls**, **time (ms)**, **time %**, **mean time**, **query**, optional **dbname**, **per-metric / call** for rows and DocDB fields when present, and **queryid**. If the previous manifest entry is loadable, a **delta** mode compares consecutive snapshots. The **queryid** column and the query text both link to the **ASH** tab filtered to that **`query_id`**. With **Merge similar SQL** on, the **queryid** column is dropped (one row now stands for several statements), and clicking its **canonical query** opens a canonical-family ASH report that includes every matching `query_id` (still scoped by **dbname** for YSQL).
2. **ycql_stat_statements** – Same layout as YSQL statements (including **time %** and **calls/s** in delta mode) for YCQL: **queryid**, **query**, **calls**, **total_time**, **is_prepared**. **Merge similar SQL** folds the Top 25 the same way (YCQL has no **dbname** split).  
3. **Active Session History** – Merged ASH: **Active Sessions/sec** (from samples ÷ `ash_window` length), **load %** (share of total samples in the current row set), wait-event and **namespace** / **object** context, **query_id** and **query** when not scoped to a single query. **Full reports** break down samples by **namespace + query**, by **namespace + object + query**, by **database**, by **node**, and by **cloud/region/zone**, etc. With a **query filter** (from a link or **`?view=ash&query=...`**), a banner shows **`query_id`** and the **SQL**; those dimensions can hide redundant **query** / **query_id** columns. With a **`table_id`** filter, the banner can show the qualified table/index name and **YSQL schema (DDL)** when the snapshot was collected with **`--snapshot-table-ddl`**.  

**Merging similar SQL**  
An application that sends `IN ($1, $2)` on one call and `IN ($1, $2, $3)` on the next gets a separate `queryid` for each variant, so a single hot query can be scattered across a dozen rows that each look too small to matter. **Merge similar SQL** — one toggle, shared by the statement, ASH, and Latency-modes tabs — treats those variants as the same shape: bind positions, `IN` value lists, and bulk `VALUES` lists collapse, while table and column names are left alone so genuinely different queries stay apart. It starts **off**, so nothing is merged until you ask. YCQL is a little different: it uses `?` bind markers, so the `$N` step never fires, and CQL has no multi-row `VALUES` lists to fold. What it does get is `IN (...)` collapsing, comment stripping, and a single-row `VALUES (...)` collapse that merges unprepared inserts written with literals. All of this happens in the browser on the snapshot already loaded, and it follows whatever cumulative/delta mode or ASH filter you have set.

Turn it on and the statement tables fold the **Top 25** into one row per canonical query (YSQL still split by **dbname**), with the query column retitled **canonical query**. There is no separate “group by template” control — merging *is* the grouping. Counters are summed first, so time %, calls/s, and delta arithmetic behave as they do ungrouped. The **queryid** column leaves the Top 25 in that mode. Clicking the canonical query opens `?view=ash&query=…&canonicalize=t` (plus `dbname` for YSQL): the viewer resolves that representative id to its canonical family, filters ASH to every member `query_id`, and aggregates the statement banner’s calls, time, mean latency, rows/call, and per-node call share for the family. If the representative id cannot be resolved in the current snapshot, the link safely falls back to the ordinary one-id report. Latency-modes still lists one row per statement, but with Merge on its **canonical query** text uses the same family link; the **queryid** column stays a single-id report for that statement.

The ASH family report stays grouped by canonical text. Its banner names the representative id and member count, and dimensional rows expose a **representative query_id** (the heaviest member for that bucket) rather than pretending that one id owns the aggregate. The regular ASH tab’s Top 50 reads **Table/Index + Canonical Query + Wait_Event**, and the Database & Query / Table/Index & Query rollups follow suit. Database & Query drops **query_id** outside a scoped family, since one row may speak for several statements. Rows with no resolved SQL stay split by `query_id` under Merge so they are not stacked into one blank bucket; with Merge off they still share one empty-text row, as they always did.

**Show recurring query templates** is a second toggle, available only while merging is on. It lists the shapes that really do have several members: the canonical query, its combined cost (**total time** / **time %**, or **calls/s** in delta mode; **Active Sessions/sec** / **Load %** in ASH), and every member `queryid` in rank order. Navigation follows one rule in every panel: clicking **canonical query** in a table opens ASH for the whole family, while clicking a specific `queryid` opens ASH for only that statement. On YSQL the summary keeps the same **dbname** split as the Top 25, so the same shape in two databases stays two rows. Member ranking follows the column you sort by — sort the summary by calls/s and the members reorder by calls/s. On Latency modes the membership is taken from every analyzed statement, so the **min tier** and **flagged only** filters change which templates you see, never what a template contains.

### Query normalization

The collapsing is `normalize_query_template`. The CLI and the browser use the same rules, so they group SQL the same way. It strips the per-call noise — bind positions, list lengths, comment tags an application stamps onto each execution — and leaves the parts that change a plan. Planner hints beginning with `/*+` (pg_hint_plan / YSQL) survive, so two queries that differ only in their hints stay separate. Step by step:

| Step | Before | After |
| --- | --- | --- |
| Strip per-call comments | `SELECT /*rewritten_pid='42'*/ id FROM orders WHERE id = $1` | `SELECT id FROM orders WHERE id = $1` |
| Preserve planner hints (`/*+ … */`) | `SELECT /*+ IndexScan(orders) */ id FROM orders WHERE id = $1` | unchanged (hint kept) |
| Collapse value-list `IN (...)` | `… WHERE id IN (1, 2, 3)` or `… WHERE id IN ($1, $2, $3)` | `… WHERE id IN (...)` |
| Leave subquery `IN` alone | `… WHERE id IN (SELECT id FROM archive)` | unchanged |
| Collapse bulk `VALUES` row-lists | `UPDATE t AS x SET … FROM (VALUES (1,'a'), (2,'b')) v …` | `UPDATE t AS x SET … FROM (VALUES (...)) v …` |
| Collapse bind positions | `… WHERE a = $1 AND b = $2` | `… WHERE a = $N AND b = $N` |
| Collapse whitespace | runs of spaces / newlines | a single space; trim ends |

Together, these two statements fold into one template:

- `SELECT /*pid=1*/ * FROM orders WHERE id IN ($1, $2)`
- `SELECT /*pid=9*/ * FROM orders WHERE id IN ($1, $2, $3)`

→ `SELECT * FROM orders WHERE id IN (...)`

4. **Tablet report** – Tablet counts **by table**, **by node**, and by **cloud:region:zone**, using `yb_local_tablets` and topology.  
5. **Latency modes** (only when `latency_histograms` is present) – Statements whose per-call latency distribution is **bimodal/multimodal**, with **tier**, **calls**, **bimodality coefficient**, **peaks**, latency **spread**, and **gap** (every adjacent mode split, comma-separated when there are 3+ peaks). Merging SQL here swaps the query column for **canonical query** with **queryid** beside it; both the query text and **queryid** deep-link to ASH the same way the statement tabs do. The recurring-templates summary shows which analyzed statements are really the same shape (member `queryid`s are also ASH links). Uses the same cumulative/delta convention as the statement tabs. When a snapshot was captured with **`--snapshot-latency-analysis`**, the tab loads the precomputed **dip-confirmed** report — real `dip_p` and FDR-corrected tiers with a **dip-confirmed** badge, no statistics run in the browser. Otherwise the browser runs the shape-based stages only; the Hartigan **dip test is not run in the browser**, so flagged rows show as **`unconfirmed`** — capture with **`--snapshot-latency-analysis`** (and `pip install 'ybtop[histogram]'`) for dip-confirmed tiers.

**Navigation**  
Use **First / Last / Prev / Next** to move along **`ybtop.manifest.json`**, or jump straight to any window: **click a bar** in the call-frequency chart, or **click the window number**, type a value, and press **Enter**. Keyboard shortcuts work anywhere outside a text box: **←/→** for Prev/Next, **Home/End** for First/Last, and **`g`** to focus the window-number box. When you step back from the newest window, the current window is pinned in the URL by its snapshot timestamp (**`?t=YYYYMMDD_HHMMSS`**, from the `ybtop.out.*.json` filename) so a **reload** returns to the same snapshot even as new snapshots arrive and old ones are GC'd; on the newest window no `t` is written, so a plain reload always follows the latest. An explicit **`?t=…`** is honored on load; if it matches no snapshot (invalid time, or rotated out of the manifest) the viewer shows a **"snapshot not found"** error instead of silently opening the newest. Deep links use **history** so the **Back** button returns to the previous view. Exact statement links use `?view=ash&query=…`; merged Top 25 links add **`canonicalize=t`** and resolve the family entirely from the loaded snapshot, without a snapshot-format change. Viewer toggles are in the URL too, and only when they are not the default: **`merge=t`**, **`templates=t`** (recurring query templates, only with merge on), **`flagged=t`**, **`dip_p=f`** when the dip_p column is hidden, and **`min_tier=`** when Latency modes is not on `high`.

**Typical column meanings (short)**  
- **time % / time (ms)**: share of *total* among rows shown, and total execution time (or delta, in delta mode).  
- **samples** (raw ASH): number of sample rows in the window; **Active Sessions/sec** scales samples by the snapshot’s `ash_window` length.  
- **per-node** counts in tablet views: how many **tablets** of that table sit on that node, etc.

For SQL details and any server-version nuances, see `src/ybtop/queries.py` and `src/ybtop/capabilities.py`.

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for the full text.
