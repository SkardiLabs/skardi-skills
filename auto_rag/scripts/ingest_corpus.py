#!/usr/bin/env python3
"""Walk a corpus directory and ingest every file into skardi-server.

Replaces the old chunk_corpus.py + embed.py + http_ingest.py trio. With
Skardi 0.4.0's chunk() UDF and the skardi-server-rag image (which bundles
chunk + embedding), the server now does both chunking and embedding inline
inside one INSERT — there is no client-side chunker, and no client-side
embedding step. We just:

  1. Walk the corpus, strip front-matter, build a per-file work list of
     {doc_id, source, content}.
  2. For each file, POST to /ingest-chunked/execute with chunk_size +
     overlap. The server runs ONE INSERT that UNNEST(chunk('markdown',
     content, ...)) → embed → write per chunk.

The unit of work is a file (not a chunk), so the progress manifest at
<workspace>/ingest_progress.json is much smaller than it used to be —
keyed by source path. Re-running skips files already ingested. To
re-ingest a changed file, DELETE FROM <table> WHERE source = '...'
first (or remove the entry from the manifest and let stable doc ids
trip the unique-key check on retry).

Why HTTP rather than bulk SQL: the same reason that has not changed —
the server may run on a different machine than this script (Docker,
Kubernetes), so we cannot assume it can read a manifest file from the
local filesystem. Per-file POST works regardless of where the server is.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

DEFAULT_INCLUDE = "*.md,*.markdown,*.txt,*.rst"
DEFAULT_CHUNK_SIZE = 1200
DEFAULT_OVERLAP = 200

FRONT_MATTER_RE = re.compile(r"^---\n.*?\n---\n", re.DOTALL)


def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def stable_doc_id(source_rel_path):
    """53-bit positive int derived from the relative source path.

    53 bits because the ingest-chunked pipeline computes per-chunk id as
    doc_id*1000 + chunk_idx. We want the final id to fit signed BIGINT
    even when a single document produces hundreds of chunks: 53 + ~10 bits
    leaves the sign bit alone."""
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


def _ensure_localhost_no_proxy(host: str) -> None:
    """Make urllib bypass the user's HTTP proxy when posting to localhost.

    Many dev environments have a transparent SOCKS / HTTP proxy on
    127.0.0.1 (mihomo, clash, corp proxies). Without this, every ingest
    POST routes through the proxy — which can't reach localhost — and
    fails with HTTP 502. We only override for local hops; non-local
    traffic still goes through the user's proxy chain unmodified."""
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return
    existing = {
        s.strip().lower()
        for s in (os.environ.get("NO_PROXY", "") + "," + os.environ.get("no_proxy", "")).split(",")
        if s.strip()
    }
    additions = ["localhost", "127.0.0.1", "::1"]
    new = sorted(existing.union(a.lower() for a in additions))
    os.environ["NO_PROXY"] = ",".join(new)
    os.environ["no_proxy"] = ",".join(new)


def load_progress(path):
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        path.rename(path.with_suffix(".json.bak"))
        return {}


def save_progress(path, progress):
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(progress))
    tmp.replace(path)


