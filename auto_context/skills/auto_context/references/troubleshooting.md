# Troubleshooting

When something fails, find the symptom in the table below before
speculating. Most of the failure surface is at the boundary between the
agent, `skardi-server`, and the user's datastore — those three layers
fail in different ways and the right fix depends on which one is at
fault.

## Connection / auth (user-supplied datastore)

| Symptom | Likely cause | Fix |
|---|---|---|
| `Failed to create PostgreSQL connection pool` at server start | DB not reachable on the connection string's host:port | Verify with the user that the DB is up; for Docker, `docker ps` and `docker logs <name>`. |
| `password authentication failed for user "..."` | `PG_USER` / `PG_PASSWORD` env vars not exported in the shell that started skardi-server | Re-export and restart the server. The server reads env at startup, not at request time. |
| `role "..." does not exist` | Same root cause as above (PG sees the unset username as the OS user) | Same fix — export `PG_USER`. |
| `relation "..." does not exist` | User has not created the schema yet | Print the SQL block from [schemas.md](schemas.md) again, wait for confirmation. Do not create the table yourself. |
| `failed to load extension "vector"` | pgvector not installed in the target DB | The user installs (`CREATE EXTENSION vector` after installing the OS package) or switches to a managed Postgres that ships pgvector (Supabase, Neon, RDS pgvector, etc.). The agent does not run this. |
| Mongo: `Authentication failed` | `MONGO_USER` / `MONGO_PASS` env vars or auth source mismatch | Check `--authenticationDatabase`, env vars, and that the user created the DB-level user (not just the root user). |
| Lance: `No such file or directory` on the dataset path | Path is relative and skardi-server's CWD doesn't resolve it, or the user hasn't created the dataset yet | Use an absolute path in `ctx.yaml`, or have the user run the dataset-bootstrap snippet from [schemas.md](schemas.md). |

## Skardi build / feature flags

