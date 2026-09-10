# skardi-skills

Agent Skills for working with [Skardi](https://github.com/SkardiLabs/skardi) — a backend engine to provide SQL pipelines as http endpoints. These skills give your AI coding agent (Claude Code, Cursor, or any [Agent Skills](https://agentskills.io/)-compatible tool) deep knowledge of Skardi's patterns so you don't have to re-explain them each session.

Check out our demo [here](https://www.youtube.com/watch?v=Cx5jG0OtUuk).

## Available skills

| Directory | Skill name | What it covers |
|---|---|---|
| `skills/auto-context/` | `auto-context` | Turn a folder of documents, a table you already have, or documents still inside a service into governed, searchable context an agent can query. Hybrid search (vector + full-text + RRF) served over HTTP by `skardi-server`. Three raw-material entries, one flow: a folder; an existing table (SQLite read directly and read-only, any other datastore piped in as NDJSON); or fetch-and-land — list, fetch each body, reconcile, ingest — where your agent writes the per-source fetch code and the skill fixes the process and its acceptance criteria. Storage is a separate choice: defaults to a local SQLite file the skill creates and owns (FTS5 + sqlite-vec `vec0` mirrors kept in sync by triggers); point it at Postgres+pgvector, MongoDB, or Lance when the index should live in a database you already own. Handles prereq checks, model resolution, chunking and embedding inline in SQL, ingest, and retrieval end-to-end across `candle` / `gguf` / `remote_embed`. Never creates schema in a datastore you own — prints the SQL and waits — and never writes to a table given as raw material. |
| `skills/retrieval/` | `retrieval` | Answer questions from live data through a running `skardi-server` with the `skardi` CLI. Discovers sources, named pipelines, and the table schemas the deployment exposes, runs the question through any semantic search surface first (`search-hybrid` and friends), then writes read-only SQL against exact qualified table names, checks truncation before trusting counts, and reports with the query attached. Consumes whatever the server already has — it builds no index, writes no data, and starts no servers. |
| `skills/graph-source/` | `graph-source` | Connect a property graph (a knowledge graph, a GraphRAG corpus — Apache AGE / openCypher) to Skardi and query it through SQL, end to end: provision AGE with a least-privilege reader role, declare the `type: graph` source and its views in context YAML, triage registration health (healthy / degraded / refused, and what recovery does and does not answer), write correct queries (`cypher_query`, `graph_schema`, the JSON getters), and wire Cypher parameters into pipelines. Encodes the traps that bite in production: positional `columns` binding (same-typed columns declared out of RETURN order swap silently), no predicate pushdown into view Cypher (the bound lives in the view, `RowCapExceeded` otherwise), wrong-getter silently-NULL columns, the deliberately-absent `->`/`->>` operators, lowercase-only view names, and the one working `{params}` pipeline spelling. Read-only by backend enforcement; AGE is a preview backend on Skardi `main` (Neo4j/Kuzu are later milestones). |
| `skills/graph-rag/` | `graph-rag` | Answer questions whose evidence is in graph relationships: find or verify the named entities, traverse from them through read-only Cypher, and report the bounds and confidence of the edges used. |
| `skills/skardi-query-log/` | `skardi-query-log` | Let pipelines grow out of the questions a user keeps asking. Reads the query audit ledger `skardi-server --query-audit-db` writes (read-only), separates ad-hoc SQL from pipeline and job runs, and — when a question really does repeat — installs the pipeline itself: writes the YAML, restarts, probes `/health`, and rolls the file back out if the server does not come up. The agent triggers this on its own; a user never asks for a log analysis. |

> **A running `skardi-server` is required.** Since Skardi's CLI became a thin HTTP client it holds no query engine and no local execution mode, so every path in `auto-context` starts a server, and `retrieval` connects to one that is already running. There is no CLI-only mode.

> **`graph-source` requires Skardi `main`, not v0.5.0.** It was verified against [`1f2ecae`](https://github.com/SkardiLabs/skardi/commit/1f2ecae0f95b0a01232fadb815eae1c1c86efc48); build that checkout with `cargo build --release -p skardi-server`. The latest release, v0.5.0, has neither `type: graph` nor `cypher_query`.

> **`skardi-query-log` requires Skardi `main` too.** The audit ledger it reads is written by `skardi-server --query-audit-db`, which landed after v0.5.0 (`git grep query_audit_db v0.5.0` is empty). Verified against the same commit, [`1f2ecae`](https://github.com/SkardiLabs/skardi/commit/1f2ecae0f95b0a01232fadb815eae1c1c86efc48). `auto-context` and `retrieval` are the released pair — they run on v0.5.0, and only `retrieval`'s `--purpose` / `--session-id` flags need `main`.

## Installation

### Claude Code (plugin marketplace, recommended)

From inside any Claude Code session:

```text
/plugin marketplace add SkardiLabs/skardi-skills
/plugin install skardi@skardi-skills
```

That's it — all five skills are now available across all your projects, and `/plugin marketplace update skardi-skills` pulls future versions.

> **Upgrading from an earlier version:** the individual plugins are now one, named `skardi`, so that every host can install this repository with its own one-line plugin command instead of a manual directory copy. Installed copies of the old per-skill plugins are not removed automatically — run `/plugin uninstall auto-context`, `/plugin uninstall retrieval`, `/plugin uninstall graph-source` and `/plugin uninstall graph-rag`, then install `skardi`. Further back: `auto-knowledge-base` and `auto-rag` were merged into `auto-context`; `skardi-deploy-and-patterns` and `feishu-connector` were retired, and Feishu cloud docs are now raw material for `auto-context`.

### Claude Code (manual copy)

If you'd rather not use the plugin marketplace, copy the skill(s) into your personal skills directory so they're available across all projects:

```bash
# auto-context (searchable context over a folder or your own datastore)
cp -r skills/auto-context ~/.claude/skills/auto-context

# retrieval (answer questions from data a skardi-server already serves)
cp -r skills/retrieval ~/.claude/skills/retrieval

# graph-source (connect a property graph and query it through SQL)
cp -r skills/graph-source ~/.claude/skills/graph-source

# graph-rag (answer questions whose evidence is in graph relationships)
cp -r skills/graph-rag ~/.claude/skills/graph-rag

# skardi-query-log (grow pipelines out of the queries that keep repeating)
cp -r skills/skardi-query-log ~/.claude/skills/skardi-query-log
```

Claude Code will automatically load the relevant skill when your request matches it — e.g. "index these docs" / "make this folder searchable" / "build a RAG" / "expose hybrid search as HTTP" / "RAG service over our pgvector DB" for `auto-context`, or "query our database" / "how many orders last month" / "what tables do we have" for `retrieval`. You can also invoke them directly:

```text
/auto-context
/retrieval
```

### Other hosts, in one command

These hosts install this repository directly through their own plugin or
extension mechanism. Each reads its own manifest in this repo and picks all
five skills up from `skills/`.

**[Gemini CLI](https://github.com/google-gemini/gemini-cli/blob/main/docs/extensions/reference.md)** —
reads `gemini-extension.json`; a skill is discovered by its location, so
`skills/retrieval/SKILL.md` becomes the `retrieval` skill:

```bash
gemini extensions install https://github.com/SkardiLabs/skardi-skills
```

**[Kimi Code](https://moonshotai.github.io/kimi-cli/en/customization/plugins.html)** —
reads `.kimi-plugin/plugin.json`, which points at `./skills/`. From inside a
session:

```text
/plugins install https://github.com/SkardiLabs/skardi-skills
```

**[Pi](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/packages.md)** —
reads the `pi.skills` field in `package.json`:

```bash
pi install git:github.com/SkardiLabs/skardi-skills
```

> **How far these three have been checked.** Each manifest follows the host's own
> published plugin spec, linked above, and is valid against it. None of the three
> has been installed and launched by us yet, so they are documented paths rather
> than measured ones; only the Claude Code path above has been run end to end.
> If a host rejects or silently ignores the plugin, please open an issue saying
> which host and version.

Codex, Cursor and Grok are not in this list because they distribute through
their own reviewed marketplaces rather than from a repository manifest, and
Hermes is not because a Hermes *plugin* has to register each skill from Python
in `__init__.py` (`skills/` is not auto-registered there) and would namespace
them as `skardi:auto-context`. For all three, and for any host below, use the
checkout path.

### Other Agent Skills hosts, from a checkout

Codex, Cursor, Pi, dsh, OpenClaw and Hermes load these skills from a directory
they resolve themselves; they differ in where that directory has to go. All of
them install from a checkout:

```bash
git clone https://github.com/SkardiLabs/skardi-skills.git && cd skardi-skills
```

> **How far each of these has been checked.** The OpenClaw commands for `auto-context` and
> `retrieval` below were run: both install and the host lists them as ready. `graph-source` and
> the rest follow each host's own published skills
> documentation and have not been installed and launched by us. They are the documented paths,
> not measured ones — if one of them does not pick the skill up, please open an issue and say
> which host and version, since that is the kind of thing only a user on that host can catch.

#### Codex, Cursor, Pi, dsh

All four read the cross-tool `~/.agents/skills/` convention, so one copy covers
every one of them:

```bash
mkdir -p ~/.agents/skills
cp -r skills/auto-context ~/.agents/skills/auto-context
cp -r skills/retrieval ~/.agents/skills/retrieval
cp -r skills/graph-source ~/.agents/skills/graph-source
cp -r skills/graph-rag ~/.agents/skills/graph-rag
cp -r skills/skardi-query-log ~/.agents/skills/skardi-query-log
```

To scope the skill to a single project instead, copy it into that repo's
`.agents/skills/`. Each host also keeps a native directory if you'd rather
install per tool — `~/.cursor/skills/` for [Cursor](https://cursor.com/docs/skills),
`~/.pi/agent/skills/` for [Pi](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/skills.md),
`~/.dsh/skills/` for [dsh](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/skills.md)
— while [Codex](https://learn.chatgpt.com/docs/build-skills) uses
`~/.agents/skills/` as its only personal location.

#### OpenClaw

[OpenClaw](https://docs.openclaw.ai/cli/skills) installs from a local path
through its own CLI rather than by copying:

```bash
openclaw skills install ./skills/auto-context
openclaw skills install ./skills/retrieval
openclaw skills install ./skills/graph-source
openclaw skills install ./skills/graph-rag
openclaw skills install ./skills/skardi-query-log
```

That installs into `~/.openclaw/workspace/skills/`, scoped to the active agent
workspace. Add `--global` to install into `~/.openclaw/skills/` instead, which
every local agent sees.

#### Hermes

[Hermes](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)
treats `~/.hermes/skills/` as its source of truth:

```bash
mkdir -p ~/.hermes/skills
cp -r skills/auto-context ~/.hermes/skills/auto-context
cp -r skills/retrieval ~/.hermes/skills/retrieval
cp -r skills/graph-source ~/.hermes/skills/graph-source
cp -r skills/graph-rag ~/.hermes/skills/graph-rag
cp -r skills/skardi-query-log ~/.hermes/skills/skardi-query-log
```

Hermes does not scan `~/.agents/skills/` as a personal directory — inside a git
repo it reads `<repo>/.hermes/skills/` and `<repo>/.agents/skills/`. To share one
personal folder with the hosts above, add it under `skills.external_dirs` in
`~/.hermes/config.yaml`.

#### Anything else

If your host isn't listed, put the skill directory wherever it resolves personal
or project skills and restart it. These files follow the
[Agent Skills open standard](https://agentskills.io/), but conforming to the
format does not guarantee a host will load them — hosts add rules of their own.
`auto-context` is named in kebab-case for that reason: dsh rejects any other
shape outright, and OpenClaw derives its install slug from the same field.

## Bundled resources per skill

### `skills/auto-context/`

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

### `skills/retrieval/`

No scripts — the skill is the procedure: `SKILL.md` is the whole skill.

### `skills/skardi-query-log/`

| Path | Purpose |
|---|---|
| `scripts/read_log.py` | Opens the audit ledger read-only and formats rows — an overview (totals, pass/fail, sessions), the last N statements, one session, failures only. Filters to `statement_kind = 'query'` by default so a pipeline run cannot be counted as another instance of the question it already answers; `--kind pipeline` asks the opposite question, whether a pipeline you built is being called. Makes no judgements: which repeats deserve hardening is the agent's call |
| `scripts/add_pipeline.py` | Installs one pipeline and verifies its own work — writes the YAML, runs the restart command you supply, polls `/health`, and on failure deletes the file and restarts again so the server is left as it was found. `--dir`, `--port` and `--restart-cmd` are required and never guessed: one pipeline that fails to plan takes the whole server down at startup |
