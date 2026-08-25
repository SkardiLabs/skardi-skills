#!/usr/bin/env bash
# Start the retrieval-skill eval fixture server (skardi-server v0.5.0).
#
# Usage:
#   SKARDI_SERVER_BIN=/path/to/skardi-server ./setup.sh [--bare] [--no-search] [--port N]
#
#   --bare       start WITHOUT the semantics overlay (eval 2: catalog with
#                no table names anywhere)
#   --no-search  exclude the search-fulltext pipeline (eval 0: no search
#                surface registered)
#   --port N     listen port (default 18080)
#
# Builds shop.db / scratch.db deterministically (make_data.py, seed 7),
# renders the pipeline set for the requested variant, starts the server in
# the background, waits for /health, and writes server.pid next to this
# script. Stop with: kill "$(cat server.pid)".
set -euo pipefail
cd "$(dirname "$0")"

PORT=18080
BARE=0
NO_SEARCH=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --bare) BARE=1 ;;
    --no-search) NO_SEARCH=1 ;;
    --port) PORT="$2"; shift ;;
    *) echo "unknown flag: $1" >&2; exit 1 ;;
  esac
  shift
done

: "${SKARDI_SERVER_BIN:?set SKARDI_SERVER_BIN to a skardi-server binary built from the v0.5.0 tag}"

python3 make_data.py

RENDER=pipelines.rendered
rm -rf "$RENDER" && mkdir "$RENDER"
cp pipelines/orders-by-status.yaml pipelines/refresh-orders.yaml "$RENDER"/
[[ "$NO_SEARCH" -eq 1 ]] || cp pipelines/search-fulltext.yaml "$RENDER"/

# v0.5.0 auto-discovers `semantics.yaml` sitting NEXT TO the ctx file even
# when --semantics is not passed (verified 2026-08-25). For --bare we must
# therefore start from a ctx copy in a directory that has no semantics.yaml;
# data paths inside ctx.yaml still resolve against this script's CWD.
CTX=ctx.yaml
SEM=()
if [[ "$BARE" -eq 1 ]]; then
  # The copy must live outside the --pipeline directory (every YAML there
  # is parsed as a pipeline) and away from semantics.yaml (auto-discovery).
  rm -rf ctx.rendered && mkdir ctx.rendered
  cp ctx.yaml ctx.rendered/ctx.yaml
  CTX="ctx.rendered/ctx.yaml"
else
  SEM=(--semantics semantics.yaml)
fi

# Refuse a port that is already answering — otherwise the health poll
# below would bless a LEFTOVER server from a previous variant while this
# launch dies with "Address already in use" (reproduced in review round 3:
# --no-search reported ready against the old full-variant server).
if curl -sf "http://127.0.0.1:${PORT}/health" > /dev/null 2>&1; then
  echo "port ${PORT} is already serving /health — stop the old server first" \
       "(kill \$(cat server.pid)) or pass a different --port" >&2
  exit 1
fi

# ${SEM[@]+...}: macOS bash 3.2 errors on expanding an empty array under
# `set -u`; this idiom expands to nothing when SEM is empty.
nohup "$SKARDI_SERVER_BIN" --ctx "$CTX" --pipeline "$RENDER" --port "$PORT" \
  ${SEM[@]+"${SEM[@]}"} > server.log 2>&1 &
PID=$!
echo "$PID" > server.pid

fail() {
  echo "$1; last server.log lines:" >&2
  tail -5 server.log >&2 || true
  kill "$PID" 2>/dev/null || true
  rm -f server.pid
  exit 1
}

# Health alone is not proof — require OUR pid to be alive on every poll,
# so a dead launch can never be mistaken for a running one.
for _ in $(seq 1 60); do
  kill -0 "$PID" 2>/dev/null || fail "server process died during startup"
  if curl -sf "http://127.0.0.1:${PORT}/health" > /dev/null 2>&1; then
    echo "ready: http://127.0.0.1:${PORT} (pid $PID, bare=$BARE, no-search=$NO_SEARCH)"
    exit 0
  fi
  sleep 0.5
done
fail "server did not become healthy within 30s"
