---
name: feishu_connector
description: 'Sync Feishu/Lark data into a local SQLite database via the official lark-cli, then register that SQLite as a Skardi data source so an AI agent can query it with plain SQL (skardi query). Three sources: (A) a Bitable / 多维表格 becomes one SQLite table of rows and columns; (B) cloud docs / 云文档 (docx) become one row per doc holding the full markdown body; (C) a chat / 聊天记录 (IM) — group or 1:1 — becomes one row per message (sender, time, type, content). Use whenever the user wants an agent to read / query / analyze data living in Feishu — e.g. 让 AI 查飞书多维表格 / 把飞书表接进来给 agent / query my Feishu base / 让 AI 读我的飞书云文档 / 把飞书文档接进来 / 让 AI 读飞书群聊天记录 / 把飞书群聊接进来 / query my Feishu chat. v1 scope — Bitable + docs + chat (not Sheets), manual one-shot sync; Bitable special fields stored as text; docs stored as whole-doc markdown (no chunking/embedding — for semantic search over a large corpus use the auto_context skill instead); chat stores message text + metadata (images/files as placeholders).'
---

# feishu_connector — query Feishu Bitables & cloud docs through Skardi

Turn Feishu (Lark) data into something an agent can query with SQL: (1) sync it to a local SQLite file via `lark-cli`, (2) register that file as a Skardi data source. Works for **Bitables** (多维表格 → rows/columns), **cloud docs** (云文档 → one row per doc), and **chats** (聊天记录 → one row per message). No server, no extra API keys beyond the user's existing lark-cli auth.

## ⚠️ Security & data boundary (read first)
This makes a **local snapshot** of Feishu content in a plain SQLite file. It does **NOT** inherit Feishu's access controls:
- Once synced, **anything that can read the `.db` sees all synced content** — it is never re-checked against Feishu. If access is later revoked in Feishu, the local copy is unaffected until you re-sync.
- The sync tightens the `.db` to **owner-only (`0600`)**, but that is not a substitute for judgment: **don't sync onto shared machines or into a shared/multi-user agent**, and be especially careful with **1:1 chats** and other private content.
- Treat the `.db` as sensitive at rest. This is a convenience/query cache, **not** a governance boundary.

