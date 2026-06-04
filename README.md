# ybtop

Terminal and browser-based observability for [YugabyteDB](https://www.yugabyte.com/). Connect to a single node, **discover the rest of the universe** with `yb_servers()`, pull statement stats, **active session history (ASH)**, and **per-node tablet placement**, and write time-stamped **JSON snapshots** plus a `ybtop.manifest.json` index. A small **static web viewer** (served as static assets with JSON loaded over HTTP) lets you explore snapshots in a browser: statement rankings, ASH groupings, tablet distribution, and optional delta views when a prior snapshot is available. Slice and dice data by query, object (table/index), nodes etc. to detect outliers.

## Install

```bash
pip3 install .
```

## Key commands

| Command | Purpose |
|--------|--------|
| **`ybtop watch`** | Live **terminal** status (iteration, last snapshot name, time, **placement/ASH summary** by cloud·region·zone) and, on every tick, a new **`ybtop.out.*.json`** in `--output-dir`, with **`ybtop.manifest.json`** updated. **By default also starts the HTTP viewer** on `127.0.0.1:8765` (same as `ybtop serve`); use **`--no-serve`** to only write files and use the TUI. Requires DB connectivity for the full run. |
| **`ybtop serve --data-dir DIR`** | **Read-only** HTTP server: serves the static viewer and JSON from a directory that already has **`ybtop.manifest.json`** and **`ybtop.out.*.json`**. **No database** is needed—ideal for **offline** review, archives, or sharing a folder of snapshots. |
| **`ybtop reset_pg_stat_statements`** | Runs `pg_stat_statements_reset()` on **each** YSQL node (via the same `yb_servers()`-based fan-out). Requires appropriate privileges. |

**Connection** (any subcommand that talks to the cluster): one of **`--dsn`**, **`--host`** (with `--port` default `5433`, etc.), or env **`YBTOP_DSN`** / **`DATABASE_URL`**. Optional **`YBTOP_PASSWORD`** for `--host`.

**Watch / viewer HTTP** (this is the **ybtop** HTTP port, not YSQL): **`--serve-bind`**, **`--serve-port`** (default `8765`); for **`serve`** the flags are **`--bind`** and **`--port`**.

**Snapshot tuning:** **`--interval`** (seconds between watch ticks, default `60`); **`--ash-window-minutes`** (rolling ASH window when `--ash-start`/`--ash-end` are not set, default `5`); or fixed **`--ash-start`** / **`--ash-end`** (ISO-8601). **`--snapshot-statements-per-node`** (default `200`) and **`--snapshot-ash-per-node`** (default `1000`) cap how many rows are stored per node in each file. **`--node-parallelism`** (default `8`) limits how many nodes are queried concurrently during each snapshot (useful on large clusters). **`--snapshot-ash-top-tables`** (default `25`, `0` disables) ranks **`table_id`** values cluster-wide by ASH samples after per-node collection. **`--snapshot-table-ddl`** (opt-in) fetches **YSQL** `CREATE TABLE` / `CREATE INDEX` DDL for those top tables (via the seed connection). **`--snapshot-retention-hours`** (default `3`, `0` disables) prunes old snapshot files and the manifest. **`--output-dir`** (default current directory) is the same path you pass to **`serve --data-dir`**.

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

**`ybtop.manifest.json`** lists snapshot files in order so the **Prev / Next** controls (and the **call-frequency chart** / window-number box) in the viewer can walk through time.

**Intervals (how often vs what window)**  
- **Snapshot interval** = **`ybtop watch --interval`**: time between *complete* collection passes and a new `ybtop.out.*.json` (default **60s**).  
- **ASH window** = either **`--ash-window-minutes`** of rolling history ending at **UTC now** each tick (default **5** minutes), or a fixed **`--ash-start`** / **`--ash-end`** range. The window is recorded in the snapshot as **`ash_window`**.

## Browser viewer: what you can do and what the columns mean

**Structure**  
The viewer has four main **tabs** (the URL can include **`?view=pgss`**, **`ycql`**, **`ash`**, or **`tablets`** so **reload** keeps the same section):

1. **pg_stat_statements** – Merged cluster view: **calls**, **time (ms)**, **time %**, **mean time**, **query**, optional **dbname**, **per-metric / call** for rows and DocDB fields when present, and **queryid**. If the previous manifest entry is loadable, a **delta** mode compares consecutive snapshots. **Query** and **queryid** can link to the **ASH** tab filtered to that **`query_id`**.  
2. **ycql_stat_statements** – Same layout as YSQL statements (including **time %** and **calls/s** in delta mode) for YCQL: **queryid**, **query**, **calls**, **total_time**, **is_prepared**.  
3. **Active Session History** – Merged ASH: **Active Sessions/sec** (from samples ÷ `ash_window` length), **load %** (share of total samples in the current row set), wait-event and **namespace** / **object** context, **query_id** and **query** when not scoped to a single query. **Full reports** break down samples by **namespace + query**, by **namespace + object + query**, by **database**, by **node**, and by **cloud/region/zone**, etc. With a **query filter** (from a link or **`?view=ash&query=...`**), a banner shows **`query_id`** and the **SQL**; those dimensions can hide redundant **query** / **query_id** columns. With a **`table_id`** filter, the banner can show the qualified table/index name and **YSQL schema (DDL)** when the snapshot was collected with **`--snapshot-table-ddl`**.  
4. **Tablet report** – Tablet counts **by table**, **by node**, and by **cloud:region:zone**, using `yb_local_tablets` and topology.

**Navigation**  
Use **First / Last / Prev / Next** to move along **`ybtop.manifest.json`**, or jump straight to any window: **click a bar** in the call-frequency chart, or **click the window number**, type a value, and press **Enter**. Keyboard shortcuts work anywhere outside a text box: **←/→** for Prev/Next, **Home/End** for First/Last, and **`g`** to focus the window-number box. When you step back from the newest window, the current window is pinned in the URL by its snapshot timestamp (**`?t=YYYYMMDD_HHMMSS`**, from the `ybtop.out.*.json` filename) so a **reload** returns to the same snapshot even as new snapshots arrive and old ones are GC'd; on the newest window no `t` is written, so a plain reload always follows the latest. An explicit **`?t=…`** is honored on load; if it matches no snapshot (invalid time, or rotated out of the manifest) the viewer shows a **"snapshot not found"** error instead of silently opening the newest. Deep links to ASH for a given statement use **history** so the **Back** button returns to the previous view.

**Typical column meanings (short)**  
- **time % / time (ms)**: share of *total* among rows shown, and total execution time (or delta, in delta mode).  
- **samples** (raw ASH): number of sample rows in the window; **Active Sessions/sec** scales samples by the snapshot’s `ash_window` length.  
- **per-node** counts in tablet views: how many **tablets** of that table sit on that node, etc.

For SQL details and any server-version nuances, see `src/ybtop/queries.py` and `src/ybtop/capabilities.py`.

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for the full text.
