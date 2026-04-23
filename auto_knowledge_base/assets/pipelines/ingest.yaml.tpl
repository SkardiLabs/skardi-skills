kind: pipeline

metadata:
  name: "ingest"
  version: "1.0.0"
  description: >
    Insert a single chunk into the KB. The seed row is wrapped as
    SELECT ... AS t (not VALUES) because DataFusion's INSERT planner
    currently collapses VALUES projections and drops the computed
    embedding column; SELECT-wrap preserves it. The AFTER INSERT trigger
    fans the row to documents_fts and documents_vec in the same
    transaction.

# Parameters:
#   {doc_id}     - BIGINT primary key (unique per chunk across the corpus)
#   {source}     - Source identifier (e.g. file path) for citation
#   {chunk_idx}  - Integer position of this chunk within its source
#   {content}    - Chunk text (also the source for the embedding)

spec:
  query: |
    INSERT INTO kb.main.documents (id, source, chunk_idx, content, embedding)
    SELECT id, source, chunk_idx, content,
           vec_to_binary({{EMBEDDING_CALL}})
    FROM (
      SELECT CAST({doc_id} AS BIGINT) AS id,
             {source} AS source,
             CAST({chunk_idx} AS BIGINT) AS chunk_idx,
             {content} AS content
    ) AS t
