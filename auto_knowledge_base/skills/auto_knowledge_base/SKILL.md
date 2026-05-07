---
name: auto_knowledge_base
description: Autonomously build and query a knowledge base from a directory of documents (markdown, text, code, PDFs-as-text) using the Skardi data platform. The skill handles the full pipeline end-to-end with no human-in-the-loop — prerequisite detection, embedding model download, schema creation, server-side chunking via Skardi 0.4.0's chunk() UDF, ingestion, and hybrid (vector + full-text) retrieval. Use this skill whenever the user asks to build a RAG system, index a corpus for search, create a local knowledge base, make documents queryable by an agent, answer questions over a document set, turn a folder of files into something the agent can retrieve from, or set up vector + full-text search over text — even if they don't say "Skardi" or "RAG" explicitly. Also trigger this skill when the user mentions embedding documents, chunking text for retrieval, grounding LLM answers in a document set, or building an agent-native wiki.
---

# auto_knowledge_base — agent-autonomous KB construction over Skardi

Your job: turn a directory of documents into a working knowledge base the agent (you) can query, with zero human intervention. Default stack is **Skardi CLI (≥ 0.4.0) + local SQLite + sqlite-vec + FTS5 + local candle embeddings**, because that path has no server, no Docker, no API keys, and the same `skardi grep` verb serves vector, keyword, and hybrid search. Other backends (Postgres+pgvector, Lance) are supported as overrides — see [references/backends.md](references/backends.md).

> **Skardi 0.4.0+ required.** The skill chunks corpus text inside SQL via the `chunk()` UDF that landed in 0.4.0; an older binary fails late at INSERT time with an opaque "Invalid function 'chunk'" error. `setup_kb.py` checks this for you and prints a clear install command if the version is too old.

## Two orthogonal choices

A knowledge base is defined by two decisions. Make them independently — the wrong combination is usually workable, the *right* combination depends on the corpus and the deployment.

### 1. Storage backend — where the rows and indices live

Read top-to-bottom and stop at the first row that matches.

| Situation | Backend | Why |
|-----------|---------|-----|
| Local corpus, single agent, no pre-existing infra (**default when unspecified**) | **SQLite + sqlite-vec + FTS5** | Zero infra. `skardi grep` serves hybrid RRF out of the box. Matches `demo/rag/cli/`. |
| User explicitly says "run on a server" / multiple agents / ACID writes | **Postgres + pgvector** via `skardi-server` | Concurrency, HNSW indices, standard backup story. See [references/backends.md](references/backends.md). |
| Millions of chunks, columnar analytics, versioned snapshots | **Lance** | Columnar + HNSW scales to TBs; use Skardi *jobs* (not pipelines) for atomic bulk writes. See [references/backends.md](references/backends.md). |
| User says "just keyword search, no embeddings" | **SQLite + FTS5 only** | Skip `sqlite-vec` and the embedding UDF entirely. `skardi fts` still works. |

### 2. Embedding function — how text becomes a vector

Skardi exposes three embedding UDFs. They're interchangeable in the SQL — the one you pick is a deployment choice (local vs. remote, full-precision vs. quantised, which provider's API).

| UDF | Signature | When to reach for it |
|-----|-----------|---------------------|
| `candle(model_dir, text)` | local HuggingFace SafeTensors (BERT / RoBERTa / e5 / bge / nomic-family) | First choice for local, CPU-friendly, reproducible. Pick a model matching the corpus language/domain — e.g. `bge-small-en` for English general text, `e5-large-multilingual` for non-English, `nomic-embed-text` for longer chunks. The skill's default is `bge-small-en-v1.5` (384-d, ~130 MB) *only because it fits almost any laptop*; any HF model with `model.safetensors` + `config.json` + `tokenizer.json` works. |
| `gguf(model_dir, text)` | local quantised llama.cpp-format model | Pick this when RAM/disk is tight, when you want to embed a GGUF-quantised version of a bigger model (e.g. `bge-large-en-v1.5-q4_k_m.gguf`), or when the desired model ships primarily as GGUF. Inference is often faster than candle at the same accuracy, with a smaller footprint. Requires Skardi built with `--features gguf`. |
| `remote_embed(provider, model, text)` | API-hosted | Use when there's no local compute budget, you want a very large embedding model, or the corpus is in a language a hosted model handles better. Providers Skardi supports out-of-the-box include **OpenAI** (`text-embedding-3-small`, `-3-large`), **Voyage** (`voyage-3`, `voyage-code-3`), **Gemini** (`text-embedding-004`), and **Mistral** (`mistral-embed`). Each needs the matching API-key env var and Skardi built with `--features remote-embed`. |

