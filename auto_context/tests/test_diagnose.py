#!/usr/bin/env python3
"""Focused unit tests for _diagnose.py (pure stdlib, no server needed).

The point of this guard is that the diagnosis must stay *conditional*. It
decodes a misleading arity error into "your server lacks a cargo feature",
which is a strong claim; if it ever starts firing on a genuinely malformed
pipeline it will send people chasing a rebuild they don't need. So the
negative cases matter at least as much as the positive ones.

Log fixtures are copied from real skardi-server output captured on
2026-08-04 (skardi @ 440ee1e, a binary built without --features remote-embed).

Run: python3 tests/test_diagnose.py
"""
import os
import sys
import tempfile

SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "skills", "auto_context", "scripts")
sys.path.insert(0, SCRIPTS)
import _diagnose  # noqa: E402

diagnose = _diagnose.diagnose_startup_failure
split_args = _diagnose._split_top_level_args


# Real search_vector.yaml body as setup_context.py renders it for
# --embedding-udf remote_embed.
VECTOR_YAML = """kind: pipeline

metadata:
  name: "search-vector"
  version: "1.0.0"

spec:
  query: |
    SELECT d.id, d.source, d.chunk_idx, d.content, v._score AS distance
    FROM sqlite_knn('kb.main.documents_vec', 'embedding',
        (SELECT remote_embed('openai','text-embedding-3-small', {query})),
        {limit}) v
    LEFT JOIN kb.main.documents d ON d.id = v.id
    ORDER BY v._score
"""

# Same pipeline with the k argument deleted outright — a real template bug
# (three passed, three delivered), which must NOT be blamed on a feature flag.
MALFORMED_YAML = VECTOR_YAML.replace("})),\n        {limit}) v", "}))) v")


def arity_log(path, udtf="sqlite_knn", expected=4, got=3):
    return (
        f'ERROR skardi_server: Failed to load pipeline from "{path}"\n'
        f"ERROR skardi_server:    Caused by (level 1): Pipeline loading failed: "
        f"Error during planning: {udtf}(table, vector_col, query_vec, k) "
        f"expects {expected} arguments, got {got}. The distance metric is "
        f"configured at vec0 table creation time.\n"
    )


def write(tmp, name, text):
    path = os.path.join(tmp, name)
    with open(path, "w") as fh:
        fh.write(text)
    return path


def test_arity_error_is_decoded_to_the_missing_feature():
    with tempfile.TemporaryDirectory() as tmp:
        path = write(tmp, "search_vector.yaml", VECTOR_YAML)
        msg = diagnose(arity_log(path), "remote-embed")
    assert msg, "the reported failure must produce a diagnosis"
    assert "remote-embed" in msg, msg
    assert "remote_embed()" in msg, msg
    # Must steer away from the wrong fix, which is the whole reason it exists.
    assert "will not fix" in msg, msg
    assert "The pipeline passes 4" in msg, msg


def test_fts_arity_error_is_decoded_too():
    """sqlite_fts takes the identical lossy planner path — verified 2026-08-04."""
    fts_yaml = VECTOR_YAML.replace(
        "sqlite_knn('kb.main.documents_vec', 'embedding',",
        "sqlite_fts('kb.main.documents_fts', 'content',",
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = write(tmp, "search_fulltext.yaml", fts_yaml)
        msg = diagnose(arity_log(path, udtf="sqlite_fts"), "remote-embed")
    assert msg and "sqlite_fts()" in msg, msg


def test_genuinely_malformed_pipeline_is_not_blamed_on_the_feature():
    """Three arguments written, three delivered: ordinary SQL bug, no guess."""
    with tempfile.TemporaryDirectory() as tmp:
        path = write(tmp, "search_vector.yaml", MALFORMED_YAML)
        msg = diagnose(arity_log(path), "remote-embed")
    assert msg is None, f"must stay silent on a real template bug, got: {msg}"


def test_invalid_function_error_is_reported_directly():
    msg = diagnose("Error during planning: Invalid function 'remote_embed'.", None)
    assert msg and "--features remote-embed" in msg, msg
    # The honest path should not repeat the DataFusion arg-dropping story.
    assert "DROPS" not in msg, msg


def test_unknown_function_outside_the_gated_set_is_not_claimed():
    assert diagnose("Error during planning: Invalid function 'my_udf'.", None) is None


def test_unrelated_failure_produces_nothing():
    assert diagnose("Error: Address already in use (os error 48)", "remote-embed") is None


def test_no_guess_without_a_readable_pipeline_or_a_feature():
    assert diagnose(arity_log("/nonexistent/p.yaml"), None) is None


def test_falls_back_to_the_workspace_feature_when_the_file_is_gone():
    msg = diagnose(arity_log("/nonexistent/p.yaml"), "remote-embed")
    assert msg and "remote_embed()" in msg, msg
    assert "The rendered pipelines pass 4" in msg, msg


def test_split_top_level_args_is_depth_and_quote_aware():
    sql = ("sqlite_knn('t', 'embedding', "
           "(SELECT remote_embed('openai','a,b', {q})), {limit})")
    args = split_args(sql, "sqlite_knn")
    assert len(args) == 4, args
    assert args[0] == "'t'"
    assert args[2].startswith("(SELECT remote_embed("), args[2]
    assert args[3] == "{limit}"
    # A comma inside a string literal must not split an argument.
    assert "'a,b'" in args[2]


def test_split_top_level_args_handles_array_literals():
    args = split_args("sqlite_knn('t', 'embedding', [0.1, 0.2, 0.3], 10)", "sqlite_knn")
    assert len(args) == 4, args
    assert args[2] == "[0.1, 0.2, 0.3]"


def test_split_top_level_args_returns_none_when_absent():
    assert split_args("SELECT 1", "sqlite_knn") is None


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
