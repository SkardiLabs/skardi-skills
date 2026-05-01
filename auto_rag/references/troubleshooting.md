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
| `unknown function: chunk` at ingest time | Server is on Skardi < 0.4.0, or built without `--features chunking` / `--features rag`. The pre-built `skardi-server-embedding` image does NOT register chunk(). | Switch to `skardi-server-rag:latest` (the v0.4.0+ image that bundles chunk + embedding via `--features rag`), or rebuild your binary with `cargo build --release -p skardi-server --features rag`. |
| `unknown function: candle` (or `gguf` / `remote_embed`) at INSERT or query time | skardi-server was built without the matching feature | Use the `skardi-server-rag` image (candle is bundled in the `rag` feature umbrella), or rebuild: `cargo build --release -p skardi-server --features <candle\|gguf\|remote-embed>`. Multiple features can be enabled at once. |
| `skardi-server: command not found` | No release binary on PATH | Either install one (`cargo install --locked --path crates/server --features rag` from a Skardi clone) or pass `--skardi-source <path>` to `start_server.py` so it can fall back to `cargo run --release`. |
| `Skardi X.Y.Z is too old for this skill` from setup_rag.py | The host CLI is < 0.4.0; the server may also be too old | Reinstall: `cargo install --locked --git https://github.com/SkardiLabs/skardi --branch main skardi-cli --features candle`. Update the server image too if you're using docker / k8s. |
| Server starts but `/pipelines` is missing `ingest-chunked` (or any other expected name) | A pipeline YAML failed to load | Read `<workspace>/server.log` — the loader is strict and rejects any file missing `kind: pipeline` at the root. Common causes: stale `*.tpl` files in `<workspace>/pipelines/` (the renderer drops `.tpl` from the filename — if you see a `.tpl` extension in the workspace, setup_rag.py didn't run cleanly). |
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
| Top-1 score is great but top-5 is off-topic | Symptom of corpus + query mismatch, not a bug | This is a retrieval-quality issue — try a paraphrased query, run two scoped queries instead of one, or fall back from `grep` (hybrid) to `vec` or `fts` depending on whether the question is conceptual or lexical. See SKILL.md § Step 6. |

## Process / lifecycle

| Symptom | Cause | Fix |
|---|---|---|
| `start_server.py` says "A server appears to be running already" | `<workspace>/server.pid` left over from a previous run that wasn't stopped cleanly | `python stop_server.py --workspace <workspace>` (it handles stale pids), then restart. |
| `stop_server.py` succeeds but port is still bound | A different process (not started by this skill) is on that port | `lsof -i :<port>` to find it. Pick a different port or stop the conflicting process. |
| Server goes silent after a long ingest | OOM (large embedding model + many concurrent inflight requests) | Lower `--concurrency`, or move to a box with more RAM. Check the system journal / `dmesg` for OOM kills. |

## When in doubt

- Read `<workspace>/server.log`. The skardi-server logs are detailed and almost always name the failing layer.
- Hit `http://localhost:<port>/` in a browser. The dashboard renders every registered pipeline with its inferred parameter list — a wrong parameter type or missing pipeline shows up immediately.
- Run `skardi query --sql "..."` against the same `SKARDICONFIG` directory. Anything the server can do, the CLI can do — the CLI just bypasses the HTTP layer, so a divergence between them is informative (HTTP-only failures usually mean a parameter-binding bug; CLI failures usually mean the data source is unreachable).
