---
name: graph-rag
description: 'Answer a question about a knowledge graph or property graph served by a skardi-server — including graphs stored in Postgres via Apache AGE. Two shapes, one skill: when the question already names the entity (what implements UpgradeStep, who calls verify_token, what does this class extend) go straight to the traversal; when it does not (how does our git integration work and who depends on it, what handles auth) find the entity by semantic or full-text search first, then traverse from it. Either way the answer comes from EDGES, and it is reported with the traversal, its bound, and the confidence field the edges carry shown. Reach for this whenever a question is about how code or entities CONNECT — what implements or extends X, who calls or imports it, what depends on it, what breaks if we change it, what is the blast radius, how are A and B related, trace the path between them, what is near this concept — and whenever the user says graph, graph RAG, GraphRAG, knowledge graph, Cypher, AGE, multi-hop, impact, lineage or dependencies. IMPORTANT: reach for it even when the user names no server and no graph, and even when the question looks like something a codebase grep could answer. A graph on a server holds a DIFFERENT codebase from the working directory, so grepping the local repo answers a different question and can truthfully report a not-found for an entity the graph has hundreds of. Run `skardi schema` first: the CLI resolves its server from ~/.skardi/config.yaml with no arguments, so one command tells you whether a graph source exists — cheaper than assuming either way. Only hand off if that comes back with no graph source: graph setup is graph-source, index building is auto-context, and row-shaped questions (counts, sums, filters over tables) are retrieval.'
---

# graph-rag — answer connection questions over a graph plus a retrieval surface

Your job: take a question whose answer lives in **relationships**, find the
right entities to start from, walk the graph from them, and answer with both
halves shown. Retrieval alone returns documents that mention things; the graph
alone cannot tell you which node the user meant from a sentence. Graph RAG is
the two used in order.

The whole flow is: **understand what to seed → find the seeds → expand from
them → synthesize and cite**.

## What this skill is not

Three sibling skills own the neighbouring work. Sending a user to the right
one is faster than half-doing its job here.

- **Not graph setup.** No graph source registered, a `degraded` source, a
  view whose contract is broken → that is `graph-source`. It provisions the
  AGE backend, declares `type: graph` in ctx YAML, and reads registration
  health.
- **Not index building.** No search surface, or the corpus was never
  embedded → that is `auto-context`. This skill consumes whatever surface
  already exists.
- **Not ordinary querying.** If the question is answered by rows in a table —
  counts, sums, filters, "how many orders are paid" — use `retrieval`. Come
  here when the answer needs **edges**.

## Prerequisites

1. **The `skardi` CLI on PATH** (`skardi --version`). Connection resolves
   `--server` → `$SKARDI_SERVER_URL` → `~/.skardi/config.yaml` → default
   `http://127.0.0.1:8080`; `--token` / `$SKARDI_API_TOKEN` if auth is on.
   Exit code `2` means the server was unreachable — an environment problem,
   not a query problem.
2. **A `type: graph` source**, registered and healthy.
3. **Something to seed from.** Usually a search surface (`auto-context`'s
   `search-vector` / `search-fulltext` / `search-hybrid`, or a `*_knn` /
   `*_fts` table function). Sometimes the graph itself is enough — see
   "Seeding without a search surface".

The graph half has to exist; the retrieval half is needed only when the
question does not already name its entity. If a question names one
(`UpgradeStep`, `verify_token`), skip hop 1 and say you did — an exact-name
lookup finds nothing for a synonym, and the user should know which coverage
they got. If the question is vague and there is no search surface, say so and
offer `auto-context` rather than substituting a keyword `WHERE` for semantic
retrieval and presenting it as the same thing.

## Rule zero: ask the server, not your memory

Which graph is registered, what labels and relationship types it holds, which
search pipelines exist — these are **deployment facts** that differ per server
and change under you. Re-discover them at the start of every session.

```bash
skardi schema                                    # sources, types, descriptions
skardi pipeline list                             # search surfaces, maybe
skardi query -e "SELECT * FROM graph_schema('kg')" --table   # labels + kinds
```

