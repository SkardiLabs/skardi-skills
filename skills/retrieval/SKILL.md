---
name: retrieval
description: 'Answer questions from live data through a running skardi-server using the skardi CLI. Discover at runtime what sources and named pipelines the server has (and table schemas where the deployment exposes them), run the question through any semantic search surface first (search-hybrid and friends), then write read-only SQL against exact qualified table names for the precise answer, check the truncated flag before trusting counts, and report with the query attached. Use whenever the user wants to query, explore, or answer questions from data a skardi-server already serves — Postgres, MySQL, SQLite, MongoDB, files, or SaaS sources registered in its ctx — or asks what data is available. Trigger on: query our database, ask our data a question, what tables do we have, run SQL through skardi, use the skardi CLI, how many orders/users/events, answer from the warehouse. Requires a reachable skardi-server; this skill does not build search indexes (auto-context does that), does not write data (the job path does that), and does not set up or start servers.'
---

# retrieval — answer questions from live data through skardi

Your job: take a concrete question, find the data behind a running skardi-server that answers it, and come back with the answer plus the query that produced it. The loop is always the same: **discover → search meaning first → SQL for precision → check what came back → report**.

The `skardi` CLI is a thin HTTP client — every command below is one request to the server, which holds the query engine, the source registrations, and the safety policy. You never need database drivers, connection strings, or credentials for the backing stores; if the server is reachable, you can work.

## What this skill is not

- **Not index building.** If the user wants documents made searchable, a RAG surface, or embeddings — that is the `auto-context` skill. This skill *consumes* whatever search surface already exists.
- **Not writing.** Retrieval means `SELECT`. Writes (INSERT / UPDATE / DELETE, scheduled materialization) belong to Skardi's job path with its own review; do not do them from here even when a source would allow it.
- **Not server operations.** No server running, no sources registered, missing token — report what is missing and stop. Do not install, start, or reconfigure a server as a side effect of answering a question.

## Prerequisites

