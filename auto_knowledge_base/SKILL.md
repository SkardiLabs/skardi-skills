---
name: auto_knowledge_base
description: Autonomously build and query a knowledge base from a directory of documents (markdown, text, code, PDFs-as-text) using the Skardi data platform. The skill handles the full pipeline end-to-end with no human-in-the-loop — prerequisite detection, embedding model download, schema creation, chunking, ingestion, and hybrid (vector + full-text) retrieval. Use this skill whenever the user asks to build a RAG system, index a corpus for search, create a local knowledge base, make documents queryable by an agent, answer questions over a document set, turn a folder of files into something the agent can retrieve from, or set up vector + full-text search over text — even if they don't say "Skardi" or "RAG" explicitly. Also trigger this skill when the user mentions embedding documents, chunking text for retrieval, grounding LLM answers in a document set, or building an agent-native wiki.
---

# auto_knowledge_base — agent-autonomous KB construction over Skardi

Your job: turn a directory of documents into a working knowledge base the agent (you) can query, with zero human intervention. Default stack is **Skardi CLI + local SQLite + sqlite-vec + FTS5 + local candle embeddings**, because that path has no server, no Docker, no API keys, and the same `skardi grep` verb serves vector, keyword, and hybrid search. Other backends (Postgres+pgvector, Lance) are supported as overrides — see [references/backends.md](references/backends.md).

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

Five steps. Steps 1–3 are one-time setup per corpus. Steps 4–5 are the per-question loop.

```
1. python SKILL_DIR/scripts/setup_kb.py --workspace ./kb     # optionally --model-path <abs-path>, --embedding-udf gguf, etc.
2. python SKILL_DIR/scripts/chunk_corpus.py --corpus ./path/to/docs --out ./kb/chunks.json
3. python SKILL_DIR/scripts/bulk_ingest.py  --workspace ./kb --chunks ./kb/chunks.json
4. SKARDICONFIG=./kb skardi grep "your question" --limit=5   # (and/or `vec` / `fts`, possibly multiple)
5. Synthesise a grounded, cited answer from the retrieved rows (see Step 5 for structure).
```

Read `SKILL_DIR` as the absolute path to the directory containing this SKILL.md. Resolve it once from the path you got when this skill was invoked and reuse.

The chunks file in step 2 is **NDJSON** (newline-delimited JSON objects, one per chunk) with a `.json` extension — that's the only extension DataFusion's JSON reader recognises, and JSON survives embedded newlines inside chunk content where CSV does not. Don't rename it to `.ndjson`.

Steps 4 and 5 are the retrieval loop the agent runs for each question. Step 5 is where most of the answer quality comes from — don't skip it.

### Step 1 — Initialize the workspace

`scripts/setup_kb.py` is idempotent. It:

1. Checks `skardi --version` is on PATH (fails with a clear install hint if not).
2. Ensures `sqlite_vec` and `huggingface_hub` Python packages are importable (installs them with `pip install --user` if missing).
3. Resolves the embedding model — either uses a pre-existing path passed via `--model-path`, or downloads `BAAI/bge-small-en-v1.5` (≈130 MB of safetensors + tokenizer) into `<workspace>/models/bge-small-en-v1.5/`.
4. Creates `<workspace>/kb.db` with three tables joined by `AFTER INSERT`/`UPDATE`/`DELETE` triggers:
   - `documents(id, source, chunk_idx, content, embedding BLOB)` — canonical row store
   - `documents_fts` — FTS5 mirror (content indexed, metadata UNINDEXED)
   - `documents_vec` — `vec0` mirror, `float[384]` (swap dim if you change the embedding model)
5. Renders `ctx.yaml`, `aliases.yaml`, and `pipelines/{ingest,search_vector,search_fulltext,search_hybrid}.yaml` from the `.tpl` files in `SKILL_DIR/assets/`, substituting **absolute** paths for the DB and the embedding model. Absolute paths matter because `skardi` resolves `candle()` model paths relative to its CWD — hard-coding absolutes removes a common foot-gun.

Typical invocation:

```bash
# Default: auto-download BAAI/bge-small-en-v1.5 into <workspace>/models/.
python SKILL_DIR/scripts/setup_kb.py --workspace ./kb

# If a compatible model is already on disk, point at it with an absolute path
# to skip the download (any HuggingFace BERT-family model dir with
# model.safetensors + config.json + tokenizer.json works):
python SKILL_DIR/scripts/setup_kb.py --workspace ./kb \
  --model-path /abs/path/to/bge-small-en-v1.5
```

The script prints the final `SKARDICONFIG` the agent should export.

### Step 2 — Chunk the corpus

`scripts/chunk_corpus.py` walks a directory (respecting `--include "*.md,*.txt,*.rst"` globs) and produces an NDJSON file with fields `id`, `source`, `chunk_idx`, `content`. The default chunker is markdown-aware:

- Splits on `## ` and `### ` headings first (keeps semantic units together).
- Within each heading section, packs paragraphs into chunks of ≤ `--max-chars` (default 1200) with `--overlap` char overlap (default 200).
- Strips front-matter, trims trailing whitespace, and preserves the heading trail as a prefix on each chunk so dense-retrieval scores aren't throwing away section titles.

For plain text it falls back to paragraph-packing with the same char budget. Non-text files are skipped.

Output uses NDJSON (not CSV) because DataFusion's CSV reader tokenises by line and mis-splits any cell containing embedded newlines — and real-world chunks routinely contain paragraph breaks. JSON escapes `\n` inside strings, so multi-paragraph chunks round-trip cleanly. A single SQL statement then embeds + inserts every chunk in one shot (Step 3).

### Step 3 — Bulk-ingest via one SQL statement

`scripts/bulk_ingest.py` runs one `skardi query` against the workspace ctx:

```sql
INSERT INTO kb.main.documents (id, source, chunk_idx, content, embedding)
SELECT CAST(id AS BIGINT), source, CAST(chunk_idx AS BIGINT), content,
       vec_to_binary(candle('<abs-model-path>', content))
FROM './kb/chunks.json'
```

A single statement embeds every row and commits; the `AFTER INSERT` trigger fans each row to `documents_fts` and `documents_vec` atomically, so the corpus becomes queryable both ways in one pass. For a 300-chunk corpus this runs in a handful of seconds; for 50k chunks, batch it in groups of ~5000 — the script handles the batching automatically via `--batch-size`.

> **Why not call `skardi run ingest` in a loop?** Correct but slow — each call re-initialises the DataFusion context and reloads the embedding model. The single-statement path over the NDJSON file loads the model once.

### Step 4 — Retrieve

```bash
export SKARDICONFIG=./kb

# Hybrid (default — usually best)
skardi grep "Who is the White Rabbit?" --limit=5

# Vector-only — good for paraphrase / conceptual queries
skardi vec "a creature that checks its pocket watch" --limit=5

# Full-text-only — good for named entities / exact strings
skardi fts '"Rabbit-Hole"' --limit=5
```

Each returned row has `id`, `source`, `chunk_idx`, `content`, and a score column. Pass these into Step 5.

> **FTS5 syntax gotcha:** FTS5 treats `?`, `"`, `+`, `-`, `~`, `^`, and `()` as operators. A bare `"What is X?"` will raise `fts5: syntax error`. Either strip punctuation for the `--text_query`, phrase-quote the whole thing (`'"what is x"'`), or pass plain words. The hybrid alias uses the raw query for vector side and the same string for FTS side, so a bad punctuation in one query will only degrade the FTS half — the vector half still works, so don't panic if you see an FTS error; search still functions.

### Step 5 — Synthesise the answer

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

Citation: `<source>`, chunk <chunk_idx> (section: *<heading if inferable>*).

## [Sub-claim 2]
...

## Citations

| Claim | Source | Section |
|---|---|---|
| Short restatement of claim 1 | `source_file.md` | Chapter I |
| Short restatement of claim 2 | `source_file.md` | Chapter III |
```

**Grounding principle: don't synthesise past the retrieval.** If a fact isn't in a chunk you retrieved, it doesn't belong in the answer — even if you "know" it from pretraining. The reason is concrete: users trust the answer because it was retrieved from *their* corpus. Side observations that feel like padding ("she is paired with Miss Abbot; less obnoxious than Abbot") are fine only if the chunks you're citing actually say that, *and* they add to the user's understanding of the specific question asked. If a sentence doesn't pass both tests, delete it.

**If the corpus genuinely doesn't answer the question**, say so plainly. Report what you searched for and what you found *instead* — don't invent a plausible-sounding quote from memory. Agents that hallucinate citations destroy trust in the entire retrieval system; agents that honestly flag absence preserve it. Cite the closest passage that *is* in the corpus to show the search actually ran.

## Customising the defaults

Most agents won't need to. But when they do:

- **Different embedding model.** Swap `--model-path` and set `--embedding-dim` on `setup_kb.py` to match the model's output dim (bge-small is 384, bge-base is 768, OpenAI `text-embedding-3-small` is 1536). The script rewrites the `float[N]` clause of the `vec0` table to match. If you change models *after* the DB exists, rebuild from scratch — dim mismatches are unrecoverable.
- **Different chunker.** Write your own NDJSON file (one object per line, fields `id`, `source`, `chunk_idx`, `content`) and skip `chunk_corpus.py`. Skardi doesn't care how the text was chunked — any NDJSON with those fields and a `.json` extension works with `bulk_ingest.py`.
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

If `rows` is 0 or far less than the number of objects in `kb/chunks.json`, the embedding UDF probably isn't available in this Skardi build. Verify by running it directly — `skardi query --sql "SELECT candle('...', 'hi')"` (or `gguf(...)` / `remote_embed(...)` depending on what you chose) will error if the matching feature wasn't compiled in. Rebuild Skardi with the right `--features` flag, or re-run `setup_kb.py` with a different `--embedding-udf`.

Then do one test retrieval and eyeball the output — scores should range across at least two orders of magnitude, and the top chunk should be visibly related to the query. If every chunk has the same score, the trigger probably didn't fire (check `SELECT COUNT(*) FROM documents_vec` — it should equal `documents`).

## Troubleshooting

If something goes wrong, read [references/troubleshooting.md](references/troubleshooting.md) — it covers: missing `sqlite_vec` extension path, candle feature not compiled, FTS5 syntax errors, embedding dim mismatch, empty result sets, and how to rebuild a corrupted workspace. Don't speculate — look up the symptom in that file and apply the prescribed fix.

## When a single KB isn't enough

If the agent needs to maintain many KBs (one per project, per user, per tenant), just pick a different `--workspace` per KB and switch `SKARDICONFIG` between them. Each workspace is self-contained: one SQLite file, one ctx, one pipelines dir. Nothing global, nothing to clean up except the directory.

If the agent needs to keep a KB *updated* as new files land (incremental ingest), re-run steps 2–3 on just the new files (append rows; the `id` column should remain unique — include the file path + chunk index in the id derivation). The `documents_vec` trigger handles inserts fine; if you need to re-embed a changed file, `DELETE FROM documents WHERE source = '<path>'` first, then re-ingest.

## Pattern reference

For the exact SQL and YAML this skill generates, see [references/pipeline_patterns.md](references/pipeline_patterns.md). That file is the place to look when you need to modify a pipeline rather than regenerate the whole workspace — e.g., adding a metadata filter to the search pipeline, or changing the RRF k-constant.