`graph_schema` gives you one `(label, kind)` row per label — it tells you the
vocabulary, not the shape. To learn the shape (which relationship types
connect which labels, and how many), ask the graph:

```bash
skardi query --table -e "SELECT * FROM cypher_query('kg',
  'MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS n ORDER BY count(*) DESC',
  '{}', '{\"t\": \"string\", \"n\": \"int\"}')"
```

That one call is worth more than any amount of guessing, and its row counts
are what tell you whether an expansion is safe to run unbounded (it usually
is not — see "Bound the expansion").

## Why this is two hops, and cannot be one query

This is the mechanical fact the whole skill is built on, and it is not
obvious:

```
cypher_query(connection, cypher, params, columns)
```

`connection`, `cypher`, and `columns` are **strict string literals**,
evaluated at **plan time** — the connection decides the source lookup, the
columns decide the schema, and the cypher is what the plan-time guard
screens. Only `params` carries a runtime value.

So you **cannot** join a retrieval result into the traversal. There is no
single SQL statement that does `knn → cypher`, because the Cypher and its
declared columns must be fixed before any row exists.

**You are what bridges the two hops.** Read hop 1's rows, then write hop 2's
`params` literal containing those seeds. That is a thing an agent can do and
a SQL statement cannot, and it is the reason this skill exists rather than a
view.

**Seeds go in `params`, never concatenated into the Cypher.** Two reasons,
both real: the Cypher is a plan-time literal screened by a keyword guard, and
values spliced into it are an injection surface — retrieved text is exactly
the untrusted input you must not splice. A JSON array inside the params
object works (verified against AGE): `'{"seeds": ["a", "b"]}'` with
`WHERE n.name IN $seeds`.

## The flow

### 1. Decide what the seeds are

Read the question and name the **entity type** the answer starts from before
running anything. "What breaks if we change the auth middleware" seeds on a
code entity; "which teams own the services that call billing" seeds on a
service. Getting this wrong wastes both hops.

Also decide the **direction and depth** the question implies. "What depends
on X" and "what does X depend on" are opposite arrow directions and answer
different questions — a wrong arrow returns plausible, confidently wrong
results. Say the direction out loud in your plan.

### 2. Hop 1 — find the seeds

Whichever surface exists:

```bash
# A search pipeline, when one is declared read-only (see the note below)
skardi run search-hybrid -p 'query=auth middleware' -p limit=8

# Or a retrieval table function, inline. The arity is
# (table, COLUMN, query, k) — the text column is the argument most easily
# dropped, and dropping it fails as an opaque HTTP 500 rather than an arity
# error, which reads as "this surface is broken". It is not; count the
# arguments before concluding anything.
skardi query --table -e "SELECT id, title FROM sqlite_fts('docs.main.docs_fts',
  'body', 'auth middleware', 10)"
```

The `*_fts` / `*_knn` families all take the column: `pg_fts(table, column,
query, k)`, `sqlite_fts(table, column, query, k)`, `pg_knn(table, column,
vector, metric, k)`. If a call 500s, re-read the signature before deciding
the deployment has no search surface — a missing search surface and a
mis-called one look identical from the error, and only one of them is worth
telling the user about.

Take a **small** seed set — 5 to 20. Seeds multiply through the expansion, so
this number is a cost knob, not a quality knob; a 200-seed expansion mostly
returns noise you then have to filter.

**Before calling any pipeline**, the same rule `retrieval` documents applies
here: `pipeline show` does not reveal a pipeline's SQL, so nothing you can
inspect at runtime proves it only reads. A pipeline is callable when someone
accountable has declared *that* pipeline read-only and said what bounds its
result. Matching `auto-context`'s standard signature is a good reason to
propose one; it is not authorization. Otherwise use an ad-hoc `SELECT`, which
the server validates read-only on every request.

**Verify the seeds resolve in the graph before expanding.** Retrieval returns
what a corpus says; the graph holds what exists, and the two drift. One
cheap check saves a confusing empty expansion:

