# skardi-skills

Agent Skills for working with [Skardi](https://github.com/skardi/skardi) — a backend engine to provide SQL pipelines as http endpoints. These skills give your AI coding agent (Claude Code, Cursor, or any [Agent Skills](https://agentskills.io/)-compatible tool) deep knowledge of Skardi's patterns so you don't have to re-explain them each session.

Check out our demo [here](https://www.youtube.com/watch?v=Cx5jG0OtUuk).

## Available skills

| Directory | Skill name | What it covers |
|---|---|---|
| `skardi_on_sealos/` | `skardi-deploy-and-patterns` | Core Skardi concepts (auth, pipelines, CSRF, DataFusion SQL dialect) + deploying to [Sealos](https://sealos.io/) via kubectl |
| `auto_knowledge_base/` | `auto_knowledge_base` | Agent-autonomous knowledge-base construction — turn any directory of text/markdown into a queryable KB using Skardi CLI + SQLite + sqlite-vec + FTS5 (Postgres/pgvector and Lance supported as overrides). Handles prereq detection, model download, chunking, ingest, and hybrid (vector + full-text + RRF) retrieval end-to-end. Supports `candle`, `gguf`, and `remote_embed` UDFs. |
| `auto_rag/` | `auto_rag` | Server-backed RAG over a user-supplied datastore (Postgres+pgvector, MongoDB, or Lance) — runs `skardi-server` in front of the user's DB so retrieval lives on a network endpoint instead of a local SQLite file. Renders ctx + ingest/search-vector/search-fulltext/search-hybrid pipelines, starts the server (local-process / Docker / Kubernetes), drives ingestion and querying over HTTP, and embeds client-side via the host CLI so the same templates run unchanged across all three runtimes. Never creates schema for the user — prints the SQL and waits. |
| `feishu_table/` | `feishu_table` | Skardi table provider over Feishu (Lark) Bitables and Sheets via the official [`lark-cli`](https://github.com/larksuite/cli). Renders a Skardi workspace (ctx + semantics + four pipelines) backed by a local SQLite mirror, drives `lark-cli base +field-list` / `+record-list` / `sheets +read` serially under the 20 req/s cap, and exposes `list-feishu-sources` / `describe-feishu-source` / `query-bitable-records` / `query-sheet-range` so an agent can read Feishu data as plain SQL. Read-only from Lark's perspective. |

## Installation

### Claude Code

Copy the skill(s) into your personal skills directory so they're available across all projects:

```bash
# skardi-deploy-and-patterns (deployment + core concepts)
mkdir -p ~/.claude/skills/skardi-deploy-and-patterns
cp skardi_on_sealos/skill_sealos_k8s_deploy.md ~/.claude/skills/skardi-deploy-and-patterns/SKILL.md
cp -r skardi_on_sealos/templates ~/.claude/skills/skardi-deploy-and-patterns/templates

# auto_knowledge_base (agent-autonomous KB construction)
cp -r auto_knowledge_base ~/.claude/skills/auto_knowledge_base

# auto_rag (server-backed RAG over a user-supplied datastore)
cp -r auto_rag ~/.claude/skills/auto_rag

# feishu_table (Skardi table provider over Feishu Bitables/Sheets via lark-cli)
cp -r feishu_table ~/.claude/skills/feishu_table
```

Claude Code will automatically load the relevant skill when your request matches it — e.g. deployment/auth/pipelines for `skardi-deploy-and-patterns`, "index these docs" / "build a RAG" / "make this folder searchable" for `auto_knowledge_base`, or "expose hybrid search as HTTP" / "RAG service over our pgvector DB" / "skardi-server with MongoDB" for `auto_rag`. You can also invoke any of them directly:

```text
/skardi-deploy-and-patterns
/auto_knowledge_base
/auto_rag
/feishu_table
```

### Cursor

Copy the skill(s) into the project-level skills directory:

```bash
# skardi-deploy-and-patterns
mkdir -p .cursor/skills/skardi-deploy-and-patterns
cp skardi_on_sealos/skill_sealos_k8s_deploy.md .cursor/skills/skardi-deploy-and-patterns/SKILL.md
cp -r skardi_on_sealos/templates .cursor/skills/skardi-deploy-and-patterns/templates

# auto_knowledge_base
cp -r auto_knowledge_base .cursor/skills/auto_knowledge_base

# auto_rag
cp -r auto_rag .cursor/skills/auto_rag

# feishu_table
cp -r feishu_table .cursor/skills/feishu_table
```

### Other Agent Skills-compatible tools

The `SKILL.md` files follow the [Agent Skills open standard](https://agentskills.io/) and work with any compatible tool. Place the skill directory wherever your tool resolves personal or project skills.

## Bundled resources per skill

### `skardi_on_sealos/templates/`

Ready-to-use files referenced by `skardi-deploy-and-patterns`:

| File | Purpose |
|---|---|
| `skardi-sealos.yaml` | Kubernetes manifest for deploying Skardi on Sealos |
| `nextjs-sealos.yaml` | Kubernetes manifest for a Next.js frontend on Sealos |
| `Dockerfile.nextjs` | Dockerfile for a Next.js app |
| `nextjs-proxy.ts` | Next.js API route that proxies requests to Skardi |
| `docker-compose.yml` | Local development stack |
| `init-db.py` | Database initialisation script |

### `auto_knowledge_base/`

Executable scripts, YAML templates, and reference docs the skill invokes:

| Path | Purpose |
|---|---|
| `scripts/setup_kb.py` | Creates a KB workspace — checks prereqs, installs missing Python deps, resolves/downloads the embedding model, renders the ctx + pipeline YAMLs with absolute paths, and initialises the SQLite schema with FTS5 + `vec0` mirrors and triggers |
| `scripts/chunk_corpus.py` | Walks a corpus directory and emits NDJSON chunks (markdown-aware heading splitting with paragraph-packed overlap; falls back to plain-text paragraph packing) |
| `scripts/bulk_ingest.py` | Embeds and inserts every chunk in a single `skardi query` — reuses the rendered ingest pipeline so `candle` / `gguf` / `remote_embed` all work without per-row overhead |
| `assets/ctx.yaml.tpl`, `assets/aliases.yaml.tpl` | Skardi v0.3 ctx + aliases templates (rendered with absolute DB path at setup) |
| `assets/pipelines/*.yaml.tpl` | The four pipelines: `ingest`, `search_vector`, `search_fulltext`, `search_hybrid` (RRF over sqlite_knn + sqlite_fts) |
| `references/backends.md` | Trade-offs and migration notes for Postgres + pgvector and Lance overrides |
| `references/pipeline_patterns.md` | The exact SQL the skill generates, with commentary on RRF, the DataFusion INSERT-VALUES quirk, and how to extend the pipelines (metadata filters, updates, deletes) |
| `references/troubleshooting.md` | Symptom → fix lookup for common failures (missing features, FTS5 syntax errors, trigger mismatches, dim mismatches, remote-API issues) |

### `auto_rag/`

Executable scripts, per-backend YAML templates, and reference docs the skill invokes:

| Path | Purpose |
|---|---|
| `scripts/setup_rag.py` | Renders the workspace — checks `skardi` CLI is on PATH, records the embedding choice (model path / provider args / dim) in a breadcrumb, renders `ctx.yaml` + the four pipeline YAMLs against the user's connection string + table, and runs a `SELECT 1` health probe before exiting |
| `scripts/start_server.py` | Starts `skardi-server` in one of three runtimes (`local-process` / `docker` / `kubernetes`), polls `/health`, verifies the four pipelines are registered, writes `server.runtime` + `server.port` for follow-up scripts |
| `scripts/stop_server.py` | Tears down whichever runtime was launched (kills the local pid, removes the docker container, or `kubectl delete`s the rendered manifests) |
| `scripts/chunk_corpus.py` | Same markdown-aware chunker as `auto_knowledge_base` — emits NDJSON with stable `(source, chunk_idx)`-derived ids so re-runs are idempotent |
| `scripts/embed.py` | Computes a single query embedding via the host `skardi` CLI and parses the float array out of the table-format output — used both at query time and inside `http_ingest.py` |
| `scripts/http_ingest.py` | Two-phase ingest: embeds every chunk via the host CLI (warm model cache), then POSTs `{doc_id, source, chunk_idx, content, embedding}` to `/ingest/execute` at `--concurrency N`. Tracks per-chunk status in `ingest_progress.json` so retries skip already-ok ids |
| `assets/postgres/ctx.yaml.tpl`, `assets/postgres/pipelines/*.yaml.tpl` | Postgres+pgvector ctx + the four pipelines (`ingest`, `search_vector`, `search_fulltext`, `search_hybrid` via RRF over `pg_knn` + `pg_fts`). Mongo and Lance asset trees follow the same layout when added |
| `references/runtimes.md` | Per-runtime walk-through (mounts, networking, lifecycle, kubectl flags, port-forward, cleanup) for `local-process` / `docker` / `kubernetes` |
| `references/schemas.md` | The exact DDL the user must run themselves for each backend (Postgres+pgvector, MongoDB index commands, Lance dataset bootstrap) |
| `references/troubleshooting.md` | Symptom → fix lookup (missing role, missing extension, dim mismatch, FTS5/tsquery syntax errors, Docker host-networking, localhost HTTP-proxy interception) |

### `feishu_table/`

Executable scripts, ctx + pipeline templates, and reference docs the skill invokes:

| Path | Purpose |
|---|---|
| `scripts/setup_feishu.py` | Verifies `lark-cli` and `skardi >= 0.4.0` are on PATH, renders `<workspace>/{ctx.yaml, semantics.yaml, pipelines/*.yaml}` against an absolute mirror DB path, bootstraps the SQLite mirror (records / fields / sheet_cells / sync_log), and runs a `SELECT 1` health probe |
| `scripts/sync_bitable.py` | Drives `lark-cli base +field-list` and `+record-list` serially for one (`base_token`, `table_id`), upserts records keyed on `(base_token, table_id, record_id)` with `fields_json` carrying the typed cells, and writes the sync ledger. `--full-refresh` forces a delete-and-replace; default is an in-place upsert |
| `scripts/sync_sheets.py` | Drives `lark-cli sheets +read` against a (`spreadsheet_token`, `sheet_id`, range), writes one row per non-empty cell into `feishu_sheet_cells` (sparse storage), and updates the sync ledger |
| `assets/ctx.yaml.tpl`, `assets/semantics.yaml.tpl` | Skardi `kind: context` + `kind: semantics` registering the `feishu` SQLite catalog with per-table / per-column descriptions surfaced via `GET /data_source` |
| `assets/pipelines/*.yaml.tpl` | Four pipelines: `list-feishu-sources` (catalog), `describe-feishu-source` (per-table schema), `query-bitable-records` (paged record reads), `query-sheet-range` (sparse cell reads) |
| `references/lark_cli.md` | `lark-cli` install, auth (OS keychain, no env-var bypass), required scopes, command cheatsheet, pagination, rate limits |
| `references/schema_mapping.md` | Lark Bitable field-type integer codes → SQLite column types, plus the materialisation pattern for hot tables when `json_extract` push-down is too slow |
| `references/pipeline_patterns.md` | Hand-written pipeline shapes — typed lists, joins against `auth.users`, sheet aggregations — and rules of thumb for when to specialise |
| `references/troubleshooting.md` | Symptom → fix lookup for setup, auth/scope, sync, query, and schema-drift failures (rate-limit code 99991663, `--base-token` vs API `app_token` confusion, `json_extract` NULLs, stale rows after a hard delete) |
