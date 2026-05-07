# Feishu Bitable field types → SQLite columns

When a query like `SELECT json_extract(fields_json, '$."fld_xxx"') FROM ...` is too slow, or DataFusion can't push `json_extract` down through Skardi's SQLite source, materialise the column at sync time. This file is the lookup the agent needs.

## Lark field type integer codes

Stored in `feishu_bitable_fields.field_type`. Source: Lark Open Platform Bitable docs.

| Code | Lark name | JSON shape returned in `fields` | SQLite column type |
|---|---|---|---|
| 1 | Text (multi-line) | `string` | `TEXT` |
| 2 | Number | `number` | `REAL` (use `NUMERIC` if you need exact decimals) |
| 3 | Single Select | `string` (option name) | `TEXT` |
| 4 | Multi Select | `array<string>` | `TEXT` (JSON-encoded) |
| 5 | DateTime | `number` (ms since epoch) | `INTEGER` (store ms; cast to ISO at read time) |
| 7 | Checkbox | `boolean` | `INTEGER` (0 / 1) |
| 11 | Person | `array<{id, name, email, ...}>` | `TEXT` (JSON) |
| 13 | Phone | `string` | `TEXT` |
| 15 | URL | `{link, text}` | `TEXT` (store `link`) |
| 17 | Attachment | `array<{file_token, name, size, type, url}>` | `TEXT` (JSON, or one row per attachment in a side table) |
| 18 | Single Link | `{link_record_ids, text_arr}` | `TEXT` (JSON) |
| 19 | Lookup | `array<...>` (depends on target field) | `TEXT` (JSON; resolve target's type at agent time) |
| 20 | Formula | depends on formula return type | match the return type's row above |
| 21 | Duplex Link | `{link_record_ids, text_arr}` | `TEXT` (JSON) |
| 22 | Location | `{address, full_address, location, ...}` | `TEXT` (JSON) |
| 23 | Group Chat | `array<{chat_id, name, avatar_url}>` | `TEXT` (JSON) |
| 1001 | Created Time | `number` (ms) | `INTEGER` |
| 1002 | Modified Time | `number` (ms) | `INTEGER` |
| 1003 | Created By | object | `TEXT` (JSON) |
| 1004 | Modified By | object | `TEXT` (JSON) |
| 1005 | AutoNumber | `string` | `TEXT` |

(Codes drift; treat the table as a starting point and re-derive from `lark-cli base +field-list --format json` when in doubt — the response includes `type` and a `property` payload that describes the variant.)

## Materialisation pattern

If you decide to specialise one (`base_token`, `table_id`) into its own typed table:

```sql
-- One column per Lark field, named by field_id (stable across renames).
CREATE TABLE bt_app_xxx_tbl_xxx (
  record_id   TEXT PRIMARY KEY,
  fld_xxx_a   TEXT,
  fld_xxx_b   REAL,
  fld_xxx_c   INTEGER,        -- ms since epoch
  fld_xxx_d   TEXT,            -- JSON for multi-valued
  synced_at   TEXT NOT NULL,
  fields_json TEXT NOT NULL    -- keep the raw payload for fallback
);
```

Then in `sync_bitable.py`, after the existing upsert, run a follow-up step that pulls `fields_json` apart and INSERT-OR-REPLACEs the typed table. Keep the EAV-style `feishu_bitable_records` populated as well — it's the universal fallback when an agent encounters a table the project hasn't materialised yet.

## A pipeline that uses materialised columns

```yaml
kind: pipeline
metadata:
  name: "active-tickets"
  version: "1.0.0"

spec:
  query: |
    SELECT record_id, fld_xxx_a AS title, fld_xxx_b AS priority
    FROM feishu.main.bt_app_xxx_tbl_xxx
    WHERE fld_xxx_c > {since_ms}
    ORDER BY fld_xxx_b DESC
    LIMIT {limit}
```

The trade-off vs. the generic `query-bitable-records` pipeline: typed columns are cheaper to filter/sort, but every schema change in Lark means re-running setup with the new column list. Reach for materialisation only when the EAV path is the actual bottleneck, not preemptively.