**Don't pick an embedding model without thinking about the corpus.** If the user has mostly code, `voyage-code-3` or `nomic-embed-code` will outperform a general model. If the user has long documents (>512 tokens per chunk), pick a model with a larger context (e.g. `nomic-embed-text` handles 8k). If the user is cost-sensitive and offline, `gguf` with a quantised bge-large is often the sweet spot. If in doubt, ask the user which model they want rather than guessing — the wrong embedding is painful to recover from because you have to rebuild the whole KB.

Whatever you pick, set `--embedding-dim` on `setup_kb.py` to match the model's output dimension (bge-small = 384, bge-base = 768, bge-large = 1024, OpenAI `-3-small` = 1536, Voyage `voyage-3` = 1024, etc.). The `vec0` table bakes the dim at create time, so a mismatch means rebuild.

If the user gives no signal about a backend or embedding, use **SQLite + sqlite-vec + FTS5** with **`candle` + `bge-small-en-v1.5`** — it's the lowest-friction path that works on any laptop. But make that choice explicitly, explain it in one sentence, and move on.

## The end-to-end flow (default path)

Four steps. Steps 1–2 are one-time setup per corpus. Steps 3–4 are the per-question loop.

```
1. python SKILL_DIR/scripts/setup_kb.py    --workspace ./kb     # optionally --model-path <abs-path>, --embedding-udf gguf, --chunk-mode character, ...
2. python SKILL_DIR/scripts/ingest_corpus.py --workspace ./kb --corpus ./path/to/docs
3. SKARDICONFIG=./kb skardi grep "your question" --limit=5    # (and/or `vec` / `fts`, possibly multiple)
4. Synthesise a grounded, cited answer from the retrieved rows (see Step 4 for structure).
```

Read `SKILL_DIR` as the absolute path to the directory containing this SKILL.md. Resolve it once from the path you got when this skill was invoked and reuse.

The big change vs. older versions of this skill: there is no separate chunking step. `chunk()` is a Skardi UDF — chunking happens inside the same SQL statement that embeds and inserts, so the embedding model loads once for the whole corpus instead of once per file. Two scripts now do the work the previous three did.

### Step 1 — Initialize the workspace

`scripts/setup_kb.py` is idempotent. It:

1. Verifies `skardi --version >= 0.4.0` is on PATH (fails with a clear install hint if not — chunk() is a hard requirement).
2. Ensures `sqlite_vec` and `huggingface_hub` Python packages are importable (installs them with `pip install --user` if missing).
3. Resolves the embedding model — either uses a pre-existing path passed via `--model-path`, or downloads `BAAI/bge-small-en-v1.5` (≈130 MB of safetensors + tokenizer) into `<workspace>/models/bge-small-en-v1.5/`.
4. Renders `ctx.yaml`, `semantics.yaml`, `aliases.yaml`, and `pipelines/{ingest,ingest_chunked,search_vector,search_fulltext,search_hybrid}.yaml` from the `.tpl` files in `SKILL_DIR/assets/`, substituting **absolute** paths for the DB and the embedding model. Absolute paths matter because `skardi` resolves `candle()` model paths relative to its CWD — hard-coding absolutes removes a common foot-gun.
5. Writes a `.embedding.txt` breadcrumb so `ingest_corpus.py` can rebuild the same embedding call without re-parsing YAML.
6. Creates `<workspace>/kb.db` with three tables joined by `AFTER INSERT`/`UPDATE`/`DELETE` triggers:
   - `documents(id, source, chunk_idx, content, embedding BLOB)` — canonical row store
   - `documents_fts` — FTS5 mirror (content indexed, metadata UNINDEXED)
   - `documents_vec` — `vec0` mirror, `float[384]` (swap dim if you change the embedding model)