```bash
skardi query --table -e "SELECT * FROM cypher_query('kg',
  'MATCH (n:Function) WHERE n.name IN \$seeds RETURN n.name AS name',
  '{\"seeds\": [\"authenticate\", \"verify_token\"]}',
  '{\"name\": \"string\"}')"
```

If a seed does not resolve, the join key is probably wrong — the corpus's
`title` is not the graph's `name`. Fix the key rather than widening the
search.

### 3. Hop 2 — expand from the seeds

Now the traversal, with the seeds as params. The shape that answers most
connection questions:

```bash
skardi query --table -e "SELECT * FROM cypher_query('kg',
  'MATCH (s:Function)<-[:CALLS]-(caller) WHERE s.name IN \$seeds
   RETURN s.name AS seed, caller.name AS caller, labels(caller)[0] AS kind
   ORDER BY s.name, caller.name LIMIT 200',
  '{\"seeds\": [\"authenticate\", \"verify_token\"]}',
  '{\"seed\": \"string\", \"caller\": \"string\", \"kind\": \"string\"}')"
```

Read `references/patterns.md` for the four recipes this generalizes — seed
and expand, entity neighbourhood, path between two things, and impact /
blast radius — each with the Cypher and the `columns` declaration written
out.

### 4. Synthesize, and show both halves

The answer is not the row dump. Say what the relationships mean for the
question, then attach the evidence — see "Reporting" below.

## The traps

These are the ones that produce **confidently wrong answers** rather than
errors, which is why they are worth naming rather than leaving to discovery.

**`columns` binds positionally against `RETURN`.** The names are labels for
SQL, not a lookup key. Two same-typed columns declared out of RETURN order
swap **silently** — same JSON kind, no type mismatch, nothing downstream can
tell. Write the `columns` declaration by reading your own `RETURN` clause
left to right, every time.

**Properties are JSON text; the getter must match the stored type.**
`json_get_str` on a numeric property returns NULL — not an error, not a
coercion. A column of NULLs after a successful query is almost always this.
`->` / `->>` / `?` are deliberately not installed; use the getter UDFs.

**A view's SQL `WHERE` does not push into its Cypher.** Filtering a graph
view from SQL still materializes the view's whole result first, so an
unbounded view fails `RowCapExceeded` even for a query that wants one row.
The bound belongs **inside** the Cypher.

**Look at what the EDGES carry before you trust an expansion.** This is the
trap that produced the worst answer in this skill's own testing, and it is
invisible from the vocabulary: a relationship type tells you nothing about
how confidently each of its edges was derived. Ask:

```bash
skardi query --table -e "SELECT * FROM cypher_query('kg',
  'MATCH ()-[r:CALLS]->() RETURN keys(r) AS k LIMIT 1', '{}', '{\"k\": \"json\"}')"
```

On a code graph built by static analysis this returned `["line",
"resolution"]`, and grouping by that property gave:

```
ambiguous    467467      <- matched on a bare name only
unique_name  103669
same_scope    62145
import         25565
```

**71% of the edges were guesses.** An unfiltered `<-[:CALLS]-` expansion over
them reports a blast radius that is mostly noise — and reports it
confidently, with real-looking function names. Filtering to the
confidently-resolved edges collapsed one measured example from 81 functions
across 45 files to a handful in a single file.

So: check the edge properties once per session, and if the graph records a
confidence, provenance or resolution field, **filter on it and say which
subset you used**. `WHERE r.resolution <> 'ambiguous'` turns a plausible
answer into a defensible one. If you deliberately include the low-confidence
edges — a blast radius is a place where a false positive is cheaper than a
miss — then report the split rather than the total: "14 provable, 46 more via
name-only matches" is honest, "46 callers" is not.

Every relationship type in that graph carried the same field, so do not
assume it is one edge type's quirk.

**Label the seed match, and keep the traversal in ONE `MATCH`.** This is not
a style preference — it is the difference between an answer and a query that
does not return. Measured on a 109k-vertex / 800k-edge AGE graph:

```
MATCH (s) WHERE s.name IN $seeds MATCH (s)<-[:CALLS]-(c) ...   -- did not return
MATCH (s:Function)<-[:CALLS]-(c) WHERE s.name IN $seeds ...    -- answered
```

