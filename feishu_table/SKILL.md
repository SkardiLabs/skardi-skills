---
name: feishu_table
description: Expose Feishu (Lark) Bitables and Sheets as a Skardi-queryable data source by mirroring them into a local SQLite catalog via the official `lark-cli`. The skill renders a Skardi workspace (ctx.yaml + semantics.yaml + four pipelines), pulls schema and rows through `lark-cli base +field-list` / `+record-list` / `sheets +read` (serial, rate-limit-aware), and keeps a sync ledger so an agent can answer "what Feishu data do we have, and how fresh is it?". Use this skill whenever the user wants to query a Bitable from SQL / from a Skardi pipeline / from an MCP tool, run analytics over a Lark-hosted operations sheet, expose Feishu data behind a stable HTTP endpoint, or join Feishu rows against another Skardi data source. Trigger on phrases like "Feishu table", "Lark bitable", "飞书多维表格", "Feishu sheet to SQL", "Bitable as data source", "ingest from lark", "Skardi over Feishu", or any time the user references `larksuite/cli` / `lark-cli` and a downstream SQL workload. Skardi >= 0.4.0 required (uses the `kind: semantics` overlay for catalog descriptions); read-only from Lark's perspective — the skill never writes back to Feishu.
---

# feishu_table — a Skardi table provider over Feishu (Lark) files

