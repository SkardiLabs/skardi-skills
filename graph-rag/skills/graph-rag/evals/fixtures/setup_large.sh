#!/usr/bin/env bash
# Start the graph-rag LARGE fixture: a synthetic graph big enough that the
# bounds this skill spends most of its warnings on can actually fail.
#
#   SKARDI_SERVER_BIN=/path/to/skardi-server ./setup_large.sh [--port N]
#                       [--kg-port N] [--vertices N] [--fanout K] [--check]
#   ./setup_large.sh --down
#
# Why a second fixture. `setup.sh` is ten vertices and eight edges, which is
# right for asserting counts and wrong for asserting budgets: on that graph an
# unlabeled scan, an undirected untyped traversal and an unbounded view all
# succeed, so a green run there says nothing about the rules that matter on a
# real graph. This one is ~50k vertices and ~250k edges, which is where those
# rules start to bite. It is opt-in because it takes a minute and a half to
# build and needs a container.
#
# Synthetic on purpose. The shape is a chain with fan-out, `name` deliberately
# conflated 100:1, and `resolution` 70% `ambiguous` to match what a real
# static-analysis graph looks like. Vertices are inserted in a scrambled order
# so that "LIMIT without ORDER BY" returns a genuinely different slice than
# ORDER BY does; with sequential insertion the two agree and the check proves
# nothing.
set -euo pipefail
cd "$(dirname "$0")"

PORT=18084
KG_PORT=5457
VERTICES=50000
FANOUT=5
CONTAINER=graph-rag-large-age
IMAGE=apache/age:release_PG16_1.6.0
DOWN=0
CHECK_ONLY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift ;;
    --kg-port) KG_PORT="$2"; shift ;;
    --vertices) VERTICES="$2"; shift ;;
    --fanout) FANOUT="$2"; shift ;;
    --down) DOWN=1 ;;
    --check) CHECK_ONLY=1 ;;
    *) echo "unknown flag: $1" >&2; exit 1 ;;
  esac
  shift
done

if [[ "$DOWN" == 1 ]]; then
  [[ -f server_large.pid ]] && kill "$(cat server_large.pid)" 2>/dev/null || true
  rm -f server_large.pid server_large.log ctx.large.rendered.yaml
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  echo "large fixture down"
  exit 0
fi

SERVER_BIN="${SKARDI_SERVER_BIN:-skardi-server}"
command -v "$SERVER_BIN" >/dev/null 2>&1 || [[ -x "$SERVER_BIN" ]] || {
  echo "skardi-server not found; set SKARDI_SERVER_BIN" >&2; exit 1; }
CLI="${SKARDI_CLI_BIN:-skardi}"
command -v "$CLI" >/dev/null 2>&1 || [[ -x "$CLI" ]] || {
  echo "skardi CLI not found; set SKARDI_CLI_BIN" >&2; exit 1; }

export KG_USER=postgres
export KG_PASS=eval-only-not-a-secret

psql_q () { docker exec "$CONTAINER" psql -U postgres -tAqc "LOAD 'age'; SET search_path = ag_catalog, public; $1"; }

if [[ "$CHECK_ONLY" == 0 ]]; then
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  docker run -d --name "$CONTAINER" \
    -p "127.0.0.1:${KG_PORT}:5432" \
    -e POSTGRES_PASSWORD="$KG_PASS" \
    "$IMAGE" >/dev/null

  for _ in $(seq 1 60); do
    docker exec "$CONTAINER" pg_isready -U postgres >/dev/null 2>&1 && break
    sleep 1
  done
  docker exec "$CONTAINER" pg_isready -U postgres >/dev/null 2>&1 || {
    echo "AGE container did not become ready" >&2; exit 1; }

  echo "building ~${VERTICES} vertices and ~$((VERTICES * FANOUT)) edges (about 90s)…"
  psql_q "SELECT drop_graph('big', true) FROM ag_graph WHERE name = 'big';" >/dev/null
  psql_q "SELECT create_graph('big');" >/dev/null

  # String concatenation in this Cypher is `+`. `||` builds a LIST, so
  # 'm' || toString(i) yields ["m","1"] and every downstream column that
  # declared `string` fails on an array instead. Measured; it is the kind of
  # mistake that shows up as a type error three steps later.
  #
  # `(i * 37) % VERTICES` scrambles insertion order relative to id, which is
  # what makes the LIMIT-without-ORDER-BY check below meaningful.
  psql_q "SELECT count(*) FROM cypher('big', \$\$
      UNWIND range(1, ${VERTICES}) AS k
      WITH ((k * 37) % ${VERTICES}) + 1 AS i
      CREATE (n:Function {id: i,
                          name: 'f' + toString(i % 500),
                          fqn: 'm' + toString(i / 100) + '.f' + toString(i)})
      RETURN 1 \$\$) AS (x agtype);" >/dev/null

  psql_q "SELECT count(*) FROM cypher('big', \$\$
      MATCH (a:Function) WHERE a.id < ${VERTICES} - ${FANOUT}
      UNWIND range(1, ${FANOUT}) AS k
      WITH a, k MATCH (b:Function) WHERE b.id = a.id + k
      CREATE (a)-[:CALLS {resolution:
        CASE WHEN a.id % 10 < 7 THEN 'ambiguous' ELSE 'import' END}]->(b)
      RETURN 1 \$\$) AS (x agtype);" >/dev/null

  sed -e "s|\${KG_PORT}|$KG_PORT|" ctx.large.yaml > ctx.large.rendered.yaml
  "$SERVER_BIN" --ctx ctx.large.rendered.yaml --port "$PORT" > server_large.log 2>&1 &
  echo $! > server_large.pid

  for _ in $(seq 1 40); do
    curl -sf -m 2 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1 && break
    sleep 1
  done
  curl -sf -m 2 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1 || {
    echo "server did not become healthy; see server_large.log" >&2
    tail -20 server_large.log >&2; exit 1; }
