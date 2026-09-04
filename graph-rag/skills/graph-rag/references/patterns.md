# The four graph-RAG recipes

Read this when you know what the question needs and want the call written
correctly the first time. Each recipe gives the Cypher, the `columns`
declaration, and the bound — the three things that have to agree.

Contents:

- [Before any recipe: learn the shape](#before-any-recipe-learn-the-shape)
- [1. Seed and expand](#1-seed-and-expand) — "what depends on X"
- [2. Entity neighbourhood](#2-entity-neighbourhood) — "tell me about X"
- [3. Path between](#3-path-between) — "how is A related to B"
- [4. Impact / blast radius](#4-impact--blast-radius) — "what breaks if"
- [Joining the two hops back together](#joining-the-two-hops-back-together)
- [The columns declaration, mechanically](#the-columns-declaration-mechanically)

All examples use `kg` as the connection name and shell-escape the inner
quotes for `skardi query -e`. Substitute your own labels — the vocabulary is
a deployment fact.

## Before any recipe: learn the shape

Two calls, once per session. The first gives the vocabulary, the second the
topology and — the part that decides your bounds — the edge counts.

```bash
skardi query --table -e "SELECT * FROM graph_schema('kg')"

skardi query --table -e "SELECT * FROM cypher_query('kg',
  'MATCH (a)-[r]->(b)
   RETURN labels(a)[0] AS from_label, type(r) AS rel, labels(b)[0] AS to_label,
          count(*) AS n
   ORDER BY count(*) DESC LIMIT 40',
  '{}',
  '{\"from_label\": \"string\", \"rel\": \"string\", \"to_label\": \"string\", \"n\": \"int\"}')"
```

The result is a map of what connects to what, and how heavily. A relationship
with hundreds of thousands of edges is one you must always name explicitly and
bound; a relationship with a few thousand can be walked more freely.

## 1. Seed and expand

The default recipe. "What depends on X", "who calls into this", "which
documents reference these entities".

```bash
skardi query --table -e "SELECT * FROM cypher_query('kg',
  'MATCH (s:Function)<-[r:CALLS]-(caller) WHERE s.fqn IN \$seeds
   RETURN s.name AS seed, caller.fqn AS caller, r.resolution AS confidence
   ORDER BY s.name, caller.fqn
   LIMIT 200',
  '{\"seeds\": [\"pkg.auth.authenticate\"]}',
  '{\"seed\": \"string\", \"caller\": \"string\", \"confidence\": \"string\"}')"
```

Four things to get right, and the first two are the ones that quietly ruin
an answer rather than failing:

- **Bind the edge and carry its confidence.** `[r:CALLS]` rather than
  `[:CALLS]`, and `r.resolution` in the projection. Without the variable the
  traversal cannot filter or report, and on a real code graph most edges are
  guesses: of 658,846 `CALLS` edges here, **467,467 (71%) are
  `resolution: "ambiguous"`** — matched on a bare name only. Copying a
  recipe that drops the edge produces exactly the mostly-noise blast radius
  the rest of this skill warns about. Either return the field and split the
  answer by it, or add `WHERE r.resolution <> 'ambiguous'` and say which
  subset you used. If the graph's edges carry no such property, say that
  instead — "unfiltered because the edges carry no confidence" is a fact
  about the graph, not a gap in the answer.
- **Seed on the unique identity, not the name.** `s.fqn`, not `s.name`.
  `main` matches 81 distinct functions on this graph; expanding from the
  name merges their callers into one answer with nothing marking the union.
- **The arrow direction is the question.** `(s)<-[:CALLS]-(caller)` is "who
  calls s" — incoming. `(s)-[:CALLS]->(callee)` is "what s calls" — outgoing.
  These answer opposite questions and both return confident, plausible rows.
- **`ORDER BY` before `LIMIT`** — without a sort the 200 rows are an
  arbitrary slice that reads as an answer.
- **What `ORDER BY` may name has two halves, and they point opposite ways.**
  Both measured, and each looks correct while failing the other's case:
  - **Sorting a `RETURN` projection: use the EXPRESSION, not the alias.**
    `ORDER BY seed, caller` fails with `could not find rte for seed` (SQL
    state 42703) even though `seed` is right there in the `RETURN` — AGE
    resolves the sort key against the match, not the projection. Sort by
    what the alias was computed from (`ORDER BY s.name, caller.fqn`), and
    for an aggregate in the same clause by the aggregate itself
    (`ORDER BY count(*) DESC`).
  - **Sorting something a `WITH` introduced: use the VARIABLE.** After
    `WITH caller, count(*) AS weight`, `ORDER BY weight DESC` is correct and
    `ORDER BY count(*) DESC` fails with an opaque HTTP 500 — the aggregate
    is out of scope past the `WITH`, and the variable is the only handle
    left. `WITH` creates a real binding; a `RETURN` alias does not.

When you need "the most connected" neighbours rather than an alphabetical
slice, sort on degree computed inside the Cypher — and keep the confidence
filter, or the degree is a ranking of guesses:

```
MATCH (s:Function)<-[r:CALLS]-(caller)
WHERE s.fqn IN $seeds AND r.resolution <> $ambiguous
WITH caller, count(*) AS weight
RETURN caller.fqn AS caller, weight
ORDER BY weight DESC LIMIT 25
```

`ORDER BY weight`, and **not** `ORDER BY count(*)`, once a `WITH` is in the
way — see the ordering rule above, whose two halves pull in opposite
directions exactly here.

## 2. Entity neighbourhood

"Tell me about X" — one entity, its immediate surroundings, grouped rather
than enumerated. Aggregate **inside** the Cypher so the row cap cannot
truncate a count.

```bash
skardi query --table -e "SELECT * FROM cypher_query('kg',
  'MATCH (s:Function)-[r:CALLS]->(n) WHERE s.name IN \$seeds
   RETURN type(r) AS rel, labels(n)[0] AS kind, count(*) AS n
   ORDER BY count(*) DESC LIMIT 50',
  '{\"seeds\": [\"authenticate\"]}',
  '{\"rel\": \"string\", \"kind\": \"string\", \"n\": \"int\"}')"
```

**An undirected, untyped `-[r]-` is not safe here** — measured on a
109k-vertex / 800k-edge graph it did not return, even with the label and a
`LIMIT`. Name one relationship type and one direction per call and run it
once per type you care about; the per-type results are also easier to read
than a mixed aggregate. Follow up with recipe 1 on whichever relationship the
counts show matters.

## 3. Path between

"How is A related to B", "is there a connection between these two". Bound the
path length explicitly — an unbounded variable-length match on a dense graph
does not come back.

**Return the path, not just its length.** "How is A related to B" is a
question about the middle, and `length(p)` throws the middle away: hop count
alone gives you nothing to explain the connection with, and nothing to check
it against. Project the nodes and the relationships, both as `json`:

```bash
skardi query --table -e "SELECT * FROM cypher_query('kg',
  'MATCH p = (a:Function)-[:CALLS*1..3]-(b:Function)
   WHERE a.fqn IN \$from AND b.fqn IN \$to
   RETURN length(p) AS hops, nodes(p) AS path, relationships(p) AS edges
   ORDER BY length(p) LIMIT 20',
  '{\"from\": [\"pkg.web.handle_login\"], \"to\": [\"pkg.audit.write_audit_row\"]}',
  '{\"hops\": \"int\", \"path\": \"json\", \"edges\": \"json\"}')"
```

**A bounded variable-length match, not `shortestPath`.** `shortestPath` is
the textbook answer and it fails with an opaque `query_execution_error`
(HTTP 500) on the AGE build measured here — with any projection, including
`length(p)` alone, so it is the function and not the columns. Sort by
`length(p)` and take the first rows instead; that gives you the shortest of
the paths found within the bound, which is the answer the question wanted.
Both directed (`-[:CALLS*1..3]->`) and undirected (`-[:CALLS*1..3]-`) forms
work; undirected is usually right for "how are these related", directed for
"does A reach B".

`nodes(p)` returns the ordered vertices with their full `properties` — the
intermediate hops the answer is actually about — and `relationships(p)`
returns each edge with ITS properties, which is where per-hop confidence
lives. Verified on AGE: a 2-hop result came back with both intermediate
nodes and both edges carrying `resolution: "same_scope"`, so the answer can
say not just "connected in 2 hops" but through what, and how well each hop
is known. A path whose middle edge is `ambiguous` is a weaker claim than one
whose hops are all `import`, and only this projection can tell them apart.

**Do not try to trim the output with a list comprehension.**
`RETURN [x IN nodes(p) | x.name] AS path` is the obvious way to ask for just
the names, and it fails with an opaque `query_execution_error` (HTTP 500) —
measured. Take the full `nodes(p)` JSON and pick the fields you want on the
client side.

`*1..3` is a real bound, not decoration: raise it one hop at a time and
watch the time. Report the depth you searched — "no path within 3 hops" is a
useful answer and an honest one; "no connection" is a claim you did not test.

And read 0 rows correctly: it means "no path within the bound", not "the
query is wrong". Measured both ways on the same statement — one pair of
seeds returned nothing at `*1..3` while another returned a 2-hop path, so an
empty result is evidence about the graph, not a reason to start rewriting
Cypher.

## 4. Impact / blast radius

"What breaks if we change X." Transitive closure, which is where fan-out
explodes — so bound the depth AND deduplicate, and return a count alongside
the sample so the reader knows whether they are seeing all of it.

```bash
skardi query --table -e "SELECT * FROM cypher_query('kg',
  'MATCH (s:Function)<-[:CALLS*1..3]-(dep) WHERE s.name IN \$seeds
   WITH DISTINCT dep
   RETURN labels(dep)[0] AS kind, count(*) AS n
   ORDER BY count(*) DESC LIMIT 20',
  '{\"seeds\": [\"verify_token\"]}',
  '{\"kind\": \"string\", \"n\": \"int\"}')"
```

Run the aggregate first to size the answer, then the enumerated version with
a `LIMIT` if the totals are small enough to be worth listing. Reporting "142
dependents across 3 kinds, here are the 20 nearest" is honest; listing 20 and
implying that is all of them is not.

## Joining the two hops back together

Hop 1 gives rows; hop 2 needs a JSON array literal. The seam is yours:

1. Read hop 1's rows and take the **join key** — the property the graph
   actually indexes entities by, and one that is UNIQUE. This is rarely the
   corpus's title, and on a code graph it is rarely `name` either: `main`
   matches 81 nodes here. Prefer `fqn` or an id. If you do not know, run the
   seed-resolution check from SKILL.md against both candidates and use
   whichever resolves to one row per seed.
2. Build the params object with that array: `{"seeds": ["a", "b", "c"]}` —
   with a JSON serializer, then SQL-escaped. See below; this step is where
   the whole thing goes wrong.
3. Keep it small (5–20). Seeds multiply through the expansion.

**Never string-concatenate retrieved text into the Cypher literal** — the
Cypher is a plan-time literal behind a keyword guard, and retrieved text is
untrusted input. Params are what that rule points you to.

**And params are not, by themselves, enough.** The params object is
delivered as a single-quoted SQL string literal, so a seed containing `'`
closes it early — Cypher parameters do nothing about the SQL one layer out,
and `skardi query` offers no parameter binding to fall back on (`-e` and
`-f` both take SQL text). Two measured outcomes from the same cause:

- An ordinary name with an apostrophe fails the whole statement:
  `sql_validation_error: Expected close delimiter`.
- A crafted seed reshapes the read. This one, pasted into the params literal
  as these examples build it, returned three rows from a *different source*
  under the traversal's own column name:

  ```
  x"]}', '{"name": "string"}') UNION ALL SELECT title FROM docs.main.docs LIMIT 3 --
  ```

  The read-only guards still hold — single statement, no DDL, no writes — so
  the ceiling is reading another source the token already permits. It is
  still not the answer you reported.

So serialize both `params` and `columns` with a JSON serializer, double every
`'` in each serialized string, and write the statement to a file for `-f` so
the shell stops being a third layer:

```python
def sql_str(s):
    return "'" + s.replace("'", "''") + "'"

sql = (f"SELECT * FROM cypher_query('kg', {sql_str(cypher)}, "
       f"{sql_str(json.dumps({'seeds': seeds}))}, "
       f"{sql_str(json.dumps(columns))})")
```

Verified: with that escaping the crafted seed above yields zero rows and no
error, which is the correct answer — it is a name that does not exist. And
since a real entity name in a code graph does not contain a quote, a seed
that needs the escaping is worth flagging as a bad join key or a hostile
corpus rather than silently cleaning up.

If the two hops disagree — a retrieved document names an entity the graph
does not contain — that is a finding, not an error to smooth over. Report the
unresolved seeds and what they were. It usually means the corpus or the graph
is stale, and which one is a question only the user can answer.

## The columns declaration, mechanically

`columns` is a JSON object of name → type, and it binds **positionally**
against the `RETURN` clause. The names are labels for SQL; the order is the
contract.

To write it without error, read your own `RETURN` left to right and emit one
entry per projected expression, in that order:

```
RETURN s.name AS seed, caller.name AS caller, count(*) AS n
        ↓                    ↓                    ↓
'{"seed": "string", "caller": "string", "n": "int"}'
```

Two same-typed columns declared out of order swap silently — no error,
no type mismatch, and nothing downstream can detect it. The count must match
too: AGE requires declared arity, so a mismatch is a targeted error rather
than a silent one (the one failure here that does announce itself).

**A map- or list-valued projection needs type `json`.** `properties(n)`,
`keys(r)`, `labels(n)` and `collect(...)` all return a JSON container, and
declaring them `string` / `map` / `object` / `any` fails with an opaque
`query_execution_error` (HTTP 500) rather than a type error. Two separate
test runs hit this independently while trying to inspect a node's properties,
which is the first thing you do on an unfamiliar graph — so it is worth
knowing before you need it:

```
RETURN keys(r) AS k        →  '{"k": "json"}'
RETURN properties(n) AS p  →  '{"p": "json"}'
```
