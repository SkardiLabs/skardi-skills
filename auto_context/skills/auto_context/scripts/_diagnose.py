#!/usr/bin/env python3
"""Turn skardi-server's startup log into the cause, when we can prove one.

Only one failure mode is handled today, because it is the one that lies to
the user. Everything else is left to the log tail the caller already prints.

The lie
-------
A server built without the cargo feature that registers the workspace's
embedding UDF does not say so when the UDF sits inside a table-function
argument list. It reports a wrong argument count instead:

    Failed to load pipeline from ".../pipelines/search_hybrid.yaml"
    Caused by (level 1): Pipeline loading failed: Error during planning:
    sqlite_knn(table, vector_col, query_vec, k) expects 4 arguments, got 3.

The pipeline passes four. Nothing in that message points at the embedding
UDF, so the natural reading — "the template is malformed" — is wrong, and
the natural fix (edit the template) cannot work.

Why it happens (verified against datafusion-sql 52.5.0 on 2026-08-04,
`src/relation/mod.rs:152`, reached for a bare `name(args)` table factor):

    let args = func_args.args.into_iter()
        .flat_map(|arg| { ... self.sql_expr_to_logical_expr(expr, ...) })
        .collect::<Vec<_>>();

`flat_map` over a `Result` yields zero items for `Err`. So an argument that
fails to plan — `remote_embed(...)` when the `remote-embed` feature is off —
is discarded without a word, and the UDTF is handed three expressions. The
sibling `TableFactor::Function` arm at line 269 collects into
`Result<Vec<Expr>>` and propagates properly; this arm does not.

The same UDF in a plain SELECT list reports honestly ("Invalid function
'remote_embed'"), which is why `ingest` and `ingest_chunked` load fine and
then fail at execute. That asymmetry is what makes the arity error worth
decoding here rather than leaving it to the log.

`sqlite_fts` is not immune — it takes the identical planner path and fails
the same way. It survives today only because all of its arguments happen to
be plannable.
"""
import re
from pathlib import Path

# Each embedding UDF is registered behind a cargo feature on skardi-server.
# Mirrors feature_for_udf() in start_server.py; kept here so this module can
# be used with only the feature name in hand.
FEATURE_FOR_UDF = {
    "candle": "candle",
    "gguf": "gguf",
    "remote_embed": "remote-embed",
}
UDF_FOR_FEATURE = {v: k for k, v in FEATURE_FOR_UDF.items()}

# Table functions whose arguments go through the lossy planner path.
LOSSY_UDTFS = ("sqlite_knn", "sqlite_fts", "seekdb_fts")

_ARITY_RE = re.compile(
    r"\b(" + "|".join(LOSSY_UDTFS) + r")\([^)]*\) expects (\d+) arguments, got (\d+)"
)
_INVALID_FN_RE = re.compile(r"Invalid function '([^']+)'")
_PIPELINE_RE = re.compile(r'Failed to load pipeline from "([^"]+)"')


def _split_top_level_args(sql, func_name):
    """Return the argument substrings of the first `func_name(...)` call.

    Depth-aware and quote-aware, so commas inside nested calls or inside
    string literals don't split an argument. Returns None if the call isn't
    found or the parens never close.
    """
    match = re.search(r"\b" + re.escape(func_name) + r"\s*\(", sql)
    if not match:
        return None

    args, depth, current, in_quote = [], 1, [], False
    i = match.end()
    while i < len(sql):
        ch = sql[i]
        if in_quote:
            # '' is an escaped quote inside a SQL string literal.
            if ch == "'" and sql[i + 1:i + 2] == "'":
                current.append("''")
                i += 2
                continue
            if ch == "'":
                in_quote = False
            current.append(ch)
        elif ch == "'":
            in_quote = True
            current.append(ch)
        elif ch in "([":
            depth += 1
            current.append(ch)
        elif ch in ")]":
            depth -= 1
            if depth == 0:
                args.append("".join(current))
                return [a.strip() for a in args]
            current.append(ch)
        elif ch == "," and depth == 1:
            args.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    return None


