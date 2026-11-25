---
name: graph-rag
description: 'Answer questions whose answer lives in the edges of a knowledge graph or property graph served by a skardi-server, including graphs stored in Postgres via Apache AGE: what implements or extends X, who calls or imports it, what depends on it, what breaks if we change it, the blast radius, how A and B are related, the path between them, what handles a concern and who depends on it. Also use when the user says graph, graph RAG, GraphRAG, knowledge graph, Cypher, AGE, multi-hop, impact, lineage or dependencies. Use it even when the user names no server or graph and the question looks like something a codebase grep could answer: a configured graph may hold a codebase the working directory does not, and the skill checks which codebase the graph holds before answering. Not for connecting a graph (graph-source), building search indexes (auto-context) or row-shaped questions such as counts, sums and filters over tables (retrieval).'
metadata:
  skardi-min-version: "0.6.0"
---

# graph-rag — answer connection questions from a graph's edges

**Version prerequisite:** Skardi v0.6.0 or later, the same prerequisite
`graph-source` states. v0.6.0 is the first release with `type: graph` sources
and the `cypher_query` UDTF that every traversal below runs on; v0.5.0 has
neither. Connect the graph with `graph-source` first.

Your job: take a question whose answer lives in **relationships**, find the
right entities to start from, walk the graph from them, and answer with both
halves shown. Retrieval alone returns documents that mention things; the graph
alone cannot tell you which node the user meant from a sentence. Graph RAG is
the two used in order.

The whole flow is: **understand what to seed → find the seeds → expand from
them → synthesize and cite**.

## First, whose graph is this?

`skardi schema` tells you a graph EXISTS. It does not tell you what is in it,
and for a question about code that distinction decides whether your answer is
about the user's project or somebody else's. A configured server is not
scoped to the directory you are standing in, and nothing about the question
reveals the mismatch — the traversal succeeds, the rows look right, and the
entities are real. They are just real somewhere else.

One sample answers it — **ask the filesystem, not the path strings**:

```bash
# No --table here, deliberately: the default output is exactly the envelope's
# `data` array, pretty-printed and nothing else, so it pipes into jq. Table
# mode adds a header, box rules and a trailing "<n> row(s) returned" line,
# none of which is a parsing contract.
skardi query -e "SELECT * FROM cypher_query('kg',
  'MATCH (f:File) RETURN f.file_path AS path LIMIT 40',
  '{}', '{\"path\": \"string\"}')" \
| jq -r '.[].path' \
| while read -r f; do [ -e "$f" ] && echo hit || echo miss; done | sort | uniq -c
```

Do the graph's own files exist where you are standing? Measured both
directions, each against a real graph:

| graph | run from | result |
|---|---|---|
| a DataHub code graph | an unrelated repo | **0 / 40** exist |
| a graph of that repo | that repo | **40 / 40** exist |

There is no threshold to tune: the two answers are 0% and 100%, because a file
path either resolves or it does not. But read the 0% correctly. It means the
graph's files are not *here*; whether that is a mismatch depends on whether
"here" is a codebase at all, which is the branch list below.

**Why not read the project name off the paths.** An earlier version of this
check did — `awk -F/ '{print $1}' | sort | uniq -c` — and it works only when
the graph happens to be a monorepo whose top-level directories are module
names. On the rig it printed `datahub-web-react 3958`, `metadata-ingestion
2920`, which is exactly the signal wanted. On an ordinary single repository the
same command prints `src 3958` and identifies nothing, and on a graph whose
`file_path` values are absolute it prints an empty first field for every row.
Nor is there anything better to read: a `File` node on this graph carries
`id`, `fqn`, `kind`, `lang`, `name`, `file_path`, `line_start`,
`is_generated` — measured — and **no repository or project property at all**.
So the question has to be asked of the filesystem, which is the one authority
that knows what repo you are in.

