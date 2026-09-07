#!/usr/bin/env bash
# Start the graph-rag eval fixture: an AGE graph and a skardi-server serving it.
#
# Usage:
#   SKARDI_SERVER_BIN=/path/to/skardi-server ./setup.sh [--port N] [--kg-port N]
#   ./setup.sh --down          # stop the server and remove the container
#
# Unlike the retrieval skill's fixture, this one needs a real Postgres: AGE is
# an extension, so there is no file-backed graph to build. It runs one
# throwaway container and tears it down with --down.
#
# Deterministic by construction: fixed node ids, fixed edges, fixed
# `resolution` values, no clock and no randomness (see seed_graph.sql), so the
# counts an eval asserts stay true.
set -euo pipefail
cd "$(dirname "$0")"

PORT=18081
KG_PORT=5455
CONTAINER=graph-rag-eval-age
IMAGE=apache/age:release_PG16_1.6.0
DOWN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift ;;
    --kg-port) KG_PORT="$2"; shift ;;
    --down) DOWN=1 ;;
    *) echo "unknown flag: $1" >&2; exit 1 ;;
  esac
  shift
done

if [[ "$DOWN" == 1 ]]; then
  [[ -f server.pid ]] && kill "$(cat server.pid)" 2>/dev/null || true
  rm -f server.pid server.log ctx.rendered.yaml
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  echo "fixture down"
  exit 0
fi

SERVER_BIN="${SKARDI_SERVER_BIN:-skardi-server}"
command -v "$SERVER_BIN" >/dev/null 2>&1 || [[ -x "$SERVER_BIN" ]] || {
  echo "skardi-server not found; set SKARDI_SERVER_BIN" >&2; exit 1; }

# The graph engine. `-e POSTGRES_PASSWORD` is a throwaway for a container that
# lives as long as the eval run.
export KG_USER=postgres
export KG_PASS=eval-only-not-a-secret
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

docker cp seed_graph.sql "$CONTAINER:/tmp/seed_graph.sql" >/dev/null
# `-o /dev/null`: the CREATE block is a `cypher(...) AS (x agtype)` select, so
# psql would print an empty result table over the fixture's own output.
docker exec "$CONTAINER" psql -U postgres -v ON_ERROR_STOP=1 -q -o /dev/null \
  -f /tmp/seed_graph.sql

# The fixture asserts its own shape before handing the server over, so a
# broken seed fails here rather than as a mysteriously wrong eval answer.
check() {
  local want="$1" got
  got=$(docker exec "$CONTAINER" psql -U postgres -tAqc \
    "LOAD 'age'; SET search_path = ag_catalog, public; $2" 2>/dev/null \
    | grep -vE '^(LOAD|SET)$' | sed '/^$/d' | head -1)
  [[ "$got" == "$want" ]] || { echo "fixture check failed: expected $want, got $got ($3)" >&2; exit 1; }
  echo "  ok: $3 = $got"
}
check 2 "SELECT n FROM cypher('kgeval', \$\$ MATCH (f:Function) WHERE f.name='verify_token' RETURN count(f) AS n \$\$) AS (n agtype);" "nodes named verify_token"
check 6 "SELECT n FROM cypher('kgeval', \$\$ MATCH (s:Function)<-[c:CALLS]-(d) WHERE s.fqn='auth.tokens.verify_token' RETURN count(DISTINCT d) AS n \$\$) AS (n agtype);" "one-hop dependents, all edges"
check 3 "SELECT n FROM cypher('kgeval', \$\$ MATCH (s:Function)<-[c:CALLS]-(d) WHERE s.fqn='auth.tokens.verify_token' AND c.resolution <> 'ambiguous' RETURN count(DISTINCT d) AS n \$\$) AS (n agtype);" "one-hop dependents, confirmed edges"
check 7 "SELECT n FROM cypher('kgeval', \$\$ MATCH (s:Function)<-[:CALLS*1..3]-(d) WHERE s.fqn='auth.tokens.verify_token' WITH DISTINCT d RETURN count(d) AS n \$\$) AS (n agtype);" "1..3 hops seeded by fqn"
check 8 "SELECT n FROM cypher('kgeval', \$\$ MATCH (s:Function)<-[:CALLS*1..3]-(d) WHERE s.name='verify_token' WITH DISTINCT d RETURN count(d) AS n \$\$) AS (n agtype);" "1..3 hops seeded by name (conflated)"

sed "s|\${KG_PORT}|$KG_PORT|" ctx.yaml > ctx.rendered.yaml
"$SERVER_BIN" --ctx ctx.rendered.yaml --port "$PORT" > server.log 2>&1 &
echo $! > server.pid

for _ in $(seq 1 30); do
  curl -sf -m 2 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1 && break
  sleep 1
done
curl -sf -m 2 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1 || {
  echo "server did not become healthy; see server.log" >&2; tail -20 server.log >&2; exit 1; }

echo "graph-rag fixture up: http://127.0.0.1:${PORT}  (graph source 'kg')"
echo "stop with: $(basename "$0") --down"
