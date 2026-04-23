#!/usr/bin/env python3
"""Bulk-ingest an NDJSON file of chunks into a Skardi KB workspace.

One `skardi query` runs a single INSERT ... SELECT from the NDJSON so the
embedding model is loaded exactly once — not once per row. The AFTER INSERT
trigger in kb.db fans each row into the FTS5 and vec0 mirrors atomically.

For very large corpora, pass --batch-size to split the input into batches
(the script writes N temp NDJSON files and runs N INSERT statements). This
caps per-statement memory and keeps SQLite transaction sizes tractable.

Input must be NDJSON with fields `id`, `source`, `chunk_idx`, `content`
and a `.json` extension (the only extension DataFusion's JSON reader
recognises for our purposes).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def extract_embedding_expr(ingest_yaml_path):
    """Read the rendered ingest.yaml and return the full vec_to_binary(...)
    expression verbatim (with matched parens). The template embeds this call
    over the row's `content` column, so it transfers verbatim to bulk."""
    text = Path(ingest_yaml_path).read_text()
    idx = text.find("vec_to_binary(")
    if idx < 0:
        die(
            f"Could not locate `vec_to_binary(` in {ingest_yaml_path}. "
            f"Did you regenerate with a different embedding UDF?"
        )
    start_paren = text.index("(", idx)
    depth = 0
    for i in range(start_paren, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                expr = text[idx : i + 1]
                if "content" not in expr:
                    die(
                        f"Extracted {expr!r} does not reference `content`; "
                        f"bulk ingest expects the row-level call to embed the "
                        f"`content` column."
                    )
                return expr
    die(f"Unbalanced parens in {ingest_yaml_path} near `vec_to_binary(`")


def count_rows(ndjson_path):
    with Path(ndjson_path).open(encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def iter_batches(ndjson_path, batch_size, tmpdir):
    """Yields (batch_path, row_count) tuples of NDJSON files with a .json
    extension. When batch_size <= 0 yields the original file once."""
    if batch_size <= 0:
        yield str(ndjson_path), count_rows(ndjson_path)
        return

    with Path(ndjson_path).open(encoding="utf-8") as f:
        batch_idx = 0
        lines = []
        for line in f:
            if not line.strip():
                continue
            lines.append(line)
            if len(lines) >= batch_size:
                path = Path(tmpdir) / f"batch_{batch_idx:06d}.json"
                path.write_text("".join(lines), encoding="utf-8")
                yield str(path), len(lines)
                batch_idx += 1
                lines = []
        if lines:
            path = Path(tmpdir) / f"batch_{batch_idx:06d}.json"
            path.write_text("".join(lines), encoding="utf-8")
            yield str(path), len(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--workspace", required=True, help="KB workspace dir (from setup_kb.py)")
    ap.add_argument("--chunks", required=True, help="Path to NDJSON chunks (must be .json)")
    ap.add_argument(
        "--batch-size",
        type=int,
        default=5000,
        help="Chunks per INSERT statement. 0 = one statement for the whole file.",
    )
    args = ap.parse_args()

    if shutil.which("skardi") is None:
        die("`skardi` CLI not found on PATH")

    workspace = Path(args.workspace).expanduser().resolve()
    ctx = workspace / "ctx.yaml"
    ingest_yaml = workspace / "pipelines" / "ingest.yaml"
    if not ctx.is_file():
        die(f"{ctx} not found. Did you run setup_kb.py?")
    if not ingest_yaml.is_file():
        die(f"{ingest_yaml} not found. Did you run setup_kb.py?")

    chunks_file = Path(args.chunks).expanduser().resolve()
    if not chunks_file.is_file():
        die(f"{chunks_file} not found")
    if chunks_file.suffix.lower() != ".json":
        die(
            f"{chunks_file} must have a .json extension so DataFusion's JSON "
            f"reader recognises it. Rename it or re-run chunk_corpus.py with "
            f"--out ending in .json."
        )
    total = count_rows(chunks_file)
    if total == 0:
        die(f"{chunks_file} has no rows")

    embedding_expr = extract_embedding_expr(ingest_yaml)
    print(f"Using embedding expression: {embedding_expr}")
    print(f"Total chunks to ingest: {total}")

    env = os.environ.copy()
    env["SKARDICONFIG"] = str(workspace)
    if "SQLITE_VEC_PATH" not in env:
        try:
            import sqlite_vec

            env["SQLITE_VEC_PATH"] = sqlite_vec.loadable_path()
            print(f"  (derived SQLITE_VEC_PATH={env['SQLITE_VEC_PATH']})")
        except ImportError:
            die(
                "SQLITE_VEC_PATH is not set and sqlite_vec is not importable. "
                "Export SQLITE_VEC_PATH=<path to vec0 lib> before running."
            )

    with tempfile.TemporaryDirectory() as tmpdir:
        ingested = 0
        for batch_path, batch_rows in iter_batches(chunks_file, args.batch_size, tmpdir):
            sql = (
                "INSERT INTO kb.main.documents (id, source, chunk_idx, content, embedding) "
                "SELECT CAST(id AS BIGINT) AS id, source, CAST(chunk_idx AS BIGINT) AS chunk_idx, "
                f"content, {embedding_expr} "
                f"FROM '{batch_path}'"
            )
            print(f"  batch of {batch_rows} rows -> skardi query ...")
            proc = subprocess.run(
                ["skardi", "query", "--sql", sql],
                env=env,
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                sys.stderr.write(proc.stdout)
                sys.stderr.write(proc.stderr)
                die(f"skardi query failed on batch {batch_path} (exit {proc.returncode})")
            ingested += batch_rows

        print(f"Ingested {ingested}/{total} chunks.")

    print("Sanity check:")
    sanity = subprocess.run(
        [
            "skardi",
            "query",
            "--sql",
            "SELECT COUNT(*) AS rows, COUNT(DISTINCT source) AS files "
            "FROM kb.main.documents",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    sys.stdout.write(sanity.stdout)
    sys.stderr.write(sanity.stderr)


if __name__ == "__main__":
    main()