Typical invocation:

```bash
# Default: auto-download BAAI/bge-small-en-v1.5 into <workspace>/models/.
python SKILL_DIR/scripts/setup_kb.py --workspace ./kb

# If a compatible model is already on disk, point at it with an absolute path:
python SKILL_DIR/scripts/setup_kb.py --workspace ./kb \
  --model-path /abs/path/to/bge-small-en-v1.5

# For unstructured prose, switch the chunk mode (default is markdown):
python SKILL_DIR/scripts/setup_kb.py --workspace ./kb --chunk-mode character
```

The script prints the final `SKARDICONFIG` to export.

### Step 2 — Ingest the corpus (one shot, server-side chunking)

`scripts/ingest_corpus.py` walks the corpus directory, builds an NDJSON manifest (`{doc_id, source, content}`, one row per file), and runs **one** `skardi query` whose INSERT does the whole chunk → embed → write loop:

```sql
INSERT INTO kb.main.documents (id, source, chunk_idx, content, embedding)
SELECT id, source, chunk_idx, content, vec_to_binary(candle('<abs-model-path>', content))
FROM (
  SELECT
    CAST(doc_id AS BIGINT) * 1000 + chunk_idx AS id,
    source, chunk_idx, content
  FROM (
    SELECT
      doc_id, source,
      ROW_NUMBER() OVER (PARTITION BY doc_id ORDER BY 1) - 1 AS chunk_idx,
      chunk_text                                              AS content
    FROM (
      SELECT
        CAST(doc_id AS BIGINT) AS doc_id, source,
        UNNEST(chunk('markdown', content, 1200, 200)) AS chunk_text
      FROM './kb/manifest.json'
    ) c
  ) c2
) AS t
```

The `chunk()` UDF (Skardi 0.4.0+) replaces the old Python chunker entirely. Two splitter modes:

- `'markdown'` — prefers heading / paragraph / code-block boundaries. Right for `.md` and structured `.txt`.
- `'character'` — generic recursive splitter (paragraph → sentence → word → grapheme). Right for unstructured prose.

`UNNEST(chunk(...))` expands the splitter's `List<Utf8>` output into one row per chunk; the embedding UDF then runs over each chunk's `content`. The `AFTER INSERT` trigger fans every new row to `documents_fts` and `documents_vec` atomically — so the corpus becomes searchable both ways in one pass.

Stable ids: `doc_id` is a 53-bit blake2b hash of the relative path, and per-chunk id is `doc_id * 1000 + chunk_idx`. Re-ingesting the same file produces the same ids, which means a second run rejects with `UNIQUE constraint failed`. Fix by either re-running `setup_kb.py --force` (rebuild from scratch) or `DELETE FROM kb.main.documents WHERE source = '<path>'` first (incremental).

> **Why one bulk INSERT instead of one per file?** The embedding model loads once per `skardi` process. Running per-file would reload it every time. For a 100-file corpus that's a 100x cold-start tax — most of which is wasted because the splitter and embedder both warm up after the first batch.

### Step 3 — Retrieve

```bash
export SKARDICONFIG=./kb

# Hybrid (default — usually best)
skardi grep "Who is the White Rabbit?" --limit=5

# Vector-only — good for paraphrase / conceptual queries
skardi vec "a creature that checks its pocket watch" --limit=5

# Full-text-only — good for named entities / exact strings
skardi fts '"Rabbit-Hole"' --limit=5
```

Each returned row has `id`, `source`, `chunk_idx`, `content`, and a score column. Pass these into Step 4.

> **FTS5 syntax gotcha:** FTS5 treats `?`, `"`, `+`, `-`, `~`, `^`, and `()` as operators. A bare `"What is X?"` will raise `fts5: syntax error`. Either strip punctuation for the `--text_query`, phrase-quote the whole thing (`'"what is x"'`), or pass plain words. The hybrid alias uses the raw query for vector side and the same string for FTS side, so a bad punctuation in one query will only degrade the FTS half — the vector half still works, so don't panic if you see an FTS error; search still functions.

