# Pipeline patterns for the Feishu mirror

The skill ships four generic pipelines. They cover catalog inspection and untyped record reads. Once an agent (or the user) knows which Feishu fields actually matter for their workload, hand-write specialised pipelines next to the rendered ones — the rendered files won't be regenerated unless `setup_feishu.py` is re-run, and even then your hand-written ones aren't touched (different filenames).

## Why specialise?

- **Filter pushdown.** `WHERE fld_xxx = 'open'` against a typed column is an indexed lookup; `WHERE json_extract(fields_json, '$."fld_xxx"') = 'open'` may or may not push through DataFusion → SQLite.
- **Type-correct sorting.** `ORDER BY` on a JSON-extracted field sorts as text. A `REAL` column sorts as a number.
- **Smaller payloads.** `query-bitable-records` returns the full `fields_json` for every row. Most callers only need three columns; specialising shrinks the response to those three.

## Common shapes

### A typed list pipeline

```yaml
kind: pipeline
metadata:
  name: "list-active-tickets"
  version: "1.0.0"

spec:
  query: |
    SELECT
      record_id,
      json_extract(fields_json, '$."fldTitle"')   AS title,
      json_extract(fields_json, '$."fldStatus"')  AS status,
      json_extract(fields_json, '$."fldUpdated"') AS updated_at
    FROM feishu.main.feishu_bitable_records
    WHERE base_token = '<app_token>'
      AND table_id   = '<table_id>'
      AND json_extract(fields_json, '$."fldStatus"') = {status}
    ORDER BY json_extract(fields_json, '$."fldUpdated"') DESC
    LIMIT {limit}
```

If `json_extract` is slow or returns NULL, swap to a materialised table per [references/schema_mapping.md](schema_mapping.md).

### Joining a Bitable to `auth.users`

The `auth.users` virtual table is available when Skardi auth is on (see `skardi-deploy-and-patterns`). A common join: a Bitable with one row per project, owner column `fldOwnerEmail`:

```yaml
kind: pipeline
metadata:
  name: "my-projects"
  version: "1.0.0"

spec:
  query: |
    SELECT
      b.record_id,
      json_extract(b.fields_json, '$."fldName"') AS name
    FROM feishu.main.feishu_bitable_records b
    JOIN auth.users au
      ON json_extract(b.fields_json, '$."fldOwnerEmail"') = au.email
    WHERE au.id        = {user_id}
      AND b.base_token = '<app_token>'
      AND b.table_id   = '<table_id>'
```

`{user_id}` is filled in from the bearer token at request time, so each user only sees their own rows even though the mirror is shared.

### Aggregating a sheet range

```yaml
kind: pipeline
metadata:
  name: "sheet-column-totals"
  version: "1.0.0"

spec:
  query: |
    SELECT
      col_idx,
      SUM(value_number) AS total,
      COUNT(value_number) AS n_numeric
    FROM feishu.main.feishu_sheet_cells
    WHERE spreadsheet_token = {spreadsheet_token}
      AND sheet_id          = {sheet_id}
      AND row_idx > 0                    -- skip header row
    GROUP BY col_idx
    ORDER BY col_idx
```

### Refresh-on-read (don't)

The temptation: write a pipeline that runs the sync as a side effect. Don't — Skardi pipelines are SQL, not arbitrary code, and even if you wrap a sync in a `kind: job` the cost of running it on every read will surprise users. Stick to the explicit `sync_bitable.py` / `sync_sheets.py` invocations and surface `feishu_sync_log.last_synced_at` so the agent can decide.

## Shape rules for hand-written pipelines

1. **One pipeline per question, not per table.** Generic pipelines belong to the skill; project pipelines belong to the project.
2. **Bake the `base_token` / `table_id` in as string literals** unless you genuinely want one pipeline to span multiple Bitables. Hardcoding makes the pipeline self-documenting and removes a class of "wrong table" bugs.
3. **Keep the catalog overlay in sync.** Add a matching entry to `semantics.yaml` for any new pipeline you check in — it's how the next agent finds your pipeline by description.
