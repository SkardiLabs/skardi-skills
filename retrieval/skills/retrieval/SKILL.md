---
name: retrieval
description: 'Answer questions from live data through a running skardi-server using the skardi CLI. Discover at runtime what sources and named pipelines the server has (and table schemas where the deployment exposes them), run the question through any semantic search surface first (search-hybrid and friends), then write read-only SQL against exact qualified table names for the precise answer, check the truncated flag before trusting counts, and report with the query attached. Use whenever the user wants to query, explore, or answer questions from data a skardi-server already serves — Postgres, MySQL, SQLite, MongoDB, files, or SaaS sources registered in its ctx — or asks what data is available. Trigger on: query our database, ask our data a question, what tables do we have, run SQL through skardi, use the skardi CLI, how many orders/users/events, answer from the warehouse. Requires a reachable skardi-server; this skill does not build search indexes (auto_context does that), does not write data (the job path does that), and does not set up or start servers.'
---

# retrieval — answer questions from live data through skardi

Your job: take a concrete question, find the data behind a running skardi-server that answers it, and come back with the answer plus the query that produced it. The loop is always the same: **discover → search meaning first → SQL for precision → check what came back → report**.

The `skardi` CLI is a thin HTTP client — every command below is one request to the server, which holds the query engine, the source registrations, and the safety policy. You never need database drivers, connection strings, or credentials for the backing stores; if the server is reachable, you can work.

## What this skill is not

- **Not index building.** If the user wants documents made searchable, a RAG surface, or embeddings — that is the `auto_context` skill. This skill *consumes* whatever search surface already exists.
- **Not writing.** Retrieval means `SELECT`. Writes (INSERT / UPDATE / DELETE, scheduled materialization) belong to Skardi's job path with its own review; do not do them from here even when a source would allow it.
- **Not server operations.** No server running, no sources registered, missing token — report what is missing and stop. Do not install, start, or reconfigure a server as a side effect of answering a question.

## Prerequisites

