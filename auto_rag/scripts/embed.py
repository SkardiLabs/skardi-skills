#!/usr/bin/env python3
"""Embed text into a Float32 list using the host's `skardi` CLI.

Embedding lives on the agent side, not in the server's SQL pipelines.
The reason: the published skardi-server-embedding images (and any release
without the embedding-UDF registration patch) reject `candle(...)`,
`gguf(...)`, and `remote_embed(...)` at plan-time even though they were
nominally built with the `embedding` feature. Computing vectors on the
agent side, where the user's locally-installed `skardi` CLI has the
matching feature flags, avoids that whole compatibility surface — and
the same code works whether the server runs as a local process, a Docker
container, or a Kubernetes pod.

Usage as a module:
    from embed import embed_text, parse_breadcrumb
    bc = parse_breadcrumb("/path/to/workspace")
    vec = embed_text("hello world", bc)        # returns list[float]

Usage as a CLI (for ad-hoc testing):
    python embed.py --workspace /path/to/workspace --text "hello world"
    # → prints the JSON array on stdout

Performance: each call shells out to `skardi query`, which is heavyweight
(loads the model on first call, caches afterwards). For bulk ingest, the
caller should reuse one process via the module API.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional


def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def parse_breadcrumb(workspace) -> dict:
    """Read <workspace>/.embedding.txt into a dict."""
    p = Path(workspace) / ".embedding.txt"
    if not p.is_file():
        die(f"{p} not found. Did you run setup_rag.py?")
    out = {}
    for line in p.read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _embed_sql(breadcrumb: dict, text_literal: str) -> str:
    """Build the SELECT SQL for a single embedding call.

    `text_literal` must already be SQL-quoted (single quotes, internal
    quotes doubled). Callers should always pass through `_quote_sql`."""
    udf = breadcrumb.get("udf")
    if udf == "candle":
        path = breadcrumb["model_path"]
        return f"SELECT candle('{path}', {text_literal})"
    if udf == "gguf":
        path = breadcrumb["model_path"]
        return f"SELECT gguf('{path}', {text_literal})"
    if udf == "remote_embed":
        args = breadcrumb["embedding_args"]
        return f"SELECT remote_embed({args}, {text_literal})"
    die(f"Unsupported udf in breadcrumb: {udf!r}")


def _quote_sql(s: str) -> str:
    """SQL-quote a string for inline use: single quotes, doubled internals."""
    return "'" + s.replace("'", "''") + "'"


# Regex to pluck a `[...]` array out of skardi query's box-drawn table
# output. The table format is enormously verbose but the float array is
# always on its own data row. Matching `[\d-]` first cheaply rejects the
# header rows ("...candle(Utf8(...)..."). Handles both - and . in floats.
_ARRAY_RE = re.compile(r"\[\s*-?\d[\d\.\-eE,\s+]*\]")


def _parse_query_output(stdout: str) -> list[float]:
    """Extract the first float array from `skardi query --sql ...` output."""
    m = _ARRAY_RE.search(stdout)
    if not m:
        snippet = stdout[:300].replace("\n", " ")
        raise ValueError(f"could not find a float array in skardi query output: {snippet!r}")
    arr = m.group(0)
    return json.loads(arr)


def embed_text(text: str, breadcrumb: dict, workspace: Optional[Path] = None) -> list[float]:
    """Return the embedding vector for `text` using the breadcrumb's UDF.

    Shells out to `skardi query` once. The CLI loads the model on the
    first call and caches it for subsequent calls in the same process —
    which is why bulk callers should reuse a single Python process and
    let the CLI's model cache amortise the cost across many `embed_text`
    calls. (Yes, that means we re-pay the load on every Python startup;
    a long-running ingest job is the right shape, not 500 short calls.)
    """
    sql = _embed_sql(breadcrumb, _quote_sql(text))
    cmd = ["skardi", "query", "--sql", sql]
    # Deliberately do NOT pass SKARDICONFIG. The query is a pure UDF
    # call — no data sources are referenced — so loading the workspace
    # ctx would only add a Postgres registration step that fails when
    # PG_USER/PG_PASSWORD aren't in the agent's env. Keeping the env
    # untouched gives us a clean SELECT-with-no-data-source path.
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        # When the UDF isn't compiled in, skardi prints a clear
        # "Invalid function 'candle'" — surface that verbatim.
        raise RuntimeError(
            f"skardi query failed (exit {proc.returncode}). "
            f"Most likely the CLI was installed without --features "
            f"{breadcrumb.get('udf')}. Install with:\n"
            f"  cargo install --locked --git https://github.com/SkardiLabs/skardi "
            f"--branch main skardi-cli --features candle\n"
            f"stderr: {proc.stderr.strip()}"
        )
    return _parse_query_output(proc.stdout)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--text", required=True)
    args = ap.parse_args()
    bc = parse_breadcrumb(args.workspace)
    vec = embed_text(args.text, bc, workspace=Path(args.workspace).expanduser().resolve())
    print(json.dumps(vec))


if __name__ == "__main__":
    main()