### Step 4 — Synthesise the answer

Retrieval gives you candidate passages. The answer is your synthesis of them, and this is where the retrieval loop earns its keep — a thin answer leaves value on the floor even when the top chunk is correct.

**Decide how many queries to run before answering.** Don't default to "one query then answer" for anything non-trivial.

- **Single-fact questions** (one named entity, one specific detail): one hybrid query at `--limit=5` is usually enough. If the top-1 score looks strong (rrf_score well above the rest, or vector distance visibly lower than others), stop there.
- **Multi-part questions** ("who is X, and quote an exchange", "compare A and B", "describe X and explain why Y"): run a query *per sub-question*. Combining everything into one search dilutes the embedding and the RRF scores collapse. Two or three well-scoped queries nearly always beat one kitchen-sink query.
- **Definitional / relational questions** ("who is Bessie?"): start with a named-entity FTS pass (`skardi fts "Bessie"`) to surface every mention, then do a conceptual vec pass (`skardi vec "Bessie as a caretaker character"`) to pick up paraphrases. Union the hits.
- **When top-k scores are flat or the top chunk feels off-topic**, widen the lens: try a second phrasing, or fall back from `grep` to `vec` (conceptual) or `fts` (lexical) depending on whether the query was abstract or concrete.

**Answer structure.** Ground every claim in a retrieved chunk, and cite it. For multi-claim answers, end with a citation table so the user can audit without re-reading.

```markdown
# [One-line answer title]

## [Sub-claim 1]
[1–3 sentences synthesising what the chunks say.]
> [Verbatim or near-verbatim quote from a retrieved chunk.]

Citation: `<source>`, chunk <chunk_idx>.

## [Sub-claim 2]
...

## Citations

| Claim | Source | Chunk |
|---|---|---|
| Short restatement of claim 1 | `source_file.md` | 3 |
| Short restatement of claim 2 | `source_file.md` | 7 |
```

**Grounding principle: don't synthesise past the retrieval.** If a fact isn't in a chunk you retrieved, it doesn't belong in the answer — even if you "know" it from pretraining. The reason is concrete: users trust the answer because it was retrieved from *their* corpus. Side observations that feel like padding ("she is paired with Miss Abbot; less obnoxious than Abbot") are fine only if the chunks you're citing actually say that, *and* they add to the user's understanding of the specific question asked. If a sentence doesn't pass both tests, delete it.

**If the corpus genuinely doesn't answer the question**, say so plainly. Report what you searched for and what you found *instead* — don't invent a plausible-sounding quote from memory. Agents that hallucinate citations destroy trust in the entire retrieval system; agents that honestly flag absence preserve it. Cite the closest passage that *is* in the corpus to show the search actually ran.

## Catalog semantics

`setup_kb.py` also renders a `semantics.yaml` next to `ctx.yaml`. Skardi auto-discovers it (see `docs/semantics.md` in the source tree) and surfaces the table + column descriptions in `skardi query --schema --all` output. That's the channel through which an agent peering into an unfamiliar workspace sees what `documents.embedding` is for or how `chunk_idx` is assigned. The file is regenerated on every `setup_kb.py` run — if the user wants hand-curated descriptions, drop a second `kind: semantics` file (any name) into a `semantics/` directory next to `ctx.yaml`; both are merged at load time.

## Customising the defaults

Most agents won't need to. But when they do:

