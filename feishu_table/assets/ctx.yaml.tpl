kind: context

metadata:
  name: feishu-mirror-context
  version: 1.0.0
  description: >
    Skardi context for a local SQLite mirror of Feishu (Lark) Bitables and
    Sheets. Rows are populated by the sync_bitable.py / sync_sheets.py
    scripts shelling out to lark-cli — Skardi never talks to the Lark Open
    Platform directly. The mirror is intentionally generic: one row per
    Bitable record (fields_json carries the typed cells), one row per
    spreadsheet cell, plus a sync ledger for catalog inspection. Per-table
    and per-column descriptions live in semantics.yaml next to this file
    and surface via `skardi query --schema --all` / GET /data_source.

spec:
  data_sources:
    - name: feishu
      type: sqlite
      path: "{{DB_PATH}}"
      access_mode: read_write
      hierarchy_level: catalog
      description: "Local SQLite mirror of Feishu Bitables/Sheets, populated by lark-cli."
