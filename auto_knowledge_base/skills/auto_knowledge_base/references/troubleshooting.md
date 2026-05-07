# Troubleshooting

Fix the symptoms below by applying the prescribed remedy. Don't retry the same command hoping it will work — each of these has a definite cause.

## `skardi: command not found`

The Skardi CLI isn't installed or isn't on PATH.

```bash
# Install from source (macOS / Linux):
git clone https://github.com/SkardiLabs/skardi.git
cd skardi
cargo install --locked --path crates/cli --features candle

# Or grab a pre-built binary (see repo README for the platform table).
```

The `--features candle` flag is what enables the local embedding UDF. Without it, `candle(...)` calls fail with "function not found".

## `Skardi X.Y.Z is too old for this skill` from setup_kb.py

`auto_knowledge_base` requires Skardi >= 0.4.0 because it uses the `chunk()` UDF (added in 0.4.0) for inline server-side chunking. Reinstall:

```bash
cargo install --locked --git https://github.com/SkardiLabs/skardi --branch main \
  skardi-cli --features candle
```

If you have a newer source tree locally, `cargo install --locked --path /path/to/skardi/crates/cli --features candle` builds from the source directly.

## `error: Invalid function 'chunk'` (or `Unknown function: chunk`) at ingest time

Same root cause as above — the `chunk()` UDF is in 0.4.0 only. The CLI binary you're running was built before that. Even if `setup_kb.py` passed (because the version-parse warning is non-fatal), `ingest_corpus.py`'s INSERT will fail at execution. Rebuild the CLI from a 0.4.0+ source.

## `error: Unknown function 'candle'` (or `gguf`, or `remote_embed`)

Skardi was built without the matching feature flag. Each embedding UDF is gated behind a cargo feature:

- `candle(...)` → `--features candle`
- `gguf(...)` → `--features gguf`
- `remote_embed(...)` → `--features remote-embed`

Three options, in order of preference:

1. Rebuild Skardi with the feature you need (e.g. `cargo install --locked --git https://github.com/SkardiLabs/skardi --branch main skardi-cli --features candle,gguf,remote-embed` for all three).
2. Re-run `setup_kb.py` with a different `--embedding-udf` whose feature *is* available in your build.
3. Embed externally (Python + sentence-transformers / a vendor SDK) and skip Skardi for the write path — feed embeddings directly into `documents.embedding` as packed f32 BLOBs via the simpler `ingest` pipeline.

## `ModuleNotFoundError: No module named 'sqlite_vec'`

The Python package `sqlite-vec` provides both the `vec0` loadable extension and a convenient `loadable_path()` helper. Install it:

```bash
pip install --user sqlite-vec
```

Then either let `setup_kb.py` derive the path automatically (it imports `sqlite_vec`) or set it manually:

```bash
export SQLITE_VEC_PATH=$(python -c "import sqlite_vec; print(sqlite_vec.loadable_path())")
```

## `sqlite_fts: fts5: syntax error near "?"` (or `"`, `+`, `-`, `~`, `^`, `(`)

FTS5 reserves those characters as query operators. A natural-language query like `"What is X?"` will blow up on the `?`. Two fixes:

1. **Hybrid search**: override `--text_query` with a cleaned version while leaving `--query` intact for the vector side:

   ```bash
   skardi grep "What is X?" --text_query="X"
   ```

2. **FTS-only**: strip punctuation or phrase-quote:

   ```bash
   skardi fts "X"
   skardi fts '"what is x"'
   ```

The vector half of hybrid search never sees FTS parsing, so if `skardi grep` returns any FTS-side errors they degrade the hybrid rank but don't break the run — you'll still get vector candidates.

## `INSERT` succeeds but `SELECT COUNT(*) FROM documents_vec` is 0

The `AFTER INSERT` trigger isn't firing. Most likely cause: the DB was not created via `setup_kb.py` (or the `sqlite-vec` extension wasn't loaded when it was created, so the `vec0` virtual table doesn't exist, and creating the trigger silently against a missing table fails).

Fix: rerun `python setup_kb.py --workspace <dir> --force` and re-ingest.

## `UNIQUE constraint failed: documents.id` on a re-ingest

`ingest_corpus.py` derives stable ids from `(source, chunk_idx)`, so re-running on the same corpus collides. Two fixes:

```bash
# Full rebuild — wipes the DB and re-runs schema:
python setup_kb.py --workspace ./kb --force
python ingest_corpus.py --workspace ./kb --corpus ./docs

# Targeted re-ingest of one file:
SKARDICONFIG=./kb skardi query --sql "DELETE FROM kb.main.documents WHERE source = 'changed_file.md'"
python ingest_corpus.py --workspace ./kb --corpus ./docs   # the rest is skipped (id matches existing rows)
```

## Empty result set from `skardi grep`

Check in order:

1. `skardi query --sql "SELECT COUNT(*) FROM kb.main.documents"` — if 0, ingest didn't run or failed silently.
2. `skardi query --sql "SELECT COUNT(*) FROM kb.main.documents_vec"` — if less than `documents`, trigger mismatch (see above).
3. Embedding dim mismatch: if you changed `--embedding-dim` after the DB was created, the vec0 table was built with the old dim and new rows will error. Rebuild with `--force`.
4. Model path broken: `skardi query --sql "SELECT candle('<abs-path>', 'hello world')"` should return a float array. If it errors, fix the absolute path in `pipelines/*.yaml`.

## `chunk: 'overlap' (N) must be strictly less than 'size' (M)`

The chunk() UDF rejects `overlap >= size` to avoid infinite loops. Pass `--overlap` < `--chunk-size` to `ingest_corpus.py` (or `--chunk-size 1200 --overlap 200`, the defaults). `--overlap 0` is always safe.

## `chunk: unsupported mode '<x>'`

Only `'character'` and `'markdown'` are supported in 0.4.0. Token-based / code-aware splitters are roadmap items. Pass `--chunk-mode markdown` (default) or `--chunk-mode character` to `setup_kb.py`.

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
python setup_kb.py --workspace <workspace> --model-path <new-model> --embedding-dim <new-dim>
python ingest_corpus.py --workspace <workspace> --corpus <docs/>
```

Don't try to `ALTER TABLE` — sqlite-vec doesn't support it. Rebuild is cheap for corpora under 100k chunks.

## "DataFusion planner drops my embedding column" on a custom INSERT

Use a `SELECT ... FROM (SELECT ... AS t)` wrapper rather than `VALUES (...)`. DataFusion's INSERT planner propagates the target schema into immediate-child VALUES clauses and validates row width, which eats any computed column added as a projection. The SELECT wrapper keeps the subquery schema in scope. This is exactly what the skill's `ingest.yaml` and `ingest_chunked.yaml` templates do — copy their shape if you write your own.