- **Different embedding model.** Swap `--model-path` and set `--embedding-dim` on `setup_kb.py` to match the model's output dim (bge-small is 384, bge-base is 768, OpenAI `text-embedding-3-small` is 1536). The script rewrites the `float[N]` clause of the `vec0` table to match. If you change models *after* the DB exists, rebuild from scratch — dim mismatches are unrecoverable.
- **Different chunk mode.** Pass `--chunk-mode character` to `setup_kb.py` for unstructured prose where heading-aware splitting buys nothing.
- **Different chunk size / overlap.** Pass `--chunk-size 800 --overlap 100` (or whatever) to `ingest_corpus.py`. The constraint is `overlap < chunk_size`; both must be positive integers. There's no "right" answer — 1200/200 (the default) suits most prose; tighter chunks (500/50) help when the corpus has dense factual passages and queries target single sentences.
- **Custom chunker.** If `chunk('markdown' | 'character', ...)` doesn't fit your corpus (token-based splitting, code-aware splitting, semantic chunking), build your own NDJSON file with fields `id`, `source`, `chunk_idx`, `content` and skip `ingest_corpus.py`. Then run a single INSERT through the simpler `ingest` pipeline (still rendered by `setup_kb.py`, used as `skardi ingest <id> <source> <chunk_idx> <content>`) — Skardi does not care how the chunks were produced.
- **GGUF quantised model.** Pass `--embedding-udf gguf --model-path /abs/path/to/model.gguf` (or a directory containing one). Skardi must be built with `--features gguf`. Great for squeezing a larger embedding model onto a modest machine — often faster than the equivalent candle model at similar accuracy.
- **Remote embeddings (no local compute).** Pass `--embedding-udf remote_embed` and `--embedding-args` naming the provider and model. Examples: `"'openai','text-embedding-3-small'"`, `"'voyage','voyage-3'"`, `"'gemini','text-embedding-004'"`, `"'mistral','mistral-embed'"`. Each needs the matching API-key env var (`OPENAI_API_KEY`, `VOYAGE_API_KEY`, `GOOGLE_API_KEY`, `MISTRAL_API_KEY`) and Skardi built with `--features remote-embed`.
- **Postgres backend.** Use [references/backends.md](references/backends.md) — the same pipeline shape, but `sqlite_knn`/`sqlite_fts`/`vec_to_binary` become `pg_knn`/`pg_fts` and the embedding writes directly as `vector(384)` without binary packing. Requires Postgres + pgvector running somewhere.

## Verifying the KB is healthy

After ingest, run this sanity check before you start answering questions:

```bash
SKARDICONFIG=./kb skardi query --sql "
SELECT COUNT(*) AS rows,
       COUNT(DISTINCT source) AS files,
       SUM(LENGTH(content)) AS total_chars
FROM kb.main.documents"
```

If `rows` is 0 or far less than what you'd expect from the corpus, the embedding UDF probably isn't available in this Skardi build. Verify by running it directly — `skardi query --sql "SELECT candle('...', 'hi')"` (or `gguf(...)` / `remote_embed(...)` depending on what you chose) will error if the matching feature wasn't compiled in. Rebuild Skardi with the right `--features` flag, or re-run `setup_kb.py` with a different `--embedding-udf`.

Then do one test retrieval and eyeball the output — scores should range across at least two orders of magnitude, and the top chunk should be visibly related to the query. If every chunk has the same score, the trigger probably didn't fire (check `SELECT COUNT(*) FROM documents_vec` — it should equal `documents`).

## Troubleshooting

If something goes wrong, read [references/troubleshooting.md](references/troubleshooting.md) — it covers: missing `sqlite_vec` extension path, candle / chunk feature not compiled, FTS5 syntax errors, embedding dim mismatch, empty result sets, and how to rebuild a corrupted workspace. Don't speculate — look up the symptom in that file and apply the prescribed fix.

## When a single KB isn't enough

If the agent needs to maintain many KBs (one per project, per user, per tenant), just pick a different `--workspace` per KB and switch `SKARDICONFIG` between them. Each workspace is self-contained: one SQLite file, one ctx, one pipelines dir. Nothing global, nothing to clean up except the directory.

If the agent needs to keep a KB *updated* as new files land (incremental ingest), `DELETE FROM documents WHERE source IN (...)` for the changed files first, then rerun `ingest_corpus.py` — it will re-walk the corpus and re-emit stable ids, so only the changed files actually move. The triggers handle the FTS + vec mirror updates automatically.

## Pattern reference

For the exact SQL and YAML this skill generates, see [references/pipeline_patterns.md](references/pipeline_patterns.md). That file is the place to look when you need to modify a pipeline rather than regenerate the whole workspace — e.g., adding a metadata filter to the search pipeline, or changing the RRF k-constant.