fi

export SKARDI_SERVER_URL="http://127.0.0.1:${PORT}"
fails=0
q () { "$CLI" query --table -e "$1" 2>/dev/null; }
q_rc () { "$CLI" query --table -e "$1" >/dev/null 2>&1; }

pass () { echo "  ok:   $1"; }
fail () { echo "  FAIL: $1" >&2; fails=$((fails + 1)); }

# What this fixture exists to check: each of these is a rule the skill states
# and the small fixture cannot exercise. The point is not that a query fails,
# it is that it fails HERE and succeeds in its recommended form, so a future
# edit that weakens the rule shows up as a green-to-red flip.
echo "checking the bounds the small fixture cannot:"

# 1. The recommended form answers. If this fails, nothing below means anything.
if q "SELECT * FROM cypher_query('kg',
      'MATCH (s:Function)<-[r:CALLS]-(c) WHERE s.fqn IN \$f
       RETURN c.fqn AS caller, r.resolution AS via ORDER BY c.fqn LIMIT 50',
      '{\"f\": [\"m250.f25000\"]}',
      '{\"caller\": \"string\", \"via\": \"string\"}')" | grep -q 'm2'; then
  pass "labeled seed, single MATCH, bounded — answers"
else
  fail "labeled seed, single MATCH, bounded — should answer and did not"
fi

# 2. The skill's central performance claim. An unlabeled MATCH split from the
#    traversal makes a full-vertex scan the left side of a join.
if q_rc "SELECT * FROM cypher_query('kg',
      'MATCH (s) WHERE s.fqn IN \$f MATCH (s)<-[r:CALLS]-(c)
       RETURN c.fqn AS caller LIMIT 50',
      '{\"f\": [\"m250.f25000\"]}', '{\"caller\": \"string\"}')"; then
  fail "unlabeled + split MATCH answered — the labeled-single-MATCH rule may no longer be load-bearing; re-time it and update SKILL.md"
else
  pass "unlabeled + split MATCH does not answer (times out)"
fi

# 3. Undirected and untyped, even labeled and with a LIMIT.
if q_rc "SELECT * FROM cypher_query('kg',
      'MATCH (s:Function)-[r]-(c) WHERE s.fqn IN \$f RETURN c.fqn AS n LIMIT 20',
      '{\"f\": [\"m250.f25000\"]}', '{\"n\": \"string\"}')"; then
  fail "undirected untyped -[r]- answered — re-time it and update SKILL.md"
else
  pass "undirected untyped -[r]- does not answer"
fi

# 4/5. A SQL WHERE over an unbounded view does not push into its Cypher, and it
#      also removes the outer row cap that was implicitly bounding the scan, so
#      this is the read that fails. A SQL LIMIT *does* push, which is why the
#      rule is about WHERE specifically.
if q_rc "SELECT * FROM kg.main.unbounded_functions WHERE fqn = 'm0.f2'"; then
  fail "SQL WHERE over an unbounded view succeeded — predicate pushdown may have changed; re-check the 'bound belongs inside the Cypher' rule"
else
  pass "SQL WHERE over an unbounded view fails (max_rows)"
fi
if q "SELECT * FROM kg.main.unbounded_functions LIMIT 5" | grep -q 'row(s) returned'; then
  pass "SQL LIMIT over the same view succeeds — the failure above is WHERE, not the view"
else
  fail "SQL LIMIT over an unbounded view failed — expected it to push"
fi

# 6. The truncation notice is on stderr, so a caller reading only stdout sees a
#    complete-looking result. Any count built on it is wrong.
out=$("$CLI" query --table -e "SELECT * FROM kg.main.unbounded_functions" 2>/dev/null | tail -1)
err=$("$CLI" query --table -e "SELECT * FROM kg.main.unbounded_functions" 2>&1 >/dev/null | tail -1)
if [[ "$err" == *"truncated"* && "$out" != *"truncated"* ]]; then
  pass "truncation notice is on stderr only ($out on stdout)"
else
  fail "truncation notice moved: stdout='$out' stderr='$err'"
fi

# 7. LIMIT without ORDER BY is an arbitrary slice, and here it is a visibly
#    different one.
un=$(q "SELECT * FROM cypher_query('kg',
      'MATCH (f:Function) RETURN f.name AS n LIMIT 3', '{}', '{\"n\": \"string\"}')" | sed -n '3,5p' | tr -d ' \n')
or=$(q "SELECT * FROM cypher_query('kg',
      'MATCH (f:Function) RETURN f.name AS n ORDER BY f.name LIMIT 3', '{}', '{\"n\": \"string\"}')" | sed -n '3,5p' | tr -d ' \n')
if [[ -n "$un" && -n "$or" && "$un" != "$or" ]]; then
  pass "LIMIT without ORDER BY returns a different slice ($un vs $or)"
else
  fail "could not observe the LIMIT slice difference (un='$un' or='$or'); the fixture's insertion scramble may have stopped working"
fi

echo
if [[ "$fails" -gt 0 ]]; then
  echo "large fixture: $fails check(s) failed" >&2
  echo "server: http://127.0.0.1:${PORT}   stop with: $(basename "$0") --down" >&2
  exit 1
fi
echo "large fixture up and all checks passed: http://127.0.0.1:${PORT}  (graph source 'kg')"
echo "re-run just the checks with: $(basename "$0") --check"
echo "stop with: $(basename "$0") --down"
