kind: context

metadata:
  name: auto-kb-context
  version: 1.0.0
  description: >
    Skardi context for a local knowledge base. One SQLite file holds the
    canonical rows, an FTS5 mirror, and a sqlite-vec vec0 mirror — all kept
    in sync by AFTER INSERT/UPDATE/DELETE triggers. Queries go through the
    `kb` catalog, so tables surface as `kb.main.documents`, etc.

spec:
  data_sources:
    - name: kb
      type: sqlite
      path: "{{DB_PATH}}"
      access_mode: read_write
      hierarchy_level: catalog
      options:
        extensions_env: SQLITE_VEC_PATH
