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
  'MATCH (s:Function)<-[:CALLS]-(caller) WHERE s.name IN \$seeds
   RETURN s.name AS seed, caller.name AS caller, labels(caller)[0] AS kind
   ORDER BY s.name, caller.name
   LIMIT 200',
  '{\"seeds\": [\"authenticate\", \"verify_token\"]}',
  '{\"seed\": \"string\", \"caller\": \"string\", \"kind\": \"string\"}')"
```

Two things to get right:

- **The arrow direction is the question.** `(s)<-[:CALLS]-(caller)` is "who
  calls s" — incoming. `(s)-[:CALLS]->(callee)` is "what s calls" — outgoing.
  These answer opposite questions and both return confident, plausible rows.
- **`ORDER BY` before `LIMIT`.** Without it the 200 rows are an arbitrary
  slice, which reads as an answer and is not one.
- **`ORDER BY` takes the EXPRESSION, not the `RETURN` alias.** `ORDER BY
  seed, caller` fails on AGE with `could not find rte for seed` (SQL state
  42703) even though `seed` is right there in the `RETURN`. Sort by what the
  alias was computed from — `ORDER BY s.name, caller.name` — and for an
  aggregate by the aggregate itself, `ORDER BY count(*) DESC`. Measured; the
  alias form looks correct and is rejected.

When you need "the most connected" neighbours rather than an alphabetical
slice, sort on degree computed inside the Cypher:

```
MATCH (s:Function)<-[:CALLS]-(caller) WHERE s.name IN $seeds
WITH caller, count(*) AS weight
RETURN caller.name AS caller, weight
ORDER BY weight DESC LIMIT 25
```

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

```bash
skardi query --table -e "SELECT * FROM cypher_query('kg',
  'MATCH p = shortestPath((a:Function)-[:CALLS*..4]-(b:Function))
   WHERE a.name IN \$from AND b.name IN \$to
   RETURN a.name AS src, b.name AS dst, length(p) AS hops
   LIMIT 20',
  '{\"from\": [\"handle_login\"], \"to\": [\"write_audit_row\"]}',
  '{\"src\": \"string\", \"dst\": \"string\", \"hops\": \"int\"}')"
```

`*..4` is a real bound, not decoration: raise it one hop at a time and watch
the time. If `shortestPath` is unavailable on the backend, fall back to a
fixed-length probe (`-[:CALLS*2]-`) and report the depth you searched — "no
path within 4 hops" is a useful answer and an honest one; "no connection" is
a claim you did not test.

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
   actually indexes entities by. This is rarely the corpus's title; it is
   usually a name, a path, or an id. If you do not know, run the
   seed-resolution check from SKILL.md against both candidates and use
   whichever resolves.
2. Build the params object with that array: `{"seeds": ["a", "b", "c"]}`.
3. Keep it small (5–20). Seeds multiply through the expansion.

**Never build the array by string-concatenating retrieved text into the
Cypher literal.** The Cypher is a plan-time literal behind a keyword guard,
and retrieved text is untrusted input; params exist for exactly this.

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
