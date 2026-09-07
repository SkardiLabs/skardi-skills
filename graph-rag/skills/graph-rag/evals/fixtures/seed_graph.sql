-- The graph the graph-rag evals traverse. Deterministic: fixed ids, fixed
-- edges, fixed `resolution` values, no clock and no randomness, so an
-- assertion about a count stays true.
--
-- Shaped to exercise the three things the skill claims are traps, because an
-- eval over a graph without them cannot tell a correct answer from a lucky one:
--
--   1. `verify_token` exists TWICE, in different modules, and the homonym has
--      a caller of its OWN. That is what makes the conflation observable:
--      seeding by `fqn` reaches 7 dependents, seeding by `name` reaches 8,
--      and the extra one belongs to a different function entirely. An earlier
--      version of this fixture gave the homonym a caller it shared with the
--      real target, so both counts came out 7 and the trap was invisible.
--   2. Of the six incoming CALLS on auth.tokens.verify_token, three are
--      `resolution: ambiguous` — a name-only guess. An impact answer that
--      reports six without saying so is reporting guesses as facts.
--   3. There is a two-hop caller, so a one-hop answer is incomplete and a
--      `*1..3` traversal is the difference.
LOAD 'age';
SET search_path = ag_catalog, public;

-- The AGE graph name is `kgeval`, not `kg`: `create_graph` refuses names
-- shorter than three characters with the unhelpful "graph name is invalid"
-- (measured: 'a' and 'ab' refused, 'abc' created). `kg` in the queries below
-- is Skardi's CATALOG name, which is a different namespace and unaffected.
SELECT drop_graph('kgeval', true) FROM ag_graph WHERE name = 'kgeval';
SELECT create_graph('kgeval');

SELECT * FROM cypher('kgeval', $$
  CREATE (vt:Function   {fqn: 'auth.tokens.verify_token',  name: 'verify_token', file_path: 'auth/tokens.py'})
  CREATE (vt2:Function  {fqn: 'legacy.auth.verify_token',  name: 'verify_token', file_path: 'legacy/auth.py'})
  CREATE (mw:Function   {fqn: 'api.middleware.require_auth', name: 'require_auth', file_path: 'api/middleware.py'})
  CREATE (lg:Function   {fqn: 'api.routes.login',          name: 'login',        file_path: 'api/routes.py'})
  CREATE (rf:Function   {fqn: 'api.routes.refresh',        name: 'refresh',      file_path: 'api/routes.py'})
  CREATE (g1:Function   {fqn: 'jobs.cleanup.sweep',        name: 'sweep',        file_path: 'jobs/cleanup.py'})
  CREATE (g2:Function   {fqn: 'scripts.audit.scan',        name: 'scan',         file_path: 'scripts/audit.py'})
  CREATE (g3:Function   {fqn: 'tools.repl.probe',          name: 'probe',        file_path: 'tools/repl.py'})
  CREATE (top:Function  {fqn: 'api.app.handler',           name: 'handler',      file_path: 'api/app.py'})
  CREATE (lc:Function   {fqn: 'legacy.cli.run',            name: 'run',          file_path: 'legacy/cli.py'})
  CREATE (f1:File {file_path: 'auth/tokens.py'})
  CREATE (f2:File {file_path: 'api/middleware.py'})
  CREATE (f3:File {file_path: 'api/routes.py'})

  // Three edges we can stand behind.
  CREATE (mw)-[:CALLS {resolution: 'import',      line: 12}]->(vt)
  CREATE (lg)-[:CALLS {resolution: 'unique_name', line: 40}]->(vt)
  CREATE (rf)-[:CALLS {resolution: 'same_scope',  line: 58}]->(vt)
  // Three that are name-only guesses.
  CREATE (g1)-[:CALLS {resolution: 'ambiguous',   line: 7}]->(vt)
  CREATE (g2)-[:CALLS {resolution: 'ambiguous',   line: 19}]->(vt)
  CREATE (g3)-[:CALLS {resolution: 'ambiguous',   line: 3}]->(vt)
  // The second hop: only reachable at depth 2.
  CREATE (top)-[:CALLS {resolution: 'import',     line: 5}]->(mw)
  // The homonym has its own, unrelated caller.
  CREATE (lc)-[:CALLS {resolution: 'import',      line: 9}]->(vt2)
$$) AS (x agtype);
