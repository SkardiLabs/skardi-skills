---
name: auto_context
description: 'Turn a folder of documents, a table you already have, or documents still inside a service (a wiki, cloud docs, a mailbox) into governed searchable context an agent can query — hybrid search (vector + full-text) served over HTTP by skardi-server. Three raw-material entries, one flow: ingest a folder; ingest an existing table (SQLite read directly and read-only, any other datastore piped in as NDJSON); or fetch service-resident documents via the fetch-and-land process — list, fetch, reconcile (listed vs fetched vs missing, by name), land in a table, ingest — where your agent writes the per-source fetch code and this skill fixes the flow and the acceptance criteria. Independently of the entry, two storage paths for the index. Default: the skill creates and owns a local SQLite file. Override: point it at a database the user already runs (PostgreSQL+pgvector, MongoDB, or Lance), where the user owns the schema and the skill never creates it. Use this skill whenever the user wants to build a knowledge base, index a corpus or a table for search, make documents queryable by an agent, answer questions over a document set, set up RAG or hybrid search, expose retrieval as a REST endpoint, share retrieval across several agents or processes, plug Skardi into an existing production datastore, or make wiki / cloud-doc / SaaS content searchable. Trigger on phrases like build a RAG system, index my docs, index this table, index the documents in our database, local knowledge base, make this folder searchable, make our wiki searchable, index our Feishu or Notion or Confluence docs, agent-native wiki, search API over my postgres, hybrid search service, expose vector search as HTTP, production RAG on our existing DB, or ground answers in a document set. Requires a running skardi-server built with --features rag (the published skardi-server-rag:0.5.0 image, or a source build from the v0.5.0 tag); the skardi CLI is optional and holds no engine of its own.'
---

# auto_context — build searchable context an agent can query

Your job: turn a corpus into a working retrieval surface, then answer questions from it. One flow; three ways raw material comes in, two places the index can live.

