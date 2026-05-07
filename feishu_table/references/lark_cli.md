# `lark-cli` reference for the `feishu_table` skill

This file captures the parts of `lark-cli` the skill leans on. The CLI's own help (`lark-cli <cmd> --help`) is authoritative — assume flag names drift between minor versions.

## Install

```bash
npm i -g @larksuite/cli
lark-cli --version
```

Node ≥ 18. The CLI also ships a Go binary path; both expose the same shortcut commands. If the user sees `command not found: lark-cli`, check `npm prefix -g` is on `PATH`.

## Auth

Two-step, **no env-var bypass**: credentials live in the OS-native keychain.

```bash
# 1. Record app credentials (interactive — paste app_id / app_secret from open.larksuite.com).
lark-cli config init

# 2. Grant scopes to the user/bot principal.
lark-cli auth login --domain base,sheets,drive
# or, headless agent variant:
lark-cli auth login --domain base,sheets,drive --no-wait

# 3. Verify.
lark-cli auth status
lark-cli auth scopes
lark-cli auth check base:record:read     # exit 0 if granted, 1 otherwise
```

`auth list` shows authenticated principals; `--as user` / `--as bot` switches identity per call. The `feishu_table` skill always runs `--as` whatever the default is; if the bot has different scopes than the user, surface that explicitly.

### Headless auth (CI / containers)

`config init` is interactive. To skip it, write the keychain entry directly:

- macOS: `security add-generic-password -a <app_id> -s 'lark-cli' -w <app_secret>`
- Linux: `lark-cli config init` reads from `LARK_CLI_CONFIG_FILE=/path/to/config.json` if set; ship a config file mounted as a secret.
- Windows: Credential Manager — see the upstream README.

For server scopes that don't need user OAuth (tenant-only API calls), the CLI auto-exchanges `app_id` + `app_secret` for a tenant access token; the user-OAuth step (`auth login`) is only required for endpoints that read user-private resources.

## Scopes the skill needs

| Pipeline / sync | Lark scopes |
|---|---|
| `sync_bitable.py` field-list | `base:table:readonly` |
| `sync_bitable.py` record-list | `base:record:retrieve`, `base:record:read` |
| `sync_sheets.py` | `sheets:spreadsheet:readonly` |
| `lark-cli drive +search` (manual discovery) | `drive:file:read`, `drive:drive:read` |

Run `lark-cli auth check <scope>` for each before kicking off a long sync — failing fast saves an hour of paginated 403s.

## Command cheatsheet (Layer-1 shortcuts)

| Goal | Command |
|---|---|
| List Bitables / Sheets the bot can see | `lark-cli drive +search --doc-types bitable,sheet --format ndjson` |
| Discover tables in a Base | `lark-cli base +table-list --base-token app_xxx --format json` |
| Inspect schema of one table | `lark-cli base +field-list --base-token app_xxx --table-id tbl_xxx --format json` |
| Read records (paged) | `lark-cli base +record-list --base-token app_xxx --table-id tbl_xxx --offset 0 --limit 500 --format json` |
| Search records by keyword | `lark-cli base +record-search --base-token app_xxx --table-id tbl_xxx --keyword "foo" --format ndjson` |
| List sheets in a spreadsheet | `lark-cli sheets +info --url "<spreadsheet share URL>" --format json` |
| Read a sheet range | `lark-cli sheets +read --url "<url>" --sheet-id <id> --range "A1:Z10000" --format json` |
| Download a Drive file | `lark-cli drive +download --file-token <tok> --type file --output ./out.xlsx` |

> **Flag pitfall.** Bitable APIs call the Base id `app_token`; the CLI flag is `--base-token`. Passing `--app-token` will be rejected with an unhelpful error.

## Output formats

Global flag on every command:

```
--format json|pretty|table|ndjson|csv
```

The skill uses `json` for record-list (one paged response is small enough to parse whole) and would prefer `ndjson` for very large responses; switch by passing `ndjson=True` to `run_lark()` in `sync_bitable.py` and adapting the parser.

## Pagination

Layer-1 record-list uses `--offset` / `--limit` (max 500) and exposes `total` in the response. There's also a `--page-all` global flag that auto-paginates; the skill walks pages manually to write a checkpoint per page so a mid-run crash doesn't re-pull rows from the beginning.

## Rate limits

- **Bitable record-list / field-list**: 20 req/s per app.
- **Bitable record-batch-create / -update**: 100 req/s, 200 rows per batch.
- **Sheets read**: 100 req/s.

The skill adds a 100 ms cushion (`SLEEP_BETWEEN_PAGES = 0.1`) between Bitable pages — that's well below the cap with one process. If the user runs multiple `sync_bitable.py` jobs in parallel they will rate-limit themselves; the upstream `lark-cli` skills doc explicitly forbids parallel record-list calls for this reason.

When rate-limited, `lark-cli` returns Lark error code `99991663`. Catch and back off; the skill currently surfaces the error and stops, which is the right default for an interactive run.