(That earlier command also sampled `LIMIT 400` and quoted the result as the
codebase's shape. It is not: AGE returns rows in storage order, so the first
400 are alphabetically early. Over all 12,779 files the top entry is
`datahub-web-react 3958`; over the first 400 it is `datahub-actions 146`.
Harmless for a yes/no scope check, wrong if read as a profile of the
codebase.)

The stakes, measured on the same rig: a question like "who calls `main`?"
asked from an unrelated shell would have been answered with **81** DataHub
functions, confidently, with no sign anything was wrong.

Do this once per session, before the first answer about local code. **The check
reports; it does not decide.** Four outcomes, and only one of them is a reason
to leave the graph alone:

- **The paths resolve where you are standing.** The graph is this project.
  Proceed, and name the project in the answer so the scope is visible.
- **The paths do not resolve, and there IS a local project they should have
  matched** — you are in a repository, it has source files, and none of the
  graph's paths are among them. This is the case the check exists for. Say
  which project the graph appears to hold, and answer the local question with
  the ordinary local-code tools. A graph is not authority over a repo it does
  not contain.
- **The paths do not resolve, and there is no local checkout to compare
  against** — an empty directory, a docs folder, a workspace that is not the
  code. Then nothing is in conflict: you were handed a graph, which is the
  ordinary case for a knowledge graph or a corpus that was never a repository.
  **Proceed.** Report the sample you took so the scope is visible, and let the
  user tell you if it is the wrong graph. Refusing here rejects the only path
  that can answer the question.