> **A server is not optional.** Since the CLI was reframed as a thin HTTP client (skardi PR #170) it holds no query engine, no data-source registration, and no local execution mode. Every path in this skill starts a `skardi-server`. Do not look for a CLI-only shortcut — there isn't one, and earlier versions of this skill that promised "no server, no Docker" are obsolete.

> **What must be new enough is the server, not the CLI.** Ingest and search each do their work inside one server-side SQL statement: chunking via `chunk()`, embedding via `candle()` / `gguf()` / `remote_embed()`, and writing — one INSERT for ingest, inline embedding for vector and hybrid search. All of those UDFs are registered by `skardi-server`, so the only thing that matters is which features *it* was built with. The image that bundles them is `ghcr.io/skardilabs/skardi/skardi-server-rag:0.5.0` (`--features rag`); the older `skardi-server-embedding` image does not register `chunk()` and breaks the rendered pipelines.
>
> **Pin the tag.** Use `:0.5.0`, the current released version — not `:latest`, which moves under you, and not a build from `main`, which carries unreleased changes you cannot name in a bug report. This skill does not check the `skardi` CLI at all: it is a thin HTTP client, it is never invoked by these scripts, and its version says nothing about the server's UDFs.

## Where the raw material comes from — three entries, one flow

Raw material and index storage are independent choices, and they read confusingly alike because both can involve "a database". This section is about where the source text **comes from**; the next section is about where the **index** lives. In particular: the "bring your own datastore" storage path stores the index in a *new* table created for that purpose — it never reads rows you already have. Reading rows you already have is this section's second entry, and it is read-only.

| Entry | The raw material is | The move |
|---|---|---|
| **A folder** (default) | files on disk | `scripts/ingest_corpus.py --corpus ./docs` (Step 3) |
| **An existing table** | rows that already hold the text — one row = one document | `scripts/ingest_table.py` (Step 3, table form) |
| **Documents still inside a service** | pages / docs / mail behind an API, fetched one at a time | the fetch-and-land process → a staging table → `ingest_table.py`. Flow and acceptance criteria in [references/fetch_and_land.md](references/fetch_and_land.md) |

All three converge on the same POST per document to `/ingest-chunked/execute` — same chunking, same embedding, same index, same search pipelines, same manifest. Only the first step differs, so a new source is never a reason for a second skill or a second index path.

**The existing-table entry.** The contract is three columns: a stable unique key, the text, and (ideally) a real locator — URL or path — for citations. `ingest_table.py` reads a SQLite file directly and strictly read-only; for a table in any other datastore, pipe rows in as NDJSON (`{"key": …, "content": …, "source": …}` per line) from whatever client already talks to it — psql with `json_build_object`, mongoexport, a five-line script. Rows are ingested as-is, with no front-matter stripping: by the time text is in a table, it is what gets indexed. Point it at a table that holds the text itself — a table of titles and hierarchy (what some source packs expose today) is a listing, not a corpus, and routes through the fetch-and-land process instead.

**The fetch-first entry.** Four stages: land the complete listing → fetch each body into a staging table → **reconcile** → ingest the table with `--require-complete`. This skill deliberately ships no per-source fetch code — your agent writes the walk for the source at hand — and the reconciliation is what makes that safe: how many documents the source holds, how many were actually fetched, and which ones are missing, *by name*, shown to the user before ingest. Under-fetching does not error — a missed page still yields a working index and well-cited answers — so the count is the only place a shortfall can surface.

Half of that is enforced and half is not, and the difference matters: `--require-complete` makes the tool refuse to index rows that were listed but never fetched, while the completeness of the *listing* cannot be checked from inside — a document that was never listed leaves no trace anywhere the tool can look. So it rests on the walk following the source's own end-of-listing signal, plus the evidence you report. Never fabricate an expected total to make the check look automatic. Full criteria in [references/fetch_and_land.md](references/fetch_and_land.md).

## The two storage paths

Where the **index** lives. Decide it before rendering the workspace; together with the raw-material entry above it is the whole structure — everything downstream is identical.

| | **Local (default)** | **Bring your own datastore** |
|---|---|---|
| When | The user hands you raw material (a folder, a table, a source to fetch) and wants it searchable. No mention of where the index should live. | The user says "store it in our Postgres / Mongo / Lance", or wants several agents and processes hitting one shared surface. |
| Storage | A SQLite file **this skill creates and owns**, inside the workspace (`<workspace>/kb.db`) — canonical rows plus an FTS5 mirror plus a sqlite-vec `vec0` mirror, kept in sync by triggers. | A table, collection or dataset **the user created**. |
| User supplies | The raw material (folder, table, or source to fetch). Nothing about storage. | Connection string, table name, credentials via env vars, and the schema itself. |
| Flag | nothing (`--backend sqlite` is the default) | `--backend postgres --connection-string ... --table ...` |

**Do not ask which backend the user wants when they have not raised the topic.** The local path exists so that "make this folder searchable" needs exactly one answer from them: where the folder is. Branch to the override path only when the user names existing infrastructure *as the place the index should live*. Naming a database as the place the raw material sits — "index the docs table in our postgres" — picks the table **entry**, not the override **storage**: read the rows out (NDJSON), index them wherever the storage decision says, default local.

## What this skill will and will not do

**Will do.** Render `ctx.yaml` + `semantics.yaml` + the five pipeline YAMLs, start `skardi-server`, ingest raw material over HTTP — a folder of files, rows from an existing table, or documents your agent fetched and landed (the server chunks and embeds inline either way) — and route each question through `/search-hybrid/execute` or its single-signal siblings to a grounded answer.

**Will not do — in a datastore the user owns.** Create databases, create schemas, run `CREATE EXTENSION`, install drivers, or hand out credentials. On the override path the user provides every connection string, every credential, and the schema. If the schema does not exist, print the SQL the user must run in their own session and stop. *Never run schema-creation DDL against a user-supplied connection without the user explicitly asking.* A stray `DROP` can lose hours of someone else's work, and `CREATE EXTENSION` on managed Postgres often needs superuser the agent does not have anyway.

**That limit does not apply to the local path.** There the `.db` file is a workspace artifact this skill created; making tables, triggers and indexes inside it is the skill doing its own job, not touching the user's data. Deleting the workspace is a complete undo.

**Will not write to a table handed over as raw material.** The table entry opens SQLite sources read-only (URI `mode=ro`, enforced by SQLite itself); rows are read and nothing else — no writes, no schema changes, no journal files left behind. Landing fetched documents goes into a staging file the agent creates, never into a table the user owns, and never into `kb.db` (the server owns that file).

**Will not carry per-source fetch code.** How to walk a specific wiki, doc service or mailbox is written by your agent when needed, against the flow and acceptance criteria in [references/fetch_and_land.md](references/fetch_and_land.md). The reconciliation there — listed vs fetched vs missing, by name, shown to the user — is mandatory, not advisory.

For testing **the skill itself** during development, disposable Docker containers are fine — that is not "the user's data".

## What to confirm before starting

Local path — two questions, sometimes fewer:

1. **Where is the raw material — a folder, an existing table, or still inside a service?** For a folder: the path. For a table: which file or datastore, which table, and which columns hold the key and the text. For a service: read [references/fetch_and_land.md](references/fetch_and_land.md) before promising anything. If the user already said, do not ask again.
2. **Embedding backend.** See below. Do not pick silently; it drives cost and the vector dimension.

Override path — the two above, plus:

3. **Backend type**: `postgres` (pgvector + pg_fts), `mongo` (mongo_knn + mongo_fts), or `lance` (lance_knn + lance_fts).
4. **Connection details.** Postgres: connection string plus `PG_USER` / `PG_PASSWORD` in the environment. Mongo: URI, DB and collection, `MONGO_USER` / `MONGO_PASS`. Lance: absolute path or `s3://` URL.
5. **Table / collection / dataset name.** One combined table holding both `content` (indexed for FTS) and `embedding` (vector type), so a single INSERT keeps both signals on the same row.
6. **Is the schema already in place?** If not, print the SQL block from [references/schemas.md](references/schemas.md) and wait for confirmation.

### Choosing the embedding backend

Three UDFs, all returning `List<Float32>`, all slotting into the same pipeline shape. The right one depends on the deployment, not on habit. Skardi's own `docs/embeddings/{candle,gguf,remote}/README.md` is authoritative; the table is the shortcut.

| UDF | Signature | Reach for it when | Server build feature |
|---|---|---|---|
| `candle(model_dir, text)` | local HuggingFace SafeTensors (BERT / RoBERTa / DistilBERT / Jina) | Local, simple deps, general English text. The default for self-hosted when the corpus fits on one box. Common: `bge-small-en-v1.5` (384-d, ~130 MB), `bge-base-en-v1.5` (768-d), `bge-large-en-v1.5` (1024-d), `all-MiniLM-L6-v2` (384-d, tiny), `multilingual-e5-large` (1024-d), `jina-embeddings-v2-base-code` (768-d) for code. | bundled in `--features rag`, or `--features candle` |
| `gguf(model_dir, text)` | local llama.cpp quantised weights | Local, RAM-constrained, or the model only ships as GGUF. Common: `embeddinggemma-300m-qat-Q8_0.gguf` (256-d), `nomic-embed-text-v1.5` GGUF (768-d). **Read the gguf note below before choosing this** — it is the one backend with a build prerequisite. | `--features gguf` |
| `remote_embed(provider, model, text)` | hosted API | No local compute, top-tier quality, willing to pay per call. `('openai','text-embedding-3-small')` 1536-d, `('openai','text-embedding-3-large')` 3072-d, `('voyage','voyage-3')` 1024-d, `('voyage','voyage-code-3')` 1024-d for code, `('gemini','text-embedding-004')` 768-d, `('mistral','mistral-embed')` 1024-d. Each needs its key in the server environment. | `--features remote-embed` |

**Decision rule.** Local-only, or is a hosted API acceptable? If local, choose candle vs gguf on memory budget. Code corpus → `voyage-code-3` or `jina-embeddings-v2-base-code`. Multilingual → `multilingual-e5-large` or `text-embedding-3-large`. Chunks over 512 tokens → `nomic-embed-text`. Otherwise bge on candle. **A 512-token cap interacts with `--chunk-size`, which is measured in characters** — see the note under Step 3 before indexing a code corpus.

**State the choice in one sentence**, then move on — e.g. *"Using `candle('bge-small-en-v1.5')` (384-d, local, ~130 MB) because your corpus is English markdown and you said no remote APIs."*

### The gguf backend needs cmake, and that collides with the local path

Worth knowing before you pick `gguf`, because the collision has no workaround inside this skill:

- **Building a server with `--features gguf` requires cmake and a C++ toolchain.** `gguf` pulls in `llama-cpp-4`, whose build script compiles llama.cpp through cmake. Measured 2026-08-04 on a machine without it: `cargo check -p skardi-server --features gguf` fails at that build script with `is 'cmake' not installed?` and exits 101. `candle` has no such dependency — this is specific to gguf.
- **The published `skardi-server-rag` image already contains gguf** (`--features rag` → `embedding` → `gguf`), so `--runtime docker` sidesteps the build entirely.
- **But `--backend sqlite --runtime docker` is refused** (no Linux sqlite-vec in the image). So *gguf on the local sqlite path means building the server yourself, with cmake installed.* gguf with `--backend postgres` can use the image and needs no local toolchain.

**Model directory layout:** point `--model-path` at a **directory containing exactly one `.gguf` file**, not at the file. The server scans the directory and treats both zero and two-or-more as errors, so a cloned GGUF repo with several quantisations side by side has to be split up first — `setup_context.py` now refuses that up front and names the files it found. Other files in the directory are ignored; the tokenizer comes from the GGUF's own metadata, not from a `tokenizer.json` beside it.

**Runtime status:** the paths above are verified — validation, rendered `gguf('<dir>', content)` SQL, and the failure you get on a server without gguf. Actual gguf *embedding* is still unverified on this machine, because it needs the cmake build.

**The dimension must match the schema** on the override path. If the user already created `vector(1024)`, a 384-d model makes every INSERT fail. Once the table holds rows, changing the dim means rebuilding — there is no in-place fix. On the local path this cannot bite: the skill creates the table at the right dim.

## Where the server runs

Three runtimes, chosen by where the agent lives. Full detail in [references/runtimes.md](references/runtimes.md).

- `--runtime local-process` (default) — `skardi-server` as a host process; prefers a release binary on PATH, falls back to `cargo run`. Laptops, dev, single user.
- `--runtime docker` — the official `skardi-server-rag` image. Ships to teammates without asking them to compile Skardi. **Only for the override path.** `--backend sqlite --runtime docker` is refused, and that is a real incompatibility, not a missing flag: the container would need a Linux build of the sqlite-vec extension, the image does not ship one (verified 2026-08-04), and the host's copy is the wrong platform to mount in. Local storage means local process.
- `--runtime kubernetes` — renders Deployment + Service + ConfigMap into `<workspace>/k8s/`, optionally applies them. Right when the agent already runs in a cluster.

## Chunking and embedding happen on the server

One SQL statement per document does the whole job: `chunk()` splits, the embedding UDF embeds each chunk, and the INSERT commits them together. Nothing is embedded client-side, so a document either lands completely or not at all.

**Chunk mode is fixed at setup time**, baked into the rendered ingest pipeline by `--chunk-mode` (`markdown` by default, `character` for unstructured prose) and recorded in the workspace breadcrumb. It is deliberately *not* a per-request parameter: an earlier version let ingest re-choose it, which silently reverted the user's setup choice to the default. Do not reintroduce that.

## Platforms this runs on

Verified on exactly one platform so far. Native Windows is refused at the entry point of every script; everywhere else the scripts try, and this table says how much confidence that deserves. **Do not upgrade a row to "verified" without actually running the flow on that platform.**

| Platform | Status | What to expect |
|---|---|---|
| macOS Apple Silicon (arm64) | **Verified 2026-08-04** | Local path end to end (create → ingest → all three searches → stop), plus `--runtime docker` for the override path under colima. Needs homebrew Python — system Python cannot load SQLite extensions (next section). |
| Linux x86_64 / arm64 | **Not verified**, no known blocker | The `skardi-server-rag` image is published for `linux/amd64` and `linux/arm64`, so `--runtime docker` should be the smoothest path here. The local path additionally needs a `sqlite-vec` wheel for the arch — pip will tell you; do not assume. |
| macOS Intel (x86_64) | **Not verified**, and no prebuilt `skardi` CLI | The release workflow builds the CLI for `aarch64-apple-darwin`, `x86_64-unknown-linux-gnu` and `aarch64-unknown-linux-gnu` only. `setup_context.py` looks for `skardi` on PATH, so on Intel macs it has to be built from source. |
| Windows (native) | **Not supported — scripts exit 2 with an explanation** | Structural, not a missing flag: `start_new_session=True` raises `ValueError`, `os.kill(pid, 0)` *terminates* a process on Windows instead of probing it, `signal.SIGKILL` does not exist, and no Windows `skardi-server` is published. Use **WSL2** and run the whole flow from the Linux side. Reasoning is in `scripts/_platform.py` — do not replace the gate with per-call shims. |

Two facts that shape every row:

- **There is no `skardi-server` release binary on any platform.** The release workflow ships the `skardi` CLI as a tarball for three targets, and skardi-server *only* as `linux/amd64` + `linux/arm64` container images. So `--runtime local-process` means either a binary you built yourself (`shutil.which("skardi-server")`) or `cargo run` — budget for a Rust build on first start, on Linux as much as on macOS.
- **A rendered workspace is not portable across platforms.** `SQLITE_VEC_PATH` points at a native `.dylib` / `.so`, so a workspace built on a mac cannot be served by a Linux box. Copy the corpus, not the workspace, and re-run `setup_context.py` on the new host.

## Two prerequisites the local path needs — check these first

Both were verified the hard way on 2026-08-04. Neither is optional, and both fail late and confusingly if skipped.

**1. Run the scripts with a Python that can load SQLite extensions.** The local path creates the `vec0` virtual table, which needs the sqlite-vec extension, which needs `sqlite3.Connection.enable_load_extension`. **macOS system Python (`/usr/bin/python3`) does not have it** and setup dies at the schema step. Check before you start, and use the interpreter that passes:

```bash
python3 -c "import sqlite3; print(hasattr(sqlite3.connect(':memory:'), 'enable_load_extension'))"   # must print True
# on macOS this usually means: brew install python  → /opt/homebrew/bin/python3
```

**2. `SQLITE_VEC_PATH` — handled for you, but know what it is.** The server reads that variable to load sqlite-vec (see `options.extensions_env` in the rendered `ctx.yaml`). `setup_context.py` resolves the path, prints it, and records it in `.embedding.txt`; `start_server.py --runtime local-process` re-exports it into the server's environment when your shell does not already carry one. A value you exported yourself always wins.

It matters because an unset value used to fail *silently in the worst direction*: the server started, all five pipelines registered, the step report was green, and every vector query returned nothing. That is now a refusal at startup instead — but only for workspaces rendered by this version. A workspace from an older version has no recorded path, so `start_server.py` stops and asks you to export one:

```bash
python -c 'import sqlite_vec; print(sqlite_vec.loadable_path())'
export SQLITE_VEC_PATH=<that path>
```

Export it in your own shell too if you plan to run `sqlite3` or other tools against `kb.db` directly.

The override path needs neither of these — no local extension is involved. It needs the datastore credentials in the environment instead (`PG_USER` / `PG_PASSWORD`, etc.).

## The end-to-end flow

Both `setup_context.py` and `start_server.py` end with a step table — per-step ok / warn / FAIL, per-step timing, and a verdict line. Read that instead of scrolling back through the log:

```
OK  Server start complete  —  3/3 checks passed  ·  1.0s total
------------------------------------------------------------------------
  [  ok ]  process launch                  2ms
  [  ok ]  server up (/health)            1.0s
  [  ok ]  pipelines registered            1ms
```

- **WARN is not a pass.** It means the step ran but could not be verified — for example `/pipelines` coming back short of the five. Those are exactly the cases that resurface as a failure at ingest or query time, so they are counted apart from the passes and printed with a reason.
- **The table prints on failure too**, with the failing step marked and the denominator intact (`stopped at step 2/3`), so a failed run still shows how far it got and where the time went. A failure in preparatory work before step 1 says so rather than blaming step 1.
- The denominator is per-run, not a constant: local setup is 4 steps, the override path 2 (no extension to resolve, no schema to create), and `--runtime kubernetes` is a single row because that runtime has never been run — inventing per-phase rows for it would be guesswork dressed up as measurement.

### Step 1 — Render the workspace

Local path:

```bash
python scripts/setup_context.py \
  --workspace ./context \
  --embedding-udf candle --model-path /abs/path/bge-small-en-v1.5 --embedding-dim 384
```

Override path adds the storage arguments:

```bash
python scripts/setup_context.py \
  --workspace ./context --backend postgres \
  --connection-string "postgresql://localhost:5432/ragdb?sslmode=disable" --table kb_chunks \
  --embedding-udf candle --model-path /abs/path/bge-small-en-v1.5 --embedding-dim 384
```

Writes `ctx.yaml`, `semantics.yaml`, `pipelines/{ingest,ingest_chunked,search_vector,search_fulltext,search_hybrid}.yaml`, and `.embedding.txt` — the breadcrumb later steps read.

**There is no pre-flight connectivity probe.** There used to be one that ran a local CLI query before anything started; that is impossible now. Server startup is the check — `skardi-server` loads `ctx.yaml` and fails naming the source it could not open.

**Re-running against an existing local workspace stops with an error**, because recreating `kb.db` would drop everything already ingested. It stops in pre-flight — before the model is resolved and before `ctx.yaml` is rewritten — so a refused re-run costs nothing and changes nothing. Pass `--force` only when losing the ingested rows is intended; otherwise reuse the workspace as-is and skip to Step 2.

### Step 2 — Start the server

```bash
python scripts/start_server.py --workspace ./context --port 8080
```

Polls `/health`, then lists `/pipelines` and warns if any of the five are missing. On failure the reason is in `<workspace>/server.log`; on the override path it is usually the connection string, a missing credentials env var, or the schema not existing yet.

> **"All five registered" does not prove ingest works.** Load-time validation is not uniform: the SELECT pipelines get planned, so an embedding UDF the server does not have makes them fail to load — but the INSERT pipelines (`ingest`, `ingest-chunked`) register without being planned and only fail when they actually run. Verified by pointing a healthy server at a model directory holding stub weights: startup was clean, all five pipelines registered, and the first ingest failed on the model load. So treat a clean startup as necessary, not sufficient — the first real ingest is the proof.
>
> **A pipeline that fails to plan aborts startup — you never see a partial server.** Re-measured 2026-08-04 (an earlier note here claimed `search-vector` / `search-hybrid` could fail to load "while `ingest-chunked` reported healthy", which is not what happens). One unplannable SELECT pipeline makes skardi-server log `❌ Failed to load server configuration` and exit 1, even though the other four loaded successfully. There is no `/pipelines` response to inspect in that case, so a missing-pipeline WARN row comes from a server that started and then answered — not from a broken build.
>
> **The error text for a missing embedding UDF names the wrong function.** Reproduced twice, with `gguf` on a server built without it and with a deliberately bogus UDF name: the planner reports `sqlite_knn(table, vector_col, query_vec, k) expects 4 arguments, got 3`. That message is about `sqlite_knn`, but the real cause is the *embedding* call inside it failing to resolve, which leaves the planner counting three arguments. Check the UDF in the rendered `search_vector.yaml` against the features the server was built with before believing anything about `sqlite_knn`.

### Step 3 — Ingest the corpus

```bash
python scripts/ingest_corpus.py --workspace ./context --corpus ./docs
```

One POST per document to `/ingest-chunked/execute`. Progress is journalled, so a re-run resumes instead of duplicating.

**Every matched file is accounted for.** The counts line adds up — `matched: 15  ingestable: 8  skipped: 7` — and each skip prints its reason and the filenames. Four reasons, all verified against a hostile corpus on 2026-08-04:

- **not UTF-8** — latin-1 and UTF-16 files are skipped, not mangled. A UTF-8 BOM is fine (decoded with `utf-8-sig`, so front-matter stripping still works).
- **unreadable** — permission denied, I/O error, symlink loop. One bad file no longer takes the run down with it.
- **no text content** — empty, whitespace-only, or nothing left after front-matter stripping. These used to vanish with no mention, so a corpus of front-matter-only stubs reported `total: 0` and exited 0, which reads as success.
- **too large for one request** — see below.

**A document has to fit in one request, and the server caps that at 2 MiB.** Measured 2026-08-04 against a 0.4.0 server; the limit is axum's default and is unchanged in v0.5.0: a 2000 KB body returns 200, 2100 KB returns `413 Payload Too Large` (skardi-server sets no limit itself, so this is axum's default). Files over the line are reported with their serialised size and skipped. Note "serialised": JSON escaping expands newlines, and non-ASCII becomes `\uXXXX`, so a CJK document is roughly twice its on-disk size on the wire. **There is no client-side splitter** — chunking happens server-side, after the whole document arrives — so an oversized file has to be split into smaller documents by hand.

