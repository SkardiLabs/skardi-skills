# Symptom → cause, for graph-RAG queries

Two classes, and neither is the one you would expect. The dangerous class is
the queries that **succeed and return a plausible wrong answer**, so this file
leads with it. The other class is not "failures that announce themselves":
almost every graph failure reaches you as one indistinguishable HTTP 500, so
the second half is mostly about how to localize a cause the error text does
not name.

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

### A column holds `["a","b"]` where you wanted `"ab"`

`||` is not string concatenation in this Cypher. It builds a LIST, so
`'m' || toString(i)` yields `["m","1"]`. String concatenation is `+`:
`'m' + toString(i)`. Measured.

Nothing complains at the point of the mistake. What you get instead is a
column of arrays, and then either a declared-type failure three steps later
(the opaque 500, if you declared it `string`) or, if you declared it `json`,
an answer full of one-element lists that reads as data. Check a single row's
type before you build anything on a concatenated property.

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

## Failures you can read, and the one that tells you nothing

Only three things reach the caller, and the third is doing almost all the work.
Measured against a live server built from `main`:

**`sql_validation_error` (HTTP 400) carries its detail.** You get the parser's
own message and a column number:

```
error: [sql_validation_error] SQL parse error: sql parser error:
Expected: ), found: ambiguous at Line: 3, Column: 28 (HTTP 400)
```

Two causes. **Policy**: the statement was DDL, COPY, a write, or multiple
statements. Graph work is read-only; rewrite as a single `SELECT`. **Or an
unescaped quote**, which `Expected close delimiter` names. The params object
and the Cypher both arrive as single-quoted SQL string literals, so any `'`
inside either one ends it early. Cypher parameters do not help; they protect
the Cypher, and this is the SQL one layer out. Double every `'` in the
serialized params, and send string values as parameters rather than writing
them into the Cypher (`patterns.md`, "Joining the two hops back together").

Treat an apostrophe in a *seed* as a signal, not just a parse error. A real
entity name in a code graph has none, so a seed carrying one is a wrong join
key or a corpus supplying hostile text. The same hole, crafted rather than
accidental, parses cleanly and returns rows from a different source instead
of failing.

**Exit code 2 means the server was unreachable.** The message names the URL it
tried. This is an environment problem, not a query problem: report the URL and
stop, do not retry in a loop, and do not start a server as a side effect of
answering a question.

**Everything else is one string.** Every graph failure that happens after
planning begins arrives as:

```
error: [query_execution_error] SQL query execution failed; see server logs for details (HTTP 500)
```

That is deliberate on the server's side, not a bug: a raw engine error can
quote row values and internal schema, so the response stays generic. The
consequence for you is that the error text is not a symptom you can look up.
Ten distinct causes were measured behind that one string, including a wrong
connection name, a `columns` count that does not match `RETURN`, a params
argument that is not a JSON object, `RowCapExceeded`, a Cypher construct the
AGE build does not support, and a column whose declared type does not match
what the backend returned. The server log distinguishes them; you may not have
it.

**A timeout is one of the ten, and it is the one that punishes the natural
reaction.** The server logs `graph query timed out after Ns (the source's
query_timeout_seconds); narrow the traversal or raise the timeout`, and you
receive the same generic 500 as a typo in a label. So the instinct is to
re-check the label and run it again, which is exactly what you must not do:
re-running an expensive traversal degrades the graph backend for everyone else
using it. If a call takes several seconds and then fails, treat it as a
timeout until proven otherwise, and NARROW it — one relationship type, one
direction, a smaller seed set, one hop less. On a dense graph an unlabeled
scan or an undirected untyped `-[r]-` will always land here.

### So bisect, do not guess

This is the only method available, and it localizes almost anything in two or
three calls. Strip the statement to the smallest thing that could possibly
work, then add back one piece at a time.

