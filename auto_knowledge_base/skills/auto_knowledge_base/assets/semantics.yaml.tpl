kind: semantics

metadata:
  name: auto-kb-semantics
  version: 1.0.0
  description: >
    Catalog overlay attached to the kb data source so that `skardi query --schema`
    and any agent reading the catalog see what the documents table holds. Keeping
    these descriptions in a separate file (rather than inline in ctx.yaml) is the
    pattern docs/semantics.md recommends for auto-generated overlays — this file
    can be regenerated without touching the ctx, and a hand-curated file can sit
    next to it in semantics/ without conflict.

spec:
  sources:
    - name: kb
      description: >
        Local knowledge base built by the auto_knowledge_base skill. One SQLite
        catalog with documents (canonical rows), documents_fts (FTS5 mirror), and
        documents_vec (sqlite-vec vec0 mirror) kept in sync by AFTER INSERT/UPDATE/
        DELETE triggers. Query via skardi grep / vec / fts.
      columns:
        - name: id
          description: >
            Stable BIGINT primary key derived from (source, chunk_idx). Computed
            as doc_id * 1000 + chunk_idx so re-ingesting the same file produces
            the same ids.
        - name: source
          description: >
            Relative file path within the original corpus directory, used for
            citation in the answer.
        - name: chunk_idx
          description: >
            0-based position of this chunk within its source document, in
            text-splitter emission order.
        - name: content
          description: >
            One chunk of the source text, as produced by Skardi's chunk() UDF
            ('markdown' or 'character' splitter, depending on --chunk-mode at
            setup time). Indexed by FTS5 and embedded into the vec0 mirror.
        - name: embedding
          description: >
            Packed Float32 BLOB consumed by sqlite-vec's vec0 virtual table.
            Dimension is fixed at workspace creation time to match the chosen
            embedding model.