**`--chunk-size` counts characters; the model caps *tokens*.** From PR #22, which the merge would otherwise have dropped. The bge / e5 / BERT / DistilBERT families max out at 512 tokens and candle does **not** auto-truncate, so an over-cap chunk fails at INSERT with `Embedding failed: Model forward pass failed: index-select invalid index 512 with dim size 512`. The trap is the units: prose runs ~4 chars/token, so the 1200-char default lands near ~300 tokens and is safe — but **code is ~2.5–3 chars/token** (camelCase, symbols and unknown identifiers all split into several word-pieces), so a 1200-char code chunk can blow past the cap. Only the *largest* files trip it, so before the accounting above they dropped out while the run still reported mostly-success. For a code corpus start at `--chunk-size 800 --overlap 120` and go lower for minified or single-line files, or switch to a long-context model (`nomic-embed-text`, 8k). Failed files are marked `err:` in the manifest, so re-running after lowering retries exactly them.

**Re-ingesting is deterministic, and changed files are surfaced rather than skipped.** Same corpus, same `--chunk-size` / `--overlap`, same local model → same chunks, same `chunk_idx`, same ids (see the determinism note in `ingest_chunked.yaml.tpl`). The manifest records a SHA-256 of each file body, so a file edited since it was ingested is reported instead of silently staying `ok` — it is *not* re-ingested automatically, because its rows still exist under stable ids and a re-POST would collide on the primary key. Delete those rows and drop the manifest entries to refresh. Caveats worth knowing: `remote_embed` is not reproducible over time (hosted models get reversioned silently), and pgvector's HNSW is approximate and insertion-order-sensitive, so a full rebuild can still reorder near-ties even with identical rows — equal-*score* rows no longer reorder, since every search pipeline breaks ties on `id`.