An unlabeled `MATCH (s)` is a scan of every vertex in the graph, and splitting
the pattern across two `MATCH` clauses makes that scan the left side of a
join. Both have to be right: labeling a two-clause form did not rescue it, and
inlining an unlabeled one did not either. Write the seed label and the
traversal as one pattern with the `WHERE` after it.

**Bound the expansion, and measure rather than trust.** Ask the edge counts
first (the `graph_schema` section above) — a relationship with hundreds of
thousands of edges will not survive an open-ended walk. Bound it three ways
together: a small seed set, a `LIMIT` inside the Cypher, and a specific
relationship type and direction instead of `-[r]-`. An undirected untyped
`-[r]-` did not return on the graph above even with a label and a `LIMIT`, so
prefer one type and one direction per call.

Timings on a dense graph are also **not stable** — the same call can answer
in a second and later time out as caches and load shift. So treat every
recipe here as a starting point you time on the graph in front of you, raise
depth one hop at a time, and when a call times out do not simply retry it:
narrow the pattern. Repeatedly re-running an expensive traversal degrades the
graph backend for everyone using it, which turns your query problem into
someone else's outage.

If you need "the most important" neighbours rather than "some", sort inside
the Cypher (`ORDER BY` on a degree or a score) — a `LIMIT` without an
`ORDER BY` returns an arbitrary slice, and an arbitrary slice presented as an
answer is the failure mode this whole skill is trying to avoid.

**A truncated result is not a result.** Ad-hoc queries cap at 1000 rows by
default and the note goes to **stderr** — read it. Any "all of X" or count
built on a capped set is wrong; push aggregation into the Cypher
(`count(*)`, `collect()`) instead of counting rows yourself.

## Seeding without a search surface

Sometimes the question names the entity precisely enough that retrieval adds
nothing — "what calls `verify_token`". Then hop 1 is a property lookup in the
graph and you skip the search surface entirely. Say that you did: an answer
that skipped semantic retrieval has different coverage from one that used it
(an exact-name lookup finds nothing for a synonym), and the user should know
which they got.

## Reporting

Lead with the answer in the question's own terms, then attach both hops so
the result can be re-run and audited:

```
Changing `verify_token` reaches 14 call sites across 3 modules — the auth
middleware, the session refresh path, and two admin handlers.

— seeds: search-hybrid 'auth middleware token validation', top 8
  → resolved 2 of 8 in the graph (authenticate, verify_token)
— expansion: cypher_query('kg', MATCH (s)<-[:CALLS]-(caller), LIMIT 200)
  → 14 rows, not truncated
— the 6 unresolved seeds were doc titles with no graph node; the corpus
  describes them but the graph does not contain them
```

State the seed set and where it came from, the traversal and its bound, and
the truncation status. When retrieval and the graph disagree — a document
describes a dependency the graph does not have — **report both**, do not
reconcile them silently. That disagreement is usually the most useful thing
you found: it means the corpus or the graph is stale, and which one is a
question the user can answer and you cannot.

## When stuck

| Symptom | Meaning | Do |
|---|---|---|
| exit code 2 | server unreachable | Report the URL you tried. Do not retry in a loop; do not start a server. |
| `RowCapExceeded` | the Cypher itself is unbounded | Put the bound inside the Cypher, not in SQL. Narrow the relationship type. |
| a column is all NULL | wrong getter for the stored JSON type | Check the property's actual type, pick the matching getter. |
| the expansion returns 0 rows | seeds do not resolve in the graph, or the arrow points the wrong way | Re-run the seed-resolution check; then try the opposite direction. |
| `cypher_query` errors on arity | `columns` count ≠ `RETURN` count | AGE requires declared arity; count them against each other. |
| the same question failed 3 times | you are guessing | Stop. Show what you tried, what came back, and your best hypothesis of what is missing. |

An honest "here is where it stopped" beats a fourth guess. Read
`references/troubleshooting.md` for the fuller symptom table.
