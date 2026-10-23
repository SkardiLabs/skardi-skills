#!/usr/bin/env python3
"""Guard: a change that reaches plugin users must come with a higher version.

Claude Code only offers an update when the plugin version changes. If a skill
is added or edited and the version stays put, existing users are told they are
already on the latest version and never get it (document-cache landed in #57
under 0.4.0 and did exactly this). scripts/check-version-bump.sh is what CI
runs to stop that; these cases are written from that requirement:

  * a skill changed, version unchanged  -> fails
  * a skill changed, version raised     -> passes
  * a skill changed, version lowered    -> fails
  * only files the plugin does not ship changed (README, tests) -> passes

Each case builds a throwaway git repository holding the real script, a base
commit at 0.4.0, and one change on top, then runs the script against the base.

Run: python3 tests/test_check_version_bump.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPT = os.path.join(ROOT, "scripts", "check-version-bump.sh")


def manifest(version):
    return '{\n  "name": "skardi",\n  "version": "%s"\n}\n' % version


def git(repo, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo, check=True, capture_output=True)


def write(repo, rel, text):
    path = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def run_case(edits):
    """Commit a 0.4.0 base, apply `edits` ({path: text}) as a second commit,
    and return the script's exit code and output when checked against base."""
    repo = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(repo, "scripts"))
        shutil.copy(SCRIPT, os.path.join(repo, "scripts"))
        write(repo, ".claude-plugin/plugin.json", manifest("0.4.0"))
        write(repo, "skills/retrieval/SKILL.md", "---\nname: retrieval\n---\n")
        write(repo, "README.md", "# skardi-skills\n")
        git(repo, "init", "-q", "-b", "main")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "base")
        for rel, text in edits.items():
            write(repo, rel, text)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "change")
        proc = subprocess.run(
            ["bash", "scripts/check-version-bump.sh", "HEAD~1"],
            cwd=repo, capture_output=True, text=True)
        return proc.returncode, proc.stdout + proc.stderr
    finally:
        shutil.rmtree(repo)


NEW_SKILL = {"skills/document-cache/SKILL.md": "---\nname: document-cache\n---\n"}


def test_changed_skill_same_version_fails():
    code, out = run_case(NEW_SKILL)
    assert code != 0, out
    assert "skills/document-cache/SKILL.md" in out, out


def test_changed_skill_bumped_version_passes():
    code, out = run_case({**NEW_SKILL, ".claude-plugin/plugin.json": manifest("0.5.0")})
    assert code == 0, out


def test_changed_skill_lowered_version_fails():
    code, out = run_case({**NEW_SKILL, ".claude-plugin/plugin.json": manifest("0.3.9")})
    assert code != 0, out


def test_docs_only_change_passes():
    code, out = run_case({"README.md": "# skardi-skills\n\nMore words.\n",
                          "tests/test_new.py": "def test_x():\n    pass\n"})
    assert code == 0, out


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as exc:
                failed += 1
                print(f"FAIL {name}\n{exc}")
    sys.exit(1 if failed else 0)