**Exit code is non-zero when nothing was ingested**, so a no-op cannot pass for success: no file matched `--include` (the patterns and the file count are printed), or every matched file was skipped. It stays zero when some files ingested with others skipped, and when every ingestable file was already `ok` in the manifest.

**A document the server already holds counts as `already-present`, not as a failure.** The doc id is derived from the source path, so re-POSTing a file that is already indexed collides on the primary key. That is not an error condition — it is the normal aftermath of an interrupted run, because the manifest is flushed at most every two seconds and a Ctrl-C drops the last few successes from it while their rows stay committed. Those files are recorded `ok` and counted on their own line (`already-present=N`), so a resumed run converges instead of retrying them forever. Treating the collision as an error is what made an interrupted ingest unrecoverable: the manifest said `err:`, the next run saw `err:` as pending, re-POSTed, collided, and wrote `err:` again.

The caveat is printed with the count and matters if you deleted the manifest on purpose: `already-present` means *rows exist*, not *rows are current*. Nothing was re-indexed. To actually refresh a document, delete its rows (`DELETE FROM <table> WHERE source = '...'`) and its manifest entry, then re-run.

#### Step 3, table form — rows instead of files

```bash
python scripts/ingest_table.py --workspace ./context \
  --db ./staging.db --table staged_documents \
  --key-column key --content-column content --source-column source
```

