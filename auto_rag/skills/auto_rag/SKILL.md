---
name: auto_rag
description: Stand up a hybrid-search RAG service over a user-supplied database (PostgreSQL+pgvector, MongoDB, or Lance) by running `skardi-server` (skardi-server-rag image, Skardi >= 0.4.0) in front of it. The skill never creates databases or schema for the user — it asks for connection details, prints the schema SQL the user must run themselves, generates the Skardi context + pipelines, starts the server, and then drives ingestion and querying via the HTTP API. Use this skill whenever the user wants to expose RAG / hybrid search as a REST endpoint, share retrieval across multiple agents or processes, plug Skardi into an existing production datastore (pgvector, atlas search, lance dataset), or move from "local SQLite KB on my laptop" to "service my team can hit". Trigger this skill on phrases like "RAG server", "search API over my postgres", "hybrid search service", "expose vector search as HTTP", "skardi-server with pgvector", "production RAG on top of our existing DB", "REST endpoint for embeddings / nearest-neighbour", or any time the user wants retrieval + answer synthesis but already has the storage in place. Prefer `auto_knowledge_base` instead when the corpus is small, single-machine, and the user is happy with a local SQLite file.
---

# auto_rag — server-backed RAG over a user-supplied datastore

Your job: bring up a working RAG pipeline on top of a database **the user already controls**, served as REST endpoints by `skardi-server`, and then ingest a corpus and answer questions over it. The user owns the data; you own the orchestration.

This skill is the server-side counterpart to `auto_knowledge_base`. The CLI skill is the right tool when the corpus fits on one laptop and one agent will query it. Reach for `auto_rag` when the retrieval has to live on a network: multiple agents, a shared pgvector cluster, an existing Atlas search index, a Lance lake — anything the user does not want a sibling SQLite file next to.

> **Skardi 0.4.0+ required.** Both ingest and search now do their work inside one server-side SQL statement: chunking via `chunk()`, embedding via `candle()` / `gguf()` / `remote_embed()`, and writing — all in one INSERT for ingest, and embedding inline for vector / hybrid search. The pre-built image that bundles all of this is `ghcr.io/skardilabs/skardi/skardi-server-rag:latest` (built with `--features rag`); the older `skardi-server-embedding` image does not register `chunk()` and breaks the rendered pipelines.

## What this skill will and will not do

**Will do.** Render the Skardi `ctx.yaml` + `semantics.yaml` + pipeline YAMLs that target the user's datastore, start `skardi-server` in the background, ingest a corpus over HTTP (server chunks + embeds inline), and route each user question through `/search-hybrid/execute` (or its single-signal siblings) to a grounded answer.

**Will not do.** Create databases, create schemas, run `CREATE EXTENSION`, install drivers, or hand out credentials. The user provides every connection string, every credential, every schema. If the schema does not exist yet, the skill prints the SQL the user must run in their own session and stops. *This is a hard line — never run schema-creation DDL against a user-supplied connection without the user explicitly asking for it.* The blast radius is too big to take on autopilot: a stray DROP can lose hours of someone else's work, and `CREATE EXTENSION` on a managed Postgres can require superuser the agent does not have anyway. Spell out what is needed and let the user run it.

For testing **the skill itself** during development you are free to spin up disposable Docker containers — that is not "the user's data". The line is about user-supplied datastores at runtime.

## Prerequisites the user must supply

Before doing anything, confirm these in one round of questions. If any are missing, ask for them — do not guess.

1. **Backend type.** One of `postgres` (pgvector + pg_fts), `mongo` (mongo_knn + mongo_fts), or `lance` (lance_knn + lance_fts). Default to `postgres` if the user has no preference — it has the cleanest dual-signal hybrid story. The Skardi source tree ships a working reference at `demo/rag/server/`.
2. **Connection details.** For Postgres: a connection string (`postgresql://host:port/db?sslmode=...`) plus a user/password the agent will read from env vars `PG_USER` / `PG_PASSWORD`. For Mongo: connection URI + DB/collection name + `MONGO_USER` / `MONGO_PASS`. For Lance: an absolute path or `s3://...` URL to the dataset directory.
3. **Table / collection / dataset name.** Where the embeddings + content will live. The skill assumes one combined table that holds both `content` (TEXT, indexed for FTS) and `embedding` (vector type) — a design choice that lets one INSERT keep both signals on the same row.
4. **Embedding backend + model + dimension.** See the next section — *do not pick this silently*. The choice has knock-on effects on the schema (vector column dim must match) and on the user's bill (remote APIs are pay-per-call). Confirm with the user.
5. **Whether the schema is already in place.** If yes, proceed. If no, the skill prints the SQL block the user must run themselves (see below) and stops until they confirm.

