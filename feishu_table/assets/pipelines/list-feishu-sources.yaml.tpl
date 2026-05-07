kind: pipeline

metadata:
  name: "list-feishu-sources"
  version: "1.0.0"
  description: >
    List every (source_type, source_key) the local mirror has seen, with
    the most recent sync timestamp, row count, and status. Use as the
    catalog endpoint when an agent wants to know what Feishu data is
    available before issuing a query.

spec:
  query: |
    SELECT
      source_type,
      source_key,
      last_synced_at,
      row_count,
      status
    FROM feishu.main.feishu_sync_log
    ORDER BY last_synced_at DESC
