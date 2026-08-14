kind: pipeline

metadata:
  name: "ingest"
  version: "1.0.0"
  description: >
    Insert one pre-chunked row into the user-supplied Postgres table, with
    the embedding computed inline server-side via the configured UDF. Use
    this when the agent already has chunk text in hand (e.g. from a custom
    chunker) and just needs to write it. For end-to-end document ingest —
    chunk + embed + write in one statement — use the `ingest-chunked`
    pipeline instead.

# Parameters:
#   {doc_id}    - BIGINT primary key (unique per chunk across the corpus)
#   {source}    - Source identifier (e.g. file path) for citation
#   {chunk_idx} - Integer position of this chunk within its source
#   {content}   - Chunk text (also the source for the embedding)

spec:
  query: |
    INSERT INTO {{TABLE}} (id, source, chunk_idx, content, embedding)
    SELECT id, source, chunk_idx, content,
           {{EMBED_CALL_OVER_CONTENT}} AS embedding
    FROM (
      SELECT
        CAST({doc_id} AS BIGINT)    AS id,
        {source}                    AS source,
        CAST({chunk_idx} AS BIGINT) AS chunk_idx,
        {content}                   AS content
    ) AS t
