#!/usr/bin/env python3
"""Walk a corpus directory and ingest every file into the KB in one bulk INSERT.

Replaces the old chunk_corpus.py + bulk_ingest.py pair. With Skardi 0.4.0+
the chunking and embedding both happen inside SQL via the chunk() UDF and
the configured embedding UDF — there is no out-of-band Python chunker
anymore. We just:

  1. Walk the corpus, strip front-matter, build a manifest.json (NDJSON)
     with one object per source file: {doc_id, source, content}.
  2. Run ONE `skardi query` whose INSERT ... SELECT does, per row of the
     manifest, UNNEST(chunk(...)) → {{EMBEDDING_CALL_INGEST}} → INSERT.
     The embedding model is loaded exactly once for the whole corpus.

Stable ids: doc_id is a 53-bit blake2b hash of the relative source path,
and per-chunk id is doc_id*1000 + chunk_idx — the same scheme the
ingest-chunked pipeline uses for single-doc calls. Re-ingesting the same
file produces identical ids, so a second run rejects with
"UNIQUE constraint failed" (that's the right behaviour — use --force on
setup_kb.py to rebuild from scratch, or DELETE WHERE source = '<path>'
before re-running for a single file).

Why NDJSON with `.json` extension: DataFusion's JSON reader only
recognises `.json` as the input extension, and JSON escapes embedded
newlines inside content cells (CSV doesn't, and real markdown bodies
have plenty of newlines).
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_INCLUDE = "*.md,*.markdown,*.txt,*.rst"
DEFAULT_CHUNK_SIZE = 1200
DEFAULT_OVERLAP = 200
DEFAULT_CHUNK_MODE = "markdown"

FRONT_MATTER_RE = re.compile(r"^---\n.*?\n---\n", re.DOTALL)


def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def stable_doc_id(source_rel_path):
    """53-bit positive int derived from the relative source path.

    53 bits because chunk ids are doc_id*1000 + chunk_idx and we want the
    final id to fit signed BIGINT comfortably even for docs with hundreds
    of chunks — 53 + ceil(log2(1000)) = 63 bits, leaves the sign bit.
    """
    h = hashlib.blake2b(source_rel_path.encode(), digest_size=8).digest()
    return int.from_bytes(h, "big") & ((1 << 53) - 1)


def strip_front_matter(text):
    return FRONT_MATTER_RE.sub("", text, count=1)


def iter_files(corpus_root, patterns):
    pats = [p.strip() for p in patterns.split(",") if p.strip()]
    seen = set()
    for pat in pats:
        for p in sorted(corpus_root.rglob(pat)):
            if p.is_file() and p not in seen:
                seen.add(p)
                yield p


def read_breadcrumb(workspace):
    """Read the embedding breadcrumb setup_kb.py left in the workspace.

    We need the rendered ingest pipeline's inline embedding call (with the
    absolute model path baked in) so we can reuse exactly that expression
    in the bulk INSERT. Re-deriving it from --model-path / --embedding-udf
    flags would mean keeping two copies of the substitution logic in sync.
    """
    p = workspace / ".embedding.txt"
    if not p.is_file():
        die(f"{p} not found. Did you run setup_kb.py first?")
    out = {}
    for line in p.read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def build_embedding_expr(breadcrumb, content_col="content"):
    """Construct the inline embedding call over the given column reference."""
    udf = breadcrumb.get("udf")
    if udf == "candle":
        return f"candle('{breadcrumb['model_path']}', {content_col})"
    if udf == "gguf":
        return f"gguf('{breadcrumb['model_path']}', {content_col})"
    if udf == "remote_embed":
        return f"remote_embed({breadcrumb['embedding_args']}, {content_col})"
    die(f"Unsupported udf in breadcrumb: {udf!r}")


def build_manifest(corpus, include, manifest_path):
    n_files = 0
    skipped = []
    with manifest_path.open("w", encoding="utf-8") as f:
        for path in iter_files(corpus, include):
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                skipped.append(str(path.relative_to(corpus)))
                continue
            text = strip_front_matter(text).strip()
            if not text:
                continue
            rel = str(path.relative_to(corpus))
            obj = {
                "doc_id": stable_doc_id(rel),
                "source": rel,
                "content": text,
            }
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
            n_files += 1
    return n_files, skipped


def bulk_ingest_sql(manifest_path, embed_expr, chunk_mode, chunk_size, overlap):
    """Construct the one-shot INSERT for the whole corpus.

    The two ROW_NUMBER() OVER (PARTITION BY doc_id ORDER BY 1) calls in
    the inner subquery yield identical values (deterministic per partition);
    we promote that to a named column so the outer projection can reuse it
    without re-computing. The outer SELECT is the SELECT-wrapper pattern
    DataFusion's INSERT planner needs to keep the embedding projection from
    being dropped on schema validation.
    """
    return f"""
