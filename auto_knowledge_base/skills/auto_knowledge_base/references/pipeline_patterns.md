# Pipeline patterns

The pipelines this skill generates are the minimum set that makes a KB useful to an agent. This file covers the shape of each — useful when you need to modify one in place rather than regenerate the whole workspace.

> The SQL below uses `candle('<abs-path>', ...)` as the embedding expression because it's the default. Substitute `gguf('<abs-path>', ...)` or `remote_embed('<provider>','<model>', ...)` if you used a different `--embedding-udf` at setup — the rest of the SQL is identical, and `setup_kb.py` already rendered the correct call into your pipeline YAMLs.

## Rendered file layout

```
<workspace>/
  ctx.yaml                      # registers kb.db as a SQLite catalog source
  semantics.yaml                # auto-discovered overlay; surfaces in `skardi query --schema --all`
  aliases.yaml                  # short verbs: ingest, ingest-doc, grep, vec, fts
  pipelines/
    ingest.yaml                 # single-row INSERT from a pre-chunked seed; trigger fans to FTS+vec
    ingest_chunked.yaml         # full document → chunk → embed → write in one statement
    search_vector.yaml          # sqlite_knn
    search_fulltext.yaml        # sqlite_fts
    search_hybrid.yaml          # RRF over sqlite_knn + sqlite_fts
  .embedding.txt                # breadcrumb (udf, model_path, dim, chunk_mode); read by ingest_corpus.py
  kb.db                         # the SQLite catalog
  manifest.json                 # NDJSON of (doc_id, source, content) — written by ingest_corpus.py
  models/                       # only if setup_kb.py downloaded a model
    <embedding-model>/
```

## The bulk write path: `ingest_corpus.py`

For a whole-corpus walk, the script writes a manifest NDJSON and runs ONE `skardi query` with this INSERT — the embedding model loads exactly once for the entire run:

```sql
INSERT INTO kb.main.documents (id, source, chunk_idx, content, embedding)
SELECT id, source, chunk_idx, content,
       vec_to_binary(candle('<abs-path>', content))
FROM (
  SELECT
    CAST(doc_id AS BIGINT) * 1000 + chunk_idx AS id,
    source, chunk_idx, content
  FROM (
    SELECT
      doc_id, source,
      ROW_NUMBER() OVER (PARTITION BY doc_id ORDER BY 1) - 1 AS chunk_idx,
      chunk_text                                              AS content
    FROM (
      SELECT
        CAST(doc_id AS BIGINT) AS doc_id, source,
        UNNEST(chunk('markdown', content, 1200, 200)) AS chunk_text
      FROM './manifest.json'
    ) c
  ) c2
) AS t
```

**Why three nested subqueries?** DataFusion's INSERT planner validates row width against the immediate child of `INSERT ... SELECT` and drops projections it thinks the schema doesn't need. Wrapping the whole computation in `SELECT ... FROM (...) AS t` keeps the inner schema in scope so `vec_to_binary(candle(...))` survives. The middle wrapper materialises `chunk_idx` once via `ROW_NUMBER() OVER (PARTITION BY doc_id ORDER BY 1) - 1` — `id` and `chunk_idx` both use it, but DataFusion will not deduplicate identical window expressions on its own.

**Why `vec_to_binary`?** sqlite-vec's `vec0` table stores vectors as little-endian packed f32 BLOBs. `vec_to_binary` converts the float array returned by `candle()` into that layout. pgvector doesn't need this — it accepts the array directly.

## The per-doc write path: `ingest_chunked.yaml`

For one-document calls (e.g. `skardi ingest-doc <doc_id> <source> "$body"`):

```sql
INSERT INTO kb.main.documents (id, source, chunk_idx, content, embedding)
SELECT id, source, chunk_idx, content,
       vec_to_binary(candle('<abs-path>', content))
FROM (
  SELECT
    CAST({doc_id} AS BIGINT) * 1000
      + (ROW_NUMBER() OVER (ORDER BY 1) - 1)              AS id,
    {source}                                              AS source,
    CAST(ROW_NUMBER() OVER (ORDER BY 1) - 1 AS BIGINT)    AS chunk_idx,
    chunk_text                                            AS content
  FROM (
    SELECT UNNEST(chunk('markdown', {content}, {chunk_size}, {overlap})) AS chunk_text
  ) c
) AS t
```

Same shape as the bulk path, minus the `PARTITION BY` (only one document per call) and minus the file-read (`{content}` is the whole-doc parameter).

## The single-row write path: `ingest.yaml`

For users who already have chunk text in hand from a custom chunker:

```sql
INSERT INTO kb.main.documents (id, source, chunk_idx, content, embedding)
SELECT id, source, chunk_idx, content,
       vec_to_binary(candle('<abs-path>', content))
FROM (
  SELECT CAST({doc_id} AS BIGINT) AS id,
         {source} AS source,
         CAST({chunk_idx} AS BIGINT) AS chunk_idx,
         {content} AS content
) AS t
```

## The read path: three shapes, one source of truth

