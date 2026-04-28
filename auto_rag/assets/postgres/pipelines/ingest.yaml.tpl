kind: pipeline

metadata:
  name: "ingest"
  version: "1.0.0"
  description: >
    Insert one chunk into the user-supplied Postgres table. The embedding
    is supplied as a parameter (computed client-side by the agent) rather
    than via an inline UDF call, so the same pipeline runs against any
    skardi-server build — including the published embedding-suffixed
    images that don't expose candle/gguf/remote_embed UDFs at runtime.

# Parameters:
#   {doc_id}    - BIGINT primary key (unique per chunk across the corpus)
#   {source}    - Source identifier (e.g. file path) for citation
#   {chunk_idx} - Integer position of this chunk within its source
#   {content}   - Chunk text
#   {embedding} - Float32 array (the agent embeds {content} before POSTing)

spec:
  query: |
    INSERT INTO {{TABLE}} (id, source, chunk_idx, content, embedding)
    VALUES (
      {doc_id},
      {source},
      {chunk_idx},
      {content},
      {embedding}
    )