Your job: make Feishu Bitables and Sheets queryable from Skardi (and therefore from any SQL caller, agent, or MCP tool that already speaks to a Skardi context) without writing a Rust connector. The strategy is *materialise then serve*: drive the official [`lark-cli`](https://github.com/larksuite/cli) to mirror Feishu data into a local SQLite file, then point a Skardi `sqlite` data source at that file with semantics-overlayed catalog descriptions and four pipelines (`list-feishu-sources`, `describe-feishu-source`, `query-bitable-records`, `query-sheet-range`).

This skill is the *table-provider* counterpart to `auto_rag` / `auto_knowledge_base`: those skills bring a corpus into Skardi for retrieval; this one brings an operational data store (Bitable / Sheet) into Skardi for direct SQL querying.

> **Why mirror, not pass-through?** A native pass-through provider would mean writing Rust against the Lark Open Platform SDK and shipping it inside the Skardi binary. That is a project, not a skill. Mirroring through `lark-cli` lets the skill stay portable, leans on the auth + rate-limit handling the official CLI already does, and gives the agent a stable SQLite shape to query — which means the same pipelines work whether the data came from one Bitable or twelve.

> **Skardi 0.4.0+ required.** The skill renders a `kind: semantics` overlay so an agent inspecting the catalog (`GET /data_source`, `skardi query --schema --all`) sees what each table holds. That overlay loader landed in 0.4.0; older binaries silently ignore the file and the catalog will look bare. `setup_feishu.py` checks the version.

## What this skill will and will not do

**Will do.** Render the Skardi workspace targeting a local mirror, drive `lark-cli` with the right offsets / page sizes to walk a Bitable serially under the rate cap, upsert rows into the mirror so reruns are idempotent, write a sync ledger, and expose the four pipelines above.

**Will not do.** Talk to the Lark Open Platform directly, store your `app_id` / `app_secret` outside the OS keychain, or write back to Feishu. The skill is read-only from Lark's perspective — every command it issues is a list/read. If you want bidirectional sync, that's a different shape (and should not auto-run on a sync schedule without explicit confirmation, because mass writes to a Bitable can wipe a team's work). Spell that out and stop.

## Prerequisites the user must supply

Confirm in one round of questions; don't guess.

1. **`lark-cli` installed and authenticated.** `npm i -g @larksuite/cli` (Node ≥ 18). Then `lark-cli config init` (interactive, asks for the `app_id` / `app_secret` from `open.larksuite.com`) and `lark-cli auth login --domain base,sheets,drive` to grant scopes. There is **no documented `LARK_APP_ID` / `LARK_APP_SECRET` env-var bypass** — credentials live in the OS keychain. If the user is in CI or another headless context, point them at [references/lark_cli.md § Headless auth](references/lark_cli.md).
2. **Skardi ≥ 0.4.0 on PATH.** `setup_feishu.py` enforces this.
3. **Targets.** For each Bitable table they want to mirror: `base_token` (the `app_xxx` portion of the share URL — note: API URLs call it `app_token`, the CLI calls it `--base-token`) plus `table_id` (`tbl_xxx`). For each Sheet: the share URL plus a `sheet_id` (run `lark-cli sheets +info --url <url>` once to list). The skill assumes the user already knows or can easily find these — if they say "all our Bitables", point them at `lark-cli drive +search --doc-types bitable,sheet` to enumerate first.
4. **Refresh cadence.** A skill run is one snapshot. If they want freshness, say so explicitly and either (a) cron `sync_bitable.py`, (b) wrap it in a Skardi `kind: job`, or (c) re-run the script on demand. The skill does not start a background daemon.

## The end-to-end flow

Three steps. Step 1 is one-time per workspace; step 2 runs once per (base, table) or (sheet, sheet_id); step 3 is the per-question loop.

```
1. python SKILL_DIR/scripts/setup_feishu.py --workspace ./feishu

2. python SKILL_DIR/scripts/sync_bitable.py \
     --workspace ./feishu \
     --base-token app_xxxxxxxxxx \
     --table-id   tbl_xxxxxxxxxx
   # ...repeat per (base_token, table_id) the user wants mirrored.

   python SKILL_DIR/scripts/sync_sheets.py \
     --workspace ./feishu \
     --url "https://example.feishu.cn/sheets/<spreadsheet_token>" \
     --spreadsheet-token <spreadsheet_token> \
     --sheet-id <sheet_id> \
     --range "A1:Z10000"

3. SKARDICONFIG=./feishu skardi query --pipeline list-feishu-sources
   SKARDICONFIG=./feishu skardi query --pipeline describe-feishu-source \
     --param base_token=app_xxx --param table_id=tbl_xxx
   SKARDICONFIG=./feishu skardi query --pipeline query-bitable-records \
     --param base_token=app_xxx --param table_id=tbl_xxx \
     --param limit=100 --param after_id=''
```

Read `SKILL_DIR` as the absolute path to the directory containing this `SKILL.md`.

### Step 1 — Render the workspace

`scripts/setup_feishu.py` is idempotent. It:

1. Verifies `lark-cli` is on PATH (the skill cannot do anything without it).
2. Verifies `skardi --version >= 0.4.0` (semantics overlay).
3. Renders `<workspace>/{ctx.yaml, semantics.yaml, pipelines/*.yaml}` from `SKILL_DIR/assets/`, baking the **absolute** mirror DB path into `ctx.yaml`. Absolute paths matter because Skardi resolves `path:` relative to its CWD when launched as a daemon and that's a common foot-gun.
4. Bootstraps the SQLite mirror with `CREATE TABLE IF NOT EXISTS` for `feishu_bitable_records`, `feishu_bitable_fields`, `feishu_sheet_cells`, and `feishu_sync_log`.
5. Runs `skardi query --sql "SELECT 1"` to surface a malformed render before any sync touches the user's Lark workspace.

### Step 2 — Sync from Feishu

**Bitable.** `scripts/sync_bitable.py` walks one (base_token, table_id) pair:

1. `lark-cli base +field-list --base-token <app> --table-id <tbl>` — schema, replaced wholesale on every run (so renamed/dropped columns disappear).
2. `lark-cli base +record-list --base-token <app> --table-id <tbl> --offset N --limit 500` — paged serially with a 100 ms cushion. The Bitable record-list endpoint is rate-limited at **20 req/s per app** and the `lark-cli` skills doc explicitly forbids parallel calls; the script does not parallelise within one table or across tables.
3. Records are upserted on `(base_token, table_id, record_id)` with the full record body stashed as `fields_json` (keys are `field_id`, looked up via `feishu_bitable_fields`). `--full-refresh` deletes prior rows for the pair before insert; default mode upserts in place, so deletions in Lark won't propagate without `--full-refresh`. Make this explicit to the user — silent stale rows after a hard delete in Lark is a real failure mode.
4. The sync ledger gets one row per (base, table) with status `ok` or `err: ...`. Failed runs leave the previous data in place.

**Sheet.** `scripts/sync_sheets.py` runs `lark-cli sheets +read --url <url> --sheet-id <id> --range <A1:Zn>` and writes one row per non-empty cell into `feishu_sheet_cells`. Sparse storage: the skill keeps blank cells out of the mirror so wide ranges stay cheap. The full range is replaced on every run — sheet syncs are always full-refresh in shape, because there's no stable cell id to upsert against.

### Step 3 — Query from Skardi

Four pipelines ship out of the box:

| Pipeline | Purpose | Parameters |
|---|---|---|
| `list-feishu-sources` | Catalog endpoint — what's been mirrored, when, with what status. | none |
| `describe-feishu-source` | Field list for one Bitable. Maps `field_name` → `field_id` (queries against `fields_json` key off `field_id`). | `base_token`, `table_id` |
| `query-bitable-records` | Page over the records of one table. Returns raw `fields_json`. | `base_token`, `table_id`, `limit`, `after_id` (`''` for first page) |
| `query-sheet-range` | Return synced cells for one sheet in `(row_idx, col_idx)` order. | `spreadsheet_token`, `sheet_id`, `row_from`, `row_to`, `col_from`, `col_to` |

Calling pattern from an agent:

```bash
# Catalog → schema → records.
SKARDICONFIG=./feishu skardi query --pipeline list-feishu-sources
SKARDICONFIG=./feishu skardi query --pipeline describe-feishu-source \
  --param base_token=app_xxx --param table_id=tbl_xxx
SKARDICONFIG=./feishu skardi query --pipeline query-bitable-records \
  --param base_token=app_xxx --param table_id=tbl_xxx \
  --param limit=100 --param after_id=''
```

The agent should always call `describe-feishu-source` before composing a `query-bitable-records` call so it has the `field_id` -> `field_name` mapping; raw `fields_json` is keyed by `field_id` only and the human-readable label is mutable.

### Joining Feishu rows against other Skardi data sources

The mirror is just a SQLite catalog (`feishu`), so any pipeline you'd write against another Skardi source can JOIN against it. Add the JOIN to a hand-written pipeline YAML next to the rendered ones — the skill's pipelines are deliberately generic so they don't get in the way of project-specific queries. The pattern is:

```yaml
kind: pipeline
metadata:
  name: "users-with-bitable-status"
  version: "1.0.0"

spec:
  query: |
    SELECT au.email,
           json_extract(b.fields_json, '$."fld_status"') AS status
    FROM auth.users au
    JOIN feishu.main.feishu_bitable_records b
      ON b.record_id = au.username
    WHERE b.base_token = {base_token}
      AND b.table_id   = {table_id}
```

`json_extract` is a SQLite-native function; whether DataFusion pushes it down through Skardi's SQLite source depends on your build. If the JOIN works but `json_extract` returns `NULL`, materialise the field into its own column at sync time — see [references/pipeline_patterns.md](references/pipeline_patterns.md).

## When to choose this skill vs. the others

Pick **`feishu_table`** when the user wants Lark/Feishu data exposed via SQL or a Skardi HTTP endpoint, and the workload is *operational* (filtering, joining, dashboards, agent workflows that read structured rows).

Pick **`auto_rag`** when they want *retrieval over Lark text content* — Docs bodies, long-form Bitable text columns, etc. Build a sync_corpus_from_lark.py wrapper that walks `lark-cli docs +read` and feeds the markdown through `auto_rag`'s ingest pipeline; the two skills compose cleanly because both end at "Skardi pipelines on a local catalog".

Pick **`auto_knowledge_base`** when retrieval is the *only* workload, no Bitable involved, and a single SQLite is fine.

The skills compose. A team can run `feishu_table` for "give the agent typed access to the inventory Bitable" and `auto_rag` for "let the agent search Confluence-equivalent Lark Docs", both backed by the same Skardi process.

## Customising

- **Materialised columns.** If queries against `json_extract(fields_json, '$."fld_xxx"')` are too slow or DataFusion doesn't push them down, edit `sync_bitable.py` to extract the columns you care about into a dedicated typed table — the EAV-ish default is generic-but-cold; per-table specialisation is one ALTER TABLE away. See [references/schema_mapping.md](references/schema_mapping.md) for the field-type integer codes.
- **Filtering at sync time.** Pass `--view-id vew_xxx` to `sync_bitable.py` to mirror only the rows visible in a Lark view — useful when the Bitable is millions of rows but the agent only cares about a tab.
- **Refresh cadence.** Wrap the sync script in a Skardi `kind: job` so refreshes run on a Skardi-managed schedule and show up in the run ledger, or stay out-of-band with cron / launchd.
- **Multiple workspaces, one Skardi.** `ctx.yaml` registers one data source (`feishu`); to host more than one mirror in the same Skardi process, render each into its own workspace and merge the `data_sources:` lists into a single ctx by hand (or symlink the SQLite files into one workspace and add a second `data_sources:` entry pointing at it).
- **Bidirectional writes.** Out of scope for this skill — see the "Will not do" section. If the user really wants writebacks, build a separate skill that wraps `lark-cli base +record-batch-create` / `+record-batch-update` and require an explicit confirmation step before each batch.

## Troubleshooting

If something looks off, read [references/troubleshooting.md](references/troubleshooting.md). The common failures:

- **`lark-cli not found`** — `npm i -g @larksuite/cli`; check `which lark-cli`. The CLI is the entire integration surface; nothing in the skill works without it.
- **`auth: scope <scope> not granted`** — re-run `lark-cli auth login --domain base,sheets,drive`. Use `lark-cli auth check base:record:read` to confirm.
- **`HTTP 99991663` or other Lark error codes from `lark-cli`** — error code reference at `https://open.larksuite.com/document/server-docs/getting-started/server-error-codes`. The most common code in this skill's path is `99991663` (rate limit) — if seen, raise `SLEEP_BETWEEN_PAGES` in `sync_bitable.py`.
- **Empty `feishu_bitable_records` after a successful sync** — almost always a wrong `--base-token` (you passed the *table* id) or a missing `base:record:read` scope. `feishu_bitable_fields` will also be empty in that case; check there first.
- **`describe-feishu-source` returns rows but `query-bitable-records` is empty** — schema synced but records didn't (rate-limit drop, scope difference, or a `view-id` that filters everything out).
- **`json_extract` returns NULL on every row** — DataFusion didn't push the function down, or the field id in your query doesn't match what's in `fields_json`. Inspect a raw row: `sqlite3 feishu.db "SELECT fields_json FROM feishu_bitable_records LIMIT 1"`.
- **Sheet rows look shifted by one** — `--range A1:...` is 1-indexed in A1 notation but `row_idx` / `col_idx` in the mirror are 0-indexed. The first cell of `A1:...` lands at `(0, 0)`.
- **Stale rows after a hard delete in Lark** — default sync upserts; hard deletions in Lark won't propagate. Re-run with `--full-refresh`, or accept the staleness and surface `last_synced_at` to the agent.