| Symptom | Cause | Fix |
|---|---|---|
| `unknown function: chunk` at ingest time | The **server** was built without `--features chunking` / `--features rag`. The pre-built `skardi-server-embedding` image does NOT register chunk(). | Switch to `ghcr.io/skardilabs/skardi/skardi-server-rag:0.5.0` (bundles chunk + embedding via `--features rag`), or rebuild your binary with `cargo build --release -p skardi-server --features rag`. Note this is a property of the server build, not of the `skardi` CLI — the CLI is a thin HTTP client and its version tells you nothing about which UDFs are registered. |
| `unknown function: candle` (or `gguf` / `remote_embed`) at INSERT or query time | skardi-server was built without the matching feature | Use the `skardi-server-rag` image (candle is bundled in the `rag` feature umbrella), or rebuild: `cargo build --release -p skardi-server --features <candle\|gguf\|remote-embed>`. Multiple features can be enabled at once. |
| `skardi-server: command not found` | No release binary on PATH | Either install one (`cargo install --locked --path crates/server --features rag` from a Skardi clone) or pass `--skardi-source <path>` to `start_server.py` so it can fall back to `cargo run --release`. |
| **At startup:** `Failed to load pipeline from .../search_hybrid.yaml` → `sqlite_knn(table, vector_col, query_vec, k) expects 4 arguments, got 3` (or the same shape for `sqlite_fts` / `pg_knn` / `pg_fts`) | **The arity complaint is a red herring — do not go counting arguments.** The rendered SQL does pass 4. The real cause is that the embedding UDF in the third argument (`candle` / `gguf` / `remote_embed`) is not registered in this server build, so the planner drops that whole call and the table function is left holding 3 arguments. Verified 2026-08-04 against a `skardi-server` built without `--features rag`: replacing the third argument with `(SELECT 1)` loads fine, and putting an unregistered UDF back reproduces the "got 3" error exactly. | Start a server that has the UDF: use the `skardi-server-rag` image, or rebuild with `--features rag` (plus `--features gguf` / `remote-embed` if the chosen UDF is outside the `rag` umbrella). Then re-run `start_server.py`. |
| `skardi: command not found` while following this skill | Nothing here needs it. `setup_context.py` no longer checks for the CLI, and every operation in this skill is an HTTP call to the server. | Ignore it. The CLI is a convenient way to talk to a *running* server (`skardi --server <url> query --sql ...`), not a prerequisite. Install it from the [v0.5.0 release](https://github.com/SkardiLabs/skardi/releases/tag/v0.5.0) if you want it. |
| Server starts but `/pipelines` is missing `ingest-chunked` (or any other expected name) | A pipeline YAML failed to load | Read `<workspace>/server.log` — the loader is strict and rejects any file missing `kind: pipeline` at the root. Common causes: stale `*.tpl` files in `<workspace>/pipelines/` (the renderer drops `.tpl` from the filename — if you see a `.tpl` extension in the workspace, setup_context.py didn't run cleanly). |
| `embedding column has dimension 384, expected 1024` (or similar) on every INSERT | Schema's `vector(N)` doesn't match the embedding model's output | Pick a model with the matching dim, OR drop and recreate the table with the right dim. There is no in-place fix once rows have been written with a different dim. |

## Embedding-specific

| Symptom | Cause | Fix |
|---|---|---|
| First INSERT takes 30+ seconds, subsequent ones are fast | Candle/GGUF model load on first call (lazy) | Expected. Pre-warm by hitting a search endpoint once before bulk ingest if latency matters. Use `RUST_LOG=info` to see load timing in `server.log`. |
| Every embedding is all zeros | Model loaded but tokenizer/architecture mismatch (e.g. picked a non-encoder model) | Pick a model from a documented family — BERT/RoBERTa/DistilBERT/Jina for candle, llama.cpp-supported encoders for GGUF, or use `remote_embed`. The Skardi source tree's `docs/embeddings/{candle,gguf,remote}/README.md` lists tested models. |
| `remote_embed` errors with `401 Unauthorized` | API key env var not in skardi-server's environment | The relevant `OPENAI_API_KEY` / `VOYAGE_API_KEY` / `GEMINI_API_KEY` / `MISTRAL_API_KEY` must be exported *before* `start_server.py` runs. Restart the server after exporting. |
| `remote_embed` errors with `429 Too Many Requests` | Provider rate-limit during bulk ingest | Lower `--concurrency` on `ingest_corpus.py` (try 1–2) or wait a minute. The progress manifest means resuming after a pause loses no work. |
| `chunk: 'overlap' (N) must be strictly less than 'size' (M)` from /ingest-chunked/execute | The user passed `overlap >= chunk_size` | Pass `--overlap` < `--chunk-size` to `ingest_corpus.py`. `--overlap 0` is always safe. |
| `chunk: unsupported mode '<x>'` | The `ingest_chunked` pipeline references a chunk mode the server doesn't know | Only `'character'` and `'markdown'` are supported in 0.4.0. The skill's templates default to `'markdown'`; if you hand-edited the pipeline, restore one of those values. |

## Pipeline / search-time

| Symptom | Cause | Fix |
|---|---|---|
| `fts5: syntax error` (CLI) or `pg_fts: syntax error in tsquery` (server) | User's question contains FTS reserved chars (`?`, `"`, `+`, `-`, `~`, `^`, parens) | Strip them from the FTS half, or phrase-quote the whole thing. The vector half of hybrid search still works, so the answer degrades but isn't empty. |
| Hybrid search returns rows but `rrf_score` is 0 for everything | Both `pg_knn` and `pg_fts` returned empty result sets, so the FULL OUTER JOIN produced rows with no rank | Check ingest succeeded (`SELECT count(*) FROM <table>`) and the embedding column is populated (`SELECT count(*) FROM <table> WHERE embedding IS NOT NULL`). |
| `search-vector` returns the same chunk for every query | Either the FTS index is fine but the embedding column is null on most rows, or the model's output happens to be near-constant on the corpus | Inspect a few rows: `SELECT id, content, embedding[0:5] FROM <table> LIMIT 3`. If embeddings look identical across rows, the embedding UDF probably isn't running (every chunk got NULL or a default); rebuild the corpus with a working build. |
| Top-1 score is great but top-5 is off-topic | Symptom of corpus + query mismatch, not a bug | This is a retrieval-quality issue — try a paraphrased query, run two scoped queries instead of one, or fall back from `search-hybrid` to `search-vector` or `search-fulltext` depending on whether the question is conceptual or lexical. See SKILL.md § Step 4. |

## Process / lifecycle

| Symptom | Cause | Fix |
|---|---|---|
| `start_server.py` says "A server appears to be running already" | `<workspace>/server.pid` left over from a previous run that wasn't stopped cleanly | `python stop_server.py --workspace <workspace>` (it handles stale pids), then restart. |
| `stop_server.py` succeeds but port is still bound | A different process (not started by this skill) is on that port | `lsof -i :<port>` to find it. Pick a different port or stop the conflicting process. |
| `start_server.py` says "127.0.0.1:&lt;port&gt; is already accepting connections" | Something already listens there — most often a skardi-server this skill started for a *different* workspace | `lsof -nP -iTCP:<port> -sTCP:LISTEN` names the owner. Stop it (`stop_server.py --workspace <that workspace>`) or re-run with a free `--port`. This is a refusal on purpose: `/health` on an occupied port answers from the process already there, so continuing would report a healthy server that never started. |
| `start_server.py` says "skardi-server exited before it was healthy" | The server process died during startup — the last 50 log lines printed above the error name the reason (unloadable pipeline, feature-flag mismatch, missing credentials) | Fix what the log names, then re-run. The script no longer waits out the full `--health-timeout` for a process that is already gone, so this appears within a second or two. |
| Server refuses to start with a table-function argument-count error, e.g. `sqlite_knn(table, vector_col, query_vec, k) expects 4 arguments, got 3` — but the template plainly passes four | **The server was built without the cargo feature that registers this workspace's embedding UDF.** The template's third argument is `(SELECT <udf>(...))`. When that UDF isn't registered, DataFusion fails to resolve it and — inside a *table function* argument list — drops the argument instead of reporting it, so four become three. The count error names `sqlite_knn` and never mentions the UDF, which points you at the template. Editing the template cannot fix it. | Rebuild/install a server carrying the feature for your `--embedding-udf`: `remote_embed`→`--features remote-embed`, `candle`→`candle`, `gguf`→`gguf` (or `--features rag` for the bundle), and make sure *that* binary is the one on PATH — `start_server.py` prefers PATH over `--skardi-source`, so a wrong-feature binary wins silently. Confirm with `strings $(which skardi-server) \| grep -c <udf_name>`; 0 means the UDF isn't in the build. Verified both directions 2026-08-04: a `--features candle` binary died on a `remote_embed` workspace, and a `--features remote-embed,chunking` binary built from main died identically on a `candle` workspace — same error, opposite builds, so this is about features, not version age. |
| Server goes silent after a long ingest | OOM (large embedding model + many concurrent inflight requests) | Lower `--concurrency`, or move to a box with more RAM. Check the system journal / `dmesg` for OOM kills. |

## When in doubt

- Read `<workspace>/server.log`. The skardi-server logs are detailed and almost always name the failing layer.
- Hit `http://localhost:<port>/` in a browser. The dashboard renders every registered pipeline with its inferred parameter list — a wrong parameter type or missing pipeline shows up immediately.
- Run ad-hoc SQL against the **running server** to separate a pipeline problem from a data-source problem: `skardi --server http://localhost:<port> query --sql "SELECT count(*) FROM ..."`, or the same thing with `curl -X POST .../query`. If raw SQL works but the pipeline does not, the fault is in the pipeline's parameters or SQL; if raw SQL fails too, the source itself is unreachable or the schema is wrong.
  - This goes through the same HTTP surface as everything else — the CLI does **not** bypass it. Since skardi PR #170 the CLI holds no engine, so there is no "run it locally instead" comparison to make. An older version of this guide suggested pointing the CLI at a `SKARDICONFIG` directory; that variable and that mode no longer exist.