- **The question was explicitly about the graph** ("what does the graph say
  about…", a named server, a graph that is not code at all) — the graph is the
  subject and the working directory is irrelevant. Skip ahead.

The distinction between the second and third outcomes is whether a local
project exists to be wrong about, so establish that before concluding anything:

```bash
# Is there local code here at all? Cheap, and it decides which branch above you
# are in. Count what is on disk, not what git knows: a checkout is not the only
# way source files arrive, and a vendored or exported tree with no .git is still
# a codebase you could answer from.
find . -maxdepth 3 \( -name node_modules -o -name .git -o -name target -o -name dist \) -prune \
  -o -type f \( -name '*.py' -o -name '*.ts' -o -name '*.tsx' -o -name '*.js' \
  -o -name '*.go' -o -name '*.rs' -o -name '*.java' -o -name '*.rb' \) -print \
  2>/dev/null | head -50 | wc -l
```

Zero means the miss rate says nothing about whose graph it is, so proceed. A
nonzero count next to a total miss is the mismatch worth reporting. Add the
extensions your ecosystem uses; the list above is a starting point, not a
contract.

The bundled eval fixture is the zero case, deliberately: its `File` nodes are
`auth/tokens.py` and friends, nothing resolves next to `setup.sh`, and the
correct behaviour there is to answer from the graph.

(Client-side `awk` rather than Cypher's `split()`: `split` needs a quoted
delimiter, and a nested quote inside the single-quoted SQL literal runs into
the escaping problem described below. Sampling `file_path` avoids the whole
question.)

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

**Seeds go in `params`, never concatenated into the Cypher — and the params
literal itself must be SQL-escaped.** These are TWO layers, and getting the
first right does not give you the second.

**This covers every string value, not just the seeds.** A filter you typed
yourself is subject to the same rule: writing `<> 'ambiguous'` inside the
Cypher closes the single-quoted SQL literal that carries it, and the statement
fails to parse before the graph ever sees it. Send it as a parameter
(`<> $ambiguous`, with `"ambiguous": "ambiguous"` in the params object) the
way the recipes in `references/patterns.md` do.

Cypher params stop Cypher injection: the Cypher is a plan-time literal
screened by a keyword guard, and values spliced into it are an injection
surface. Retrieved text is exactly the untrusted input you must not splice.
A JSON array inside the params object works (verified against AGE):
`'{"seeds": ["a", "b"]}'` with `WHERE n.name IN $seeds`.

But that params object is delivered as a **single-quoted SQL string
literal**, and a seed containing `'` closes it early. `skardi query` has no
parameter binding — only `-e` and `-f`, both of which take SQL text — so
there is no mechanism that escapes this for you. Measured, on a real server:

```
seed: x"]}', '{"name": "string"}') UNION ALL SELECT title FROM docs.main.docs LIMIT 3 --
```

That seed, pasted into the params literal the way this skill's examples build
it, returned three rows from the `docs` corpus under a column named `caller`,
from a statement that was supposed to be a graph traversal. Retrieval output
is untrusted input, so a corpus that can be written to can reshape your read.
The read-only guards hold — one statement only, no DDL, no writes — so the
ceiling is reading another source the token already permits. That is still a
different answer than the one you reported.

**So build the statement, never format it.** Serialize `params` and `columns`
with a real JSON serializer, then double every `'` in each serialized string
before it goes into the SQL, and write the result to a file for `-f` so the
shell is not a third layer:

```python
import json, os, subprocess, tempfile
def sql_str(s):                       # SQL layer: '' escapes a quote
    return "'" + s.replace("'", "''") + "'"
params  = json.dumps({"seeds": seeds})            # JSON layer
columns = json.dumps({"caller": "string"})
sql = (f"SELECT * FROM cypher_query('kg', {sql_str(CYPHER)}, "
       f"{sql_str(params)}, {sql_str(columns)})")
# A temporary file, NOT `q.sql` in the working directory. You are standing in
# somebody's repository, and a fixed name truncates a `q.sql` that repo already
# had — silently, and before the query being written has even run.
fd, path = tempfile.mkstemp(suffix=".sql")
try:
    with os.fdopen(fd, "w") as fh:
        fh.write(sql)
    subprocess.run(["skardi", "query", "--table", "-f", path])
finally:
    os.unlink(path)
```

Verified: with the escaping above, the seed that produced three foreign rows
produces zero rows and no error — it is treated as a name that does not
exist, which is what it is.

One consequence worth acting on: a real entity name in a code graph does not
contain a quote. A seed that needs this escaping is either a wrong join key
or hostile, so it is worth *noticing* rather than only escaping — say so if
one appears.

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
# A search pipeline, when one is declared read-only (see the note below).
# `search-hybrid` takes FIVE parameters and defaults none of them —
# `query` feeds the embedding, `text_query` the FTS match, and the two
# weights blend the halves. Omitting any is a `parameter_validation_error`
# before the pipeline runs, so a short invocation fails on the vague-question
# path where this is the only way to get seeds. `search-fulltext` is the
# one that takes `query` alone; the signatures are in `auto-context`.
skardi run search-hybrid \
  -p 'query=how does the auth middleware work' \
  -p 'text_query=auth middleware' \
  -p vector_weight=0.5 -p text_weight=0.5 -p limit=8

# Or a retrieval table function, inline. The arity is
# (table, COLUMN, query, k) — the text column is the argument most easily
# dropped, and dropping it fails as an opaque HTTP 500 rather than an arity
# error, which reads as "this surface is broken". It is not; count the
# arguments before concluding anything.
skardi query --table -e "SELECT id, title FROM sqlite_fts('docs.main.docs_fts',
  'body', 'middleware', 10)"
