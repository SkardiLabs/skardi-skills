# Fetch and land — documents that still live inside a service

The third raw-material entry. Use it when the documents are not on disk and
not in a table yet: a wiki, cloud docs, a mailbox, a CMS — anything where the
text sits behind an API and has to be pulled out one document at a time.

The shape is four stages, and it ends where the second entry begins:

```
list what exists  →  fetch each body  →  reconcile  →  ingest the table
   (stage A)            (stage B)         (stage C)      (stage D = ingest_table.py)
```

**This file gives you the process and the acceptance criteria, not fetch
code.** How to walk a specific service — which endpoint lists pages, how its
pagination cursor works, what auth it wants — is written by you, per source,
at the moment you need it. That is a deliberate boundary, not a gap: source
APIs change too often and there are too many of them for a skill to carry
working fetchers for each (the same reasoning that keeps per-source
capability lists out of SKILL.md). What makes agent-written fetch code safe
to rely on is stage C. Do not skip it.

## Why reconciliation is the load-bearing stage

Under-fetching does not error. A pagination loop that misses one page, a
tree walk that skips one level of children, a rate-limited run that stops
early — all of them return cleanly. The 80% you did fetch chunks, embeds and
indexes without complaint; searches return plausible, well-cited answers;
and nothing anywhere will ever mention the 20% that is missing. The person
searching for one of those documents gets "no results" and no hint that the
corpus is incomplete.

Once the listing and the bodies are rows in one table, the check is three
SQL queries. Refusing to run them is the only way this process fails
silently, which is why they are acceptance criteria and not a suggestion.

## Stage A — land the complete listing first

Before fetching a single body, write one row per document that *exists at
the source*: its stable key, a human-meaningful locator, its title, and no
content yet. The listing is the denominator for every later check — a
document that never makes it into the listing is invisible to stage C, which
makes listing completeness the one thing you must establish here:

- **Page to the end the way the API defines the end** — its has-more flag or
  cursor-exhaustion, never "the page came back short" or a guessed count.
  (Real trap, measured on Feishu wiki listings: the final page returns a
  NON-empty page token together with `hasMore: false` — stopping on "empty
  token" is wrong there.)
- **Recurse containers all the way down.** Wiki trees and folder hierarchies
  list one level per call; a full listing is a client-side walk. Track which
  containers you have expanded — the walk is done when every node whose
  `has_children`-style flag is true has been expanded, not when the queue
  "seems" empty.
- **Record the source's own total if it shows one** (a UI count, an API
  `total` field). `listed == source total` is the cheapest completeness
  check you will ever get. If the source shows no total, write down *how*
  you established the end of the listing instead.

The staging file is a SQLite database **you create** — like the workspace,
it is yours; it is not the user's datastore and it is never `kb.db` (the
server owns that). Minimum shape, extend freely:

```sql
CREATE TABLE IF NOT EXISTS staged_documents (
  key        TEXT PRIMARY KEY,  -- stable id at the source (page id, node token, message id)
  source     TEXT NOT NULL,     -- locator a citation can use (URL, path); unique
  title      TEXT,
  content    TEXT,              -- NULL until stage B lands it
  fetched_at TEXT,
  error      TEXT               -- last fetch failure, NULL after a success
);
```

## Stage B — fetch bodies one document at a time

For each listed row: fetch the body, convert it to the text you actually
want indexed, and update the row (`content`, `fetched_at`, `error = NULL`).
On failure, write `error` and move on — one document's failure is one row's
state, never the run's.

- **Make the loop resumable**: skip rows whose `content` is already
  non-empty. Then a rate-limited or interrupted run is re-run, not redone.
- **Clean the text here, not later.** `ingest_table.py` ingests rows as-is —
  no front-matter stripping, no boilerplate removal. Whatever you land is
  what gets chunked, embedded and cited.
- **Mind the request ceiling**: one document must serialise under the
  server's 2 MiB request cap (SKILL.md Step 3). Split oversized documents
  into parts at landing time — give each part its own key and source — or
  they will surface as `too large for one request` skips at stage D.

## Stage C — reconcile, and show the numbers

Three queries over the staging table:

```sql
SELECT count(*) FROM staged_documents;                     -- listed
SELECT count(*) FROM staged_documents
 WHERE content IS NOT NULL AND trim(content) <> '';        -- fetched
SELECT key, title, coalesce(error, 'never attempted')
  FROM staged_documents
 WHERE content IS NULL OR trim(content) = '';              -- the missing, by name
```

Acceptance criteria — all three, before stage D:

1. **Listed matches the source.** Where the source exposes a total, `listed`
   equals it; where it does not, state how the end of the listing was
   established (which has-more signal, which containers were expanded).
