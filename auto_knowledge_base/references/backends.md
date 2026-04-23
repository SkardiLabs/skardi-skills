# Backend selection

This skill defaults to SQLite + sqlite-vec + FTS5 because it has no infra. The other backends exist for genuinely different shapes of problem — don't reach for them unless the user's situation matches one of the rows below.

> The SQL snippets in this file use `candle('<abs-model-path>', ...)` as the embedding expression because it's the most common local option. The expression is interchangeable: substitute `gguf('<abs-model-path>', ...)` for a quantised local model, or `remote_embed('<provider>','<model>', ...)` for a hosted API (openai / voyage / gemini / mistral). Pick based on deployment constraints, not on habit.

## When to pick each

| Backend | Good for | Bad for | Key trade-offs |
|---|---|---|---|
| **SQLite + sqlite-vec + FTS5** (default) | Single agent, local dev, <1M chunks, no service to run | Many concurrent writers (SQLite's single-writer lock is the bottleneck) | Zero setup, everything in one `.db` file, trigger keeps vec + FTS in sync atomically |
| **Postgres + pgvector** | Multiple concurrent agents, ACID-strict writes, 1M–100M chunks | Very-small local agents (overkill), environments without Docker/network | Requires a running Postgres with pgvector extension; HNSW indexing; standard SQL monitoring |
| **Lance** | Columnar, versioned datasets, 10M+ chunks, want to keep snapshots for replay, batch jobs writing at high throughput | Single-row synchronous writes (its sweet spot is batch) | Use Skardi **jobs** (not pipelines) for atomic bulk commits; great for periodic re-indexing but not for chat-app-style per-row inserts |

## Postgres + pgvector

Use this when the user says "production", "server", "multiple agents", or when ACID guarantees matter.

### One-time setup

```bash
docker run --name kb-postgres \
  -e POSTGRES_DB=kbdb \
  -e POSTGRES_USER=kb_user \
  -e POSTGRES_PASSWORD=kb_pass \
  -p 5432:5432 \
  -d pgvector/pgvector:pg16

docker exec -i kb-postgres psql -U kb_user -d kbdb << 'EOF'
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE documents (
    id         BIGINT PRIMARY KEY,
    source     TEXT NOT NULL,
    chunk_idx  INTEGER NOT NULL,
    content    TEXT NOT NULL,
    embedding  vector(384)
);
CREATE INDEX ON documents USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);
CREATE INDEX documents_fts_idx
  ON documents
  USING GIN (to_tsvector('english', content));
EOF
```

### ctx.yaml

```yaml
kind: context
metadata:
  name: auto-kb-pg-context
  version: 1.0.0
spec:
  data_sources:
    - name: "documents"
      type: "postgres"
      access_mode: "read_write"
      connection_string: "postgresql://localhost:5432/kbdb?sslmode=disable"
      options:
        table: "documents"
        schema: "public"
        user_env: "PG_USER"
        pass_env: "PG_PASSWORD"
```

### Pipelines

The shape is identical to the default SQLite pipelines — substitute:

- `sqlite_knn('kb.main.documents_vec', 'embedding', ...)` → `pg_knn('documents', 'embedding', ...)`
- `sqlite_fts('kb.main.documents_fts', 'content', ...)` → `pg_fts('documents', 'content', ...)`
- `vec_to_binary(candle(...))` → just `candle(...)` (pgvector accepts the raw float array; no binary pack step)

Example ingest:

```sql
INSERT INTO documents (id, source, chunk_idx, content, embedding)
VALUES (
  {doc_id}, {source}, {chunk_idx}, {content},
  candle('<abs-model-path>', {content})
)
```

Example hybrid search (RRF same as SQLite — just swap the functions):

```sql
WITH vec AS (
  SELECT id, ROW_NUMBER() OVER (ORDER BY _score ASC) AS rk
  FROM pg_knn('documents', 'embedding',
      (SELECT candle('<abs-model-path>', {query})),
      80)
),
fts AS (
  SELECT id, content, ROW_NUMBER() OVER (ORDER BY _score DESC) AS rk
  FROM pg_fts('documents', 'content', {text_query}, 60)
)
SELECT COALESCE(v.id, f.id) AS id,
       d.source, d.chunk_idx, COALESCE(f.content, d.content) AS content,
       COALESCE({vector_weight} / (60.0 + v.rk), 0)
         + COALESCE({text_weight}  / (60.0 + f.rk), 0) AS rrf_score
FROM vec v
FULL OUTER JOIN fts f ON v.id = f.id
LEFT JOIN documents d ON d.id = COALESCE(v.id, f.id)
ORDER BY rrf_score DESC
LIMIT {limit};
```

### Server or CLI?

Either works. For multi-agent or web-facing use, start `skardi-server` with `--pipeline <dir>` so pipelines are exposed as REST endpoints (POST `/search-hybrid/execute`). For single-agent use, `skardi run search-hybrid --param query=...` is fine.

## Lance

Use this for **write-heavy batch pipelines** — e.g., nightly re-embedding of millions of chunks, or maintaining versioned snapshots of a KB for reproducibility. Lance's columnar format and HNSW indices scale to TBs and its versioning is first-class (each commit is a new manifest; old snapshots remain readable).

### Ingest via a Skardi job (not a pipeline)

Pipelines are for synchronous online serving; large Lance writes want the async commit + run ledger that **jobs** provide.

```yaml
# jobs/backfill_kb_to_lake.yaml
kind: job

metadata:
  name: "backfill-kb-to-lake"
  version: "1.0.0"

spec:
  query: |
    SELECT CAST(id AS BIGINT) AS id,
           source,
           CAST(chunk_idx AS BIGINT) AS chunk_idx,
           content,
           candle('<abs-model-path>', content) AS embedding
    FROM './kb/chunks.csv'

  destination:
    table: "kb_lake"
    mode: append
    create_if_missing: true

  execution:
    timeout_ms: 3600000
```

Submit:

```bash
skardi job run backfill-kb-to-lake --server http://localhost:8080
skardi job status <run_id>
```

Atomicity: Lance only writes the new manifest after the whole stream drains. A mid-run crash leaves the previous version visible; re-running the job is safe.

### Querying Lance

```sql
SELECT id, source, chunk_idx, content, _distance
FROM lance_knn('<path-to-kb_lake.lance>', 'embedding',
    candle('<abs-model-path>', {query}),
    {limit})
ORDER BY _distance
LIMIT {limit}
```

For FTS over Lance, create an INVERTED index on the text column (Python side, using `lance.Dataset.create_scalar_index(..., index_type='INVERTED')`), then use `lance_fts(...)` with the BM25 query syntax (`+must -mustnot "phrase" fuzzy~1`).

## Skardi server or CLI?

Independent of backend choice:

- **CLI** — agent has Bash, wants zero service setup, one invocation per call. Perfect for Claude Code / Cursor / custom shell-based loops. This is what the default path uses.
- **Server** — agent is remote, need concurrent request handling, want REST endpoints for non-Claude hosts, or plan to hand the pipeline out to other callers. Start with `skardi-server --ctx ... --pipeline ... --port 8080`.

Both read the same pipeline YAMLs — you can start with CLI, then flip to server without rewriting anything.
