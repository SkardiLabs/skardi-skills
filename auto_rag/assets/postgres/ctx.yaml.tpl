kind: context

metadata:
  name: auto-rag-pg-context
  version: 1.0.0
  description: >
    Skardi context for a server-backed RAG service over a user-supplied
    PostgreSQL+pgvector table. The same `<TABLE>` row holds both the raw
    content (searched via pg_fts) and the embedding (searched via pg_knn),
    so one INSERT keeps both signals in sync. Per-table and per-column
    descriptions live in semantics.yaml next to this file and surface on
    GET /data_source.

spec:
  data_sources:
    - name: "{{TABLE}}"
      type: "postgres"
      access_mode: "read_write"
      connection_string: "{{CONNECTION_STRING}}"
      description: "User-supplied Postgres table with pgvector embeddings + content for FTS."
      options:
        table: "{{TABLE}}"
        schema: "{{SCHEMA}}"
        user_env: "PG_USER"
        pass_env: "PG_PASSWORD"