If the user only says "set up RAG over our pgvector DB", you have backend + (probably) connection details. Confirm dimension and table name explicitly — those two are the most common silent-failure surface.

### Choosing the embedding backend

Skardi exposes three embedding UDFs, all of which return a `List<Float32>` and slot into the same pipeline shape. The right one depends on the user's deployment, not on habit. The Skardi source tree's `docs/embeddings/{candle,gguf,remote}/README.md` is the authoritative per-backend walkthrough; the table below is the decision shortcut.

| UDF | Signature | Reach for it when | Skardi build feature |
|---|---|---|---|
| `candle(model_dir, text)` | local HuggingFace SafeTensors (BERT / RoBERTa / DistilBERT / Jina families) | Local, simple deps, general English text. The default for self-hosted setups when the corpus fits on one box. Common picks: `bge-small-en-v1.5` (384-d, ~130 MB), `bge-base-en-v1.5` (768-d, ~430 MB), `bge-large-en-v1.5` (1024-d, ~1.3 GB), `all-MiniLM-L6-v2` (384-d, very small), `multilingual-e5-large` (1024-d) for non-English, `jina-embeddings-v2-base-code` (768-d) for code corpora. | bundled in `--features rag`; or `--features candle` à la carte |
| `gguf(model_dir, text)` | local llama.cpp-format quantised weights | Local, RAM-constrained, want a bigger model at a smaller footprint, or the model only ships as GGUF. Common picks: `embeddinggemma-300m-qat-Q8_0.gguf` (256-d, ~330 MB, requires accepting Google's Gemma licence), `nomic-embed-text-v1.5` GGUF (768-d), or any GGUF quantisation of bge-large. | `--features gguf` |
| `remote_embed(provider, model, text)` | hosted API | No local compute, top-tier model quality, willing to pay per call. Providers + dims: `('openai','text-embedding-3-small')` 1536-d, `('openai','text-embedding-3-large')` 3072-d, `('voyage','voyage-3')` 1024-d, `('voyage','voyage-code-3')` 1024-d (best for code), `('gemini','text-embedding-004')` 768-d, `('mistral','mistral-embed')` 1024-d. Each requires its API key in the server's environment (`OPENAI_API_KEY`, `VOYAGE_API_KEY`, `GEMINI_API_KEY`, `MISTRAL_API_KEY`). | `--features remote-embed` |

**Decision rule.** Ask the user (or infer) and pick:

1. **Hosted-API budget, or must it be local?** If local-only, choose between candle and gguf. If a hosted API is fine, `remote_embed` is usually the easiest highest-quality path — no model files to manage, no GPU.
2. **Dominant content type?** Code → `voyage-code-3` (remote) or `jina-embeddings-v2-base-code` (candle). Multilingual → `multilingual-e5-large` (candle) or `text-embedding-3-large` (remote). Long documents (>512 tokens per chunk) → `nomic-embed-text` (candle/gguf, 8k context). Otherwise → bge family on candle.
3. **Memory budget on the server?** Small VM (≤2 GB free) → bge-small candle or a Q4/Q8 gguf. 8 GB+ → bge-large candle is fine. No on-box compute → remote.
4. **Image / build feature flags?** `skardi-server-rag` already includes candle and chunking; for `gguf` or `remote_embed` you need a server build with that extra feature flag. `setup_rag.py` doesn't auto-rebuild — surface the cargo command if the wrong build is on PATH.

**State the choice explicitly** — one sentence to the user — and then move on. Something like *"I'm using `candle('bge-small-en-v1.5')` (384-d, local, ~130 MB) because your corpus is English markdown and you said no remote APIs; the schema needs `vector(384)`."* Don't bury the decision; the user will want to know, and an explicit framing makes the dim and feature flag obvious.

**The dim must match the schema.** If the user says they already created `vector(1024)`, you cannot pick a 384-d model — every INSERT will fail with a dim mismatch. If the schema is being created fresh, lock the dim to whatever the chosen model produces; if the schema already exists, only models with that exact dim are eligible. A dim mismatch after the table is populated means dropping and rebuilding — there is no in-place fix.

### Schema the user needs to create (per backend)

Print the matching block for the chosen backend, ask the user to run it in their own session, and wait for confirmation before continuing. The exact SQL also lives in [references/schemas.md](references/schemas.md).

#### PostgreSQL + pgvector

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE <table> (
    id        BIGINT PRIMARY KEY,
    source    TEXT NOT NULL,
    chunk_idx INTEGER NOT NULL,
    content   TEXT NOT NULL,
    embedding vector(<DIM>)         -- must match the chosen embedding model's output dim
);

-- HNSW for vector ANN
CREATE INDEX ON <table> USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

-- GIN for FTS over the same content column
CREATE INDEX <table>_content_fts_idx
  ON <table>
  USING GIN (to_tsvector('english', content));
```

Note the `id BIGINT` (not `BIGSERIAL`): DataFusion's INSERT planner mis-handles `SERIAL` columns — the symptom is `Invalid batch column at '0' has null but schema specifies non-nullable` on every INSERT. The skill emits stable 64-bit ids client-side derived from `(source, chunk_idx)`, so client-side ids work fine.

#### MongoDB / Lance

See [references/schemas.md](references/schemas.md) for the document shape and index-creation snippets. The agent never creates these — the user runs them.

## Where does the server actually run? Three runtimes

`start_server.py` supports three execution targets via `--runtime`:

| Runtime | Pick when | Notes |
|---|---|---|
| `local-process` (default) | Single-laptop dev, prototyping. | Needs `skardi-server` on PATH (or pass `--skardi-source` to fall back to `cargo run --features rag`). |
| `docker` | Shipping a contained service to a teammate, isolating from host libs. | Pulls `ghcr.io/skardilabs/skardi/skardi-server-rag:latest` (chunk + candle + gguf + remote-embed bundled via `--features rag`). Connection string must be reachable from inside the container — `host.docker.internal` is auto-mapped. |
| `kubernetes` | The agent already runs in a cluster, multi-replica, shared retrieval. | Renders Deployment + Service + ConfigMap + Secret into `<workspace>/k8s/`; `--apply` actually deploys; `--port-forward` exposes locally for debug. |

Pick based on **where the agent calling this RAG service ultimately runs**, not on what's most familiar. If the agent ends up in a Kubernetes deployment, deploying skardi-server alongside it removes the host-network hop entirely. If the agent is a CLI on a developer laptop, local-process is fastest. The full per-runtime walk-through (mounts, networking, lifecycle, kubectl flags, port-forward, cleanup) lives in [references/runtimes.md](references/runtimes.md) — read it once when you pick a non-default runtime, especially for kubernetes where PG-reachability-from-cluster is the user's responsibility.

## Embedding and chunking happen on the server

A note on architecture: with Skardi 0.4.0 + the `skardi-server-rag` image, both `chunk()` and the embedding UDFs are registered server-side. So the rendered pipelines do everything inside one INSERT (for ingest) or one SELECT (for search):

- **Ingest** (`/ingest-chunked/execute`) — the agent POSTs the raw document body. The server splits it via `UNNEST(chunk('markdown', content, ...))`, embeds each chunk, and INSERTs one row per chunk in a single transaction.
- **Search** (`/search-vector/execute`, `/search-hybrid/execute`) — the agent POSTs the question as plain text. The server embeds it inline as the `pg_knn` argument; no client-side embedding step.

This is a deliberate simplification from earlier versions of this skill, which split embedding off to the agent side because the published `skardi-server-embedding` image historically did not register the candle UDF. The new `skardi-server-rag` image does, so the server is the right place for it: every agent talking to the same service gets the same embedding model, and adding new languages of agents (an MCP client, a curl wrapper) doesn't require shipping the model and the CLI to every caller.

If you really want client-side embedding (e.g. you're running `skardi-server-embedding` and can't redeploy), see the troubleshooting note at the bottom of this file — but it's strictly the older path and not the default.

## The end-to-end flow (Postgres path — the default)

Five steps. 1–2 are one-time setup; 3–5 are per-question (or per-corpus for step 3).

```
1. Confirm prerequisites (above) and have the user run the schema SQL.
2. python SKILL_DIR/scripts/setup_rag.py \
       --workspace ./rag --backend postgres \
       --connection-string "postgresql://localhost:5432/ragdb?sslmode=disable" \
       --table documents \
       --embedding-udf candle \
       --model-path /abs/path/to/<chosen-model-dir> \
       --embedding-dim <model-dim>
   # Or for gguf:    --embedding-udf gguf  --model-path /abs/path/to/<model-dir-with-.gguf>
   # Or for remote:  --embedding-udf remote_embed --embedding-args "'openai','text-embedding-3-small'"
3. python SKILL_DIR/scripts/start_server.py --workspace ./rag --port 8080
   # Local-process default; add --runtime docker or --runtime kubernetes as needed.
4. python SKILL_DIR/scripts/ingest_corpus.py --workspace ./rag --corpus <docs/>
   # One POST per file to /ingest-chunked/execute; server chunks + embeds inline.
5. For each user question:
   curl -X POST http://localhost:8080/search-hybrid/execute \
     -H 'Content-Type: application/json' \
     -d '{"query":"...", "text_query":"...",
          "vector_weight":0.5, "text_weight":0.5, "limit":5}'
   → synthesise grounded answer (see Step 5).