INSERT INTO kb.main.documents (id, source, chunk_idx, content, embedding)
SELECT id, source, chunk_idx, content, vec_to_binary({embed_expr})
FROM (
  SELECT
    doc_id * 1000 + chunk_idx AS id,
    source,
    chunk_idx,
    content
  FROM (
    SELECT
      doc_id,
      source,
      ROW_NUMBER() OVER (PARTITION BY doc_id ORDER BY 1) - 1 AS chunk_idx,
      chunk_text                                              AS content
    FROM (
      SELECT
        doc_id,
        source,
        UNNEST(chunk('{chunk_mode}', content, {chunk_size}, {overlap})) AS chunk_text
      FROM '{manifest_path}'
    ) c
  ) c2
) AS t
""".strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--workspace", required=True, help="KB workspace dir (from setup_kb.py)")
    ap.add_argument("--corpus", required=True, help="Root directory of documents")
    ap.add_argument(
        "--include",
        default=DEFAULT_INCLUDE,
        help=f"Comma-separated glob patterns (default: {DEFAULT_INCLUDE})",
    )
    ap.add_argument(
        "--chunk-mode",
        default=DEFAULT_CHUNK_MODE,
        choices=["markdown", "character"],
        help=(
            "Splitter mode passed to chunk(). 'markdown' prefers heading / "
            "paragraph / code-block boundaries (good for .md and structured "
            ".txt). 'character' is a generic recursive splitter — paragraph → "
            "sentence → word — for unstructured prose. Default: markdown."
        ),
    )
    ap.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE,
                    help=f"Target max chunk length in characters (default: {DEFAULT_CHUNK_SIZE}).")
    ap.add_argument("--overlap", type=int, default=DEFAULT_OVERLAP,
                    help=f"Characters of overlap between adjacent chunks (default: {DEFAULT_OVERLAP}).")
    ap.add_argument(
        "--manifest",
        default=None,
        help="Where to write the manifest NDJSON. Default: <workspace>/manifest.json",
    )
    args = ap.parse_args()

    if shutil.which("skardi") is None:
        die("`skardi` CLI not found on PATH")

    workspace = Path(args.workspace).expanduser().resolve()
    if not (workspace / "ctx.yaml").is_file():
        die(f"{workspace}/ctx.yaml not found. Did you run setup_kb.py?")

    corpus = Path(args.corpus).expanduser().resolve()
    if not corpus.is_dir():
        die(f"--corpus {corpus} is not a directory")

    if args.overlap >= args.chunk_size:
        die(f"--overlap ({args.overlap}) must be strictly less than --chunk-size ({args.chunk_size})")

    manifest = Path(args.manifest).expanduser().resolve() if args.manifest \
        else workspace / "manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)

    print(f"[1/3] Walking corpus and writing manifest -> {manifest}")
    n_files, skipped = build_manifest(corpus, args.include, manifest)
    if n_files == 0:
        die(f"No matching files in {corpus} for patterns {args.include!r}")
    print(f"  wrote {n_files} source documents")
    if skipped:
        head = ", ".join(skipped[:5])
        more = f" (+{len(skipped) - 5} more)" if len(skipped) > 5 else ""
        print(f"  skipped {len(skipped)} non-UTF8 files: {head}{more}")

    print(f"[2/3] Reading embedding breadcrumb")
    breadcrumb = read_breadcrumb(workspace)
    embed_expr = build_embedding_expr(breadcrumb, "content")
    print(f"  udf={breadcrumb.get('udf')}  expr over `content`: {embed_expr}")

    sql = bulk_ingest_sql(manifest, embed_expr, args.chunk_mode, args.chunk_size, args.overlap)

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

    print(f"[3/3] Running bulk INSERT (one statement; model loads once)")
    proc = subprocess.run(
        ["skardi", "query", "--sql", sql],
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        die(
            f"skardi query failed (exit {proc.returncode}). Common causes: "
            f"chunk() UDF not available — needs Skardi >= 0.4.0; "
            f"embedding UDF not compiled in — rebuild with the matching "
            f"--features flag; UNIQUE constraint — corpus already ingested, "
            f"re-run setup_kb.py with --force or DELETE first."
        )

    print()
    print("Sanity check:")
    sanity = subprocess.run(
        [
            "skardi", "query", "--sql",
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