1. **The `skardi` CLI on PATH.** One small binary, no runtime deps. Check with `skardi --version`. Install from the [GitHub releases page](https://github.com/SkardiLabs/skardi/releases) — download the tarball for your platform, untar, put `skardi` on PATH.
2. **A reachable skardi-server.** Connection resolves in this order: `--server <URL>` flag → `$SKARDI_SERVER_URL` → `~/.skardi/config.yaml` → default `http://127.0.0.1:8080`. If auth is enabled on the server, pass `--token` or set `$SKARDI_API_TOKEN`.
3. **Exit code contract:** `2` means the server was unreachable. That is an environment problem, not a query problem — see the stuck protocol.

This skill is written and tested against **v0.5.0** of both CLI and server (the current release). Deployment behavior — which sources exist, what is read-only, row caps — is discovered live and is never assumed from this text.

## Rule zero: ask the server, not your memory

Which tables exist, which pipelines are registered, what a column means, what is writable — these are **deployment facts**. They differ per server and change under you. Re-discover them at the start of every session; never carry them over from a previous conversation or from this file.

| Question | Command | What v0.5.0 returns |
|---|---|---|
| Is the server up? | `skardi health` | overall status only (`healthy` + timestamp). |
| Is source X reachable for pipeline Y? | `skardi health <pipeline>` | per-source checks for that pipeline's sources. **Known false negative:** a source registered at catalog level reports `unhealthy — No table named '<source>'` even when queries against it work fine (the probe assumes table-level registration). Confirm with a real `DESCRIBE` before believing a source is down. |
| What data is there? | `skardi schema` | every source with its type and path/URL, **plus operator-written source and column descriptions** when the server was started with a semantics overlay. Column enumeration depends on how the source was registered — see step 1. |
| What pre-approved queries exist? | `skardi pipeline list` | pipeline names, versions, endpoints. Names only — no descriptions. |
| What does pipeline X take? | `skardi pipeline show X` | parameter names and inferred types. **On v0.5.0 this does not return the pipeline's SQL or description**, so judge what a pipeline does from its name and parameter shape, and ask the operator when that is not enough. |

## The retrieval flow

### 1. Locate the data

Run `skardi schema`. Sources come in two registration shapes, and they read differently:

- **Table-level source** (the default): one source = one table, addressed by the bare source name — `SELECT ... FROM orders`. `skardi schema` lists its full column set with types. Easy case.
- **Catalog-level source**: a whole database mounted as a named catalog. Tables live at `<source>.<schema>.<table>` — e.g. `shop.main.orders` for SQLite, where the schema segment is `main`. **On v0.5.0 `skardi schema` does not enumerate a catalog's tables** (you will see `schema: []`), and `SHOW TABLES` / `information_schema` are not available either. Get table names from the source's semantics description (well-run deployments name their tables there) or from the operator; a name the user's question mentions may be tried too. Whatever its origin, a candidate name earns **exactly one `DESCRIBE` verification** before you build on it — and if no candidate verifies, stop and ask for the table list instead of iterating guesses:

  ```bash
  skardi query -e "DESCRIBE shop.main.orders" --table
  ```

  `DESCRIBE` on a fully qualified name works and returns columns + types; it is your schema tool for catalog sources.

Extract two things before writing any real query:

- **Exact table names, exactly qualified.** Bare names for table-level sources, three-part names for catalog sources. A wrong or unqualified name fails as an opaque `query_execution_error (HTTP 500)` — the server does not tell you "no such table", so treat that error as "check my table name first".
- **Column meanings.** If descriptions are present (semantics overlay), trust them over column-name guesses — `amount_cents` meaning "divide by 100" is the kind of fact that silently corrupts an answer when skipped.

If the question names data you cannot find, say so — do not query a lookalike table and present it as the answer.

### 2. Search meaning first, SQL second — in that order, not either/or

Check `skardi pipeline list` for a search surface: the `auto_context` standard names (`search-hybrid`, `search-vector`, `search-fulltext`), or a pipeline the operator has declared to be one. Name-shape is how you *find* candidates; whether you may *call* one is decided by the allowlist rule below.

- **If one exists, run the user's question through it first**, even when you expect to write SQL afterwards. It searches indexed content — documentation, policies, definitions, past decisions — and what comes back tells you what the terms in the question *mean* before you count anything. A question like "how many refunds under the new policy" needs the policy text (semantic surface) before the refund count (SQL) can be right:

  ```bash
  skardi run search-fulltext -p 'query=refund policy' -p limit=5
  # → "Refund policy changed on 2026-07-15: refunds are now allowed within 30 days …"
  ```

  (`search-hybrid` takes more parameters — `query`, `text_query`, `vector_weight`, `text_weight`, `limit`. Confirm the signature with `pipeline show` before calling: an exact match is what puts a pipeline on the allowlist below.)

- **If the search result alone answers the question** (a "what does X say / find the doc about Y" question), stop there and cite the sources it returned.
- **Search hits inform your SQL; they never override the data.** An indexed document can be stale or off-topic — use what it says to shape filters and interpret terms, but the registered tables are the system of record for numbers. If a document and the data disagree, report both, don't average them.
- **If no search surface exists, go straight to SQL.** Do not build one — that is `auto_context`'s job, offer it as a follow-up instead.

**Which pipelines may you run at all? Default: none.** Pipeline SQL is validated when the server loads it, so a pipeline that would write to a `read_only` source cannot even register (the server refuses to start with it). But a write to a source the operator marked `read_write` is a perfectly legal pipeline — every `auto_context` deployment ships `ingest` / `ingest-chunked`, which insert rows — and `pipeline show` does not reveal the SQL, so **nothing you can inspect at runtime proves a pipeline is read-only**. Names and parameter shapes are labels, not proof: an INSERT pipeline named `search-notes` with `query`/`limit` parameters would pass every look-based test. So invocation is allowlist-only. A pipeline is callable when **one** of these holds:

1. **It matches an `auto_context` standard search pipeline by exact name and parameter signature** — `search-fulltext(query, limit)`, `search-vector(query, limit)`, or `search-hybrid(query, text_query, vector_weight, text_weight, limit)`. These ship in this repo and are read-only by construction. Stated assumption: a deployment that redefines these names with different SQL has broken the convention this skill relies on — if results from one look like writes or nonsense, stop and say so.
2. **The operator or user has told you this specific pipeline is a read-only search/lookup surface** — in this session, or in the source's semantics description. Ask once per pipeline and keep the answer for the session.

Everything else — however retrieval-flavored its name — you do not call from this skill. Answer with an ad-hoc `SELECT` (validated read-only on every request) or ask what the pipeline is. Never run a pipeline to find out what it does.

When you do call one, **always pass an explicit, small limit** (start at 5–20 and raise deliberately) — `skardi run` output is not row-capped, and a search surface without a tight limit can pull an entire index. Invocation: `skardi run <name> -p key=value` — values parse as JSON first (numbers, booleans), then fall back to plain strings.

### 3. Ad-hoc SQL: read-only, one statement at a time

```bash
skardi query -e "/* purpose: order counts by lifecycle state */ SELECT status, COUNT(*) AS n FROM shop.main.orders GROUP BY status" --table
```

- **Open every query with a one-line purpose comment**: `/* purpose: ... */`. On v0.5.0 this is a readability habit and nothing more — it helps whoever re-runs or audits the SQL. Skardi's structured audit contract (an `ai_context: { purpose, session_id }` object in the request body, which is what session-level learning aggregates on) exists server-side only after v0.5.0, and this CLI cannot send it: `skardi query` carries just the SQL and `--max-rows` (CLI flags are tracked in skardi#218). **Do not claim query purposes are being captured for Skardi's self-improving loop** until the CLI grows those flags. Mechanics: use the block-comment form — a leading `-- comment` makes the CLI misparse `-e "--..."` as a flag (only the `--sql="..."` form tolerates it).
- **SELECT only.** The server already rejects DDL and COPY outright, and rejects writes to any source not explicitly configured `read_write` — but do not lean on that: retrieval work is read work, even on writable sources.
- **One statement per request.** The server rejects multi-statement SQL (`Expected exactly one SQL statement`). Run follow-ups as separate calls.
- **Peek before the real query.** `SELECT * FROM <table> LIMIT 5` shows you actual value shapes — date formats, status spellings, NULL patterns — that schema output cannot. One peek prevents most wrong-filter answers.
- **Aggregate in SQL, not in your head.** `COUNT`, `SUM`, `GROUP BY` on the server beat pulling rows and counting them yourself — pulled rows are capped (next section) and the cap corrupts client-side aggregation silently.
- **The dialect is DataFusion SQL** (PostgreSQL-flavored), regardless of what the backing store is. Write standard SQL; do not use backend-specific syntax (SQLite pragmas, MySQL backticks, Postgres extensions).
- **Never go around the server.** Even if you can see a connection string or the SQLite file path, do not query the backing store directly with `sqlite3` / `psql` / a driver. The server *is* the governed path — read-only enforcement, row caps, and the audit trail all live there, and an answer produced behind its back has none of them.

### 4. Read the response before repeating it

Ad-hoc query results are capped at **1000 rows by default** (`--max-rows` to change). When the cap bites, the CLI prints the rows it got to stdout and this note to **stderr**:

```
note: results truncated; pass a higher --max-rows to see the rest
```

- **Check stderr, not just the rows.** A truncated row set looks complete if you only read stdout. Any count, sum, or "all X" claim built on it is wrong — push the aggregation into SQL (preferred) or re-run with an explicit `--max-rows` high enough to be provably complete.
- **Pipeline results are not capped by this mechanism.** `skardi run` returns whatever the pipeline's SQL returns — control size through the pipeline's own parameters (`limit` and friends).
- **Cross-check a number that matters.** Before reporting a business-relevant figure, confirm it with one differently-shaped query — e.g. verify a `GROUP BY` total against a plain `COUNT(*)`. Two queries that agree are evidence; one query is a draft.
- **Empty result ≠ "there is none".** First re-check the filter values against a `LIMIT 5` peek (status spelled `paid` vs `PAID`), then say "none found under these filters" — with the filters named.

### 5. When stuck, stop cleanly

| Symptom | Meaning | Do |
|---|---|---|
| exit code 2 | server unreachable | Report it with the URL you tried. Do not retry in a loop; do not start a server yourself. |
| `sql_validation_error` (HTTP 400) | the statement hit policy: DDL/COPY, a write to a read-only source, or multiple statements — the message names which | Rewrite as a single read-only SELECT. If the task genuinely needs a write, say it belongs to the job path and hand it back. |
| `query_execution_error` (HTTP 500), no detail | most often a wrong or under-qualified table name; also non-DataFusion syntax | Re-check the name against step 1 (`DESCRIBE` it), fix qualification, retry once. The precise cause is only in the server logs, which the operator has and you may not. |
| pipeline parameter errors | wrong names or types | Re-read `skardi pipeline show <name>`, match names exactly. |
| the same question failed 3 times | you are guessing | Stop. Show the user what you tried, what came back, and your best hypothesis of what is missing (a source not registered? a semantics overlay that would explain columns? a search surface that does not exist?). |

An honest "here is what I tried and where it stopped" beats a fourth guess.

## Reporting the answer

Lead with the answer, then attach the evidence so the result can be re-run and audited:

```
410 orders are paid, totalling ¥507,467.60.

— from shop.main.orders via skardi query
  /* purpose: paid order count and revenue */
  SELECT COUNT(*) AS n, SUM(amount_cents)/100.0 AS total_yuan
  FROM shop.main.orders WHERE status = 'paid'
  1 row, not truncated; cross-checked against GROUP BY status over all 1500 orders
```

State which source and table the answer came from, the exact query or pipeline + parameters, and the truncation status. If the semantic surface contributed context, cite what it returned too. If anything about the answer is approximate — sampled peek, capped rows, ambiguous column meaning — say so in the same breath as the number, not as a footnote.