```

Read `SKILL_DIR` as the absolute path to the directory containing this SKILL.md.

The big change vs. older versions of this skill: there is no separate chunker, no client-side embedder, no client-side vector parameter. Three Python scripts (`chunk_corpus.py`, `embed.py`, `http_ingest.py`) collapse into one (`ingest_corpus.py`) that just walks files and POSTs whole-document content to `/ingest-chunked/execute`.

### Step 1 — Confirm prerequisites

Already covered above. The agent's posture here is "I will do nothing destructive — please confirm what is in place." If the user says "I haven't created the table yet", print the schema SQL and wait. Do not get clever and try to run it via `psql` or `skardi query` — even when the agent technically *can* connect, doing the user's DDL behind their back is a trust violation, and the schema choices (index parameters, secondary indices, auth) are the user's call.

### Step 2 — Render the workspace

`scripts/setup_rag.py` is idempotent. It:

1. Verifies `skardi --version >= 0.4.0`. The CLI is used here only for the `SELECT 1` health probe, but the version is also a proxy for whether the server build the user has access to is recent enough to register `chunk()` and the semantics overlay loader.
2. Resolves the embedding model based on `--embedding-udf`:
   - `candle` — `--model-path` required (any HuggingFace repo dir with `model.safetensors` + `config.json` + `tokenizer.json`).
   - `gguf` — `--model-path` required (directory containing the `.gguf` file).
   - `remote_embed` — no model files; the relevant API key (`OPENAI_API_KEY` / `VOYAGE_API_KEY` / `GEMINI_API_KEY` / `MISTRAL_API_KEY`) must be in the server's env when start_server runs.
3. Renders `<workspace>/{ctx.yaml, semantics.yaml, pipelines/{ingest,ingest_chunked,search_vector,search_fulltext,search_hybrid}.yaml}` from templates in `SKILL_DIR/assets/<backend>/`. The embedding UDF call is baked into each pipeline at render time over the right column reference (`content`, `chunk_text`, or `{query}`).
4. **Health-checks the user's connection before printing the next-step banner.** Runs `skardi query --sql "SELECT 1 FROM <table> LIMIT 1"` against the rendered ctx — a fast, harmless probe that surfaces auth / network / table-missing errors at setup time rather than during ingest. Pass `--skip-health-check` when the connection string can't be resolved from where setup_rag.py runs (e.g. when the rendered ctx points at `host.docker.internal` for the docker runtime). If the live probe fails, print the error and stop; do not write a half-finished workspace.
5. Writes a `.embedding.txt` breadcrumb so other scripts know which UDF / model / dim was chosen without re-parsing the YAML.

### Step 3 — Start `skardi-server`

`scripts/start_server.py` picks an execution target via `--runtime` (default `local-process`). All three runtimes use the same rendered workspace. Per-runtime details — mounts, networking, lifecycle — live in [references/runtimes.md](references/runtimes.md); the SKILL.md summary:

- `--runtime local-process` (default) — runs `skardi-server` as a host process. Needs the binary on PATH (build with `--features rag` or at minimum `--features chunking` + the embedding UDF you chose). Backgrounded under `nohup`, logs at `<workspace>/server.log`, pid at `server.pid`.
- `--runtime docker` — runs `ghcr.io/skardilabs/skardi/skardi-server-rag:latest` under `docker run`. Mounts the workspace at the same absolute path inside the container so paths in ctx.yaml resolve unchanged. Forwards `PG_USER`, `PG_PASSWORD`, and embedding API keys; auto-maps `host.docker.internal` for cross-OS host access. Container ID and name are stashed in `<workspace>/server.state.json` for clean teardown.
- `--runtime kubernetes` — renders Deployment + Service + ConfigMap + Secret into `<workspace>/k8s/`. Without `--apply` we stop there; with `--apply` we `kubectl apply -f <workspace>/k8s/` and wait for the deployment to roll out. With `--port-forward` we additionally background a `kubectl port-forward svc/<release> <local>:<port>` so the agent can hit `http://localhost:<port>` from outside the cluster.