All three search pipelines read from the **same** `documents` table, via the FTS5 / vec0 mirrors that triggers keep in sync. So there's no cross-store JOIN problem — every id in `documents_vec` has a matching row in `documents` and `documents_fts`.

### Vector only (`search_vector.yaml`)

```sql
SELECT d.id, d.source, d.chunk_idx, d.content, v._score AS distance
FROM sqlite_knn('kb.main.documents_vec', 'embedding',
    (SELECT candle('<abs-path>', {query})),
    {limit}) v
LEFT JOIN kb.main.documents d ON d.id = v.id
ORDER BY v._score
```

`_score` is cosine distance (lower = closer). The subquery around the query embedding is required — DataFusion will not implicitly scalarize a UDF call as a table-function argument.

Use when: the query is paraphrastic or conceptual ("something about a creature checking the time" → chunks about the White Rabbit's watch).

### FTS only (`search_fulltext.yaml`)

```sql
SELECT f.id, d.source, d.chunk_idx, f.content, f._score AS score
FROM sqlite_fts('kb.main.documents_fts', 'content', {query}, {limit}) f
LEFT JOIN kb.main.documents d ON d.id = f.id
ORDER BY f._score DESC
```

`_score` is BM25 relevance (higher = more relevant). FTS5 tokenises by whitespace + unicode categories; punctuation is mostly stripped but a few characters are operators — see [troubleshooting.md](troubleshooting.md) for the escape rules.

Use when: the query has strong lexical signal — named entities, rare n-grams, exact phrases.

### Hybrid (`search_hybrid.yaml`)

```sql
WITH vec AS (
  SELECT id, ROW_NUMBER() OVER (ORDER BY _score ASC) AS rk
  FROM sqlite_knn('kb.main.documents_vec', 'embedding',
      (SELECT candle('<abs-path>', {query})),
      80)
),
fts AS (
  SELECT id, content, ROW_NUMBER() OVER (ORDER BY _score DESC) AS rk
  FROM sqlite_fts('kb.main.documents_fts', 'content', {text_query}, 60)
)
SELECT COALESCE(v.id, f.id) AS id,
       d.source, d.chunk_idx,
       COALESCE(f.content, d.content) AS content,
       COALESCE({vector_weight} / (60.0 + v.rk), 0)
         + COALESCE({text_weight}  / (60.0 + f.rk), 0) AS rrf_score
FROM vec v
FULL OUTER JOIN fts f ON v.id = f.id
LEFT JOIN kb.main.documents d ON d.id = COALESCE(v.id, f.id)
ORDER BY rrf_score DESC
LIMIT {limit}
```

**Reciprocal Rank Fusion.** Each candidate gets a score of `weight / (60 + rank)` from each signal; totals are summed. The constant 60 is standard — it softens the contribution of top-1 results so a weak signal with strong top-1 doesn't dominate.

The `FULL OUTER JOIN` is what lets hybrid beat either signal alone: rows that only appear in FTS get vector_rk=NULL (contributes 0 to their score), and vice versa. Rows that appear in *both* get both terms.

The pool sizes (80 vector, 60 FTS) are larger than `{limit}` because RRF needs candidates to fuse. If your corpus is small (<100 rows), dropping these to 20 each is fine.

Use when: you don't know upfront whether the query is lexical or conceptual. Which is most of the time — default to this.

## Catalog semantics (`semantics.yaml`)

`setup_kb.py` renders one `kind: semantics` file describing the `documents` table and its columns. Skardi auto-discovers it next to `ctx.yaml` (see `docs/semantics.md` in the source) and merges it into the catalog view at request time. To extend it, drop a second file (any name) into a `semantics/` directory next to `ctx.yaml` — both files are merged, and a duplicate key (same source name or same `catalog.schema.table`) is a load-time error so accidental overwrites surface immediately.

## Adding a metadata filter to search

Say you want to filter results to a specific source path prefix. Edit the search pipeline to accept a `{source_prefix}` param and add a WHERE clause:

```sql
SELECT d.id, d.source, d.chunk_idx, d.content, v._score AS distance
FROM sqlite_knn('kb.main.documents_vec', 'embedding',
    (SELECT candle('<abs-path>', {query})),
    {limit}) v
LEFT JOIN kb.main.documents d ON d.id = v.id
WHERE d.source LIKE {source_prefix} || '%'
ORDER BY v._score
```

Note the filter runs **after** KNN, so if the filter is very selective, bump the KNN pool size (`80` → `500` or so) to compensate.

## Updating existing rows

Trigger-driven. `UPDATE kb.main.documents SET content = ..., embedding = vec_to_binary(candle(...)) WHERE id = ?` — the `AFTER UPDATE` trigger refreshes the FTS and vec mirrors automatically.

If the update is just text (no embedding), re-embed anyway. Otherwise the vector mirror will drift from the content.

## Deleting rows

`DELETE FROM kb.main.documents WHERE source = 'path/to/file.md'` cascades through the `AFTER DELETE` trigger to both mirrors. Use this before re-ingesting a changed file (since stable doc_ids would otherwise collide on the primary key).
