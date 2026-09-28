#!/usr/bin/env python3
"""Guard the retrieval skill's copy-paste paths for audit intent.

The skill is a public procedure. These checks keep its first query runnable
and prevent a released v0.5.0 user from reporting audit flags their binary
cannot send.

Run: python3 tests/test_retrieval_ai_context_examples.py
"""
import json
import os
import re
import subprocess
import tempfile
import textwrap
from pathlib import Path


SKILL = Path(__file__).parents[1] / "skills" / "retrieval" / "SKILL.md"


def text():
    return SKILL.read_text(encoding="utf-8")


def test_main_build_mints_a_session_before_the_first_query():
    """A first-step peek must never expand an unset $SKARDI_SESSION."""
    content = text()
    session_setup = content.index("SKARDI_SESSION=$(uuidgen")
    first_audited_query = content.index("skardi query --purpose")
    assert session_setup < first_audited_query, (
        "mint the main-build session before the first audited query, including "
        "the step-1 peek"
    )


def test_v050_reporting_example_does_not_claim_audit_flags():
    """Release users need evidence wording that matches their executable CLI."""
    content = text()
    report = content[content.index("Lead with the answer"):]
    assert "v0.5.0 build" in report
    assert "/* purpose: paid order count and revenue */" in report




def bash_blocks(content):
    """Fenced ```bash blocks, in document order."""
    return re.findall(r"```bash\n(.*?)```", content, re.S)


# The examples are checked by running them, not by re-implementing bash:
# every hand-written parser misses some case (a comment between
# continuations, a space after a backslash, `;` glued to a token). bash runs
# each snippet with a stand-in `skardi` on PATH that records the argv it
# receives, so the assertions see exactly what the real CLI would get.
STUB = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["SKARDI_ARGV_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
"""


def run_bash(script):
    """Run a snippet under bash; return (argv lists `skardi` received, stderr)."""
    with tempfile.TemporaryDirectory() as tmp:
        stub = Path(tmp) / "skardi"
        stub.write_text(STUB)
        stub.chmod(0o755)
        log = Path(tmp) / "argv.log"
        env = dict(
            os.environ,
            PATH=f"{tmp}{os.pathsep}{os.environ['PATH']}",
            SKARDI_ARGV_LOG=str(log),
            SKARDI_SESSION="sess-test",
        )
        proc = subprocess.run(
            ["bash", "-c", textwrap.dedent(script)],
            env=env, capture_output=True, text=True, timeout=30,
        )
        calls = [json.loads(ln) for ln in log.read_text().splitlines()] if log.exists() else []
        return calls, proc.stderr


def flags(argv):
    """{flag: value} for a `skardi query` argv, the way a CLI parser reads it.

    Accepts `--f v` and `--f=v`; stops at `--`, after which nothing is a flag.
    """
    assert argv[:1] == ["query"], argv
    out, i = {}, 1
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            break
        if tok.startswith("-"):
            if "=" in tok:
                name, value = tok.split("=", 1)
            elif i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                name, value = tok, argv[i + 1]
                i += 1
            else:
                name, value = tok, None
            out[name] = value
        i += 1
    return out


def sql_of(f):
    return f.get("-e") or f.get("--sql")


PAIR = {"--purpose", "--session-id"}


def fallback_forms(block):
    """The commented alternatives in a block, each as a runnable snippet.

    An alternative is introduced by a comment label ending in `:` and runs
    until the next label. Uncommenting strips exactly `#` and one space and
    keeps everything else, trailing whitespace included, so a form that would
    break once a reader deletes the `#` also breaks here.
    """
    forms, current = [], None
    for raw in block.splitlines():
        m = re.match(r"\s*# ?(.*)$", raw)
        if not m:
            continue
        body = m.group(1)
        if body.rstrip().endswith(":") and not body.lstrip().startswith("skardi"):
            current = []
            forms.append(current)
        elif current is not None:
            current.append(body)
    return ["\n".join(f) for f in forms]


def audited_examples():
    """(block, runnable argv list, stderr) for every block with an audited query."""
    out = []
    for block in bash_blocks(text()):
        calls, err = run_bash(block)
        audited = [c for c in calls if c[:1] == ["query"] and PAIR & flags(c).keys()]
        if audited:
            out.append((block, audited, calls, err))
    return out


def test_task_probe_runs_before_the_first_audited_query():
    """The --task probe is a real command, separate from the pair probe, run before any audited query."""
    blocks = bash_blocks(text())
    probe = next(
        (i for i, b in enumerate(blocks)
         if any(ln.strip().startswith("skardi query --help | grep -q -- '--task'")
                for ln in b.splitlines())),
        None,
    )
    assert probe is not None, "no executable --task probe line in a bash block"
    assert "'--purpose'" not in next(
        ln for ln in blocks[probe].splitlines() if "'--task'" in ln
    ), "probe --task on its own line, not folded into the pair probe"
    first_audited = next(
        i for i, b in enumerate(blocks)
        if any(c[:1] == ["query"] and PAIR & flags(c).keys() for c in run_bash(b)[0])
    )
    assert probe < first_audited, "the --task probe must come before the first audited query"


def test_runnable_audited_queries_carry_the_same_task():
    """Run as written, each example sends the pair, its SQL, and one identical task, and nothing else runs."""
    examples = audited_examples()
    assert examples, "no runnable audited query found"
    tasks = set()
    for block, audited, calls, err in examples:
        assert not err.strip(), f"example fails under bash: {err}\n{block}"
        assert len(calls) == len(audited) == 1, f"one query per example block, got {calls}"
        f = flags(audited[0])
        assert PAIR <= f.keys() and all(f[k] for k in PAIR), f
        assert sql_of(f), f"audited query lost its SQL: {audited[0]}"
        assert f.get("--task"), f"audited query without --task: {audited[0]}"
        tasks.add(f["--task"])
    assert len(tasks) == 1, f"examples reword the task: {sorted(tasks)}"


def test_every_audited_example_has_both_fallback_forms():
    """Each example is followed by exactly a pair-only form and a v0.5.0 form, same SQL, each runnable."""
    for block, audited, _, _ in audited_examples():
        sql = sql_of(flags(audited[0]))
        forms = fallback_forms(block)
        assert len(forms) == 2, f"want 2 fallback forms, got {len(forms)}:\n{block}"
        seen = []
        for form in forms:
            calls, err = run_bash(form)
            assert not err.strip(), f"fallback fails once uncommented: {err}\n{form}"
            assert len(calls) == 1, f"fallback should run one query, got {calls}\n{form}"
            f = flags(calls[0])
            assert sql_of(f) == sql, f"fallback belongs to another query: {form}"
            seen.append(f)
        pair_only, v050 = seen
        assert PAIR <= pair_only.keys() and "--task" not in pair_only, pair_only
        assert not ((PAIR | {"--task"}) & v050.keys()), v050


def test_no_query_sends_task_without_the_pair():
    """The CLI refuses --task alone, in runnable and fallback forms alike."""
    for block in bash_blocks(text()):
        calls, _ = run_bash(block)
        for form in fallback_forms(block):
            calls += run_bash(form)[0]
        for argv in calls:
            if argv[:1] != ["query"] or "--help" in argv:
                continue
            f = flags(argv)
            if "--task" in f:
                assert PAIR <= f.keys(), argv


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  ok    {name}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {name}: {exc}")
    print(f"\n{'FAILED' if failures else 'all passed'} ({failures} failure(s))")
    raise SystemExit(bool(failures))