One row = one document; everything else matches the folder form — same endpoint, same manifest (`ingest_progress.json` is shared, because the index is shared), same resume, same three-way `ok` / `already-present` / `fail` handling. The SQLite file is opened read-only (URI `mode=ro`), so the run cannot write to, alter, or leave journal files in a table you were handed.

- **Identity is the source string**, exactly as the folder form derives ids from relative paths. Default source is `<label>#<key>` with the label defaulting to the table name; pass `--source-column` when rows carry a real locator (URLs cite better). Keep the label and the source scheme stable across runs — changing them re-ingests every row under new ids *next to* the old rows, the same way moving a corpus root does.
- **Source strings must be unique across the whole workspace, not just within one run.** The manifest and the index are shared by every entry and every run, so two documents that produce the same source string are one document id — and the failure is quiet both ways: identical content is written off as `already ok`, differing content is reported as a "changed document" and then never indexed, because its ids are taken. Both scripts therefore record which **raw-material set** each source came from (the corpus root, the db file plus table, or the NDJSON label) and refuse to start when a source would mean a different document, naming both sets. This catches the case that matters most in practice: two corpus roots that each contain a `README.md`, ingested into one workspace. Judgement is on set *and* content hash together, so an ordinary re-run from a moved directory or under a new `--label` is recognised as the same material rather than refused. If you do hit the refusal, give each corpus root its own workspace, or make the strings distinct with `--source-column` (real locators) or a non-colliding `--label`.
- **Every row is accounted for**, same contract as files: the `rows: N  ingestable: …  skipped: …` line adds up, and each skip prints its reason and names — `null key`, `duplicate source`, `no text content`, `not UTF-8`, `non-text content`, `too large for one request`, plus `not valid JSON` / `missing key or content field` on the NDJSON path. `--limit` is a debugging trial: what it holds back is counted separately as `limited` and the run prints INCOMPLETE, so a truncated run cannot be mistaken for a finished one.
- **Empty-content rows are a signal, not noise.** On a table landed by the fetch-and-land process they are documents that were listed but never fetched. Pass **`--require-complete`** there and the run refuses to index a partial corpus instead of merely mentioning it; without the flag it warns and continues, which is fine for an ordinary table but not for a fetched one. When the user has seen the named list and decided to build the corpus anyway, **`--accept-missing N`** lets it through with the exact count — it records the decision in the command and stops again if the shortfall later changes. See [references/fetch_and_land.md](references/fetch_and_land.md) for what all this does and does not prove.
- **Every run ends with one machine-readable verdict**: `RESULT complete=true`, or `complete=false` with a reason (`limit`, `accepted-shortfall`, `failed-posts`). Read that line rather than the exit code when anything downstream consumes the run — a deliberate `--limit` trial exits 0 exactly like a finished ingest.
- **A table in Postgres, Mongo, or anything else**: export rows as NDJSON with the client you already use and pipe them in. One JSON object per line; `key` and `content` required, `source` optional; `--label` is required in this mode (it namespaces ids when rows carry no `source`). Postgres, verified end to end against postgres:16 on 2026-08-25:

  ```bash
  psql "postgresql://user@localhost:5432/appdb" -Atc \
    "SELECT json_build_object('key', id, 'content', body, 'source', url) FROM docs" \
    | python scripts/ingest_table.py --workspace ./context --ndjson - --label docs
  ```

  Keep `-A` (unaligned) and `-t` (tuples only). Measured without them on the same data: the header, the `---` rule and the `(3 rows)` footer come through as three `not valid JSON` skips while the real rows still ingest — so the run "succeeds" with a skip count that has nothing to do with your data. The same shape works for any client that can emit one JSON object per row (`mongoexport` does it natively).
