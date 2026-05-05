# Per-backend schema requirements

The skill never runs DDL against a user-supplied datastore. Print the
matching block from this file, ask the user to run it themselves, and
wait for confirmation before continuing. The Postgres path is the one
the rest of the skill is fully wired for; Mongo and Lance live here as
guidance the agent can hand to the user.

## PostgreSQL + pgvector

### Required extension

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

If the user is on a managed Postgres that doesn't allow `CREATE EXTENSION`
without superuser, ask them to enable pgvector via their provider's
control panel (Supabase, Neon, RDS, Cloud SQL, etc. all expose it).

### Table

```sql
CREATE TABLE <table> (
    id        BIGINT PRIMARY KEY,        -- NOT BIGSERIAL — see below
    source    TEXT NOT NULL,
    chunk_idx INTEGER NOT NULL,
    content   TEXT NOT NULL,
    embedding vector(<DIM>)              -- must match the embedding model output
);
```

`BIGINT` (not `BIGSERIAL`) because DataFusion's INSERT planner currently
mis-handles SERIAL — the symptom is `Invalid batch column at '0' has null
but schema specifies non-nullable` on every INSERT. The skill's chunker
emits stable 64-bit ids derived from `(source, chunk_idx)`, so client-side
ids work fine and re-running the chunker on the same corpus produces the
same ids (idempotent re-ingest).

`<DIM>` must match the embedding model the user picked in *Choosing the
embedding backend*. A mismatch is unrecoverable — `vector(N)` is fixed
at table-create time, every INSERT will fail with a dim-mismatch error,
and the only fix is `DROP TABLE` + recreate. Lock this down before the
user runs the SQL.

### Indices

```sql
-- ANN over the embedding (cosine; the search pipelines target '<=>')
CREATE INDEX ON <table> USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

-- FTS over the same content column the embedding came from
CREATE INDEX <table>_content_fts_idx
  ON <table>
  USING GIN (to_tsvector('english', content));
```

`m=16, ef_construction=64` is fine up to ~1M rows. Bump to `m=32,
ef_construction=200` for ~10M+ at the cost of slower index build. Switch
`'english'` to whatever PostgreSQL text search configuration matches the
corpus language — `'simple'`, `'spanish'`, `'german'`, etc.

### Tenant / metadata columns

If the user wants per-tenant retrieval, add `tenant_id BIGINT NOT NULL`
(or whatever shape they want) to the table and to the FTS index, and
either filter in the search pipeline SQL or render a tenant-aware
variant. The skill's default pipelines don't include tenant filtering —
add it deliberately when needed, not by default.

### Verifying the schema

After the user reports running the SQL, the agent can confirm via
`skardi query`:

```bash
SKARDICONFIG=<workspace> skardi query --sql \
  "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = '<table>'"
```

Look for `id`, `source`, `chunk_idx`, `content`, and `embedding`. The
`embedding` column will report as `USER-DEFINED` because pgvector is
custom; that's fine. If the column is missing or its dim doesn't match
the chosen model, stop and have the user rebuild before ingest.

---

## MongoDB

### Document shape

```js
{
  _id: <stable-int64>,        // matches the chunker's id field
  source: "<rel-path>",
  chunk_idx: <int>,
  content: "<chunk text>",
  embedding: [<float>, ...]   // length == model dim
}
```

Skardi's `mongo_knn` and `mongo_fts` table functions read whichever
collection is registered in `ctx.yaml`; nothing about the shape above is
mandatory beyond having `embedding` (vector) and `content` (text).

### Indices

```js
// Text index for FTS
db.<coll>.createIndex({ content: "text" })

// Vector index — Atlas Search variant
db.<coll>.createSearchIndex({
  name: "embedding_vec",
  definition: {
    fields: [
      { type: "vector", path: "embedding", numDimensions: <DIM>, similarity: "cosine" }
    ]
  }
})
```

For self-hosted Mongo with the community vector plugin, the syntax is
slightly different; refer the user to the plugin's README.

### Caveats vs. Postgres

- `_id` collisions are an *upsert* on Mongo (silent), not an error like
  Postgres. The skill's `http_ingest.py` keeps an explicit progress
  manifest so retries don't corrupt the corpus, but the user should
  understand the semantics if they hand-write any client code.
- Mongo's text search has no equivalent to `to_tsvector`'s language
  configurations; the default is fine for English, less so for other
  languages. If the corpus is multilingual, vector-only search is often
  the better default.

---

## Lance

Lance datasets are append-only directories; the schema is set on first
write. Two ways to bootstrap:

### Option 1: User creates the empty dataset themselves

```python
import lance
import pyarrow as pa

schema = pa.schema([
    pa.field("id",         pa.int64()),
    pa.field("source",     pa.string()),
    pa.field("chunk_idx",  pa.int64()),
    pa.field("content",    pa.string()),
    pa.field("embedding",  pa.list_(pa.float32(), <DIM>)),
])
lance.write_dataset(pa.table([], schema=schema), "<path>/kb.lance")
lance.dataset("<path>/kb.lance").create_scalar_index("content", index_type="INVERTED")
```

### Option 2: Skardi job creates + populates in one go

A `kind: job` with `destination.create_if_missing: true` writes the
dataset on first run. Suitable for batch re-ingests where the user is OK
with the destination being managed by the job ledger. See [jobs.md](
jobs.md) once that file lands; until then the auto_knowledge_base
skill's [references/backends.md](
../../auto_knowledge_base/references/backends.md) Lance section is the
working example.

### Why not per-chunk HTTP ingest for Lance

Each Lance commit creates a new manifest. Doing one commit per chunk
turns a 1k-chunk corpus into a thousand manifests, which is both slow
and a versioning mess. Always batch via a job for Lance — the skill's
`http_ingest.py` is the wrong tool for this backend.
