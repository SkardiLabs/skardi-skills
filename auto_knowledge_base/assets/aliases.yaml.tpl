kind: aliases

metadata:
  name: auto-kb-aliases
  version: 1.0.0
  description: Short-verb shortcuts for the auto_knowledge_base skill

spec:
  ingest:
    pipeline: ingest
    positional:
      - doc_id
      - source
      - chunk_idx
      - content
    description: Insert one pre-chunked row; the AFTER INSERT trigger mirrors it into FTS + vec.
  ingest-doc:
    pipeline: ingest-chunked
    positional:
      - doc_id
      - source
      - content
    defaults:
      chunk_size: "1200"
      overlap: "200"
    description: Chunk a whole document inline, embed each chunk, and insert one row per chunk.
  grep:
    pipeline: search-hybrid
    positional:
      - query
    defaults:
      text_query: "{query}"
      vector_weight: "0.5"
      text_weight: "0.5"
      limit: "10"
    description: Hybrid search (RRF of sqlite_knn + sqlite_fts).
  vec:
    pipeline: search-vector
    positional:
      - query
    defaults:
      limit: "10"
    description: Vector-only semantic search via sqlite_knn.
  fts:
    pipeline: search-fulltext
    positional:
      - query
    defaults:
      limit: "10"
    description: Full-text-only search via sqlite_fts.