def post_doc(endpoint, doc_id, source, content, chunk_size, overlap, timeout):
    """POST one document to /ingest-chunked/execute.

    The server runs the rendered ingest-chunked pipeline: UNNEST(chunk(
    'markdown', content, chunk_size, overlap)) → embed each chunk → INSERT.
    A success response means every chunk for this document was committed
    in one transaction."""
    body = {
        "doc_id": doc_id,
        "source": source,
        "content": content,
        "chunk_size": chunk_size,
        "overlap": overlap,
    }
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
            if not payload.get("success", True):
                return False, f"server returned success=false: {payload.get('error')}"
            return True, None
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8")
        except Exception:
            err_body = ""
        return False, f"HTTP {e.code}: {err_body[:300]}"
    except urllib.error.URLError as e:
        return False, f"connection error: {e}"
    except Exception as e:  # noqa: BLE001 — surfaced verbatim into manifest
        return False, f"unexpected: {type(e).__name__}: {e}"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--workspace", required=True, help="Workspace dir from setup_rag.py")
    ap.add_argument("--corpus", required=True, help="Root directory of documents")
    ap.add_argument(
        "--include",
        default=DEFAULT_INCLUDE,
        help=f"Comma-separated glob patterns (default: {DEFAULT_INCLUDE})",
    )
    ap.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE,
                    help=f"Target max chunk length in characters (default: {DEFAULT_CHUNK_SIZE}).")
    ap.add_argument("--overlap", type=int, default=DEFAULT_OVERLAP,
                    help=f"Characters of overlap between adjacent chunks (default: {DEFAULT_OVERLAP}).")
    ap.add_argument(
        "--port",
        type=int,
        default=None,
        help=(
            "Server port. Defaults to whatever start_server.py wrote to "
            "<workspace>/server.port; falls back to 8080 if neither is set."
        ),
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--concurrency", type=int, default=1, help="Inflight POSTs (default: 1)")
    ap.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help=(
            "Per-request timeout in seconds (default: 300). One POST chunks "
            "+ embeds an entire document, which may include a model cold-start "
            "on the first request — keep this generous."
        ),
    )
    ap.add_argument("--limit", type=int, default=0, help="Only ingest the first N files (0 = all).")
    args = ap.parse_args()

    workspace = Path(args.workspace).expanduser().resolve()
    if not (workspace / "ctx.yaml").is_file():
        die(f"{workspace}/ctx.yaml not found. Did you run setup_rag.py?")
    corpus = Path(args.corpus).expanduser().resolve()
    if not corpus.is_dir():
        die(f"--corpus {corpus} is not a directory")
    if args.overlap >= args.chunk_size:
        die(f"--overlap ({args.overlap}) must be strictly less than --chunk-size ({args.chunk_size})")

    progress_path = workspace / "ingest_progress.json"

    port = args.port
    if port is None:
        port_file = workspace / "server.port"
        if port_file.is_file():
            try:
                port = int(port_file.read_text().strip())
                print(f"  port:        {port} (from {port_file})")
            except ValueError:
                pass
        if port is None:
            port = 8080
            print(f"  port:        {port} (default — start_server.py didn't leave a server.port)")

    _ensure_localhost_no_proxy(args.host)
    endpoint = f"http://{args.host}:{port}/ingest-chunked/execute"
    print(f"  endpoint:    {endpoint}")
    print(f"  corpus:      {corpus}")
    print(f"  manifest:    {progress_path}")
    print(f"  concurrency: {args.concurrency}")
    print(f"  chunk_size:  {args.chunk_size}  overlap: {args.overlap}")

    progress = load_progress(progress_path)

    # Build the work list. Each entry is one source file; the server will
    # split it into chunks server-side via chunk('markdown', ...).
    work = []
    skipped = []
    for path in iter_files(corpus, args.include):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            skipped.append(str(path.relative_to(corpus)))
            continue
        text = strip_front_matter(text).strip()
        if not text:
            continue
        rel = str(path.relative_to(corpus))
        work.append({
            "doc_id": stable_doc_id(rel),
            "source": rel,
            "content": text,
        })
    if args.limit > 0:
        work = work[: args.limit]

    if skipped:
        head = ", ".join(skipped[:5])
        more = f" (+{len(skipped) - 5} more)" if len(skipped) > 5 else ""
        print(f"  skipped {len(skipped)} non-UTF8 files: {head}{more}")

    pending = [w for w in work if w["source"] not in progress or progress[w["source"]] != "ok"]
    print(f"  total: {len(work)}  skipped (already ok): {len(work) - len(pending)}  to ingest: {len(pending)}")

    if not pending:
        print("  nothing to do")
        return

    started = time.time()
    last_save = started
    ok = 0
    failed = []

    def _record(item, success, err):
        nonlocal ok, last_save
        if success:
            progress[item["source"]] = "ok"
            ok += 1
        else:
            progress[item["source"]] = f"err: {err}"
            failed.append((item["source"], err))
        if time.time() - last_save > 2.0:
            save_progress(progress_path, progress)
            last_save = time.time()
        done = ok + len(failed)
        if done % 5 == 0 or done == len(pending):
            print(f"    {done}/{len(pending)} (ok={ok} failed={len(failed)})")

    if args.concurrency <= 1:
        for item in pending:
            success, err = post_doc(
                endpoint, item["doc_id"], item["source"], item["content"],
                args.chunk_size, args.overlap, args.timeout,
            )
            _record(item, success, err)
    else:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {
                pool.submit(
                    post_doc, endpoint, item["doc_id"], item["source"],
                    item["content"], args.chunk_size, args.overlap, args.timeout,
                ): item
                for item in pending
            }
            for fut in as_completed(futures):
                item = futures[fut]
                success, err = fut.result()
                _record(item, success, err)

    save_progress(progress_path, progress)
    elapsed = time.time() - started
    rate = (ok + len(failed)) / elapsed if elapsed > 0 else 0
    print(f"  done in {elapsed:.1f}s ({rate:.2f} files/s)  ok={ok}  failed={len(failed)}")

    if failed:
        print("  failures (first 10):")
        for src, err in failed[:10]:
            print(f"    {src}  {err}")
        sys.exit(1)


if __name__ == "__main__":
    main()
