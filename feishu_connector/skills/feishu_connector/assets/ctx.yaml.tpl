kind: context
metadata:
  name: feishu
  version: 1.0.0
  description: Local SQLite synced from Feishu (Bitable / docs / chat) via lark-cli.
spec:
  data_sources:
    - name: {{TABLE_NAME}}
      type: sqlite
      path: {{DB_PATH}}
      options: { table: {{TABLE_NAME}} }
