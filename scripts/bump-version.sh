#!/usr/bin/env bash
#
# bump-version.sh — set one version across every host manifest at once.
#
# The plugin version lives in four files, because each host reads its own
# manifest: two Claude Code manifests (the plugin and the marketplace entry),
# the Gemini extension, and the Pi/npm package. They must agree — a reader on
# one host sees a different version from a reader on another otherwise, and a
# missed file is silent: nothing fails, the number is just wrong where you
# didn't look. This sets all four from one place so bumping can't drift.
#
# Usage:
#   scripts/bump-version.sh 0.5.0     # set the version
#   scripts/bump-version.sh --check   # verify all four already agree
#
# The version value is replaced in place; nothing else in the file is
# reformatted, so a bump is a four-line diff. Each manifest is expected to
# hold exactly one `"version": "<semver>"`; the script refuses to touch a
# file that has zero or more than one.

set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

files=(
  ".claude-plugin/plugin.json"
  ".claude-plugin/marketplace.json"
  "gemini-extension.json"
  "package.json"
)

# The version token, semver value captured. Matches the one key across all
# four manifests (plugin/gemini/package at top level, marketplace under
# metadata) — they share the same `"version": "x.y.z"` spelling.
version_re='"version"[[:space:]]*:[[:space:]]*"[0-9]+\.[0-9]+\.[0-9]+"'

read_one() {
  # Print the semver in $1, or fail if it does not hold exactly one.
  local file="$1" matches
  matches="$(grep -oE "$version_re" "$file" || true)"
  local count
  count="$(printf '%s' "$matches" | grep -c . || true)"
  if [ "$count" -ne 1 ]; then
    echo "error: $file has $count version fields, expected 1" >&2
    return 1
  fi
  printf '%s' "$matches" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+'
}

usage() {
  echo "usage: $0 <version> | --check" >&2
  exit 2
}

[ $# -eq 1 ] || usage

if [ "$1" = "--check" ]; then
  seen=""
  ok=1
  for file in "${files[@]}"; do
    v="$(read_one "$file")"
    printf '%-34s %s\n' "$file" "$v"
    if [ -z "$seen" ]; then
      seen="$v"
    elif [ "$v" != "$seen" ]; then
      ok=0
    fi
  done
  if [ "$ok" -eq 1 ]; then
    echo "OK: all manifests at $seen"
  else
    echo "MISMATCH: versions disagree" >&2
    exit 1
  fi
  exit 0
fi

new="$1"
if ! printf '%s' "$new" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo "error: '$new' is not a semver (expected e.g. 0.5.0)" >&2
  exit 2
fi

for file in "${files[@]}"; do
  read_one "$file" >/dev/null   # asserts exactly one field before writing
  # Replace only the semver inside the version token, leaving all other
  # formatting untouched. A temp file keeps the write atomic.
  tmp="$(mktemp)"
  perl -pe "s/(\"version\"\\s*:\\s*\")[0-9]+\\.[0-9]+\\.[0-9]+(\")/\${1}${new}\${2}/" "$file" >"$tmp"
  mv "$tmp" "$file"
  echo "set $file -> $new"
done
echo "done. review with: git diff"
