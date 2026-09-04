# skardi-skills

Agent Skills for working with [Skardi](https://github.com/SkardiLabs/skardi) — a backend engine to provide SQL pipelines as http endpoints. These skills give your AI coding agent (Claude Code, Cursor, or any [Agent Skills](https://agentskills.io/)-compatible tool) deep knowledge of Skardi's patterns so you don't have to re-explain them each session.

Check out our demo [here](https://www.youtube.com/watch?v=Cx5jG0OtUuk).

## Available skills

| Directory | Skill name | What it covers |
|---|---|---|
| `auto-context/` | `auto-context` | Turn a folder of documents, a table you already have, or documents still inside a service into governed, searchable context an agent can query. Hybrid search (vector + full-text + RRF) served over HTTP by `skardi-server`. Three raw-material entries, one flow: a folder; an existing table (SQLite read directly and read-only, any other datastore piped in as NDJSON); or fetch-and-land — list, fetch each body, reconcile, ingest — where your agent writes the per-source fetch code and the skill fixes the process and its acceptance criteria. Storage is a separate choice: defaults to a local SQLite file the skill creates and owns (FTS5 + sqlite-vec `vec0` mirrors kept in sync by triggers); point it at Postgres+pgvector, MongoDB, or Lance when the index should live in a database you already own. Handles prereq checks, model resolution, chunking and embedding inline in SQL, ingest, and retrieval end-to-end across `candle` / `gguf` / `remote_embed`. Never creates schema in a datastore you own — prints the SQL and waits — and never writes to a table given as raw material. |
| `retrieval/` | `retrieval` | Answer questions from live data through a running `skardi-server` with the `skardi` CLI. Discovers sources, named pipelines, and the table schemas the deployment exposes, runs the question through any semantic search surface first (`search-hybrid` and friends), then writes read-only SQL against exact qualified table names, checks truncation before trusting counts, and reports with the query attached. Consumes whatever the server already has — it builds no index, writes no data, and starts no servers. |
| `graph-source/` | `graph-source` | Connect a property graph (a knowledge graph, a GraphRAG corpus — Apache AGE / openCypher) to Skardi and query it through SQL, end to end: provision AGE with a least-privilege reader role, declare the `type: graph` source and its views in context YAML, triage registration health (healthy / degraded / refused, and what recovery does and does not answer), write correct queries (`cypher_query`, `graph_schema`, the JSON getters), and wire Cypher parameters into pipelines. Encodes the traps that bite in production: positional `columns` binding (same-typed columns declared out of RETURN order swap silently), no predicate pushdown into view Cypher (the bound lives in the view, `RowCapExceeded` otherwise), wrong-getter silently-NULL columns, the deliberately-absent `->`/`->>` operators, lowercase-only view names, and the one working `{params}` pipeline spelling. Read-only by backend enforcement; AGE is the shipped backend (Neo4j/Kuzu are later Skardi milestones). |
| `graph-rag/` | `graph-rag` | Answer a natural-language question whose answer lives in **relationships**, over a server that has both a graph source and a retrieval surface. Seeds the question (semantic search when it is vague, a property lookup when it names the entity), verifies the seeds resolve in the graph, expands from them with `cypher_query`, and reports with both hops and their bounds attached. Encodes the mechanical reason this is **two hops and not one query** — `connection`/`cypher`/`columns` are plan-time literals, so no single statement can join a retrieval result into a traversal, and the agent is what bridges them — plus the rules that follow: seeds travel in `params` (never concatenated into the Cypher, which is untrusted-input injection), and a dense graph demands three bounds at once (small seed set, `LIMIT` inside the Cypher, a named relationship type). Complements `graph-source`, which connects the graph, and `retrieval`, which answers row questions. |

> **A running `skardi-server` is required.** Since Skardi's CLI became a thin HTTP client it holds no query engine and no local execution mode, so every path in `auto-context` starts a server, and `retrieval` connects to one that is already running. There is no CLI-only mode.

## Installation

### Claude Code (plugin marketplace, recommended)

From inside any Claude Code session:

```text
/plugin marketplace add SkardiLabs/skardi-skills
/plugin install auto-context@skardi-skills
/plugin install retrieval@skardi-skills
/plugin install graph-source@skardi-skills
/plugin install graph-rag@skardi-skills
```

That's it — the skills are now available across all your projects, and `/plugin marketplace update skardi-skills` pulls future versions.

> **Upgrading from an earlier version:** `auto-knowledge-base` and `auto-rag` have been merged into `auto-context`; `skardi-deploy-and-patterns` and `feishu-connector` have been retired. Feishu cloud docs are now raw material for `auto-context`, and Feishu Bitables and chats are moving to Skardi's own Feishu source pack. Installed copies of retired plugins are not removed automatically — run `/plugin uninstall feishu-connector`.

### Claude Code (manual copy)

If you'd rather not use the plugin marketplace, copy the skill(s) into your personal skills directory so they're available across all projects:

```bash
# auto-context (searchable context over a folder or your own datastore)
cp -r auto-context/skills/auto-context ~/.claude/skills/auto-context

# retrieval (answer questions from data a skardi-server already serves)
cp -r retrieval/skills/retrieval ~/.claude/skills/retrieval
```

Claude Code will automatically load the relevant skill when your request matches it — e.g. "index these docs" / "make this folder searchable" / "build a RAG" / "expose hybrid search as HTTP" / "RAG service over our pgvector DB" for `auto-context`, or "query our database" / "how many orders last month" / "what tables do we have" for `retrieval`. You can also invoke them directly:

```text
/auto-context
/retrieval
```

### Other Agent Skills hosts

Codex, Cursor, Pi, dsh, OpenClaw, and Hermes load `auto-context` and `retrieval`
too; they differ in where the skill directory has to go. All of them install from a checkout:

```bash
git clone https://github.com/SkardiLabs/skardi-skills.git && cd skardi-skills
```

#### Codex, Cursor, Pi, dsh

All four read the cross-tool `~/.agents/skills/` convention, so one copy covers
every one of them:

```bash
mkdir -p ~/.agents/skills
cp -r auto-context/skills/auto-context ~/.agents/skills/auto-context
cp -r retrieval/skills/retrieval ~/.agents/skills/retrieval
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
openclaw skills install ./auto-context/skills/auto-context
openclaw skills install ./retrieval/skills/retrieval
```

That installs into `~/.openclaw/workspace/skills/`, scoped to the active agent
workspace. Add `--global` to install into `~/.openclaw/skills/` instead, which
every local agent sees.

#### Hermes

[Hermes](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)
treats `~/.hermes/skills/` as its source of truth:

```bash
mkdir -p ~/.hermes/skills
cp -r auto-context/skills/auto-context ~/.hermes/skills/auto-context
cp -r retrieval/skills/retrieval ~/.hermes/skills/retrieval
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

### `auto-context/`

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

### `graph-rag/`

No scripts — the skill is the procedure, and the two hops are deliberately the agent's to bridge. What ships alongside it:

| Path | Purpose |
|---|---|
| `references/patterns.md` | The four recipes, each with its Cypher, its `columns` declaration and its bound written out: seed-and-expand, entity neighbourhood, path-between, and impact / blast radius — plus the mechanical procedure for writing a positional `columns` declaration against your own `RETURN` clause, and how to join the two hops without splicing retrieved text into the Cypher |
| `references/troubleshooting.md` | Symptom → cause, split into the failures that **announce themselves** and the ones that **return plausible wrong answers** — an all-NULL column (wrong getter), two columns holding each other's values (positional swap), a backwards answer (arrow direction), and an unrepresentative sample (`LIMIT` with no `ORDER BY`) |
| `evals/evals.json` | Three behavioural cases: an impact question that must discover the graph's shape before expanding, a vague question that must seed semantically and report unresolved seeds, and a rows-not-edges question the skill must hand off rather than invent a graph angle for |

### `retrieval/`

No scripts — the skill is the procedure. What ships alongside it is the eval harness:

| Path | Purpose |
|---|---|
| `evals/evals.json` | Seven behavioural cases: no search surface present, semantic-first with a stale-document conflict, a catalog whose tables cannot be enumerated, an undeclared pipeline that must not be probed, truncated rows that must not feed client-side statistics, no learning-chain claim on v0.5.0, and a `limit`-named parameter that does not bound anything |
| `evals/fixtures/make_data.py` | Deterministic fixture data (seed 7): 1500 orders across four statuses, plus a full-text table holding a current policy note and a superseded one that outranks it |
| `evals/fixtures/setup.sh` | Starts a `skardi-server` on the fixture in one of three shapes — full, `--bare` (a catalog with no table names anywhere), `--no-search` (no search surface registered). Refuses a port that is already answering before it touches any fixture state |
| `evals/fixtures/pipelines/` | Includes two deliberate traps: `refresh-orders` really inserts into a writable source, and `recent-orders` is truthfully declared read-only yet uses its `limit` parameter as an id threshold |