## Prerequisites
- `lark-cli` — the official Lark CLI ([@larksuite/cli](https://github.com/larksuite/cli)). If it's not already on PATH, install it first: `npm install -g @larksuite/cli`. Then authenticate with **least-privilege scope for the mode you use** — request only what's needed, NOT a bare `lark-cli auth login` (which asks for every domain — the "lots of unrelated permissions" consent screen):
  - Bitable only: `lark-cli auth login --domain base`
  - Cloud docs: `lark-cli auth login --domain docs,wiki`
  - Group chat: `lark-cli auth login --domain im`
  - All: `lark-cli auth login --domain base,docs,wiki,im`
  The user must have read access to the target Bitable / docs, and be a member of the target chat.
- `skardi` CLI >= 0.4.0 on PATH.
- `python3` (stdlib `sqlite3` only — no extra deps).

## Three modes — pick by what the user has

### A) Bitable (多维表格) — structured rows/columns
Ask for: **`app_token`** (from the URL `.../base/<app_token>`), **`table_id`** (`tbl...`), and optionally a table name (default `feishu_table`).
- **Sync**: `python scripts/sync_bitable.py --base-token <app_token> --table-id <tbl...> --out feishu.db --table-name <name>`
  Pulls fields + all records (paginated) via `lark-cli base` and writes one SQLite table. numbers → REAL, checkbox → 0/1, select/multi-select/link → text (v1).
- Then **Register & query** (below). Sample queries — the column names below are illustrative placeholders; use your table's actual Feishu field names, wrapped in double quotes:
  ```bash
  # aggregate
  skardi query --ctx ctx.yaml --sql 'SELECT "负责人", ROUND(SUM("工时"),1) AS 总工时 FROM feishu_table GROUP BY "负责人" ORDER BY 总工时 DESC'
  # filter
  skardi query --ctx ctx.yaml --sql "SELECT \"标题\", \"状态\" FROM feishu_table WHERE \"状态\" = '进行中'"
  ```

### B) Cloud docs (云文档 / docx) — one row per doc, full markdown body
For when the user keeps notes / tasks / knowledge in Feishu docs and wants the agent to read them. Each doc becomes one row `(doc_id, title, url, content_md, synced_at)`; the agent filters by title/keyword, then reads `content_md`. No chunking/embedding — this suits a modest set of docs the agent can read directly; for semantic search over a large corpus use the `auto_context` skill instead.
Ask for: a set of **doc URLs / tokens**, or a **wiki node token + space id** (to pull a whole subtree). Optional table name (default `feishu_docs`).
- **Sync** an explicit list:
  `python scripts/sync_docs.py --doc <url_or_token> [--doc <url_or_token> …] --out feishu.db --table-name feishu_docs`
  …or a whole wiki subtree: `python scripts/sync_docs.py --node <node_token> --space <space_id> --out feishu.db --table-name feishu_docs`
  Fetches each doc's full markdown via `lark-cli docs`. Wiki links auto-resolve to their docx. **Any failure (inaccessible doc, failed subtree page) aborts the sync without touching the existing table** — pass `--allow-partial` to instead sync what succeeded and list what failed. The `url` column stores the input URL for `--doc` items, and a `wiki/<node_token>` reference for subtree items (not a full clickable link).
- Then **Register & query** (below). Sample queries:
  ```bash
  skardi query --ctx ctx.yaml --sql "SELECT title, url FROM feishu_docs WHERE title LIKE '%任务%'"
  skardi query --ctx ctx.yaml --sql "SELECT title, content_md FROM feishu_docs WHERE content_md LIKE '%关键词%'"
  ```

### C) Chat (聊天记录 / IM) — one row per message; group **or** 1:1
For when the user wants the agent to read / search a Feishu chat — a **group** or a **1:1 (P2P)** conversation (who said what, when, decisions, discussion). Reads **your own** chats via `lark-cli im --as user` — **no bot needs to join the chat** (unlike server/bot integrations). Each message → one row `(chat_id, chat_name, message_id, sender, send_time, msg_type, content)`; text is decoded plain text, images/files/audio are stored as `[image]` / `[file]` placeholders (not downloaded).
Ask for: which chat (by **`--chat-id`** `oc_...`, or **`--chat-name`** — matches group names and 1:1s by the other person's name, e.g. `--chat-name "张三"`), and a time window (default last 30 days; `--days 0` = all). `--chat-name` **must resolve to exactly one chat**; if it matches more than one (commonly a group and a 1:1 with the same name), the sync lists the candidates and exits — **ask the user whether they mean the group or the 1:1 and re-run with `--chat-type group|p2p`** (只在同类型也重名时才回退到 `--chat-id`). Chat data is sensitive, so it never guesses which one. The sync verifies the collected count against the server's reported total and **aborts rather than write a partial table** if messages were dropped (e.g. rate-limited).
- **Sync**: `python scripts/sync_chat.py --chat-id <oc_...> [--days 30] --out feishu.db --table-name feishu_chat`
  (or `--chat-name "群名"`). Fetches messages via `lark-cli im` (paginated with `--order desc`; the IM endpoint rate-limits, so calls retry with backoff).
- Then **Register & query** (below). Sample queries:
  ```bash
  skardi query --ctx ctx.yaml --sql 'SELECT sender, COUNT(*) AS 条数 FROM feishu_chat GROUP BY sender ORDER BY 条数 DESC'
  skardi query --ctx ctx.yaml --sql "SELECT send_time, sender, content FROM feishu_chat WHERE content LIKE '%关键词%' ORDER BY send_time"
  ```

## Register & query (all modes)
1. **Render `ctx.yaml`** from `assets/ctx.yaml.tpl`: fill `{{DB_PATH}}` (absolute path to `feishu.db`) and `{{TABLE_NAME}}` (the `--table-name` you synced to).
2. **Query** with `skardi query --ctx ctx.yaml --sql '…'`. The same source works over `skardi-server` too if a REST endpoint is wanted.
- Field/column names keep their Chinese names — quote them in SQL (`"工时"`); use single quotes for string literals (`状态='进行中'`).

## v1 limitations (by design)
- **Bitable + docs + chat only** — Sheets out of scope.
- **Manual one-shot sync** — no incremental/scheduled sync; re-run to refresh.
- **Bitable special fields as text** — select/multi-select become "a / b"; dates are Feishu's string; links are text. Typed mapping is future work (with eng).
- **Docs = whole-doc markdown** — one row per doc, no per-section/heading split, no embedding. Deeply-nested list items may be flattened by the markdown export. For semantic retrieval over a large corpus, use `auto_context` instead.
- **Chat = message text + metadata** — images/files/audio become `[type]` placeholders (not downloaded); reactions and thread structure are not captured; only chats the authed user belongs to are readable.

## Notes
- Bitable mode verified end-to-end on a real Bitable (9 cols): sync + `skardi query` aggregation + filter.
- Docs mode verified end-to-end on real docs (multi-doc, raw token, wiki subtree, bad-token skip) against **lark-cli 1.0.68**, including `skardi query` over the synced `feishu_docs` table (2026-07-13).
- Chat mode verified end-to-end on a real group chat (38 msgs: text/image/file/system) and a 1:1/P2P chat, against **lark-cli 1.0.68**, including `skardi query` group-by-sender + keyword search (2026-07-13). `im +chat-list` needs `--types p2p,group` to include 1:1 chats (groups-only by default).
- Targets current lark-cli (1.0.68+): `base` needs `--json`; `docs +fetch` needs `--doc-format markdown` (content under `data.document.content`); `im +chat-list`→`data.chats`, `+chat-messages-list`→`data.messages`. No back-compat with older CLIs.
- Reuse: the doc-backup repo's `_export_base.sh` does a similar Bitable→JSON export.