- **Refreshing changed rows** works like changed files: the manifest hash surfaces them, nothing is re-ingested automatically, and the fix is the same delete-rows-and-manifest-entries-then-rerun described above.

### Step 4 — Retrieve and answer

```bash
# Hybrid (default — RRF over the vector and full-text signals; the server embeds {query} inline)
curl -X POST http://localhost:8080/search-hybrid/execute \
  -H 'Content-Type: application/json' \
  -d '{"query":"how does retry backoff work","text_query":"retry backoff","vector_weight":0.5,"text_weight":0.5,"limit":5}'

# Full-text only — named entities, exact strings. No embedding, so also the cheapest.
curl -X POST http://localhost:8080/search-fulltext/execute \
  -H 'Content-Type: application/json' \
  -d '{"query":"retry","limit":5}'
```

Use `/search-vector/execute` for paraphrase and conceptual questions. Cite the `source` of every chunk used in an answer.

**Parameter names are inferred from each pipeline's SQL and are not interchangeable.** Verified against a running server 2026-08-04:

| Endpoint | Parameters |
|---|---|
| `ingest` | `doc_id`, `source`, `chunk_idx`, `content` |
| `ingest-chunked` | `doc_id`, `source`, `content`, `chunk_size`, `overlap` |
| `search-fulltext` | `query`, `limit` — **not** `text_query` |
| `search-vector` | `query`, `limit` |
| `search-hybrid` | `query`, `text_query`, `vector_weight`, `text_weight`, `limit` |

