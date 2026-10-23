#!/usr/bin/env bash
#
# check-version-bump.sh — fail when a change ships to users without a new
# plugin version.
#
# Claude Code decides whether an installed plugin is out of date by comparing
# the version string, not the content. Merge a new or changed skill without
# raising the version and every existing user who runs
# `claude plugin update skardi@skardi-skills` is told they are already on the
# latest version and never receives it. That happened with document-cache
# (#57): it landed under 0.4.0, and updates kept reporting 0.4.0 as current.
#
# Everything the plugin ships counts as a change that needs a new version:
# anything under skills/, and the host manifests (their descriptions and
# keywords are what a host shows). Changes elsewhere (README, tests, CI,
# scripts/) do not reach an installed plugin and pass without a bump.
#
# Usage:
#   scripts/check-version-bump.sh <base-ref>
#
# <base-ref> is the commit the change is compared against: the base branch
# tip in CI. The version is read from .claude-plugin/plugin.json on both
# sides; scripts/bump-version.sh --check separately keeps the other host
# manifests in step with it, and scripts/bump-version.sh <x.y.z> raises all
# of them at once.

set -euo pipefail

[ $# -eq 1 ] || { echo "usage: $0 <base-ref>" >&2; exit 2; }
base="$1"

cd "$(dirname "$0")/.."

shipped=(skills .claude-plugin gemini-extension.json package.json)
manifest=".claude-plugin/plugin.json"

changed="$(git diff --name-only "$base" -- "${shipped[@]}")"
if [ -z "$changed" ]; then
  echo "ok: nothing the plugin ships changed; no version bump needed"
  exit 0
fi

semver() { grep -oE '"version"[[:space:]]*:[[:space:]]*"[0-9]+\.[0-9]+\.[0-9]+"' | grep -oE '[0-9]+\.[0-9]+\.[0-9]+'; }

base_v="$(git show "$base:$manifest" | semver)"
head_v="$(semver <"$manifest")"

# Higher means strictly greater by semver order; sort -V orders x.y.z correctly.
if [ "$head_v" != "$base_v" ] &&
   [ "$(printf '%s\n%s\n' "$base_v" "$head_v" | sort -V | tail -n 1)" = "$head_v" ]; then
  echo "ok: shipped files changed and the version went $base_v -> $head_v"
  exit 0
fi

echo "::error::This change ships to plugin users but the version is still $head_v (base: $base_v)."
echo "Existing users run 'claude plugin update skardi@skardi-skills', see $head_v as current, and never get it."
echo "Raise it with: scripts/bump-version.sh <higher x.y.z>"
echo "Changed shipped files:"
printf '%s\n' "$changed" | sed 's/^/  /'
exit 1