In every runtime, after startup we:
1. Poll `GET /health` for up to `--health-timeout` seconds.
2. Check `GET /pipelines` lists the five expected names (`ingest`, `ingest-chunked`, `search-vector`, `search-fulltext`, `search-hybrid`) and warn about any missing.
3. Write `<workspace>/server.runtime` and `<workspace>/server.port` so `ingest_corpus.py` and `stop_server.py` know what was launched.

The dashboard at `http://localhost:<port>/` is a fast sanity check — open it in a browser and confirm the pipelines show up with the right parameter lists.

### Step 4 — Ingest the corpus (server-side chunk + embed)

`scripts/ingest_corpus.py` walks the corpus directory, strips front-matter, and POSTs one request per source file to `/ingest-chunked/execute`:

```json
{
  "doc_id":     <stable 53-bit id>,
  "source":    "<rel/path/within/corpus>",
  "content":   "<full file body>",
  "chunk_size": 1200,
  "overlap":    200
}
```

The server's `ingest-chunked` pipeline runs:

```sql
INSERT INTO <table> (id, source, chunk_idx, content, embedding)
SELECT
  CAST({doc_id} AS BIGINT) * 1000 + chunk_idx       AS id,
  {source}                                          AS source,
  chunk_idx,
  chunk_text                                        AS content,
  candle('<abs-model-path>', chunk_text)            AS embedding
FROM (
  SELECT
    UNNEST(generate_series(CAST(0 AS BIGINT),
                           CAST(array_length(chunks) AS BIGINT) - 1)) AS chunk_idx,
    UNNEST(chunks)                                                    AS chunk_text
  FROM (
    SELECT chunk('markdown', {content}, {chunk_size}, {overlap}) AS chunks
  ) c
) r
```