1. **The `skardi` CLI on PATH.** One small binary, no runtime deps. Check with `skardi --version`. Install from the [GitHub releases page](https://github.com/SkardiLabs/skardi/releases) — download the tarball for your platform, untar, put `skardi` on PATH.
2. **A reachable skardi-server.** Connection resolves in this order: `--server <URL>` flag → `$SKARDI_SERVER_URL` → `~/.skardi/config.yaml` → default `http://127.0.0.1:8080`. If auth is enabled on the server, pass `--token` or set `$SKARDI_API_TOKEN`.
3. **Exit code contract:** `2` means the server was unreachable. That is an environment problem, not a query problem — see the stuck protocol.

This skill is written and tested against **v0.5.0** of both CLI and server (the current release). Deployment behavior — which sources exist, what is read-only, row caps — is discovered live and is never assumed from this text.

## Rule zero: ask the server, not your memory

Which tables exist, which pipelines are registered, what a column means, what is writable — these are **deployment facts**. They differ per server and change under you. Re-discover them at the start of every session; never carry them over from a previous conversation or from this file.

| Question | Command |
|---|---|
| Is the server up? | `skardi health` |
| Is source X reachable for pipeline Y? | `skardi health <pipeline>` |
| What data is there? | `skardi schema` |
| Which pipelines are registered? | `skardi pipeline list` |
| What does pipeline X take? | `skardi pipeline show X` |

**Read what came back; do not check it against an expected shape.** These
commands answer differently across deployments, and a route that exists on one
is absent on another — a hosted gateway may answer `404` with an empty body to
`pipeline list`, `pipeline show` and `health <pipeline>` while `health` and
`schema` work fine. That is not a broken server; it is a deployment without
that surface. Treat an empty or 404 answer as "this deployment does not offer
it" and take the documented fallback (step 2 goes straight to SQL), rather than
retrying or assuming the command is misused.

Two consequences worth stating, because instructions further down lean on them:

- **`skardi schema` may or may not carry operator-written descriptions.** They
  come from a semantics overlay the server was started with, and plenty of
  deployments have none. Where they are absent, column meaning has to come from
  a `LIMIT 5` peek (step 3) and from asking, not from this file.
- **`skardi health <pipeline>`, where it exists, has a known false negative:** a
  source registered at catalog level can report `unhealthy — No table named
  '<source>'` while queries against it work. Confirm with a real query before
  believing a source is down.


## The retrieval flow

### 1. Locate the data

Run `skardi schema` and read the answer it gives you. Each source arrives with a
name, a type, and a list of tables; each table arrives with a name and a column
list. **Both the name shape and whether columns are enumerated vary by how the
source was registered, so take them from the response rather than from a
category in this file.**

- **Use table names exactly as `schema` printed them.** Some are bare
  (`orders`), some are two- or three-part (`shop.main.orders`,
  `saas.github_repo.issues`). Copy the string; do not reassemble it from parts
  and do not add or strip qualification.
- **If a table came back with its columns, that is your schema** — no further
  verification is needed or available.
- **If a table came back with an empty column list**, the deployment does not
  enumerate it. `SHOW TABLES` and `information_schema` are not available, and
  `DESCRIBE` is refused on some deployments (`HTTP 400 unsupported-statement`),
  so do not build a verification step on either. Peek instead:

  ```bash
  skardi query --purpose "peek at orders to learn its columns" --session-id "$SKARDI_SESSION" \
    -e "SELECT * FROM shop.main.orders LIMIT 5" --table
  ```

  (`$SKARDI_SESSION` is the task's session id — step 3 explains the pair;
  mint it before the first query, peeks included.)

  A successful peek both proves the name and shows the columns. If the peek
  fails, stop and ask for the table list rather than iterating guesses.

Extract two things before writing any real query:

- **Exact table names, exactly as printed.** A wrong or under-qualified name fails without saying so: deployments answer either `query_execution_error (HTTP 500)` or `HTTP 400 planning-failed`, and neither names the table. Treat any unexplained planning or execution failure as "check my table name first".
- **Column meanings.** If descriptions are present, trust them over column-name guesses; where the deployment has no semantics overlay they will be absent, and a `LIMIT 5` peek plus asking is the only honest substitute — `amount_cents` meaning "divide by 100" is the kind of fact that silently corrupts an answer when skipped.

If the question names data you cannot find, say so — do not query a lookalike table and present it as the answer.

### 2. Search meaning first, SQL second — in that order, not either/or

Check `skardi pipeline list` for a search surface: the `auto-context` standard names (`search-hybrid`, `search-vector`, `search-fulltext`), or a pipeline the operator has declared to be one. Name-shape is how you *find* candidates; whether you may *call* one is decided by the declaration rule below.

- **If one exists (and clears the declaration rule below), run the user's question through it first**, even when you expect to write SQL afterwards. It searches indexed content — documentation, policies, definitions, past decisions — and what comes back tells you what the terms in the question *mean* before you count anything. A question like "how many refunds under the new policy" needs the policy text (semantic surface) before the refund count (SQL) can be right:

  ```bash
  skardi run search-fulltext -p 'query=refund policy' -p limit=5
  # → "Refund policy changed on 2026-07-15: refunds are now allowed within 30 days …"
  ```

  (`search-hybrid` takes more parameters — `query`, `text_query`, `vector_weight`, `text_weight`, `limit`. Check the signature with `pipeline show`: matching the standard shape makes a pipeline worth *proposing*; the declaration rule below still decides whether you may call it.)

- **If the search result alone answers the question** (a "what does X say / find the doc about Y" question), stop there and cite the sources it returned.
- **Search hits inform your SQL; they never override the data.** An indexed document can be stale or off-topic — use what it says to shape filters and interpret terms, but the registered tables are the system of record for numbers. If a document and the data disagree, report both, don't average them.
- **If no search surface exists, go straight to SQL.** Do not build one — that is `auto-context`'s job, offer it as a follow-up instead.

**Which pipelines may you run at all? Default: none — a declaration is required.** Pipeline SQL is validated when the server loads it, so a pipeline that would write to a `read_only` source cannot even register (the server refuses to start with it). But a write to a source the operator marked `read_write` is a perfectly legal pipeline — every `auto-context` deployment ships `ingest` / `ingest-chunked`, which insert rows — and `pipeline show` does not reveal the SQL, so **nothing you can inspect at runtime proves a pipeline is read-only**. Names and parameter shapes are labels, not proof: an INSERT pipeline *named* `search-fulltext` with `query`/`limit` parameters would pass every look-based test. Not writing is a hard promise of this skill, so looks never authorize a call. A pipeline is callable only when **both** hold:

1. **Someone accountable has declared this specific pipeline read-only.** Two forms count: the operator wrote it into the source's semantics description, which you read in `skardi schema` (e.g. an index table described as "served by the **read-only** search-fulltext pipeline"), or the user/operator confirms it in this session. **Many deployments carry no descriptions at all** — where `schema` returns none, the first form is simply unavailable and asking in this session is the only route; the absence authorizes nothing on its own. A description that merely *mentions* a pipeline's name declares nothing — what authorizes is the operator saying it does not write. Matching an `auto-context` standard signature — `search-fulltext(query, limit)`, `search-vector(query, limit)`, `search-hybrid(query, text_query, vector_weight, text_weight, limit)` — is a strong reason to *propose* one ("this looks like auto-context's standard search pipeline — confirm it's the unmodified read-only one and I'll use it"), but the declaration is what authorizes, not the match. Ask once per pipeline and keep the answer for the session.
2. **The declaration also covers the result bound.** Parameter shapes prove nothing here either: a parameter *named* `limit` is not evidence the SQL applies it as a `LIMIT`, and a parameter named `id` is not evidence of a unique key — `pipeline show` cannot show you the SQL. The bound must be part of the accountable statement: "honors `limit` — returns at most that many rows", "unique-key lookup, at most one row", or "the unmodified auto-context standard pipeline" (whose shipped templates bound results by construction). With the bound declared, still pass `limit` explicitly and small (start at 5–20, raise deliberately) wherever the parameter exists, because `skardi run` output is not row-capped. A pipeline whose declaration is silent on the bound is not callable — get those rows through ad-hoc SQL with a `LIMIT` instead.

Everything else — however retrieval-flavored its name — you do not call from this skill. Answer with an ad-hoc `SELECT` (validated read-only on every request) or ask what the pipeline is. Never run a pipeline to find out what it does.

**This rule is a workaround for a gap in v0.5.0, and is meant to go away.** Asking a human to vouch for a pipeline is what you do when the machine cannot answer — and here it nearly can: the server already determines each pipeline's statement kind when it validates the SQL at load time (that is how a pipeline writing to a `read_only` source gets rejected at startup). It simply does not expose that verdict, so `pipeline show` returns parameters and nothing about what the statement does. Once the server reports statement kind — ideally alongside a result-bound signal — this whole declaration dance collapses into one runtime question the agent asks the engine, and the two conditions above should be deleted rather than maintained. That belongs to the runtime capability contract the engine still owes its callers; until it exists, declarations are the only honest way to keep the read-only promise.

Invocation: `skardi run <name> -p key=value` — values parse as JSON first (numbers, booleans), then fall back to plain strings.

### 3. Ad-hoc SQL: read-only, one statement at a time

Mint one session id when the task starts and reuse it for every query in the task:

```bash
SKARDI_SESSION=$(uuidgen)
skardi query --purpose "order counts by lifecycle state" --session-id "$SKARDI_SESSION" \
  -e "SELECT status, COUNT(*) AS n FROM shop.main.orders GROUP BY status" --table
```

- **Every `skardi query` carries `--purpose` and `--session-id`.** They are sent as the `ai_context: { purpose, session_id }` object on the request, which is what the server's audit ledger records, the daily brief's intent breakdown groups by, and session-level learning aggregates on — a query without them lands as "(no declared intent)" with no session. The pair is all-or-nothing (the server rejects one without the other; the CLI enforces it too, since skardi#232). `--purpose` is one plain-language line on why this query runs, ≤2000 characters; `--session-id` is any non-empty string ≤200 characters — one id per task, minted once (`uuidgen`) and reused across the task's queries so they group as one session. Purpose belongs in the flag, not in a SQL comment: a comment rides inside the SQL text where nothing aggregates it. (If SQL text does start with a comment, use the block form — a leading `-- comment` makes the CLI misparse `-e "--..."` as a flag.)
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
| HTTP 400, refusal code, no detail | the statement hit policy. Codes seen in the wild: `sql_validation_error`, `unsupported-statement` (a write, `DESCRIBE`, `SHOW`), `planning-failed` (a name that did not resolve, multiple statements). **The message names nothing** — the code is all you get, and the same code covers several causes | Read the code, not a message. For `unsupported-statement`, rewrite as a single read-only `SELECT`; if the task genuinely needs a write, say it belongs to the job path and hand it back. For `planning-failed`, re-check the table name against what `schema` printed, then that the SQL is one statement. |
| HTTP 500, no detail | a wrong or under-qualified table name; also non-DataFusion syntax | Re-check the name against step 1 by peeking at it, fix qualification, retry once. The precise cause is only in the server logs, which the operator has and you may not. |
| HTTP 403 | authorization did not resolve — not the same as being denied. Message is generic by design (the resource stays in the operator's log so a listing cannot be used to enumerate hidden sources) | Retry; a transient one clears. If it persists while other callers succeed, report it and ask the operator for the log line — you cannot tell from the response which of several causes it was. |
| pipeline parameter errors | wrong names or types | Re-read `skardi pipeline show <name>`, match names exactly. |
| the same question failed 3 times | you are guessing | Stop. Show the user what you tried, what came back, and your best hypothesis of what is missing (a source not registered? a semantics overlay that would explain columns? a search surface that does not exist?). |

An honest "here is what I tried and where it stopped" beats a fourth guess.

## Reporting the answer

Lead with the answer, then attach the evidence so the result can be re-run and audited:

```
410 orders are paid, totalling ¥507,467.60.

— from shop.main.orders via skardi query
  --purpose "paid order count and revenue" --session-id 3fa4…
  SELECT COUNT(*) AS n, SUM(amount_cents)/100.0 AS total_yuan
  FROM shop.main.orders WHERE status = 'paid'
  1 row, not truncated; cross-checked against GROUP BY status over all 1500 orders
```

State which source and table the answer came from, the exact query or pipeline + parameters, and the truncation status. If the semantic surface contributed context, cite what it returned too. If anything about the answer is approximate — sampled peek, capped rows, ambiguous column meaning — say so in the same breath as the number, not as a footnote.
