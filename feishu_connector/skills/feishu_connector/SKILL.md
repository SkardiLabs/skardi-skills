---
name: feishu_connector
description: 'Sync a Feishu/Lark Bitable (multi-dimensional table / 多维表格) into a local SQLite database via the official lark-cli, then register that SQLite as a Skardi data source so the agent can query Feishu table data with plain SQL (skardi query). Use whenever the user wants an AI agent to read / query / analyze data living in a Feishu (Lark) Bitable — e.g. "让 AI 查飞书多维表格" / "把飞书表接进来给 agent" / "query my Feishu base" / "analyze a Lark bitable". v1 scope — Bitable only (not Docs/IM), manual one-shot sync, special fields (select/date/link) stored as text.'
---

# feishu_connector — query a Feishu Bitable through Skardi

Turn a Feishu (Lark) Bitable into something the agent can query with SQL: (1) sync it to a local SQLite file via `lark-cli`, (2) register that file as a Skardi data source. No server, no extra API keys beyond the user's existing lark-cli auth.

## Prerequisites
- `lark-cli` — the official Lark CLI ([@larksuite/cli](https://github.com/larksuite/cli)). If it's not already on PATH, install it first: `npm install -g @larksuite/cli`. Then authenticate as the user (`lark-cli auth login`) with Base read scope; the user must have read access to the target Bitable.
- `skardi` CLI >= 0.4.0 on PATH.
- `python3` (stdlib `sqlite3` only — no extra deps).

## What to ask the user
1. **Bitable `app_token`** (base_token) — from the URL `.../base/<app_token>`.
2. **`table_id`** (`tbl...`) — the specific table.
3. (optional) a friendly table name for SQL (default `feishu_table`).

## Steps
1. **Sync** the Bitable to SQLite:
   `python scripts/sync_bitable.py --base-token <app_token> --table-id <tbl...> --out feishu.db --table-name <name>`
   Pulls fields + all records (paginated) via `lark-cli base` and writes one SQLite table. numbers -> REAL, checkbox -> 0/1, select/multi-select/link -> text (v1).
2. **Render `ctx.yaml`** from `assets/ctx.yaml.tpl`: fill `{{DB_PATH}}` (absolute path to feishu.db) and `{{TABLE_NAME}}`.
3. **Query** via Skardi. Sample queries (from the work-log Base used in verification — columns keep their Feishu field names, so wrap them in double quotes; `feishu_table` is the default `--table-name`):
   ```bash
   # aggregate: total hours per work-stream
   skardi query --ctx ctx.yaml --sql \
     'SELECT "工作方向", ROUND(SUM("用时(h)"),1) AS 总时长 FROM feishu_table GROUP BY "工作方向" ORDER BY 总时长 DESC'

   # filter: items flagged as report highlights
   skardi query --ctx ctx.yaml --sql \
     'SELECT "日期", "事项", "用时(h)" FROM feishu_table WHERE "汇报亮点"=1'
   ```
   The same source works over `skardi-server` too if a REST endpoint is wanted.

## v1 limitations (by design)
- **Bitable only** — Docs / Sheets / IM out of scope (Docs is v2).
- **Manual one-shot sync** — no incremental/scheduled sync; re-run to refresh.
- **Special fields as text** — select/multi-select become "a / b"; dates are Feishu's string; links are text. Typed mapping is future work (with eng).
- Column names keep their Chinese field names — quote them in SQL (`"用时(h)"`); use single quotes for string literals (`状态='完成'`).

## Notes
- Verified end-to-end on a real work-log Base (9 cols): sync + `skardi query` aggregation + filter all work. The two step-3 samples were re-run 2026-07-11 and return correctly.
- Reuse: the doc-backup repo's `_export_base.sh` does a similar Bitable->JSON export.