That is one statement, one transaction: chunk → embed → write per chunk for the entire document. The embedding model loads once per server process, so the first POST pays the cold-start cost (~5–30 s depending on model size) and every subsequent POST runs at the throughput of the embedding inference itself.

**Why `chunk_idx` is derived this way (determinism).** `chunk()` returns an *ordered* `List<Utf8>`. The pipeline enumerates it with `generate_series(0 … array_length-1)` and unnests that index list in the **same projection** as `UNNEST(chunks)`; DataFusion zips two unnests in one projection positionally, so index *i* is paired with chunk *i* by construction. This matters: an earlier version numbered chunks with `ROW_NUMBER() OVER (ORDER BY 1)`, but `ORDER BY 1` orders by the constant literal `1` — every row is a peer, so the executor was free to assign `chunk_idx` in any order, and the same chunk could land at a different `chunk_idx` (hence a different `id`) on every run. That is the classic cause of "the same files produce different chunks/ids each time." `UNNEST … WITH ORDINALITY` would be the tidy fix but DataFusion 52 rejects it (`not_impl`), which is why the index is generated explicitly.

**Resumability.** The progress manifest at `<workspace>/ingest_progress.json` is keyed by source path; each value records `{status, hash}` where `status` is `ok` or `err: ...` and `hash` is a SHA-256 of the file body. On retry, unchanged already-ok files are skipped — important because stable doc_ids mean a re-POST of the same file collides on the primary key, which Postgres rejects loudly. Files whose content changed since they were ingested are detected via the hash and surfaced (see *Reproducibility* below), not silently skipped. (Manifests written by older versions stored a bare `ok`/`err` string with no hash; those are read back as `hash: null` and backfilled on the next run.)

**Concurrency.** `--concurrency 1` is the default. The bottleneck for self-hosted candle/gguf is single-thread embedding throughput, so going wider rarely helps; `remote_embed` benefits from 4–8 inflight POSTs because each one is mostly waiting on network. Tune to taste.

