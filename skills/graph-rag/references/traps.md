# The traps, with the evidence

`SKILL.md` lists these as one-line rules. This file keeps why each one holds
and what was measured, for when a rule seems not to apply to the graph in
front of you, or before you change one of them.

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

A SQL `LIMIT` behaves differently, and the difference is worth knowing before
you conclude a view is unusable: `LIMIT` *does* push, so `SELECT * FROM
kg.main.some_view LIMIT 5` returns five rows from the same view whose `WHERE`
read fails. Measured on a 50k-vertex graph, all three ways: no clause returns
the ad-hoc cap with a truncation notice, `LIMIT 5` returns five rows, and a
`WHERE` fails on `max_rows`. Declare the bound in the Cypher anyway; relying on
the caller to always pass a `LIMIT` is the same unbounded view with a longer
fuse.

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

This rule and the undirected-untyped one below are the two the small eval
fixture cannot test, so they have a regression test of their own:
`evals/fixtures/setup_large.sh` builds a synthetic 50k-vertex graph where the
recommended form answers and both of these time out. If you weaken either
rule, run that and check it still flips.

**Bound the expansion, and measure rather than trust.** Ask the edge counts
first (the `graph_schema` section of SKILL.md) — a relationship with hundreds of
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
