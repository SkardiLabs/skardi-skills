---
name: auto_rag
description: Stand up a hybrid-search RAG service over a user-supplied database (PostgreSQL+pgvector, MongoDB, or Lance) by running `skardi-server` in front of it. The skill never creates databases or schema for the user — it asks for connection details, prints the schema SQL the user must run themselves, generates the Skardi context + pipelines, starts the server, and then drives ingestion and querying via the HTTP API. Use this skill whenever the user wants to expose RAG / hybrid search as a REST endpoint, share retrieval across multiple agents or processes, plug Skardi into an existing production datastore (pgvector, atlas search, lance dataset), or move from "local SQLite KB on my laptop" to "service my team can hit". Trigger this skill on phrases like "RAG server", "search API over my postgres", "hybrid search service", "expose vector search as HTTP", "skardi-server with pgvector", "production RAG on top of our existing DB", "REST endpoint for embeddings / nearest-neighbour", or any time the user wants retrieval + answer synthesis but already has the storage in place. Prefer `auto_knowledge_base` instead when the corpus is small, single-machine, and the user is happy with a local SQLite file.
---

# auto_rag — server-backed RAG over a user-supplied datastore

Your job: bring up a working RAG pipeline on top of a database **the user already controls**, served as REST endpoints by `skardi-server`, and then ingest a corpus and answer questions over it. The user owns the data; you own the orchestration.

This skill is the server-side counterpart to `auto_knowledge_base`. The CLI skill is the right tool when the corpus fits on one laptop and one agent will query it. Reach for `auto_rag` when the retrieval has to live on a network: multiple agents, a shared pgvector cluster, an existing Atlas search index, a Lance lake — anything the user does not want a sibling SQLite file next to.

## What this skill will and will not do

**Will do.** Render the Skardi `ctx.yaml` + pipeline YAMLs that target the user's datastore, resolve a local embedding model, start `skardi-server` in the background, ingest a corpus over HTTP, and route each user question through `/search-hybrid/execute` (or its single-signal siblings) to a grounded answer.

**Will not do.** Create databases, create schemas, run `CREATE EXTENSION`, install drivers, or hand out credentials. The user provides every connection string, every credential, every schema. If the schema does not exist yet, the skill prints the SQL the user must run in their own session and stops. *This is a hard line — never run schema-creation DDL against a user-supplied connection without the user explicitly asking for it.* The blast radius is too big to take on autopilot: a stray DROP can lose hours of someone else's work, and `CREATE EXTENSION` on a managed Postgres can require superuser the agent does not have anyway. Spell out what is needed and let the user run it.

For testing **the skill itself** during development you are free to spin up disposable Docker containers — that is not "the user's data". The line is about user-supplied datastores at runtime.

## Prerequisites the user must supply

Before doing anything, confirm these in one round of questions. If any are missing, ask for them — do not guess.

