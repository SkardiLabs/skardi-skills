kind: pipeline

metadata:
  name: "query-bitable-records"
  version: "1.0.0"
  description: >
    Page over the records of one Bitable table. fields_json is returned
    verbatim — the caller is expected to parse it and pluck whatever
    fields it cares about (keys are field_id, looked up via
    describe-feishu-source). Filtering by specific cell values requires a
    user-defined per-table pipeline; see references/pipeline_patterns.md.

# Parameters:
#   {base_token} - Bitable Base id.
#   {table_id}   - Table id inside the Base.
#   {limit}      - Max rows to return (server-side cap; page client-side via {after_id}).
#   {after_id}   - Pass the previous page's last record_id, or '' for the first page.

spec:
  query: |
    SELECT
      record_id,
      fields_json,
      synced_at
    FROM feishu.main.feishu_bitable_records
    WHERE base_token = {base_token}
      AND table_id   = {table_id}
      AND ({after_id} = '' OR record_id > {after_id})
    ORDER BY record_id
    LIMIT {limit}
