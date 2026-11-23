#!/bin/sh
# Gate for the working-memory hooks (hooks/hooks.json).
#
# The plugin's hooks run in every session of everyone who installed it, and
# most of those sessions have nothing to do with Skardi working memory. This
# script decides that in the shell, before an interpreter starts, so such a
# session pays for one `sh` and nothing else: no output, exit 0.
#
#   start   a session is starting: worth a look only where the skardi CLI has
#           been logged in, or memory was used on this machine before
#   active  everything after that: only where some session has used memory
#   fresh   a `skardi memory …` command on a machine that has never used
#           memory (the one case `active` would miss)
#   always  a Skardi memory MCP tool was called, which is use of memory
#
# SKARDI_MEMORY_HOOKS=off turns the hooks off altogether.

[ "${SKARDI_MEMORY_HOOKS:-on}" = "off" ] && exit 0

dir="${SKARDI_CONFIG_DIR:-$HOME/.skardi}"
sessions="$dir/memory/sessions"

case "$1" in
  start)  [ -f "$dir/config.yaml" ] || [ -d "$sessions" ] || exit 0 ;;
  active) [ -d "$sessions" ] || exit 0 ;;
  fresh)  [ -d "$sessions" ] && exit 0 ;;
  always) ;;
  *)      exit 0 ;;
esac

command -v python3 >/dev/null 2>&1 || exit 0
exec python3 "$(dirname "$0")/memory_hook.py"
