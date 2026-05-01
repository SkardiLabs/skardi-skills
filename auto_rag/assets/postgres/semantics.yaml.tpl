kind: semantics

metadata:
  name: auto-rag-semantics
  version: 1.0.0
  description: >
    Catalog overlay attached to the user-supplied Postgres data source so that
    GET /data_source on skardi-server returns meaningful descriptions, and
    `skardi query --schema --all` renders them next to the schema. Sits next
    to ctx.yaml so Skardi auto-discovers it on startup (docs/semantics.md).

spec:
  sources:
    - name: "{{TABLE}}"
      description: >
        Hybrid-search corpus: one row per chunk. Content is searchable via
        pg_fts (GIN over to_tsvector(content)); the embedding column is
        searchable via pg_knn (HNSW over pgvector cosine). Chunks are
        produced by Skardi's chunk() UDF on ingest, so re-running ingest
        with a different chunk size means dropping and rebuilding the table.
      columns:
        - name: id
          description: >
            Stable BIGINT primary key derived from (source, chunk_idx).
            Computed as doc_id * 1000 + chunk_idx so re-ingesting the same
            file produces the same ids (and so the second INSERT fails fast
            on the unique constraint instead of silently duplicating rows).
        - name: source
          description: >
            Relative file path within the original corpus directory, used
            for citation. Filter on this for per-document queries.
        - name: chunk_idx
          description: >
            0-based position of this chunk within its source document, in
            text-splitter emission order.
        - name: content
          description: >
            One chunk of the source text, as produced by chunk(). The same
            text is fed into pg_fts (via to_tsvector) and into the embedding
            UDF on ingest, so vector and FTS signals stay on a single source
            of truth.
        - name: embedding
          description: >
            pgvector column. Dimension is fixed at table-create time and must
            match the embedding model's output. Searched via pg_knn with
            cosine distance ('<=>').
