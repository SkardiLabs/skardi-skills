# skardi-skills

Agent Skills for working with [Skardi](https://github.com/SkardiLabs/skardi) — a backend engine to provide SQL pipelines as http endpoints. These skills give your AI coding agent (Claude Code, Cursor, or any [Agent Skills](https://agentskills.io/)-compatible tool) deep knowledge of Skardi's patterns so you don't have to re-explain them each session.

Check out our demo [here](https://www.youtube.com/watch?v=Cx5jG0OtUuk).

## Available skills

| Directory | Skill name | What it covers |
|---|---|---|
| `auto_context/` | `auto_context` | Turn a folder of documents, a table you already have, or documents still inside a service into governed, searchable context an agent can query. Hybrid search (vector + full-text + RRF) served over HTTP by `skardi-server`. Three raw-material entries, one flow: a folder; an existing table (SQLite read directly and read-only, any other datastore piped in as NDJSON); or fetch-and-land — list, fetch each body, reconcile, ingest — where your agent writes the per-source fetch code and the skill fixes the process and its acceptance criteria. Storage is a separate choice: defaults to a local SQLite file the skill creates and owns (FTS5 + sqlite-vec `vec0` mirrors kept in sync by triggers); point it at Postgres+pgvector, MongoDB, or Lance when the index should live in a database you already own. Handles prereq checks, model resolution, chunking and embedding inline in SQL, ingest, and retrieval end-to-end across `candle` / `gguf` / `remote_embed`. Never creates schema in a datastore you own — prints the SQL and waits — and never writes to a table given as raw material. |
| `feishu_connector/` | `feishu_connector` | Sync a Feishu/Lark Bitable into local SQLite via `lark-cli`, then register it as a Skardi data source so an agent can query it with SQL. v1: manual one-shot sync, Bitable only. |

> **A running `skardi-server` is required.** Since Skardi's CLI became a thin HTTP client it holds no query engine and no local execution mode, so every path in `auto_context` starts a server. There is no CLI-only mode.

## Installation

### Claude Code (plugin marketplace, recommended)

From inside any Claude Code session:

```text
/plugin marketplace add SkardiLabs/skardi-skills
/plugin install auto-context@skardi-skills
/plugin install feishu-connector@skardi-skills
```

That's it — the skills are now available across all your projects, and `/plugin marketplace update skardi-skills` pulls future versions.

> **Upgrading from an earlier version:** `auto-knowledge-base` and `auto-rag` have been merged into `auto-context`, and `skardi-deploy-and-patterns` has been retired (its still-current material now lives in the main repo's `docs/`). Installed copies of the old plugins are not migrated automatically — install `auto-context` and remove the old ones.

### Claude Code (manual copy)

If you'd rather not use the plugin marketplace, copy the skill(s) into your personal skills directory so they're available across all projects:

```bash
# auto_context (searchable context over a folder or your own datastore)
cp -r auto_context/skills/auto_context ~/.claude/skills/auto_context

# feishu_connector (query a Feishu Bitable through Skardi)
cp -r feishu_connector/skills/feishu_connector ~/.claude/skills/feishu_connector
```

Claude Code will automatically load the relevant skill when your request matches it — e.g. "index these docs" / "make this folder searchable" / "build a RAG" / "expose hybrid search as HTTP" / "RAG service over our pgvector DB" for `auto_context`. You can also invoke it directly:

```text
/auto_context
/feishu_connector
```

### Other Agent Skills hosts

Codex, Cursor, Pi, dsh, OpenClaw, and Hermes read the same `SKILL.md` format Claude Code does — they differ only in where the skill directory has to go. All of them install from a checkout:

```bash
git clone https://github.com/SkardiLabs/skardi-skills.git && cd skardi-skills
```

`*/skills/*` in the commands below copies every skill in this repo. To install a single one, name it instead: `cp -r auto_context/skills/auto_context <destination>`.

#### Codex, Cursor, Pi, dsh

All four read the cross-tool `~/.agents/skills/` convention, so one copy covers every one of them:

```bash
mkdir -p ~/.agents/skills
cp -r */skills/* ~/.agents/skills/
```

To scope the skills to a single project instead, copy them into that repo's `.agents/skills/`. Each host also keeps a native directory if you'd rather install per tool — `~/.cursor/skills/` for [Cursor](https://cursor.com/docs/skills), `~/.pi/agent/skills/` for [Pi](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/skills.md), `~/.dsh/skills/` for [dsh](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/skills.md) — while [Codex](https://learn.chatgpt.com/docs/build-skills) uses `~/.agents/skills/` as its only personal location.

#### OpenClaw

[OpenClaw](https://docs.openclaw.ai/tools/skills) installs from a local path through its own CLI rather than by copying:

```bash
openclaw skills install ./auto_context/skills/auto_context
openclaw skills install ./feishu_connector/skills/feishu_connector
```

That puts the skills in the active workspace's `skills/` directory. OpenClaw's shared location, visible to every local agent, is `~/.openclaw/skills/`; its docs pair the `--global` flag with that directory for ClawHub installs and don't say whether the flag also applies to a local path.

#### Hermes

[Hermes](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills) treats `~/.hermes/skills/` as its source of truth:

```bash
mkdir -p ~/.hermes/skills
cp -r */skills/* ~/.hermes/skills/
```

Hermes does not scan `~/.agents/skills/` as a personal directory — inside a git repo it reads `<repo>/.hermes/skills/` and `<repo>/.agents/skills/`. To share one personal folder with the hosts above, add it under `skills.external_dirs` in `~/.hermes/config.yaml`.

#### Anything else

The `SKILL.md` files follow the [Agent Skills open standard](https://agentskills.io/), so any compatible host works. Place the skill directory wherever your tool resolves personal or project skills, and restart the host if the skill doesn't show up.

## Bundled resources per skill

### `auto_context/`

Executable scripts, per-backend YAML templates, and reference docs the skill invokes:

| Path | Purpose |
|---|---|
| `scripts/setup_context.py` | Renders the workspace — resolves the embedding choice (model path / provider args / dim), renders `ctx.yaml` + `semantics.yaml` + the five pipeline YAMLs for the chosen backend, and records a breadcrumb (`.embedding.txt`) the later scripts read. Defaults to `--backend sqlite`, where it owns the `.db` inside the workspace; `--backend postgres` requires a connection string and table the user created |
| `scripts/start_server.py` | Starts `skardi-server` in one of three runtimes (`local-process` / `docker` / `kubernetes`), polls `/health`, verifies the five pipelines are registered, writes `server.runtime` + `server.port` for follow-up scripts. Server startup **is** the connectivity check — it fails naming the source it could not open |
| `scripts/stop_server.py` | Tears down whichever runtime was launched (kills the local pid, removes the docker container, or `kubectl delete`s the rendered manifests) |
| `scripts/ingest_corpus.py` | Walks a corpus directory and POSTs one document per request to `/ingest-chunked/execute`. Chunking (`chunk()`) and embedding both happen inline in server-side SQL, so a document lands completely or not at all. Progress is journalled, so a re-run resumes instead of duplicating |
| `assets/sqlite/`, `assets/postgres/` | Per-backend `ctx.yaml` + `semantics.yaml` + `pipelines/*.yaml` templates. Same five pipeline names on both sides (`ingest`, `ingest_chunked`, `search_vector`, `search_fulltext`, `search_hybrid`), so only the backend-specific lines differ. Mongo and Lance trees follow the same layout when added |
| `references/schemas.md` | The exact DDL the user must run themselves per backend (Postgres+pgvector, MongoDB index commands, Lance dataset bootstrap) |
| `references/runtimes.md` | Per-runtime walk-through (mounts, networking, lifecycle, kubectl flags, port-forward, cleanup) |
| `references/pipeline_patterns.md` | The exact SQL the skill generates, with commentary on RRF, the DataFusion INSERT-VALUES quirk, and how to extend the pipelines (metadata filters, updates, deletes) |
| `references/troubleshooting.md` | Symptom → fix for server and own-datastore failures (missing role, missing extension, dim mismatch, tsquery syntax, Docker host-networking, localhost HTTP-proxy interception) |
| `references/troubleshooting_sqlite.md` | Symptom → fix for the local path (sqlite-vec loading and extension paths, FTS5 syntax, trigger mismatches, model download) |
