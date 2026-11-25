# Upgrading from auto-knowledge-base / auto-rag

This skill replaced two earlier plugins, `auto-knowledge-base` (local SQLite, driven through the CLI) and `auto-rag` (server in front of a datastore the user ran). A workspace either of them rendered still exists on disk after the plugin update. All of the following was tested on 2026-08-04 against real pre-merge workspaces rebuilt from the deleted code — it is measured behaviour, not an expectation.

**A legacy workspace is served as-is; it is not a migration.** `start_server.py --runtime local-process` starts on an `auto-knowledge-base` workspace, registers all five pipelines, and the rows that were already ingested stay queryable over the new HTTP surface (`/search-fulltext/execute` returned them). `ingest_corpus.py` also talks to it correctly. Nothing needs converting to read what is already there.

**What actually differs:**

| | `auto-knowledge-base` | `auto-rag` | current |
|---|---|---|---|
| `.embedding.txt` keys | udf, model_path, embedding_args, dim, chunk_mode | udf, model_path, embedding_args, dim, table, schema | all of those **plus `backend` and `db_path`** |
| extra files | `aliases.yaml` (nothing reads it now — harmless) | — | — |
| storage | `kb.db` inside the workspace | user's database | either |

The missing `backend` key is the one that mattered. `start_server.py` used to default it to `postgres`, which mislabelled every legacy knowledge-base workspace: `--runtime docker` then skipped the sqlite refusal and started a container that dies later on an opaque extension error. It now decides from the workspace itself — a workspace that owns a `kb.db` is the sqlite kind — and prints a one-off note saying the workspace is pre-merge and which backend was inferred.

**Migrating, when the user wants a current workspace:**

- **sqlite path — do not re-run setup in place.** `setup_context.py` correctly refuses an existing `kb.db` without `--force`, and the refusal leaves the data alone (verified: rows still present afterwards). But `--force` deletes the `.db`, which is everything they ingested. Render a *new* workspace in a fresh directory and re-ingest the corpus; keep the old directory until the new one answers queries.
- **override path — re-running setup is safe**, because it never touches a database the user owns. The user has to supply `--backend postgres --connection-string ... --table ...` again: the old breadcrumb never recorded them.
