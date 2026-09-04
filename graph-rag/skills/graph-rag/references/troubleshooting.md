# Symptom → cause, for graph-RAG queries

Ordered by how often each one bites, and separated into the two classes that
matter: failures that **announce themselves**, and failures that **return
plausible wrong answers**. The second class is the dangerous one — it is why
this file leads with it.

## Silent wrong answers

### A column is entirely NULL, but the query succeeded

The getter does not match the property's stored JSON type. `json_get_str` on
a numeric property returns NULL — not an error, not a coercion. Check what
the property actually holds, then pick the getter for that type.

**Ask for the whole property bag as `json`** — not the one property declared
as `string`. `columns` is validated against the returned JSON kind, so
declaring a numeric property `"string"` fails with an opaque
`query_execution_error` (HTTP 500) rather than showing you `42` vs `"42"`.
Measured, and worth knowing precisely because that is the same error this
file attributes below to a wrong connection name or a missing label: the
shortcut does not merely fail, it points at the wrong cause.

```bash
skardi query --table -e "SELECT * FROM cypher_query('kg',
  'MATCH (n:Function) WHERE n.name IN \$seeds RETURN properties(n) AS p LIMIT 3',
  '{\"seeds\": [\"authenticate\"]}',
  '{\"p\": \"json\"}')"
```

Every property comes back with its JSON type visible — `"line_start":51`
unquoted is a number, `"name":"authenticate"` quoted is a string — and that
is what tells you the getter. Read the single property back afterwards with
the type it actually has (`'{"raw": "int"}'`) to confirm.

`->` / `->>` / `?` are deliberately not installed — the rewrite would break
federated pushdown session-wide — so the getter UDFs are the route.

### Two columns hold each other's values

`columns` binds positionally against `RETURN`, and two same-typed columns
declared out of order swap with no error. Read the `RETURN` clause left to
right and rewrite the declaration against it. See the mechanical procedure at
the end of `patterns.md`.

### The answer merges two unrelated things

The seed was a NAME, and the name is not unique. `WHERE s.name IN $seeds`
expands from every node that matches, and the result carries nothing to say
it did: `main` matches 81 distinct functions on a 109k-vertex code graph,
`wrapper` 36. A caller list or blast radius built that way is a union of
unrelated entities presented as one.

Re-run the seed check returning `n.fqn` and `n.file_path` alongside the name.
More than one row for a seed means pick one and traverse from `s.fqn`, or
report the union explicitly and group by it.

### The blast radius is enormous and mostly unfamiliar

The traversal bound no edge variable, so the answer includes every
low-confidence guess. On this graph 71% of `CALLS` edges carry
`resolution: "ambiguous"` — matched on a bare name. Bind the edge
(`[r:CALLS]`), then either filter (`WHERE r.resolution <> 'ambiguous'`) or
return `r.resolution` and split the answer by it. Say which you did.

### The rows look right but the answer is backwards

The arrow direction. `(s)<-[:CALLS]-(x)` is "who calls s"; `(s)-[:CALLS]->(x)`
is "what s calls". Both return confident rows. Restate the question as a
sentence with an explicit direction ("callers OF authenticate") and check the
arrow against it.

### A count or an "all of X" claim is wrong

The row cap bit and the note went to **stderr**, not stdout:

```
note: results truncated; pass a higher --max-rows to see the rest
```

A truncated set looks complete if you only read the rows. Push the
aggregation into the Cypher (`count(*)`, `collect()`, `WITH DISTINCT`) rather
than counting rows client-side — the cap corrupts client-side aggregation
silently, and a bigger `--max-rows` only moves the threshold.

### The sample is unrepresentative

`LIMIT` without `ORDER BY` returns an arbitrary slice. On a dense
relationship that slice is whatever the planner reached first, which has no
relationship to importance. Sort inside the Cypher — by a computed degree, a
score, or at minimum a name so the slice is at least reproducible.

## Failures that announce themselves

### `RowCapExceeded`

The Cypher itself is unbounded. A SQL `WHERE` over a graph view does **not**
push into the view's Cypher — the view materializes fully first, so even a
one-row question fails. The bound has to be inside the Cypher: a `LIMIT`, a
named relationship type instead of `-[r]-`, and a bounded path length
(`*..3`, never `*`).

### The expansion returns 0 rows

Two causes, in this order:

1. **The seeds do not resolve.** Retrieval returned corpus identifiers (a
   document title) and the graph indexes something else (a name, a path, an
   id). Run the seed-resolution check from SKILL.md; if it returns nothing,
   the join key is wrong — fix the key, do not widen the search.