```

The `*_fts` / `*_knn` families all take the column: `pg_fts(table, column,
query, k)`, `sqlite_fts(table, column, query, k)`, `pg_knn(table, column,
vector, metric, k)`. If a call 500s, re-read the signature before deciding
the deployment has no search surface — a missing search surface and a
mis-called one look identical from the error, and only one of them is worth
telling the user about.

Three measured properties of these functions, each of which produces a
wrong conclusion rather than an error:

- **The column argument is the ONLY column searched.** `sqlite_fts(...,
  'body', 'git integration', 10)` returned 0 rows on a corpus that has a
  document titled "Git integration" — because `integration` appears in the
  `title`, and the call asked about `body`. Zero hits means "not in that
  column", not "not in the corpus". Search the column the words are in, or
  try more than one.
- **Multi-word queries are AND, not a phrase or an OR.** Same corpus:
  `'git wrapper'` and `'shelling git'` each returned the document (both
  words are in its body), `'git integration'` returned nothing. So a longer
  query is a NARROWER one — the opposite of how a semantic search behaves,
  and the reason a vague question fed verbatim into FTS comes back empty.
  Send few, high-signal terms; keep the full sentence for the vector side.
- **You cannot aggregate over the call.** `SELECT count(*) FROM
  sqlite_fts(...)` fails with an opaque HTTP 500, and wrapping it in a
  subquery fails the same way; `SELECT id, title FROM sqlite_fts(...)`
  works. Return the rows and count them yourself. Note this is the exact
  reverse of the rule for graph work below, where aggregation MUST be pushed
  into the Cypher — the instinct does not transfer.

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

**Verify the seeds resolve in the graph before expanding — and verify how
MANY things each one resolves to.** Retrieval returns what a corpus says;
the graph holds what exists, and the two drift. The check has to return an
IDENTITY, not the name you already had: a name is not unique, and a name
that matches many nodes is the failure this step exists to catch.

```bash
skardi query --table -e "SELECT * FROM cypher_query('kg',
  'MATCH (n:Function) WHERE n.name IN \$seeds
   RETURN n.name AS name, n.fqn AS fqn, n.file_path AS file
   ORDER BY n.name, n.fqn LIMIT 40',
  '{\"seeds\": [\"authenticate\", \"verify_token\"]}',
  '{\"name\": \"string\", \"fqn\": \"string\", \"file\": \"string\"}')"
```

Three outcomes, three different actions:

- **One row per seed.** Expand.
- **No row for a seed.** The join key is probably wrong — the corpus's
  `title` is not the graph's `name`. Fix the key rather than widening the
  search.
- **Many rows for one seed** — the common case, and the dangerous one.
  `WHERE s.name IN $seeds` then expands from ALL of them and merges
  unrelated entities into one answer, with nothing in the output saying so.
  Measured on a 109k-vertex code graph: `main` resolves to **81** distinct
  functions, `wrapper` to 36, `test_resources_dir` to 33. A blast radius
  "for `main`" built on the name is 81 unrelated functions' callers reported
  as one number.

  So do not expand on the name. Traverse from the unique identity instead —
  `WHERE s.fqn IN $seeds`, since `fqn` carries the module path and `name`
  does not. If the question really is about all of them, say that in the
  answer and group by `fqn` so the reader can see it is a union. When the
  question does not settle which one, show the candidate list with its
  files and ask; picking the first row is choosing for the user without
  telling them.

### 3. Hop 2 — expand from the seeds

Now the traversal, with the seeds as params. The shape that answers most
connection questions:

```bash
skardi query --table -e "SELECT * FROM cypher_query('kg',
  'MATCH (s:Function)<-[r:CALLS]-(caller) WHERE s.fqn IN \$seeds
   RETURN s.name AS seed, caller.fqn AS caller, r.resolution AS confidence
   ORDER BY s.name, caller.fqn LIMIT 200',
  '{\"seeds\": [\"pkg.auth.authenticate\"]}',
  '{\"seed\": \"string\", \"caller\": \"string\", \"confidence\": \"string\"}')"