**Verify the corpus.** After ingest, hit `/search-fulltext/execute` with a known token from the corpus and confirm rows come back. Or run a count query directly: `psql -c "SELECT count(*) FROM <table>"` against the user's PG. A zero count means INSERTs failed silently — read `<workspace>/server.log` and the progress manifest.

### Step 5 — Retrieve and synthesise

This is where the answer quality is made or lost. Most of the guidance from `auto_knowledge_base/SKILL.md § Step 4` applies verbatim — read it for the full reasoning. The HTTP-specific bits:

The vector / hybrid endpoints embed the question server-side, so callers just pass plain text. Two-call pattern looks like:

```bash
# Hybrid (default — RRF over pg_knn + pg_fts; server embeds {query} inline)
curl -s -X POST http://localhost:8080/search-hybrid/execute \
  -H 'Content-Type: application/json' \
  -d '{"query":"who is the white rabbit?",
       "text_query":"white rabbit",
       "vector_weight":0.5, "text_weight":0.5, "limit":5}' | jq .

# Vector-only — paraphrase / conceptual queries
curl -s -X POST http://localhost:8080/search-vector/execute \
  -H 'Content-Type: application/json' \
  -d '{"query":"a creature that checks its pocket watch","limit":5}' | jq .

# FTS-only — named entity / exact string. No embedding involved.
curl -s -X POST http://localhost:8080/search-fulltext/execute \
  -H 'Content-Type: application/json' \
  -d '{"query":"\"Rabbit-Hole\"","limit":5}' | jq .
```

`pg_fts` uses `websearch_to_tsquery`, which is strict about query syntax — `?`, bare `"`, `+`, `-`, `~`, `^`, and parentheses are operators. Phrase-quote multi-word terms and strip stray punctuation from natural-language queries.

**Multi-query when the question is multi-part.** Same rule as the CLI skill: don't shove everything into one search. Two or three scoped queries (one per sub-question, or vector + FTS pair for a definitional question) consistently beat one kitchen-sink query, because RRF dilutes when the embedding has to cover too many concepts at once. The scoring constant in `search_hybrid.yaml` is the standard RRF k=60.

**Grounding principle: don't synthesise past the retrieval.** Every claim in the answer must come from a chunk you actually retrieved. If a fact isn't there, say so plainly — report what you searched for, what you found, and that the corpus doesn't speak to the gap. Don't invent a plausible-sounding citation from training data. This rule is what makes the retrieval *useful*; agents that hallucinate citations destroy trust in the whole stack, while agents that flag absence honestly preserve it. Side observations only land if the chunks you cite actually say them *and* they bear on the specific question; otherwise they're padding.

**Answer structure.** Same template as `auto_knowledge_base` — sub-claims with verbatim quotes from chunks, citations to `(source, chunk_idx)`, optional citation table at the end for multi-claim answers.

## Catalog semantics

`setup_rag.py` also renders a `semantics.yaml` next to `ctx.yaml`. Skardi auto-discovers it on startup and surfaces the table + column descriptions on `GET /data_source` (the catalog endpoint agents inspect when picking a tool) and inside `skardi query --schema --all`. That's the channel through which an unfamiliar agent learns what `documents.embedding` is for or how `chunk_idx` is assigned. The file is regenerated on every `setup_rag.py` run; for hand-curated descriptions, drop a second `kind: semantics` file (any name) into a `semantics/` directory next to `ctx.yaml` and both will be merged at load time.

## Reproducibility — same corpus in, same index out

If the goal is a stable reference library (e.g. a code-search index rebuilt as connected repos change), the same input files must produce the same chunks, ids, and — as far as the stack allows — the same retrieval order. What the skill guarantees, and what you have to hold fixed:

- **`chunk_idx` and `id` are deterministic by construction** (see the ingest SQL above). Re-ingesting the same file body yields the identical `(chunk_idx, content)` pairs and identical ids. This is the fix for "different chunks/datasets every run."
- **Hold the chunk parameters fixed.** `--chunk-size` / `--overlap` change chunk boundaries, so changing them changes every chunk, id, and embedding. Pin them per library. `chunk()` itself (text-splitter) is deterministic for a given mode + size + overlap.
- **Pin a *local* embedding model for reproducible vectors.** `candle` / `gguf` over fixed local weights are deterministic (CPU inference, no dropout), so identical chunk text → identical embedding. `remote_embed` is **not** reproducible over time: hosted providers silently reversion models and may return slightly different vectors, which can reorder near-tie neighbours. For a library that must reproduce exactly, prefer a pinned local model (e.g. `jina-embeddings-v2-base-code` for code) and record the model + dim.
- **HNSW is approximate and insertion-order-sensitive.** pgvector's HNSW graph depends on insert order, so top-k and recall can differ slightly across a full rebuild even with identical rows. For byte-identical retrieval on smaller corpora, either accept the small variance, raise `ef_search`, or use exact search. The search pipelines already break score ties on `id` so *equal-score* rows no longer reorder run to run — but ANN candidate selection itself is still approximate.
- **Changed files are surfaced, not silently skipped.** `ingest_corpus.py` records a content hash per source. A re-run skips unchanged files, ingests new/errored ones, and prints a warning listing files whose content changed since ingest (with the `DELETE FROM <table> WHERE source = '…'` + re-run steps) — it does not auto-delete user rows.

## Building a code reference library over multiple repos

This is a common use of the skill; a few things that are easy to get wrong:

- **Include code extensions.** `ingest_corpus.py`'s default `--include` is `*.md,*.markdown,*.txt,*.rst` — pass code globs explicitly, e.g. `--include "*.py,*.ts,*.tsx,*.go,*.rs,*.java,*.rb,*.md"`. Without this, a code repo ingests almost nothing.
- **Chunk mode is `markdown`.** The pipeline chunks with `chunk('markdown', …)`, which splits on prose/markdown structure, not code structure — deterministic, but boundaries can straddle functions. For code-heavy corpora consider a code-embedding model (`voyage-code-3` remote, or `jina-embeddings-v2-base-code` local) so the vectors at least understand code, and keep chunk size generous. A code-aware chunk mode is a Skardi-side enhancement, not something this skill can do client-side today.
- **Keep `source` stable across rebuilds.** `source` (and therefore `doc_id`/`id`) is the path **relative to `--corpus`**. If you point `--corpus` at a repo root one run and at a parent directory-of-repos the next, every `source` shifts and all ids change. For a multi-repo library, ingest each repo from a fixed layout (e.g. always `--corpus <root>` where `<root>/<repo>/…`) so `source` reads as `repo/path/within/repo` consistently. If you re-clone repos to fresh temp dirs, normalise to that same relative layout before ingesting.

## Scaling ingest

The HTTP loop is fine up to a few thousand files. The bottleneck for larger corpora is the per-call overhead (HTTP round-trip + chunk + embed for the whole document), not the server's INSERT throughput. Two ways to push past that:

- **Bulk INSERT via a Skardi job.** Define a `kind: job` that reads pre-embedded chunks from object storage (`s3://...`, a parquet file, etc.) and writes them in one transaction — useful when you have a large corpus of pre-chunked, pre-embedded data. Submit via `POST /jobs/<name>/run`, poll `/jobs/runs/<id>` until done. The Skardi `docs/jobs.md` is the working reference for the YAML shape and run ledger.
- **Filesystem-shared bulk INSERT.** When the server has filesystem access to a manifest file (typical for `--runtime local-process`), one `INSERT INTO <table> SELECT ... FROM './manifest.json'` collapses the whole corpus into a single statement and amortises the cold-start once. Useful when you control both the agent host and the server host.

If the user's question is "I have 5M chunks", neither this skill nor `auto_knowledge_base` is right out-of-the-box — point them at Lance + jobs and say so explicitly rather than letting either skill grind for hours.

## Customising

- **Switching the embedding backend after setup.** Re-run `setup_rag.py` with the new `--embedding-udf` and `--model-path` / `--embedding-args`. Rendered pipelines bake in the embedding call at render time, so the change requires re-rendering and restarting the server. **The vector dim must still match the schema** (the `vector(N)` column is fixed at table-create time); a dim change means rebuild from scratch.
- **Tighter ANN parameters.** Postgres HNSW takes `m` and `ef_construction` at index-build time; raising them helps recall on larger corpora. The schema SQL printed in Step 1 uses `m=16, ef_construction=64` — fine for <1M rows. For a 10M-row corpus, suggest `m=32, ef_construction=200` and warn the user the index build will be slow.
- **Filter pushdown.** `pg_knn` and `pg_fts` accept an optional WHERE-style filter as a final argument. If the user wants per-tenant retrieval, they can either add a `tenant_id` column and filter in the pipeline SQL (`WHERE tenant_id = {tenant_id}`), or add a metadata-aware pipeline like `search_hybrid_for_tenant.yaml`. Keep this off the default path — most users don't need it and adding it is cheap when they do.
- **Different chunk size / overlap.** Pass `--chunk-size` / `--overlap` to `ingest_corpus.py` (per-corpus knobs) — they're pipeline parameters, not template-time substitutions, so changing them needs no re-render. The constraint is `overlap < chunk_size`; both must be positive integers.
- **Custom chunker.** The `ingest` pipeline (one row per call) is rendered alongside `ingest-chunked` for users who already have chunk text in hand from a custom chunker (token-based, code-aware, semantic). Just POST `{doc_id, source, chunk_idx, content}` to `/ingest/execute` and the server embeds + writes the row.

