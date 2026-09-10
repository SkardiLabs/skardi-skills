#!/usr/bin/env python3
"""Guard the retrieval skill's copy-paste paths for audit intent.

The skill is a public procedure. These checks keep its first query runnable
and prevent a released v0.5.0 user from reporting audit flags their binary
cannot send.

Run: python3 tests/test_retrieval_ai_context_examples.py
"""
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
