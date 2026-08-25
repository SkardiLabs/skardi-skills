#!/usr/bin/env python3
"""Guard: the pre-#170 CLI story must not creep back into current instructions.

Before skardi PR #170 the CLI carried its own engine: you pointed
`SKARDICONFIG` at a directory and ran `skardi grep` / `skardi fts` locally,
with no server. None of that exists in the v0.5.0 release — its subcommands
are query / run / pipeline / job / schema / health, and it only ever talks to
a running server over HTTP. Instructions that still use the old shapes send
an agent down a path that cannot work, and the failure is confusing rather
than obvious (`command not found` on a subcommand, or a silently ignored
environment variable).

Two more traps in the same family:

  * `--branch main` installs. The skill has to be reproducible against a
    tagged release; `main` carries unreleased changes that cannot be named
    in a bug report.
  * A floating `:latest` image tag. It moves under the user, so a workspace
    that works today can break tomorrow with no version to point at.

This test only guards text an agent or user would *follow*. Prose that names
an obsolete shape in order to say it is obsolete is fine and is what the
allowlist below is for — the point is to keep the history readable without
letting it become an instruction again.

Run: python3 tests/test_no_legacy_cli.py
"""
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SKILL = os.path.join(ROOT, "skills", "auto_context")

# (regex, why it is banned)
BANNED = [
    (r"SKARDICONFIG",
     "removed with PR #170; the CLI has no local config dir"),
    (r"\bskardi\s+(grep|fts|vec)\b",
     "not a v0.5.0 subcommand (query/run/pipeline/job/schema/health)"),
    (r"--branch\s+main",
     "pin a released tag instead; main carries unreleased changes"),
    (r"skardi-server-rag:latest",
     "pin :0.5.0; :latest moves under the user"),
    (r"auto_knowledge_base skill",
     "the current skill is auto_context"),
]

# Lines allowed to mention a banned shape because they explicitly mark it as
# gone. Matched as substrings against the offending line.
ALLOWED_SUBSTRINGS = [
    "no longer exist",
    "no longer possible",
    "do not exist in v0.5.0",
    "NOTE ON THE REMOVED PRE-FLIGHT CHECK",
    "used to probe",
    "An older version of this guide",
    "pre-merge",
    "before the auto_knowledge_base + auto_rag merge",
]

SCANNED_SUFFIXES = (".md", ".py", ".tpl", ".yaml", ".json")


def iter_files():
    # The repo README documents this skill too, so it is in scope: a stale
    # sentence there is just as followable as one inside the skill.
    repo_readme = os.path.join(os.path.dirname(ROOT), "README.md")
    if os.path.isfile(repo_readme):
        yield repo_readme
    for base in (SKILL, os.path.join(ROOT, "tests")):
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for fn in filenames:
                if fn.endswith(SCANNED_SUFFIXES):
                    yield os.path.join(dirpath, fn)


def test_no_legacy_cli_shapes_in_current_instructions():
    here = os.path.abspath(__file__)
    offenders = []
    for path in iter_files():
        if os.path.abspath(path) == here:
            continue  # this file names them on purpose
        with open(path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                for pattern, why in BANNED:
                    if re.search(pattern, line):
                        if any(a in line for a in ALLOWED_SUBSTRINGS):
                            continue
                        rel = os.path.relpath(path, ROOT)
                        offenders.append(f"{rel}:{lineno}  [{why}]\n      {line.strip()[:150]}")
    assert not offenders, (
        "legacy pre-#170 shapes found in current instructions:\n    "
        + "\n    ".join(offenders)
    )


def test_allowlist_actually_matches_something():
    """A stale allowlist silently widens the guard. Every entry must still
    be earning its place in at least one file."""
    blob = ""
    for path in iter_files():
        if os.path.abspath(path) == os.path.abspath(__file__):
            continue
        with open(path, encoding="utf-8") as fh:
            blob += fh.read()
    dead = [a for a in ALLOWED_SUBSTRINGS if a not in blob]
    assert not dead, f"allowlist entries no longer used, remove them: {dead}"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  ok    {name}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL  {name}: {e}")
    print(f"\n{'FAILED' if failures else 'all passed'} ({failures} failure(s))")
    sys.exit(1 if failures else 0)


# Sibling skills live and die on their own schedule. auto_context's own
# instructions must not depend on one existing, or retiring that skill turns
# into an edit here — and the edit would be to a paragraph that only just
# landed. Note the asymmetry: the repo README and the marketplace manifest
# SHOULD list sibling plugins (that is what they are for), so this guard
# covers the skill directory only.
SIBLING_SKILLS = ["feishu_connector", "feishu-connector",
                  "skardi_query_log", "skardi-query-log"]


def test_the_skill_does_not_depend_on_a_sibling_skill():
    offenders = []
    for dirpath, dirnames, filenames in os.walk(SKILL):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            if not fn.endswith(SCANNED_SUFFIXES):
                continue
            path = os.path.join(dirpath, fn)
            with open(path, encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    for name in SIBLING_SKILLS:
                        if name in line:
                            rel = os.path.relpath(path, ROOT)
                            offenders.append(
                                f"{rel}:{lineno}  names sibling skill "
                                f"{name!r}\n      {line.strip()[:150]}")
    assert not offenders, (
        "auto_context must stand on its own — state the source's shape and "
        "the table it should land in, not which other skill produces it:\n    "
        + "\n    ".join(offenders)
    )
