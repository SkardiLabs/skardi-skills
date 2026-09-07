# Provisioning the AGE backend

The read-only guarantee is **backend-enforced** (a `READ ONLY`
transaction wraps every query; the plan-time keyword guard is UX, not
the boundary). That is why the deployment posture matters: Skardi's
credential defines what a compromised or buggy query could ever do.

## The least-privilege reader role

Run Skardi's graph connection as a role that can read the graph and
nothing else — **never a superuser**:

```sql
-- As the administrator, once:
CREATE ROLE kg_reader LOGIN PASSWORD '…';
GRANT CONNECT ON DATABASE graphrag TO kg_reader;  -- explicit, in case PUBLIC's default was revoked
GRANT USAGE ON SCHEMA ag_catalog TO kg_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA ag_catalog TO kg_reader;

-- Per graph (AGE stores each graph in a schema of the same name):
GRANT USAGE ON SCHEMA your_graph TO kg_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA your_graph TO kg_reader;
```

`ag_catalog` access is what lets `graph_schema()` enumerate labels and
what registration probes (`ag_catalog.ag_graph`) to distinguish "AGE is
absent" from other failures.

## Why `LOAD 'age'` is best-effort, and what to do instead

The client issues `LOAD 'age'` on each connection as best-effort,
deliberately: `LOAD` is superuser-only for libraries outside
`$libdir/plugins`, and requiring it would force the exact credential
this recipe exists to avoid.

- The official `apache/age` image ships
  `shared_preload_libraries = age` — a reader role works as-is.
- On self-managed Postgres, set `shared_preload_libraries = 'age'` (or
  `session_preload_libraries`) in postgresql.conf.
- If registration fails with an `ag_catalog.ag_graph` probe error, AGE
  is genuinely absent from the server. Install it there. Do NOT solve
  this by upgrading Skardi's credential to superuser so `LOAD` works —
  that trades a one-line postgresql.conf fix for a standing privilege
  escalation.

## Credentials: env-var names only

Config carries credentials as environment-variable **names**
(`username_env`, `password_env`), never values. A password embedded in
`connection_string` is rejected at config load — this is enforcement,
not convention. Consequences for how you work:

- Export the variables in the server's environment (or the operator's
  Secret on cloud deployments); the YAML names them.
- Never echo a connection string into logs, error reports, or chat —
  on deployments that violate the rule elsewhere, the string is where
  the credential hides.

## A local AGE for development

```bash
docker run -d --name age -p 5455:5432 \
  -e POSTGRES_PASSWORD=devonly apache/age
```

Then create the graph and the reader role inside it:

```sql
SELECT * FROM ag_catalog.create_graph('knowledge');
-- …CREATE ROLE kg_reader… as above, plus your seed data via cypher()
```

The container image already preloads AGE, so the reader role works
without any superuser step. Seed data goes in with AGE's own
`cypher('knowledge', $$ CREATE (:Person {name: 'ada', age: 36}) $$)`
as the admin role — Skardi's surface is read-only and cannot seed.

### `create_graph` rejects some names, and says only "invalid"

Every refusal below is the same message — `ERROR: graph name is invalid` —
with nothing to say which rule was broken. Measured:

| name | |
|---|---|
| `a`, `ab`, `zz`, `Kg`, `aB` | **refused** — shorter than three characters |
| `1ab`, `1a`, `12345` | **refused** — starts with a digit |
| `a b` | **refused** — contains a space |
| `abc`, `ab1`, `AB1`, `k_g`, `__x`, `a-b` | created |

The three-character floor is the one that catches people, because the
natural short name is exactly the one this skill's query examples use.
**`kg` in `cypher_query('kg', …)` is Skardi's CATALOG name** — the
`data_sources[].name` from the context YAML — and it is a different
namespace from the AGE graph. Nothing stops the two from matching, and
nothing requires it; a source named `kg` can serve a graph named
`knowledge`. But `create_graph('kg')` is refused, so if you want them to
match, pick a name of three characters or more for both.

Uppercase, underscores and hyphens are all accepted once the name is long
enough, so the floor is a length rule and not an identifier rule.

### Inside `$$ … $$`, a comment is `//` and never `--`

The seed statement above nests Cypher inside SQL, and each layer has its
own comment marker. `--` is SQL's; the Cypher parser rejects it outright:

```sql
-- This line is fine: it is SQL, outside the dollar quotes.
SELECT * FROM cypher('knowledge', $$
  -- ERROR:  syntax error at or near "-"
  MATCH (p:Person) RETURN p.name AS name
$$) AS (name agtype);

SELECT * FROM cypher('knowledge', $$
  // This one works.
  MATCH (p:Person) RETURN p.name AS name
$$) AS (name agtype);
```

Worth knowing because a seed script is usually the first place anyone
writes a long Cypher block, and it is the natural place to comment what
the fixture data is for — which is exactly where a `--` gets typed out of
SQL habit and fails the whole statement.
