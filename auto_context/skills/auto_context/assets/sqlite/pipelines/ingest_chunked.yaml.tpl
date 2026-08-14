kind: pipeline

metadata:
  name: "ingest-chunked"
  version: "1.0.0"
  description: >
    Ingest one document by splitting it into chunks inline with chunk(<mode from setup>, ...),
    embedding each chunk with the configured UDF, and writing one row per chunk.
    The whole loop — chunk → embed → write — runs in a single SQL statement, so an
    N-chunk document yields N rows that are immediately searchable via sqlite_fts
    and sqlite_knn (the AFTER INSERT trigger fans each row to the mirrors).

    Synthesised chunk ids: `id = doc_id * 1000 + chunk_idx` (0-based), so callers
    must pick `doc_id` values whose chunks won't collide (chunks-per-doc < 1000 in
    practice). `chunk_idx` is derived from an explicit positional index (see below),
    NOT from row order, so the same document always yields the same
    (chunk_idx, content) pairs and the same ids on every run. Source is recorded
    verbatim for citation.

    Determinism note: chunk() returns an ORDERED List<Utf8>. We enumerate it with
    generate_series(0 .. array_length-1) and unnest that index list in the SAME
    projection as UNNEST(chunks); DataFusion zips two unnests in a projection
    positionally, so index i is paired with chunk i by construction. The earlier
    `ROW_NUMBER() OVER (ORDER BY 1)` form was non-deterministic: `ORDER BY 1`
    orders by the constant literal 1, making every row a peer, so DataFusion was
    free to number chunks in any order — the same chunk could get a different
    chunk_idx (and therefore a different id) on each run. This template had it
    twice, once for `id` and once for `chunk_idx`, so the two could in principle
    disagree and break the `id = doc_id * 1000 + chunk_idx` invariant; both now
    come from the one index. DataFusion 52 rejects `UNNEST ... WITH ORDINALITY`
    (`not_impl`), which is why the index is generated explicitly.

    Wrapped as `SELECT ... FROM (...) AS t` rather than a bare INSERT-from-SELECT
    because DataFusion's INSERT planner otherwise validates row width against the
    inner subquery and drops the vec_to_binary(...) projection.

# Parameters:
#   {doc_id}     - Source-doc id (BIGINT); used as the prefix for synthesised chunk ids
#   {source}     - Source path / identifier kept verbatim on every emitted row
#   {content}    - Full document text (any length); chunked inline by chunk()
#   {chunk_size} - Target max chunk length in characters
#   {overlap}    - Characters of overlap between adjacent chunks (must be < chunk_size)

spec:
  query: |
    INSERT INTO kb.main.documents (id, source, chunk_idx, content, embedding)
    SELECT id, source, chunk_idx, content,
           -- The inner subquery renames chunk_text to content, so `content`
           -- is the only chunk column in scope here. The CHUNK_TEXT variant
           -- fails at execution with "No field named chunk_text" — the
           -- postgres template keeps chunk_text in scope and uses that one,
           -- so the two are NOT interchangeable.
           vec_to_binary({{EMBED_CALL_OVER_CONTENT}})
    FROM (
      SELECT
        CAST({doc_id} AS BIGINT) * 1000 + chunk_idx            AS id,
        {source}                                              AS source,
        chunk_idx,
        chunk_text                                            AS content
      FROM (
        SELECT
          UNNEST(generate_series(CAST(0 AS BIGINT),
                                 CAST(array_length(chunks) AS BIGINT) - 1)) AS chunk_idx,
          UNNEST(chunks)                                                    AS chunk_text
        FROM (
          SELECT chunk('{{CHUNK_MODE}}', {content}, {chunk_size}, {overlap}) AS chunks
        ) c
      ) r
    ) AS t