2. **Fetched equals listed** — or the user has seen the exact list of
   missing documents (keys, titles, per-row reason) and explicitly said to
   continue without them.
3. **The numbers go in your report to the user** — listed, fetched, failed,
   and the named misses. Not in a log file: in the message the user reads.
   An index built from a shortfall the user never saw is worse than no
   index, because it looks finished.

A mismatch is not an error state to route around; it is the finding. Fix the
walk (the missed page, the unexpanded level), re-run stage B, reconcile
again.

### What is machine-enforced, and what is not

Be precise about this, because the two halves have very different strength
and it would be easy to read the enforced half as covering both:

- **Enforced.** "Listed but never fetched" is caught by the tool: pass
  `--require-complete` at stage D and any row without content stops the
  ingest, named, before anything is indexed. Criterion 2 above is a gate.
- **Not enforceable, by anyone.** Whether the *listing itself* is complete —
  criterion 1 — cannot be checked from inside this process. The staging
  table is the only thing the tool can see, and a document that was never
  listed leaves no trace in it. No amount of tooling closes this: the
  denominator lives at the source.

So do not invent a number to make the check look automatic. Fabricating an
`expected_count` produces a green reconciliation that proves nothing, which
is strictly worse than an honest "the source exposes no total; here is how I
established the end of the listing". Completeness of the listing rests on
two things and only these: the fetch logic actually following the source's
own end-of-listing signal, and the evidence you report to the user. Where a
real total exists, use it — it is the one case where criterion 1 becomes
checkable.

## Stage D — ingest the staging table

This is the second raw-material entry, exactly as documented in SKILL.md:

```bash
python scripts/ingest_table.py --workspace ./context \
  --db ./staging.db --table staged_documents \
  --key-column key --content-column content --source-column source \
  --require-complete
```

**`--require-complete` is part of the command on this path, not an option.**
It refuses the ingest if any row is not ingestable — most importantly the
`no text content` rows, which on a landed table are documents that were
listed and never fetched. Without it the run prints the same counts and
indexes the partial corpus anyway, which is the exact failure this process
exists to prevent.

It is still not a substitute for stage C: a document your listing never
captured produces no row at all, so nothing here can notice it. Only stage
A's completeness makes stage C's arithmetic mean anything — see the section
above on which half is enforced.

Use `--limit` only for a trial POST or two while debugging. It holds rows
back by design, so the run reports itself INCOMPLETE and refuses to combine
with `--require-complete`; its result never counts as a finished ingest.

## Source notes (state of 2026-08, verify before relying on them)

What changes per source is stages A and B; C and D never change. Three
sources whose body-shaped gap is the same, checked against their pack /
provider sources in 2026-08:

- **Feishu / Lark wiki**: the `wiki_nodes` listing (via the Open Connector
  pack) carries node token, title, hierarchy and URL — **no body column** —
  and lists one tree level per call, so stage A is a recursion over
  `has_child`. Bodies come from the gateway's `fetch_document`, one document
  per call; that single-object shape is exactly what the pack row model
  cannot map ([skardi#197](https://github.com/SkardiLabs/skardi/issues/197)),
  which is why bodies are fetched by you instead of arriving as a table.
  The `feishu_connector` skill in this repo is one implementation of exactly
  these four stages, and **its docs output can be ingested directly**: a
  fixed `(doc_id, title, url, content_md, synced_at)` shape maps onto
  `--key-column doc_id --content-column content_md --source-column url`.
  Its **Bitable output cannot** be assumed ingestable — that table's columns
  are whatever fields the Bitable happens to have, so there is no guaranteed
  stable key and no single text column. Use it only after picking a column
  that is genuinely stable and unique as the key and deciding what the
  content column is (often one long-text field, sometimes several fields
  concatenated in a view or a `SELECT` you land yourself). An upstream pack
  for Bitable rows is planned but unscheduled as of 2026-08.
- **Notion**: bodies are nested blocks — stage B must recurse
  `has_children` to the bottom of every page, and an unexpanded level is
  precisely the silent under-fetch stage C exists to catch. The pack maps
  block metadata only; its own comment defers content extraction to a
  future rendered-markdown call.
- **A mailbox (Gmail-shaped APIs)**: the message *listing* returns ids and
  metadata, never bodies; each body is its own fetch. Same four stages.

One trap that applies to any pack-fed source: a pack table whose *name*
suggests documents may hold only titles and hierarchy (`wiki_nodes` is
exactly this), and as of 2026-08 packs cannot ship a table description an
agent could read. Confirm what a table actually holds — `SELECT * ... LIMIT
3` and look — before treating it as a corpus; if it turns out to be a
listing, it is a stage-A input, not raw material.
