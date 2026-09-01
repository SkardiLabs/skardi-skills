# Troubleshooting

Fix the symptoms below by applying the prescribed remedy. Don't retry the same command hoping it will work — each of these has a definite cause.

**Read this first: the local path needs a `skardi-server` on the host, and that is the thing to install.** Every UDF in this skill (`chunk`, `candle`, `gguf`, `remote_embed`) is registered by the *server*, and every operation is an HTTP call to it. The `skardi` CLI is a thin HTTP client — optional, and its version tells you nothing about which UDFs are available. So when an error below says "function not found", the binary to fix is always the server.

## Getting a `skardi-server` for the local (SQLite) path

`--backend sqlite` cannot use the container image: it needs the `sqlite-vec` extension, which the published image does not ship for Linux, and the host's copy is the wrong platform to mount in. So the local path means a server binary on this machine.

**There is no published `skardi-server` binary for any platform.** The v0.5.0 GitHub Release attaches the `skardi` CLI only; the server ships as a container image (which the local path can't use) or as source. Build it from the released tag so you get exactly what v0.5.0 contains:

```bash
git clone --depth 1 --branch v0.5.0 https://github.com/SkardiLabs/skardi.git
cd skardi
cargo install --locked --path crates/server --features rag
```

`--features rag` bundles `chunk` plus all three embedding UDFs, which is what the rendered pipelines expect. Budget for a Rust build the first time — this is the main cost of the local path, and it is unavoidable until a server binary is published.

Pin the tag. Do **not** install from `main`: `main` carries unreleased changes, so a workspace that works today can break on the next pull with no version to point at.

> The **override path** (`--backend postgres` / `mongo` / `lance`) has no such constraint — use `--runtime docker` with `ghcr.io/skardilabs/skardi/skardi-server-full:X.Y.Z` and skip the toolchain entirely.

## `error: Invalid function 'chunk'` (or `Unknown function: chunk`) at ingest time

The **server** you are pointed at was built without `--features chunking` / `--features rag`. Rebuild or re-pull it per the section above; the pre-built `skardi-server-embedding` image does not register `chunk()` either. This is not about the CLI: `setup_context.py` never inspects it, and a matching CLI version would not change which UDFs the server has.

## `error: Unknown function 'candle'` (or `gguf`, or `remote_embed`)

Skardi was built without the matching feature flag. Each embedding UDF is gated behind a cargo feature:

- `candle(...)` → `--features candle`
- `gguf(...)` → `--features gguf`
- `remote_embed(...)` → `--features remote-embed`

Three options, in order of preference:

1. Rebuild the **server** with the feature you need, from the released tag (e.g. `cargo install --locked --path crates/server --features candle,gguf,remote-embed` inside a `v0.5.0` checkout, for all three; `--features rag` covers `candle` + `gguf` + `remote-embed` + `chunking` in one).
2. Re-run `setup_context.py` with a different `--embedding-udf` whose feature *is* available in your build.
3. Embed externally (Python + sentence-transformers / a vendor SDK) and skip Skardi for the write path — feed embeddings directly into `documents.embedding` as packed f32 BLOBs via the simpler `ingest` pipeline.

## `sqlite_knn(...) expects 4 arguments, got 3` at server startup

**The pipeline is not malformed. Do not edit it.** This is the previous
problem wearing a disguise: your server was built without the feature that
registers the embedding UDF.

```
Failed to load pipeline from ".../pipelines/search_hybrid.yaml"
Caused by (level 1): Pipeline loading failed: Error during planning:
sqlite_knn(table, vector_col, query_vec, k) expects 4 arguments, got 3.
```

`search_vector.yaml` and `search_hybrid.yaml` do pass four arguments — the
third is `(SELECT <your embedding UDF>(...))`. When that UDF isn't
registered, DataFusion **silently discards** the argument instead of
reporting the missing function, because the table-function argument planner
drops any argument that fails to plan (verified against datafusion-sql
52.5.0, `src/relation/mod.rs:152`: `flat_map` over a `Result` yields nothing
for an `Err`). The UDTF then receives three expressions and complains about
the count. `sqlite_fts` takes the same path and fails the same way.

Fix it by rebuilding the server with the feature your workspace needs
(`.embedding.txt` records which UDF that is):

```bash
cargo install --locked --path crates/server --features rag
```

`start_server.py` prints this diagnosis for you when startup fails this way.

Two things worth knowing:

- Pipeline loading is alphabetical and the server exits at the first
  failure, so you'll usually only see `search_hybrid` named. `search_vector`
  has the same problem.
- `ingest` and `ingest_chunked` call the same UDF but **load without
  complaint** — an INSERT's projection isn't resolved at load time. They
  fail at execute with the honest `Invalid function '<udf>'`. So a server
  that starts cleanly is not proof the embedding UDF is present.

## `ModuleNotFoundError: No module named 'sqlite_vec'`

The Python package `sqlite-vec` provides both the `vec0` loadable extension and a convenient `loadable_path()` helper. Install it:

```bash
pip install --user sqlite-vec
```

Then either let `setup_context.py` derive the path automatically (it imports `sqlite_vec`) or set it manually:

```bash
export SQLITE_VEC_PATH=$(python -c "import sqlite_vec; print(sqlite_vec.loadable_path())")
```

## `sqlite_fts: fts5: syntax error near "?"` (or `"`, `+`, `-`, `~`, `^`, `(`)

FTS5 reserves those characters as query operators. A natural-language query like `"What is X?"` will blow up on the `?`. Two fixes:

1. **Hybrid search**: send a cleaned `text_query` while leaving `query` intact for the vector side:

   ```bash
   curl -X POST http://localhost:8080/search-hybrid/execute \
     -H 'Content-Type: application/json' \
     -d '{"query":"What is X?","text_query":"X","vector_weight":0.5,"text_weight":0.5,"limit":5}'
   ```

2. **FTS-only**: strip punctuation or phrase-quote:

   ```bash
   curl -X POST http://localhost:8080/search-fulltext/execute \
     -H 'Content-Type: application/json' -d '{"query":"X","limit":5}'

   curl -X POST http://localhost:8080/search-fulltext/execute \
     -H 'Content-Type: application/json' -d '{"query":"\"what is x\"","limit":5}'
   ```

The vector half of hybrid search never sees FTS parsing, so FTS-side errors degrade the hybrid rank but don't break the run — you'll still get vector candidates.

> Both calls above can equally be made with the CLI against the same running server — `skardi --server http://localhost:8080 run search-hybrid -p query="What is X?" -p text_query=X`. There is no `skardi grep` or `skardi fts`; those were subcommands of the pre-#170 CLI and do not exist in v0.5.0, whose subcommands are `query`, `run`, `pipeline`, `job`, `schema`, `health`.

## `INSERT` succeeds but `SELECT COUNT(*) FROM documents_vec` is 0

The `AFTER INSERT` trigger isn't firing. Most likely cause: the DB was not created via `setup_context.py` (or the `sqlite-vec` extension wasn't loaded when it was created, so the `vec0` virtual table doesn't exist, and creating the trigger silently against a missing table fails).

Fix: rerun `python setup_context.py --workspace <dir> --force` and re-ingest.

## `UNIQUE constraint failed: documents.id` on a re-ingest

`ingest_corpus.py` derives stable ids from `(source, chunk_idx)`, so re-running on the same corpus collides. Two fixes:

```bash
# Full rebuild — wipes the DB and re-runs schema:
python setup_context.py --workspace ./kb --force
python ingest_corpus.py --workspace ./kb --corpus ./docs

# Targeted re-ingest of one file — needs the server running (see Step 2):
skardi --server http://localhost:8080 query \
  --sql "DELETE FROM kb.main.documents WHERE source = 'changed_file.md'"
python ingest_corpus.py --workspace ./kb --corpus ./docs   # the rest is skipped (id matches existing rows)
```

## Empty result set from hybrid search

All four checks below run SQL against the **running server**. `--server` defaults to `http://127.0.0.1:8080`, so it can be omitted when the server is on the default port; `curl -X POST .../query -d '{"sql":"..."}'` does the same thing without the CLI.

1. `skardi query --sql "SELECT COUNT(*) FROM kb.main.documents"` — if 0, ingest didn't run or failed silently.
2. `skardi query --sql "SELECT COUNT(*) FROM kb.main.documents_vec"` — if less than `documents`, trigger mismatch (see above).
3. Embedding dim mismatch: if you changed `--embedding-dim` after the DB was created, the vec0 table was built with the old dim and new rows will error. Rebuild with `--force`.
4. Model path broken: `skardi query --sql "SELECT candle('<abs-path>', 'hello world')"` should return a float array. If it errors, fix the absolute path in `pipelines/*.yaml`.

## `search-fulltext` returns 0 rows for a Chinese term that is in the corpus

Not a bug in your setup, and not fixable by re-ingesting. `documents_fts` is created without a `tokenize=` clause, so FTS5 uses `unicode61`, which breaks tokens on non-alphanumeric characters. An unbroken run of Han characters has no such boundary, so the entire run is stored as one token and only an exact full-run query matches it.

Measured 2026-08-13 against a real workspace: `预跑` → 0 rows, `上下文` → 0 rows (both present in the ingested text), `Skardi` → 1, `Agent` → 1. A minimal repro needs no Skardi at all:

```python
db.execute("create virtual table t using fts5(c)")
db.execute("insert into t values ('预跑机制指的是重复查询 Skardi hello')")
db.execute("select count(*) from t where t match '预跑'")                   # 0
db.execute("select count(*) from t where t match 'Skardi'")                 # 1
db.execute("select count(*) from t where t match '预跑机制指的是重复查询'")  # 1
```

Postgres has the same shape via `to_tsvector` with the default `pg_catalog.english`; the `simple` configuration does not help, because it changes stemming and stop words rather than segmentation.

**What to do:** rebuild the workspace with the trigram tokenizer.

```bash
python3 setup_context.py --workspace <ws> --force --fts-tokenizer trigram   # plus your original flags
python3 ingest_corpus.py --workspace <ws> --corpus <dir>
```

trigram indexes every 3-character window, so a 3+ character CJK term matches through the index; `search-fulltext` falls back to a `LIKE` scan below that width, so `预跑` and `查询` are found too rather than returning nothing.

**Why this is opt-in rather than the default:** trigram turns English word search into substring search — measured, a query for `cat` also matches `concatenate` — and it enlarges the index. An English corpus is better served by `unicode61`, so the tokenizer is chosen per corpus. `ingest_corpus.py` warns when a largely-CJK corpus is about to be fed into a `unicode61` index, since that failure is otherwise silent.

**Postgres has the same shape and no equivalent fix here** (`to_tsvector` with `pg_catalog.english`; `simple` changes stemming, not segmentation). There, use `search-vector` or `search-hybrid`. Discussion and measurements in [skardi-skills#26](https://github.com/SkardiLabs/skardi-skills/issues/26).

## `chunk: 'overlap' (N) must be strictly less than 'size' (M)`

The chunk() UDF rejects `overlap >= size` to avoid infinite loops. Pass `--overlap` < `--chunk-size` to `ingest_corpus.py` (or `--chunk-size 1200 --overlap 200`, the defaults). `--overlap 0` is always safe.

## `chunk: unsupported mode '<x>'`

Only `'character'` and `'markdown'` are supported as of v0.5.0. Token-based / code-aware splitters are roadmap items. Pass `--chunk-mode markdown` (default) or `--chunk-mode character` to `setup_context.py`.

## `skardi` hangs indefinitely on the first query

The embedding backend is loading the model (first call only, then cached in the process). Typical cold-load times: small SafeTensors (bge-small, ~100MB) take a couple of seconds on a laptop; quantised GGUF models vary with size; `remote_embed` adds network RTT per call. Subsequent calls are sub-ms (local) or RTT-bounded (remote).

If it takes longer than 30s, the process is stuck. Common causes:

- **Local models (candle / gguf):** the model path passed into the UDF is wrong and something is trying to fetch from HuggingFace — confirm with `lsof -p <pid>` and look for outbound connections.
- **`remote_embed`:** the API key env var is unset, or network egress is blocked. Check the stderr for 401 / connection-refused errors.

## `SQLITE_VEC_PATH` set but `vec0` still missing

`sqlite3` connections created without `enable_load_extension(True)` ignore the env var. Skardi's ctx.yaml sets `extensions_env: SQLITE_VEC_PATH` to opt into loading — make sure that key is present in your `ctx.yaml` (the skill's template includes it).

## Re-indexing after model change

Embedding dim is baked into the `vec0` table at create time. To switch models:

```bash
rm <workspace>/kb.db
python setup_context.py --workspace <workspace> --model-path <new-model> --embedding-dim <new-dim>
python ingest_corpus.py --workspace <workspace> --corpus <docs/>
```

Don't try to `ALTER TABLE` — sqlite-vec doesn't support it. Rebuild is cheap for corpora under 100k chunks.

## "DataFusion planner drops my embedding column" on a custom INSERT

Use a `SELECT ... FROM (SELECT ... AS t)` wrapper rather than `VALUES (...)`. DataFusion's INSERT planner propagates the target schema into immediate-child VALUES clauses and validates row width, which eats any computed column added as a projection. The SELECT wrapper keeps the subquery schema in scope. This is exactly what the skill's `ingest.yaml` and `ingest_chunked.yaml` templates do — copy their shape if you write your own.