1. **Backend type.** One of `postgres` (pgvector + pg_fts), `mongo` (mongo_knn + mongo_fts), or `lance` (lance_knn + lance_fts). Default to `postgres` if the user has no preference — it has the cleanest dual-signal hybrid story. The Skardi source tree ships a working reference at `demo/rag/server/` (whichever local clone or fork the user is pointing at; or [github.com/SkardiLabs/skardi](https://github.com/SkardiLabs/skardi) if you need to look it up).
2. **Connection details.** For Postgres: a connection string (`postgresql://host:port/db?sslmode=...`) plus a user/password the agent will read from env vars `PG_USER` / `PG_PASSWORD`. For Mongo: connection URI + DB/collection name + `MONGO_USER` / `MONGO_PASS`. For Lance: an absolute path or `s3://...` URL to the dataset directory.
3. **Table / collection / dataset name.** Where the embeddings + content will live. The skill assumes one combined table that holds both `content` (TEXT, indexed for FTS) and `embedding` (vector type) — a design choice that lets one INSERT keep both signals on the same row.
4. **Embedding backend + model + dimension.** See the next section — *do not pick this silently*. The choice has knock-on effects on the schema (vector column dim must match), on the Skardi build (`--features candle | gguf | remote-embed`), and on the user's bill (remote APIs are pay-per-call). Confirm with the user.
5. **Whether the schema is already in place.** If yes, proceed. If no, the skill prints the SQL block the user must run themselves (see below) and stops until they confirm.

If the user only says "set up RAG over our pgvector DB", you have backend + (probably) connection details. Confirm dimension and table name explicitly — those two are the most common silent-failure surface.

### Choosing the embedding backend

Skardi exposes three embedding UDFs, all of which return a `List<Float32>` and slot into the same pipeline shape. The right one depends on the user's deployment, not on habit. The Skardi source tree's `docs/embeddings/{candle,gguf,remote}/README.md` is the authoritative per-backend walkthrough; the table below is the decision shortcut.

| UDF | Signature | Reach for it when | Skardi build feature |
|---|---|---|---|
| `candle(model_dir, text)` | local HuggingFace SafeTensors (BERT / RoBERTa / DistilBERT / Jina families) | Local, simple deps, general English text. The default for self-hosted setups when the corpus fits on one box. Common picks: `bge-small-en-v1.5` (384-d, ~130 MB), `bge-base-en-v1.5` (768-d, ~430 MB), `bge-large-en-v1.5` (1024-d, ~1.3 GB), `all-MiniLM-L6-v2` (384-d, very small), `multilingual-e5-large` (1024-d) for non-English, `jina-embeddings-v2-base-code` (768-d) for code corpora. | `--features candle` |
| `gguf(model_dir, text)` | local llama.cpp-format quantised weights | Local, RAM-constrained, want a bigger model at a smaller footprint, or the model only ships as GGUF. Common picks: `embeddinggemma-300m-qat-Q8_0.gguf` (256-d, ~330 MB, requires accepting Google's Gemma licence), `nomic-embed-text-v1.5` GGUF (768-d), or any GGUF quantisation of bge-large. | `--features gguf` |
| `remote_embed(provider, model, text)` | hosted API | No local compute, top-tier model quality, willing to pay per call. Providers + dims: `('openai','text-embedding-3-small')` 1536-d, `('openai','text-embedding-3-large')` 3072-d, `('voyage','voyage-3')` 1024-d, `('voyage','voyage-code-3')` 1024-d (best for code), `('gemini','text-embedding-004')` 768-d, `('mistral','mistral-embed')` 1024-d. Each requires its API key in the server's environment (`OPENAI_API_KEY`, `VOYAGE_API_KEY`, `GEMINI_API_KEY`, `MISTRAL_API_KEY`). | `--features remote-embed` |

**Decision rule the agent should run.** Ask the user (or infer from the conversation) and pick:

1. **Is there a hosted-API budget, or must it be local?** If local-only, you're choosing between candle and gguf. If a hosted API is fine, `remote_embed` is usually the easiest highest-quality path — no model files to manage, no GPU.
2. **What's the dominant content type?** Code → `voyage-code-3` (remote) or `jina-embeddings-v2-base-code` (candle). Multilingual → `multilingual-e5-large` (candle) or `text-embedding-3-large` (remote). Long documents (>512 tokens per chunk) → `nomic-embed-text` (candle/gguf, 8k context). Otherwise → bge family on candle.
3. **What's the memory budget on the server?** A small VM (≤2 GB free) → bge-small candle or a Q4/Q8 gguf. A box with 8 GB+ → bge-large candle is fine. No on-box compute → remote.
4. **Has Skardi been built with the matching feature?** Check before promising the user anything — `setup_rag.py` runs `skardi-server --version` (or equivalent) and surfaces which features are compiled in. If the wrong build is on PATH, point the user at the correct `cargo build --features <...>` line.

**State the choice explicitly** — one sentence to the user — and then move on. Something like *"I'm using `candle('bge-small-en-v1.5')` (384-d, local, ~130 MB) because your corpus is English markdown and you said no remote APIs; the schema needs `vector(384)`."* Don't bury the decision; the user will want to know, and an explicit framing makes the dim and feature flag obvious.

**The dim must match the schema.** If the user says they already created `vector(1024)`, you cannot then pick a 384-d model — the INSERT will fail with a dim mismatch on every row. If the schema is being created fresh, lock the dim to whatever the chosen model produces; if the schema already exists with a fixed dim, only models with that exact dim are eligible. A dim mismatch after the table is populated means dropping and rebuilding — there is no in-place fix.

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

Note the `id BIGINT` (not `BIGSERIAL`): DataFusion's INSERT planner mis-handles `SERIAL` columns — the symptom is `Invalid batch column at '0' has null but schema specifies non-nullable` on every INSERT. See the Skardi `docs/postgres/README.md` "Troubleshooting" section for the long form. The chunker emits stable 64-bit ids derived from `(source, chunk_idx)`, so client-side ids work fine.

#### MongoDB (Atlas Search or self-hosted with vector index)

Document shape:

```js
{
  _id: <stable-int64>,
  source: "<rel-path>",
  chunk_idx: <int>,
  content: "<chunk>",
  embedding: [<float>, ...]   // length matches model dim
}
```

The user must create:
- A **vector index** on `embedding` (cosine similarity, dim matching the model).
- A **text index** on `content` (`db.<coll>.createIndex({ content: "text" })`).

If the user is on Atlas Search, the vector index is created in the Atlas UI; if self-hosted with the [community vector index plugin](https://www.mongodb.com/products/platform/atlas-vector-search), it's created via `db.<coll>.createSearchIndex(...)`. Either way, the agent doesn't run it.

#### Lance

Lance datasets are append-only files; the schema is set on first write. Ask the user whether they have an existing `<dataset>.lance` directory or whether the agent should create it via a Skardi job. If creating via job:

```python
# User runs once, in their own Python:
import lance, pyarrow as pa
schema = pa.schema([
    pa.field("id",         pa.int64()),
    pa.field("source",     pa.string()),
    pa.field("chunk_idx",  pa.int64()),
    pa.field("content",    pa.string()),
    pa.field("embedding",  pa.list_(pa.float32(), <DIM>)),
])
lance.write_dataset(pa.table([], schema=schema), "<path>/kb.lance")
# Then create the FTS inverted index:
lance.dataset("<path>/kb.lance").create_scalar_index("content", index_type="INVERTED")
```

Lance ingest goes through a Skardi **job** (async, atomic commit) rather than the synchronous pipeline path — see [references/lance.md](references/lance.md). Per-chunk HTTP ingest is a wrong pattern for Lance (each commit creates a new manifest).

## Where does the server actually run? Three runtimes

`start_server.py` supports three execution targets via `--runtime`:

| Runtime | Pick when | Notes |
|---|---|---|
| `local-process` (default) | Single-laptop dev, prototyping. | Needs `skardi-server` on PATH (or pass `--skardi-source` to fall back to `cargo run`). |
| `docker` | Shipping a contained service to a teammate, isolating from host libs. | Pulls `ghcr.io/skardilabs/skardi/skardi-server-embedding:latest`. Connection string must be reachable from inside the container — `host.docker.internal` is auto-mapped. |
| `kubernetes` | The agent already runs in a cluster, multi-replica, shared retrieval. | Renders Deployment + Service + ConfigMap + Secret into `<workspace>/k8s/`; `--apply` actually deploys; `--port-forward` exposes locally for debug. |

Pick based on **where the agent calling this RAG service ultimately runs**, not on what's most familiar. If the agent ends up in a Kubernetes deployment, deploying skardi-server alongside it removes the host-network hop entirely. If the agent is a CLI on a developer laptop, local-process is fastest. The full per-runtime walk-through (mounts, networking, lifecycle, kubectl flags, port-forward, cleanup) lives in [references/runtimes.md](references/runtimes.md) — read it once when you pick a non-default runtime, especially for kubernetes where PG-reachability-from-cluster is the user's responsibility.

## Embedding lives client-side, not in the server

A note on architecture before the flow: **embedding is computed on the agent side**, via the host's `skardi` CLI, not by an inline UDF call inside the server's SQL. Each chunk's embedding is computed before the ingest POST and passed in as a parameter; each query's embedding is computed before the search POST. [scripts/embed.py](scripts/embed.py) wraps the CLI and parses the float array out of its table-format output; `http_ingest.py` does this automatically per chunk before each POST.

This is a deliberate decoupling. The published `skardi-server-embedding` images expose `pg_knn` and `pg_fts` (the storage-side table functions) but currently do **not** expose the `candle` / `gguf` / `remote_embed` scalar UDFs at runtime, even though the binary was built with the `embedding` feature. Computing vectors on the agent side — where the user's locally-installed `skardi` CLI does have the matching feature flags — sidesteps that gap entirely and makes the same templates run unchanged across local-process, Docker, and Kubernetes. As a side benefit, the embedder is swappable without rebuilding the server, and multiple agents can choose their own embedders against one server.

For this to work, the agent's host must have `skardi` CLI installed with the matching feature: `cargo install --locked --git https://github.com/SkardiLabs/skardi --branch main skardi-cli --features candle` (substitute `gguf` or `remote-embed` as appropriate; multiple features can be combined). The skill checks this on first embed call and surfaces a clear rebuild hint if the feature is missing.

## The end-to-end flow (Postgres path — the default)

Six steps. 1–3 are one-time setup; 4–6 are per-question.

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
   # Pick a runtime based on where the agent will run; see "Three runtimes" above.
   # Local-process default; add --runtime docker or --runtime kubernetes as needed.
4. python SKILL_DIR/scripts/chunk_corpus.py --corpus <docs/> --out ./rag/chunks.json
5. python SKILL_DIR/scripts/http_ingest.py --workspace ./rag --chunks ./rag/chunks.json
   # http_ingest embeds each chunk via the host CLI then POSTs the precomputed vector.
6. For each user question:
   QVEC=$(python SKILL_DIR/scripts/embed.py --workspace ./rag --text "<question>")
   curl -X POST http://localhost:8080/search-hybrid/execute \
     -d "{\"query_vec\": $QVEC, \"text_query\":\"...\",
          \"vector_weight\":0.5, \"text_weight\":0.5, \"limit\":5}"
   → synthesise grounded answer (see Step 6).
```

Read `SKILL_DIR` as the absolute path to the directory containing this SKILL.md.

The `chunks.json` from step 4 is **NDJSON** with a `.json` extension — same shape as `auto_knowledge_base`, same reasoning (DataFusion's CSV reader mis-splits embedded newlines; JSON escapes them; only `.json` is recognised). Don't rename it.

### Step 1 — Confirm prerequisites

Already covered above. The agent's posture here is "I will do nothing destructive — please confirm what is in place." If the user comes back saying "I haven't created the table yet", print the schema SQL, wait. Do not get clever and try to run it via `psql` or `skardi query` — even when the agent technically *can* connect, doing the user's DDL behind their back is a trust violation, and the schema choices (index parameters, secondary indices, auth) are the user's call.

### Step 2 — Render the workspace

`scripts/setup_rag.py` is idempotent. It:

1. Checks `skardi` CLI is on PATH (used for the pre-flight health probe and at runtime by `embed.py` for client-side vector computation). The CLI must be built with the matching embedding feature flag — `setup_rag.py` doesn't verify this on its own, but the first `embed.py` call surfaces a clear rebuild hint if it's missing.
2. Resolves the embedding model based on `--embedding-udf`. Note that the model is consumed by the *host CLI* during embedding, not by the server — but we still record the choice (path / provider args / dim) in `<workspace>/.embedding.txt` so `embed.py` and `http_ingest.py` know how to invoke `skardi query`:
   - `candle` — `--model-path` required (any HuggingFace repo dir with `model.safetensors` + `config.json` + `tokenizer.json`). The skill never auto-picks; the user makes the call (see *Choosing the embedding backend* above).
   - `gguf` — `--model-path` required (directory containing the `.gguf` file, plus a `tokenizer.json` if the model needs one). Never auto-downloads — Gemma is licence-gated, GGUF has many quantisations.
   - `remote_embed` — no model files; the relevant API key (`OPENAI_API_KEY` / `VOYAGE_API_KEY` / `GEMINI_API_KEY` / `MISTRAL_API_KEY`) must be in the agent's env when `embed.py` runs.
3. Renders `<workspace>/ctx.yaml` and `<workspace>/pipelines/{ingest,search_vector,search_fulltext,search_hybrid}.yaml` from templates in `SKILL_DIR/assets/<backend>/`. Substitutes the connection string, table name, and (in the breadcrumb file) the embedding-call shape that `embed.py` reconstructs at run time.
4. **Health-checks the user's connection before starting the server.** Runs `skardi query --sql "SELECT 1"` against the rendered ctx — a fast, harmless probe that surfaces auth / network / TLS errors at setup time rather than during ingest. Pass `--skip-health-check` when the connection string can't be resolved from where setup_rag.py runs (e.g. when the rendered ctx points at `host.docker.internal` for the docker runtime). If the live probe fails, print the error and stop; do not write a half-finished workspace.
5. Prints the next-step commands and the env vars the user has to export (`PG_USER`, `PG_PASSWORD`, the relevant embedding API key, etc.).

### Step 3 — Start `skardi-server`

`scripts/start_server.py` picks an execution target via `--runtime` (default `local-process`). All three runtimes use the same rendered ctx + pipelines from Step 2. Per-runtime details — mounts, networking, lifecycle — live in [references/runtimes.md](references/runtimes.md); the SKILL.md summary:

- `--runtime local-process` (default) — runs `skardi-server` as a host process. Needs the binary on PATH or a `--skardi-source` fallback. Backgrounded under `nohup`, logs at `<workspace>/server.log`, pid at `server.pid`.
- `--runtime docker` — runs `ghcr.io/skardilabs/skardi/skardi-server-embedding:latest` under `docker run`. Mounts the workspace at the same absolute path inside the container so paths in ctx.yaml resolve unchanged. Forwards `PG_USER`, `PG_PASSWORD`, and embedding API keys; auto-maps `host.docker.internal` for cross-OS host access. Container ID and name are stashed in `<workspace>/server.state.json` for clean teardown.
- `--runtime kubernetes` — renders Deployment + Service + ConfigMap + Secret into `<workspace>/k8s/`. Without `--apply` we stop there; with `--apply` we `kubectl apply -f <workspace>/k8s/` and wait for the deployment to roll out. With `--port-forward` we additionally background a `kubectl port-forward svc/<release> <local>:<port>` so the agent can hit `http://localhost:<port>` from outside the cluster.

In every runtime, after startup we:
1. Poll `GET /health` for up to `--health-timeout` seconds.
2. Check `GET /pipelines` lists the four expected names and warn about any missing.
3. Write `<workspace>/server.runtime` and `<workspace>/server.port` so follow-up scripts (especially `http_ingest.py` and `stop_server.py`) know what was launched.

The dashboard at `http://localhost:<port>/` is a fast sanity check whenever the runtime exposes a local URL — the user can open it in a browser and confirm the pipelines show up with the right parameter lists.

### Step 4 — Chunk the corpus

Reuse the chunker shape from `auto_knowledge_base`: markdown-aware splitting on H2/H3, paragraph-pack within sections to `--max-chars` (default 1200) with `--overlap` (default 200), heading trail prefixed onto each chunk so the embedding picks up section context.

Output is NDJSON with `id`, `source`, `chunk_idx`, `content`. Stable ids derived from `(source, chunk_idx)` mean re-running on the same corpus is idempotent — the user can append new files without renumbering. If you need a different chunker (semantic, code-aware, ...), produce the same NDJSON shape and skip this step; everything downstream is agnostic.

### Step 5 — Ingest over HTTP

`scripts/http_ingest.py` reads the NDJSON file, embeds each chunk via the host CLI, then POSTs `{doc_id, source, chunk_idx, content, embedding: [...]}` to `http://127.0.0.1:<port>/ingest/execute`. The pipeline SQL (template in [assets/postgres/pipelines/ingest.yaml.tpl](assets/postgres/pipelines/ingest.yaml.tpl)) is:

```sql
INSERT INTO <table> (id, source, chunk_idx, content, embedding)
VALUES ({doc_id}, {source}, {chunk_idx}, {content}, {embedding})
```

The embedding is a Float32 array passed as a parameter — no UDF call inside the pipeline SQL, no dependence on what's compiled into the server. See [scripts/embed.py](scripts/embed.py) for the CLI-shelling helper used to compute each vector. The script runs in two well-defined phases: first it embeds every pending chunk (so the host CLI's model cache is hot), then it POSTs in a loop. Logs and the `<workspace>/ingest_progress.json` manifest report each phase separately.

**Concurrency.** The script embeds serially (the host CLI's model cache amortises across calls) and POSTs at `--concurrency N` (default 1; 4–8 is the sweet spot for inflight HTTP, beyond which the DB write becomes the bottleneck rather than the network).

**Failure handling.** The progress manifest at `<workspace>/ingest_progress.json` keys by chunk id with `ok` or `err: ...` values. On retry, already-ok ids are skipped — important because the `id` column is the primary key and a duplicate INSERT is rejected by Postgres but silently upserted by Mongo, so a "retry only the failed ones" loop is the only cross-backend correct behaviour. Embedding failures and POST failures are recorded with their distinct error prefixes (`err: embed: ...` vs `err: HTTP 500: ...`) so the user can see at a glance whether the host CLI or the server pushed back.

**Why client-side embedding rather than inline UDF?** The published `skardi-server-embedding` images currently expose only `pg_knn` and `pg_fts` at runtime — they don't register `candle` / `gguf` / `remote_embed` even though they were built with the embedding feature. By computing vectors on the host (where the CLI does have those UDFs registered) and passing them in as parameters, the same templates run unchanged across local-process / Docker / Kubernetes. The architectural side benefit: embedding becomes swappable without rebuilding the server.

**Why HTTP, not bulk SQL?** The CLI skill ingests via one big `INSERT … SELECT FROM './chunks.json'` because the CLI process can read files directly. The server can too — but only files **on the server's filesystem**, and we don't want to assume the agent and the server share one. The HTTP loop works regardless of where the server is running, which is the whole point of the server backend. For tens of thousands of chunks where this matters, see *Scaling ingest* below.

**Verify the corpus.** After ingest, hit `/search-fulltext/execute` with a known token from the corpus and confirm rows come back. Or run a count query directly: `psql -c "SELECT count(*) FROM <table>"` against the user's PG. A zero count means INSERTs failed silently — read `<workspace>/server.log` and the progress manifest.

### Step 6 — Retrieve and synthesise

This is where the answer quality is made or lost. Most of the guidance from `auto_knowledge_base/SKILL.md § Step 5` applies verbatim — read it for the full reasoning. The HTTP-specific bits:

Vector / hybrid search take a precomputed `query_vec` (use `scripts/embed.py` to make one). FTS-only takes plain text. Two-call pattern looks like:

```bash
# 1. Embed once for both vector + hybrid
QVEC=$(python SKILL_DIR/scripts/embed.py --workspace ./rag --text "who is the white rabbit?")

# Hybrid (default — RRF over pg_knn + pg_fts)
curl -s -X POST http://localhost:8080/search-hybrid/execute \
  -H 'Content-Type: application/json' \
  -d "{\"query_vec\": $QVEC, \"text_query\":\"white rabbit\",
       \"vector_weight\":0.5, \"text_weight\":0.5, \"limit\":5}" | jq .

# Vector-only — paraphrase / conceptual queries
curl -s -X POST http://localhost:8080/search-vector/execute \
  -H 'Content-Type: application/json' \
  -d "{\"query_vec\": $QVEC, \"limit\":5}" | jq .

# FTS-only — named entity / exact string. No embedding needed.
curl -s -X POST http://localhost:8080/search-fulltext/execute \
  -H 'Content-Type: application/json' \
  -d '{"query":"\"Rabbit-Hole\"","limit":5}' | jq .
```

`pg_fts` uses `websearch_to_tsquery`, which is strict about query syntax — `?`, bare `"`, `+`, `-`, `~`, `^`, and parentheses are operators. Phrase-quote multi-word terms and strip stray punctuation from natural-language queries.

**Multi-query when the question is multi-part.** Same rule as the CLI skill: don't shove everything into one search. Two or three scoped queries (one per sub-question, or vector + FTS pair for a definitional question) consistently beat one kitchen-sink query, because RRF dilutes when the embedding has to cover too many concepts at once. The scoring constant in `search_hybrid.yaml` is the standard RRF k=60.

**Grounding principle: don't synthesise past the retrieval.** Every claim in the answer must come from a chunk you actually retrieved. If a fact isn't there, say so plainly — report what you searched for, what you found, and that the corpus doesn't speak to the gap. Don't invent a plausible-sounding citation from training data. This rule is what makes the retrieval *useful*; agents that hallucinate citations destroy trust in the whole stack, while agents that flag absence honestly preserve it. Side observations only land if the chunks you cite actually say them *and* they bear on the specific question; otherwise they're padding.

**Answer structure.** Same template as `auto_knowledge_base` — sub-claims with verbatim quotes from chunks, citations to `(source, chunk_idx)`, optional citation table at the end for multi-claim answers.

## Scaling ingest

The HTTP loop is fine up to a few thousand chunks. The bottleneck for larger corpora is the per-call overhead (HTTP round-trip + embedding subprocess), not the server's INSERT throughput. Two ways to push past that:

- **Bulk INSERT via a Skardi job.** Define a `kind: job` that reads pre-embedded chunks from object storage (`s3://...`, a parquet file, etc.) and writes them in one transaction. Embeddings would have been computed offline by the agent and dropped into the file alongside the content. Submit via `POST /jobs/<name>/run`, poll `/jobs/runs/<id>` until done. The Skardi `docs/jobs.md` is the working reference for the YAML shape and run ledger.
- **Re-shape the embedding step.** The host CLI's per-call cold-start dominates small jobs but is amortised across longer runs. For tens of thousands of chunks, batch the embed step into a long-running Python process that holds the model loaded (e.g. via the `sentence-transformers` library directly, or by piping multiple texts to one `skardi query` call) — `embed.py`'s subprocess-per-chunk shape is the right *correctness* model but the wrong *performance* model at that scale.

If the user's question is "I have 5M chunks", neither this skill nor `auto_knowledge_base` is right out-of-the-box — point them at Lance + jobs and say so explicitly rather than letting either skill grind for hours.

## Customising

- **Switching the embedding backend after setup.** Re-run `setup_rag.py` with the new `--embedding-udf` and `--model-path` / `--embedding-args` — the breadcrumb is what `embed.py` reads at run time, so the change is picked up immediately. **The vector dim must still match the schema** (the `vector(N)` column is fixed at table-create time); a dim change means rebuild from scratch. The pipelines themselves don't bake in the embedding choice — they just take a `query_vec` / `embedding` parameter — so the rendered files survive an embedder swap unchanged.
- **Tighter ANN parameters.** Postgres HNSW takes `m` and `ef_construction` at index-build time; raising them helps recall on larger corpora. The schema SQL printed in Step 1 uses `m=16, ef_construction=64` — fine for <1M rows. For a 10M-row corpus, suggest `m=32, ef_construction=200` and warn the user the index build will be slow.
- **Filter pushdown.** `pg_knn` and `pg_fts` accept an optional WHERE-style filter as a final argument. If the user wants per-tenant retrieval, they can either add a `tenant_id` column and filter in the pipeline SQL (`WHERE tenant_id = {tenant_id}`), or add a metadata-aware pipeline like `search_hybrid_for_tenant.yaml`. Keep this off the default path — most users don't need it and adding it is cheap when they do.

## Troubleshooting

If something looks off, read [references/troubleshooting.md](references/troubleshooting.md). The most common failure modes:

- **`role "<user>" does not exist`** — env vars not exported. The server reads `PG_USER`/`PG_PASSWORD` from its own environment at startup, so they must be set *before* `start_server.py` runs. In the docker runtime they're forwarded into the container automatically; in kubernetes they're carried in the rendered Secret (which is built from `os.environ` at render time).
- **`relation "<table>" does not exist`** — the user hasn't run the schema SQL. Print it again, wait.
- **`failed to load extension "vector"`** — pgvector not installed. Either point at `pgvector/pgvector:pg16` (Docker) or run `CREATE EXTENSION vector` after installing the OS package. The agent does not do this; it surfaces the error.
- **`embed.py`: `Invalid function 'candle'`** — the host's `skardi` CLI was installed without the matching feature flag. Re-install with `cargo install --locked --git https://github.com/SkardiLabs/skardi --branch main skardi-cli --features candle` (or `gguf` / `remote-embed`).
- **`fts5: syntax error`** / **`pg_fts: syntax error in tsquery`** — the user's question contains web-search-syntax reserved characters. Strip them or phrase-quote. Doesn't break the vector half of hybrid search, so the answer is degraded but not empty.
- **HTTP 500 from `/ingest/execute` mentioning `null but schema specifies non-nullable`** — table was created with `SERIAL PRIMARY KEY`. Switch to plain `BIGINT PRIMARY KEY` (the chunker emits explicit ids).
- **Docker container can't reach Postgres on the host** — connection string says `localhost`; the container's loopback isn't the host's. Re-render with `host.docker.internal` (or `--network host` on Linux).
- **HTTP 502 on every ingest/search call** — a system HTTP proxy on `127.0.0.1:7897` (mihomo / clash) is intercepting localhost requests. `http_ingest.py` already adds `localhost,127.0.0.1` to `NO_PROXY` for its own urllib calls; if you're hitting raw `curl`, prefix with `NO_PROXY=localhost,127.0.0.1` or unset `http_proxy`/`https_proxy`/`all_proxy` for the call.

## When to choose this skill vs. `auto_knowledge_base`

Pick `auto_knowledge_base` for: single-machine corpora, demos, "I just want to query my notes folder", anywhere the user is happy with one SQLite file and zero infra. The CLI path is faster to set up and easier to ship to a teammate as one directory.

Pick `auto_rag` for: shared infra (multiple agents, multi-process serving), datastores the user already runs in production, RAG that needs to live behind an HTTP boundary so non-Claude callers (other services, MCP clients, browser tools) can hit it, and corpora large enough that running them on a laptop is awkward.

The pipeline YAMLs are largely shape-compatible — you can prototype with the CLI skill and swap to `auto_rag` later by re-pointing `ctx.yaml` at the production datastore and re-running the ingest loop. Nothing in the corpus or the chunker changes.