```

Note what the edge variable `r` is doing there. Binding it is not decoration:
without it the traversal can neither filter nor report edge confidence, and
on a real code graph most edges are guesses. Measured: of 658,846 `CALLS`
edges, **467,467 (71%) carry `resolution: "ambiguous"`** — matched on a bare
name alone. An unfiltered, unreported expansion is therefore mostly noise
presented as fact. Return the field (as above) and split the answer by it, or
filter to `WHERE r.resolution <> 'ambiguous'` and say you did.

Read `references/patterns.md` for the four recipes this generalizes — seed
and expand, entity neighbourhood, path between two things, and impact /
blast radius — each with the Cypher and the `columns` declaration written
out.

### 4. Synthesize, and show both halves

The answer is not the row dump. Say what the relationships mean for the
question, then attach the evidence — see "Reporting" below.

## The traps

These produce **confidently wrong answers** rather than errors. Apply every
one of them while writing the query, not after something looks off. Why each
holds, and what was measured, is in `references/traps.md`; read it before
deciding a rule does not apply to your graph.

- **`columns` binds positionally against `RETURN`.** Declare the columns by
  reading your own `RETURN` clause left to right. Two same-typed columns out
  of order swap silently.
- **The getter must match the stored type.** `json_get_str` on a number
  returns NULL, not an error. `->` / `->>` / `?` are not installed; use the
  getter UDFs.
- **Bound the view inside its Cypher.** A SQL `WHERE` on a graph view does not
  push into the Cypher, so an unbounded view fails `RowCapExceeded` even for a
  one-row question. A SQL `LIMIT` does push, but do not rely on callers to
  pass one.
- **Check what the edges carry before trusting an expansion.** Once per
  session, read the edge properties (`keys(r)`). If they record a confidence,
  provenance or resolution field, filter on it and say which subset you used;
  if you keep the low-confidence edges, report the split, not the total.
- **Label the seed and keep the traversal in one `MATCH`.** `MATCH
  (s:Function)<-[:CALLS]-(c) WHERE s.name IN $seeds` answers; an unlabeled
  seed or a two-`MATCH` form scans the whole graph and does not return.
- **Bound the expansion three ways at once:** a small seed set, a `LIMIT`
  inside the Cypher, and one relationship type and direction per call (never
  `-[r]-`). Raise depth one hop at a time; when a call times out, narrow the
  pattern instead of retrying. Sort inside the Cypher (`ORDER BY`) when you
  need the most important neighbours, not an arbitrary slice.
- **A truncated result is not a result.** Ad-hoc queries cap at 1000 rows and
  the notice goes to stderr. Push counts into the Cypher (`count(*)`,
  `collect()`) instead of counting returned rows.

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

Two of these are things you can read. The rest arrive as the same
`query_execution_error` (HTTP 500), so the symptom column is what you
*observe*, not what the error says.

| What you see | Meaning | Do |
|---|---|---|
| exit code 2 | server unreachable | Report the URL you tried. Do not retry in a loop; do not start a server. Then **answer the question with the ordinary local-code tools** unless the user asked for graph data specifically — the same handling as a graph that turns out to be a different project. An unreachable graph is a reason this skill cannot help, not a reason the question goes unanswered. |
| `sql_validation_error` (HTTP 400) | a write, multiple statements, or an unescaped `'` in the params or Cypher literal | This one names its own cause. Send string values as parameters; double every `'` in the serialized params. |
| `query_execution_error` (HTTP 500) | could be nine different things, and the text says none of them | **Bisect.** Strip to `graph_schema('kg')`, then the simplest one-column traversal, then add back the label, the relationship, the `WHERE`, the params, and each `RETURN` column one at a time. The first call that fails names the cause. Procedure and the shortlist of usual culprits: `references/troubleshooting.md`. |
| a column is all NULL, query succeeded | wrong getter for the stored JSON type | Ask for `properties(n)` as `json`, read the real types off it, then pick the matching getter. |
| the expansion returns 0 rows | seeds do not resolve, or the arrow points the wrong way | Re-run the seed-resolution check; then try the opposite direction. Not an error. |
| the same question failed 3 times | you are guessing | Stop. Show what you tried, what came back, and your best hypothesis of what is missing. |

Do not read the 500's text as a diagnosis, and do not conclude from it that a
label or connection name is wrong. `RowCapExceeded`, an arity mismatch, a
params argument that is not an object, a declared type that does not match the
data, and three Cypher constructs this AGE build does not support all produce
exactly that string. Bisecting separates them; re-reading the message does not.

An honest "here is where it stopped" beats a fourth guess. Read
`references/troubleshooting.md` for the full procedure and the log lines to ask
an operator for.
