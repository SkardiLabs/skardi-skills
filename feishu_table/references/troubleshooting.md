# Troubleshooting — `feishu_table`

Symptom → fix lookup. Read top to bottom and stop at the first match.

## Setup

**`lark-cli not found on PATH`.** Run `npm i -g @larksuite/cli`. Confirm `which lark-cli`. If npm's global bin is not on `PATH`, fix `PATH` rather than aliasing — the skill calls `lark-cli` from a Python `subprocess.run`, which doesn't resolve shell aliases.

**`skardi <version>` is too old.** Upgrade to ≥ 0.4.0. The semantics overlay loader (`kind: semantics`) and a few catalog improvements landed in 0.4.0; older binaries silently ignore the file and the catalog will look bare to any agent.

**`Health probe failed: SELECT 1`.** Inspect `<workspace>/ctx.yaml` — the most common cause is a relative `path:` that resolves differently from the directory where Skardi was launched. `setup_feishu.py` writes an absolute path; if you've moved the workspace, re-render with `setup_feishu.py --workspace <new>`.

## Auth / scopes

**`auth: scope <scope> not granted`** from `lark-cli`. Re-run `lark-cli auth login --domain base,sheets,drive`. The skill needs at minimum `base:table:readonly`, `base:record:read` and (if syncing sheets) `sheets:spreadsheet:readonly`. Verify with `lark-cli auth check base:record:read`.

**`HTTP 401` / `invalid access_token`.** `lark-cli auth status` will show if the token is expired. Re-login. If you're using a tenant-only path (no user OAuth), check that `app_id` / `app_secret` in the keychain are correct: `lark-cli config show`.

## Sync

**`field-list returned no rows`.** Almost always a wrong `--base-token` (you passed the *table* id by mistake — the API URL says `app_token`, but the CLI flag is `--base-token`). Confirm `lark-cli base +table-list --base-token <yours> --format json` lists tables.

**Sync runs but records are 0.** Schema synced (`feishu_bitable_fields` has rows) but `record-list` returned empty. Possibilities:
1. The Bitable is genuinely empty (check in the Lark UI).
2. The bot user doesn't have read access to records (the *Base* might be shared with the app, but a per-table ACL can hide records — toggle "Permissions" in the Bitable).
3. A `--view-id` filter is hiding everything; drop the flag and re-run.

**Lark error code `99991663`** (rate limit). Increase `SLEEP_BETWEEN_PAGES` in `sync_bitable.py` (default 0.1 → try 0.2). Stop running multiple sync scripts in parallel — the Bitable record-list endpoint forbids concurrent calls per the upstream `lark-cli` skills doc, and the rate cap is per-app (not per-process).

**Lark error code `99991668`** (no permission). The bot is in the workspace but not granted access to that specific Bitable. Add the bot as a collaborator inside the Bitable's "Permissions" panel.

**`record_id` collisions when re-running with `--full-refresh`.** That's the point — `--full-refresh` deletes the prior rows and re-inserts. If you see the script fail with a primary-key violation *without* `--full-refresh`, your mirror is corrupt; `DELETE FROM feishu_bitable_records WHERE base_token=? AND table_id=?` and re-run.

**Sheet sync says "0 cells" on a non-empty range.** `lark-cli sheets +read` returns an empty matrix when the `--range` is outside the sheet's actual extent. Lark APIs are strict — `A1:Z10000` works on most sheets but won't bring back data if the sheet has a `frozenRowCount` larger than 0 and you forgot to include it. Try `A1:ZZ100000` and `lark-cli sheets +info --url <url>` to inspect the bounds.

## Querying via Skardi

**`describe-feishu-source` works, `query-bitable-records` is empty.** The records didn't sync — check `feishu_sync_log` for the `(bitable, <base>/<table>)` row's `status`. If it's `err: ...`, fix and re-sync.

**`json_extract` returns NULL on every row.** Two causes:
1. DataFusion did not push the call down through Skardi's SQLite source. Confirm by running the same query directly: `sqlite3 <workspace>/feishu.db 'SELECT json_extract(fields_json, "$.\"fld_xxx\"") FROM feishu_bitable_records LIMIT 1'`. If that returns NULL too, it's cause 2.
2. The field id in your query doesn't match the JSON keys. Inspect a raw row: `sqlite3 <workspace>/feishu.db 'SELECT fields_json FROM feishu_bitable_records LIMIT 1'`. The keys are `field_id`s (e.g. `fldXXX`), not field names. Look the id up in `feishu_bitable_fields`.

If cause 1 is real, materialise the column — see [references/schema_mapping.md](schema_mapping.md).

**`HTTP 502` on a Skardi pipeline call.** Look at Skardi's stderr — the pipeline yaml may have failed to load (e.g. trailing `{}` in a `WHERE` clause), in which case `list-feishu-sources` will work but the broken pipeline won't.

**A row I just edited in Lark isn't here yet.** The mirror only refreshes when a sync script runs. Re-run `sync_bitable.py` for that `(base, table)`. Surface `last_synced_at` from `feishu_sync_log` to the agent so it can warn about staleness instead of silently returning old data.

## Schema drift

**A column I expected is missing from `feishu_bitable_fields`.** It was deleted in Lark, or a permission change hid it. `field-list` returns only fields the bot can see; if you renamed a Bitable column, the *id* is stable but the name moved — query by `field_id`, not `field_name`.

**A column I expected is in `fields_json` but not in `feishu_bitable_fields`.** Schema and records were pulled at different times; rerun `sync_bitable.py` to refresh both atomically.
