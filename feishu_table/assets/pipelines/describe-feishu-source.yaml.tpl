kind: pipeline

metadata:
  name: "describe-feishu-source"
  version: "1.0.0"
  description: >
    Return the column list for one (base_token, table_id). The agent
    inspects this before composing a query so it can map field_name ->
    field_id (queries against fields_json key off field_id, not the
    human label which can be renamed).

# Parameters:
#   {base_token} - Bitable Base id (app_token in API URLs).
#   {table_id}   - Table id inside the Base.

spec:
  query: |
    SELECT
      field_id,
      field_name,
      field_type,
      is_primary,
      description
    FROM feishu.main.feishu_bitable_fields
    WHERE base_token = {base_token}
      AND table_id   = {table_id}
    ORDER BY is_primary DESC, field_name
