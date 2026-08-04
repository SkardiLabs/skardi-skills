kind: pipeline

metadata:
  name: "ingest-chunked"
  version: "1.0.0"
  description: >
    Ingest one document end-to-end on the server: split it into chunks inline
    with chunk(<mode from setup>, ...), embed each chunk with the configured UDF, and
    INSERT one row per chunk in a single SQL statement. The agent POSTs the
    raw document content; the server does the chunking and embedding. This
    requires the skardi-server-rag image (Skardi >= 0.4.0; --features rag),
    where both chunk() and the chosen embedding UDF are registered.

    Synthesised chunk ids: `id = doc_id * 1000 + chunk_idx` (0-based), so
    callers must pick `doc_id` values whose chunks won't collide. `chunk_idx`
    is derived from an explicit positional index (see below), NOT from row
    order, so the same document always yields the same (chunk_idx, content)
    pairs and the same ids on every run. `source` is recorded verbatim for
    citation.

    Determinism note: chunk() returns an ORDERED List<Utf8>. We enumerate it
    with generate_series(0 .. array_length-1) and unnest that index list in
    the SAME projection as UNNEST(chunks); DataFusion zips two unnests in a
    projection positionally, so index i is paired with chunk i by
    construction. The earlier `ROW_NUMBER() OVER (ORDER BY 1)` form was
    non-deterministic: `ORDER BY 1` orders by the constant literal 1, making
    every row a peer, so DataFusion was free to number chunks in any order —
    the same chunk could get a different chunk_idx (and therefore a different
    id) on each run. DataFusion 52 does not support `UNNEST ... WITH
    ORDINALITY` (it errors `not_impl`), which is why the index is generated
    explicitly rather than via ordinality.

# Parameters:
#   {doc_id}     - Source-doc id (BIGINT); used as the prefix for synthesised chunk ids
#   {source}     - Source path / identifier kept verbatim on every emitted row
#   {content}    - Full document text (any length); chunked inline by chunk()
#   {chunk_size} - Target max chunk length in characters
#   {overlap}    - Characters of overlap between adjacent chunks (must be < chunk_size)

spec:
  query: |
    INSERT INTO {{TABLE}} (id, source, chunk_idx, content, embedding)
    SELECT
      CAST({doc_id} AS BIGINT) * 1000 + chunk_idx       AS id,
      {source}                                          AS source,
      chunk_idx,
      chunk_text                                        AS content,
      {{EMBED_CALL_OVER_CHUNK_TEXT}}                    AS embedding
      -- ^ this template keeps chunk_text in scope at the outer level
      --   (the subquery selects it through), unlike the sqlite one
      --   which renames it to content. The two are NOT interchangeable.
    FROM (
      SELECT
        UNNEST(generate_series(CAST(0 AS BIGINT),
                               CAST(array_length(chunks) AS BIGINT) - 1)) AS chunk_idx,
        UNNEST(chunks)                                                    AS chunk_text
      FROM (
        SELECT chunk('{{CHUNK_MODE}}', {content}, {chunk_size}, {overlap}) AS chunks
      ) c
    ) r