Only `search-hybrid` takes both `query` and `text_query` (one feeds the embedding, the other the FTS match). Standalone `search-fulltext` takes `query`. Sending the wrong name returns `parameter_validation_error` listing what it expected — read that list rather than guessing.

**The relevance field is named differently per backend.** `search-vector` returns `distance` on sqlite and `_score` on postgres; `search-hybrid` returns `rrf_score`. Read the field that is actually present instead of assuming one — defaulting a missing key to `0` silently turns every result into a tie, which looks like broken ranking when the ranking is fine.

**`search-fulltext` does not segment CJK — on a Chinese, Japanese or Korean corpus it returns nothing.** Both backends split on non-alphanumeric boundaries, and an unbroken run of Han characters has none, so the whole run becomes a single token: only an exact, full-run query matches. Measured 2026-08-13 on a real workspace — `预跑` and `上下文` each returned **0 rows while present in the corpus**, `Skardi` and `Agent` returned 1 each. Postgres behaves the same way (`to_tsvector` with the default `pg_catalog.english`; the `simple` configuration does not help — it changes stemming, not segmentation).

The failure is quiet in two ways: the endpoint answers `success: true` with an empty set rather than erroring, and `search-hybrid` keeps returning correct results because the vector half carries the query. **So on a CJK corpus, reach for `search-vector` or `search-hybrid` and treat `search-fulltext` as unavailable** — do not report "no matches" to the user from a bare full-text call.