2. **The arrow points the wrong way.** Try the opposite direction before
   concluding there is no connection.

### `could not find rte for <name>` (SQL state 42703)

`ORDER BY` naming a `RETURN` alias. AGE resolves the sort key against the
match, not against the projection, so an alias that is visibly present in the
`RETURN` clause is still undefined to the sort. Order by the expression the
alias came from (`ORDER BY s.name`), or by the aggregate itself
(`ORDER BY count(*) DESC`).

**The reverse holds after a `WITH`, and gets a different error.** A variable
`WITH` introduced must be sorted BY NAME: after
`WITH caller, count(*) AS weight`, `ORDER BY weight DESC` is correct, and
restating `ORDER BY count(*) DESC` there fails with an opaque HTTP 500
instead of 42703 — the aggregate no longer exists in that scope. So "never
use the alias" is the rule for a `RETURN` projection only; `WITH` creates a
real binding and the alias is then the only handle you have.

### `cypher_query` errors on arity

The number of `columns` entries does not equal the number of `RETURN`
expressions. AGE must declare its result arity, so this is checked — the one
declaration mistake that fails loudly rather than silently.

### `cypher_query 'params' must be a JSON object, got …`

The params argument has to be an object at the top level. Arrays go
**inside** it as values — `'{"seeds": ["a","b"]}'`, not `'["a","b"]'`.

### `sql_validation_error` (HTTP 400)

Two quite different causes.

**Policy.** The statement was DDL, COPY, a write, or multiple statements.
Graph work is read-only; rewrite as a single `SELECT`. If the task genuinely
needs a write, it belongs to the job path, not here.

**Or an unescaped quote in a seed** — `Expected close delimiter` names this
one. The params object is a single-quoted SQL string literal, so a value
containing `'` ends it early. Cypher parameters do not help: they protect the
Cypher, and this is the SQL one layer out. Double every `'` in the serialized
params before it goes into the statement (`patterns.md`, "Joining the two
hops back together" has the procedure).

Treat it as a signal, not just a parse error. A real entity name in a code
graph has no apostrophe, so a seed carrying one is a wrong join key or a
corpus supplying hostile text — and the same hole, crafted rather than
accidental, parses cleanly and returns rows from a different source instead
of failing.

### `query_execution_error` (HTTP 500) from a supported-looking Cypher feature

Three measured cases where the natural way to write it is simply not
available on the AGE build, and the error says nothing:

- **`shortestPath(...)`** — fails with any projection, `length(p)` alone
  included. Use a bounded variable-length match sorted by `length(p)`.
- **A list comprehension**, e.g. `RETURN [x IN nodes(p) | x.name]` — the
  obvious way to keep a path projection small. Return `nodes(p)` /
  `relationships(p)` whole as `json` and pick fields client-side.
- **`ORDER BY count(*)` after a `WITH`** — see the 42703 entry above; past
  a `WITH` the aggregate is out of scope and only the alias works.

The lesson generalizes: on this backend an opaque 500 is at least as likely
to be an unsupported construct as a wrong name. Before re-checking your
labels, strip the query to its simplest form and add pieces back — that
localizes it in two or three calls.

### `query_execution_error` (HTTP 500) with no detail

Usually a wrong connection name in `cypher_query`'s first argument, or a
label/property that does not exist. Re-check the connection name against
`skardi schema`, and the vocabulary against `graph_schema`. The precise cause
is in the server logs, which the operator has and you may not.

### exit code 2

The server was unreachable. Report the URL you tried and stop — this is an
environment problem, not a query problem, and retrying in a loop does not
fix it. Do not start a server as a side effect of answering a question.

## When the source itself is the problem

A `degraded` or `refused` graph source is not this skill's repair job — hand
it to `graph-source`, which owns registration health. The short version so
you can recognize which you have:

- **`refused`** (server does not start): the backend answered and a
  declaration is broken, or credentials/AGE/the graph are wrong. A bug in the
  deployment, not an outage.
- **`degraded`**: genuine connectivity failure only. Views still register on
  their declared schemas and the first scan retries; any server response
  flips it healthy.

Graph health also surfaces through `GET /data_source`. Note that the
skardi-cloud gateway's projection deliberately hides `status_reason` from
tenants — operator text, a DSN, a hostname — so a cloud tenant sees less
detail here than a self-hosted operator does.

## When you have failed three times

Stop and report. Show the seeds you used and where they came from, the
traversal you ran with its bound, what came back, and your best hypothesis of
what is missing — a source not registered, a join key you cannot find, a
relationship type that does not exist in this graph. An honest account of
where it stopped is more useful than a fourth guess, and it hands the user
the one piece of context they have and you do not.
