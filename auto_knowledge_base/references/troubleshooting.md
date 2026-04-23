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

## `error: Unknown function 'candle'` (or `gguf`, or `remote_embed`) when running a search or ingest

Skardi was built without the matching feature flag. Each embedding UDF is gated behind a cargo feature:

- `candle(...)` → `--features candle`
- `gguf(...)` → `--features gguf`
- `remote_embed(...)` → `--features remote-embed`

Three options, in order of preference:

1. Rebuild Skardi with the feature you need (e.g. `cargo install --locked --git https://github.com/SkardiLabs/skardi --branch main skardi-cli --features candle,gguf,remote-embed` for all three).
2. Re-run `setup_kb.py` with a different `--embedding-udf` whose feature *is* available in your build. `--embedding-udf remote_embed` requires no local compute (pass `--embedding-args` with the provider/model pair — e.g. `"'openai','text-embedding-3-small'"`, `"'voyage','voyage-3'"`, `"'gemini','text-embedding-004'"`, `"'mistral','mistral-embed'"` — and the matching API-key env var).
3. Embed externally (Python + sentence-transformers / a vendor SDK, etc.) and skip Skardi for the write path — feed embeddings directly into `documents.embedding` as packed f32 BLOBs. `skardi grep` still works for reads.

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

## Empty result set from `skardi grep`

Check in order:

1. `skardi query --sql "SELECT COUNT(*) FROM kb.main.documents"` — if 0, ingest didn't run or failed silently.
2. `skardi query --sql "SELECT COUNT(*) FROM kb.main.documents_vec"` — if less than `documents`, trigger mismatch (see above).
3. Embedding dim mismatch: if you changed `--embedding-dim` after the DB was created, the vec0 table was built with the old dim and new rows will error. Rebuild with `--force`.
4. Model path broken: `skardi query --sql "SELECT candle('<abs-path>', 'hello world')"` should return a float array. If it errors, fix the absolute path in `pipelines/*.yaml`.

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
python bulk_ingest.py --workspace <workspace> --chunks <workspace>/chunks.csv
```

Don't try to `ALTER TABLE` — sqlite-vec doesn't support it. Rebuild is cheap for corpora under 100k chunks.

## Large corpus (>100k chunks) is slow to ingest

`bulk_ingest.py --batch-size 5000` is the default. Try `--batch-size 2000` for lower memory or `10000` for faster commit amortization. If it's still slow, the bottleneck is the candle forward pass — consider:

- A smaller model (`BAAI/bge-micro-v2`, 384-d, ~50% faster than bge-small).
- A quantized gguf model via `--embedding-udf gguf` (requires Skardi built with `--features gguf`).
- Remote embeddings (`--embedding-udf remote_embed`) if your network is fast and your budget tolerates per-token cost.

## "DataFusion planner drops my embedding column" on a custom INSERT

Use a `SELECT ... FROM (SELECT ... AS t)` wrapper rather than `VALUES (...)`. DataFusion's INSERT planner propagates the target schema into immediate-child VALUES clauses and validates row width, which eats any computed column added as a projection. The SELECT wrapper keeps the subquery schema in scope. This is exactly what the skill's `ingest.yaml` template does — copy its shape if you write your own.
