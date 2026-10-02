#!/usr/bin/env bash
# A skill marked skardi-min-version: "main" is waiting for a capability that
# no Skardi release has yet. It must also name that capability as
# skardi-needs: "<identifier>", something `git grep` finds in the skardi
# source once it exists. This script looks for each identifier in the latest
# skardi release and fails when it is there: the release shipped what the
# skill was waiting for, so "main" should become that version.
#
#   scripts/check-unreleased.sh                # checks against the latest release
#   SKARDI_SRC=/path/to/skardi TAG=v0.6.0 scripts/check-unreleased.sh

set -euo pipefail
cd "$(dirname "$0")/.."

field() { awk -v k="$2" 'NR==1 && /^---/ {f=1; next} f && /^---/ {exit} f && $1 == k":" {gsub(/"/, "", $2); print $2; exit}' "$1"; }

pending=()
status=0
for md in skills/*/SKILL.md; do
  [ "$(field "$md" skardi-min-version)" = "main" ] || continue
  needs="$(field "$md" skardi-needs)"
  if [ -z "$needs" ]; then
    echo "::error::$md is marked \"main\" but has no skardi-needs naming what it waits for"
    status=1
    continue
  fi
  pending+=("$md:$needs")
done
if [ "${#pending[@]}" -eq 0 ]; then
  [ "$status" -eq 0 ] && echo "no skill is waiting on an unreleased capability"
  exit $status
fi

TAG="${TAG:-$(gh release view --repo SkardiLabs/skardi --json tagName --jq .tagName)}"
if [ -z "${SKARDI_SRC:-}" ]; then
  SKARDI_SRC="$(mktemp -d)"
  git -c advice.detachedHead=false clone -q --depth 1 --branch "$TAG" https://github.com/SkardiLabs/skardi.git "$SKARDI_SRC"
fi

for entry in "${pending[@]}"; do
  md="${entry%%:*}"; needs="${entry#*:}"
  if git -C "$SKARDI_SRC" grep -q -F -e "$needs" "$TAG" -- crates; then
    echo "::error::$md waits for '$needs', which Skardi $TAG ships; change skardi-min-version from \"main\" to \"${TAG#v}\""
    status=1
  else
    echo "ok: $md still waits for '$needs' (not in $TAG)"
  fi
done
exit $status