## Troubleshooting

If something looks off, read [references/troubleshooting.md](references/troubleshooting.md). The most common failure modes:

- **`unknown function: chunk`** — the server is an older `skardi-server-embedding` image (or a build without `--features chunking` / `--features rag`). Switch to `skardi-server-rag:latest` or rebuild with `--features rag`.
- **`unknown function: candle`** (or `gguf` / `remote_embed`) — the server build is missing the matching feature. Same fix as above (`--features rag` bundles candle; gguf and remote-embed are à la carte).
- **`role "<user>" does not exist`** — env vars not exported. The server reads `PG_USER`/`PG_PASSWORD` from its own environment at startup, so they must be set *before* `start_server.py` runs. In the docker runtime they're forwarded into the container automatically; in kubernetes they're carried in the rendered Secret (which is built from `os.environ` at render time).
- **`relation "<table>" does not exist`** — the user hasn't run the schema SQL. Print it again, wait.
- **`failed to load extension "vector"`** — pgvector not installed. Either point at `pgvector/pgvector:pg16` (Docker) or run `CREATE EXTENSION vector` after installing the OS package. The agent does not do this; it surfaces the error.
- **`fts5: syntax error`** / **`pg_fts: syntax error in tsquery`** — the user's question contains web-search-syntax reserved characters. Strip them or phrase-quote. Doesn't break the vector half of hybrid search, so the answer is degraded but not empty.
- **HTTP 500 from `/ingest-chunked/execute` mentioning `null but schema specifies non-nullable`** — table was created with `SERIAL PRIMARY KEY`. Switch to plain `BIGINT PRIMARY KEY` (the chunker emits explicit ids).
- **Docker container can't reach Postgres on the host** — connection string says `localhost`; the container's loopback isn't the host's. Re-render with `host.docker.internal` (or `--network host` on Linux).
- **HTTP 502 on every ingest/search call** — a system HTTP proxy on `127.0.0.1:7897` (mihomo / clash) is intercepting localhost requests. `ingest_corpus.py` already adds `localhost,127.0.0.1` to `NO_PROXY` for its own urllib calls; if you're hitting raw `curl`, prefix with `NO_PROXY=localhost,127.0.0.1` or unset `http_proxy`/`https_proxy`/`all_proxy` for the call.

### Falling back to client-side embedding

If the only server image you can run is the older `skardi-server-embedding` (or you're stuck on Skardi 0.3.x), you can still wire this skill up by computing embeddings on the agent side and POSTing them as a precomputed `query_vec` parameter — that's the architecture earlier versions of this skill used. The pipeline templates in this version don't render that shape, but you can copy the pre-0.4.0 templates from git history (`git log --all -- assets/postgres/pipelines/`) and adjust accordingly. We don't recommend this — the new path is simpler and removes a whole category of "client and server are out of sync about what the embedding looks like" bugs.

## When to choose this skill vs. `auto_knowledge_base`

Pick `auto_knowledge_base` for: single-machine corpora, demos, "I just want to query my notes folder", anywhere the user is happy with one SQLite file and zero infra. The CLI path is faster to set up and easier to ship to a teammate as one directory.

Pick `auto_rag` for: shared infra (multiple agents, multi-process serving), datastores the user already runs in production, RAG that needs to live behind an HTTP boundary so non-Claude callers (other services, MCP clients, browser tools) can hit it, and corpora large enough that running them on a laptop is awkward.

The pipeline YAMLs are largely shape-compatible — you can prototype with the CLI skill and swap to `auto_rag` later by re-pointing `ctx.yaml` at the production datastore and re-running the ingest loop. Nothing in the corpus changes.