def _gated_udf_in(text):
    """Name of a feature-gated embedding UDF called in `text`, if any."""
    for udf in FEATURE_FOR_UDF:
        if re.search(r"\b" + re.escape(udf) + r"\s*\(", text):
            return udf
    return None


def _rebuild_hint(feature):
    return (
        f"    cargo install --locked --path crates/server --features {feature}\n"
        f"  from a Skardi checkout (or --features rag for chunk + every\n"
        f"  embedding UDF at once), then re-run this script."
    )


def diagnose_startup_failure(log_text, feature=None):
    """Return an explanation of why the server died, or None.

    `feature` is the cargo feature the workspace needs (from
    feature_for_udf()); it lets the message name the UDF even when the log
    only shows the arity error. None is returned whenever the log does not
    match a cause this module can actually prove — an unexplained failure is
    better than a confident wrong guess.
    """
    expected_udf = UDF_FOR_FEATURE.get(feature) if feature else None

    # Case 1: the honest error. The server names the missing function.
    invalid = _INVALID_FN_RE.search(log_text)
    if invalid and invalid.group(1) in FEATURE_FOR_UDF:
        udf = invalid.group(1)
        gate = FEATURE_FOR_UDF[udf]
        return (
            f"Cause: this skardi-server was built without --features {gate}, so\n"
            f"  the {udf}() UDF is not registered. Every pipeline in this\n"
            f"  workspace embeds with {udf}(), so none of them can run.\n"
            f"  Rebuild with the feature:\n"
            f"{_rebuild_hint(gate)}\n"
            f"  Or re-run setup_context.py with an --embedding-udf whose feature\n"
            f"  your build does have."
        )

    # Case 2: the misleading arity error. Only claim a dropped argument when
    # the pipeline file really does pass the number the server says it wants.
    arity = _ARITY_RE.search(log_text)
    if not arity:
        return None
    udtf, expected, got = arity.group(1), int(arity.group(2)), int(arity.group(3))
    if got >= expected:
        return None

    pipeline_path = _PIPELINE_RE.search(log_text)
    written = culprit = None
    if pipeline_path:
        try:
            sql = Path(pipeline_path.group(1)).read_text()
        except OSError:
            sql = None
        if sql:
            args = _split_top_level_args(sql, udtf)
            if args is not None:
                written = len(args)
                culprit = next(
                    (u for u in (_gated_udf_in(a) for a in args) if u), None
                )

    if written is not None and written != expected:
        # The pipeline genuinely passes the wrong number of arguments. That is
        # an ordinary malformed-SQL error, not the planner dropping anything.
        return None

    udf = culprit or expected_udf
    if not udf:
        return None
    gate = FEATURE_FOR_UDF[udf]
    counted = (
        f"  The pipeline passes {written} — counted from the file"
        if written is not None
        else "  The rendered pipelines pass 4"
    )
    return (
        f"Cause: {udtf}() did not lose an argument you wrote. This\n"
        f"  skardi-server was built without --features {gate}, so {udf}()\n"
        f"  is not a registered function, and DataFusion silently DROPS a\n"
        f"  table-function argument it cannot plan (datafusion-sql 52.5.0,\n"
        f"  src/relation/mod.rs:152 — flat_map over a Result discards Err).\n"
        f"  The missing function surfaces as a wrong argument count.\n"
        f"{counted}; the planner delivered {got}.\n"
        f"  Editing the pipeline SQL will not fix this. Rebuild with:\n"
        f"{_rebuild_hint(gate)}\n"
        f"  Note: ingest / ingest-chunked call {udf}() too. They load without\n"
        f"  complaint and fail at execute for the same reason."
    )


def emit_startup_diagnosis(log_file, feature=None, stream=None):
    """Print a diagnosis for `log_file` to `stream`, if one can be proved.

    Silent when the log names no cause this module recognises, so callers can
    invoke it unconditionally on any startup failure.
    """
    import sys

    stream = stream or sys.stderr
    try:
        log_text = Path(log_file).read_text()
    except OSError:
        return
    message = diagnose_startup_failure(log_text, feature)
    if message:
        print(f"  {message}", file=stream)