```bash
# 1. Does the connection resolve at all? Nothing else matters until it does.
skardi query --table -e "SELECT * FROM graph_schema('kg')"

# 2. Simplest possible traversal, one declared column, no params.
skardi query --table -e "SELECT * FROM cypher_query('kg',
  'MATCH (n) RETURN n.name AS name LIMIT 1', '{}', '{\"name\": \"string\"}')"

# 3. Add the label, then the relationship, then the WHERE, then the params,
#    then each extra RETURN column with its columns entry. One at a time.
```

The call that first fails names the cause. Some shortcuts worth knowing,
because they are what the added pieces usually break on:

- **A `columns` entry per `RETURN` expression, in `RETURN` order.** A count
  mismatch is checked by AGE and a wrong order is not, so this is worth
  reading off your own `RETURN` clause rather than testing.
- **A map- or list-valued projection needs type `json`.** `properties(n)`,
  `keys(r)`, `labels(n)`, `collect(...)` and `nodes(p)` all return a JSON
  container. Declaring one `string` fails here.
- **A numeric property declared `string` fails too.** Ask for
  `properties(n)` as `json` first and read the types off it.
- **`params` must be a JSON object at the top level.** Arrays go inside it as
  values: `'{"seeds": ["a","b"]}'`, never `'["a","b"]'`.
- **Three Cypher constructs are simply not available on this AGE build**, and
  each one fails with the opaque 500 rather than saying so: `shortestPath(...)`
  (use a bounded variable-length match sorted by `length(p)`), a list
  comprehension such as `RETURN [x IN nodes(p) | x.name]` (return
  `nodes(p)` whole and pick fields client-side), and `ORDER BY count(*)` after
  a `WITH` (order by the alias the `WITH` introduced). If a query fails and the
  simpler form works, suspect the construct before you re-check your labels.
- **`RowCapExceeded` hides here too.** A SQL `WHERE` over a graph view does not
  push into the view's Cypher, so the view materializes fully first and even a
  one-row question fails. The bound has to be inside the Cypher: a `LIMIT`, a
  named relationship type instead of `-[r]-`, and a bounded path length
  (`*1..3`, never `*`). If step 2 above succeeds and adding the real pattern
  fails, this is the first thing to suspect on a dense graph.

### The expansion returns 0 rows

Not an error, and worth reading correctly. Two causes, in this order:

1. **The seeds do not resolve.** Retrieval returned corpus identifiers (a
   document title) and the graph indexes something else (a name, a path, an
   id). Run the seed-resolution check from SKILL.md; if it returns nothing,
   the join key is wrong. Fix the key, do not widen the search.
2. **The arrow points the wrong way.** Try the opposite direction before
   concluding there is no connection.

On a bounded path query, 0 rows means "no path within the bound", which is
evidence about the graph rather than a reason to start rewriting Cypher.

### What to ask an operator for

When you have bisected to a call that fails and cannot tell why, the server
log has the answer and it is usually specific enough to act on immediately.
Ask for the `ERROR` line matching your request. These are the shapes it takes:

| server-side log text | what it means |
|---|---|
| `graph connection 'x' is not registered (known connections: …)` | wrong first argument to `cypher_query` |
| `[42804] return row and column definition list do not match` | `columns` count ≠ `RETURN` count |
| `cypher_query 'params' must be a JSON object, got an array` | params is not an object |
| `graph scan exceeded max_rows = N` | `RowCapExceeded`; bound inside the Cypher |
| `graph column 'c' … declared 'string' but the backend returned a number` | wrong declared type; the message names the fix |
| `[42601] syntax error at or near "…"` | unsupported construct or a typo in the Cypher |
| `[42703] could not find rte for <name>` | `ORDER BY` naming a `RETURN` alias; order by the expression instead |
| `[42704] could not find properties for x` | a list comprehension or `all(...)` over a path |
| `cypher_query is read-only: the keyword 'X' at byte N is not allowed` | a write or `CALL` in the Cypher |

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