Fixing it is a real trade-off rather than a one-line default change (`trigram` on sqlite reaches 3-character terms but still misses 2-character ones, costs ~1.8× the index, and turns English word search into substring search; postgres needs an installed segmenter such as `zhparser`). Tracked in [skardi-skills#26](https://github.com/SkardiLabs/skardi-skills/issues/26).

### Step 5 — Stop when done

```bash
python scripts/stop_server.py --workspace ./context
```

## Upgrading from auto-knowledge-base / auto-rag

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

## References

- [references/fetch_and_land.md](references/fetch_and_land.md) — the fetch-and-land process for documents still inside a service: list → fetch → reconcile → ingest, with the acceptance criteria.
- [references/schemas.md](references/schemas.md) — the SQL the user runs on the override path, per backend.
- [references/runtimes.md](references/runtimes.md) — local-process vs docker vs kubernetes in full.
- [references/pipeline_patterns.md](references/pipeline_patterns.md) — pipeline shapes, including the SQLite FTS5 + vec0 mirror design.
- [references/troubleshooting.md](references/troubleshooting.md) — server and override-path failures.
- [references/troubleshooting_sqlite.md](references/troubleshooting_sqlite.md) — local-path failures: sqlite-vec loading, extension paths, model download.

## Boundaries

- Never claim a capability without checking it against the running server. If `/pipelines` does not list a pipeline, it does not exist.
- Never run schema DDL in a datastore the user owns without being asked.
- Never write to, alter, or lock a table handed over as raw material — reading its rows is the entire interaction.
- Never call a fetched corpus complete without showing the user the reconciliation numbers: listed, fetched, and the missing documents by name. An index that builds cleanly is not evidence that nothing is missing, and neither is a passing `--require-complete` — it proves every *listed* document was fetched, never that the listing was whole.
- Never invent a source total to make a reconciliation pass. "The source exposes no total; here is how the end of the listing was established" is an acceptable report. A fabricated denominator is not.
- Never report a `--limit` run as a finished ingest, or an `--accept-missing` run as a complete corpus; both print `RESULT complete=false` and say why.
- Do not promise a CLI-only or serverless mode. There isn't one.
