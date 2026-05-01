kind: pipeline

metadata:
  name: "ingest-chunked"
  version: "1.0.0"
  description: >
    Ingest one document end-to-end on the server: split it into chunks inline
    with chunk('markdown', ...), embed each chunk with the configured UDF, and
    INSERT one row per chunk in a single SQL statement. The agent POSTs the
    raw document content; the server does the chunking and embedding. This
    requires the skardi-server-rag image (Skardi >= 0.4.0; --features rag),
    where both chunk() and the chosen embedding UDF are registered.

    Synthesised chunk ids: `id = doc_id * 1000 + chunk_idx` (0-based), so
    callers must pick `doc_id` values whose chunks won't collide. ROW_NUMBER
    drives chunk_idx and matches text-splitter's emission order. `source` is
    recorded verbatim for citation.

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
    FROM (
      SELECT
        ROW_NUMBER() OVER (ORDER BY 1) - 1              AS chunk_idx,
        chunk_text
      FROM (
        SELECT UNNEST(chunk('markdown', {content}, {chunk_size}, {overlap})) AS chunk_text
      ) c
    ) r
