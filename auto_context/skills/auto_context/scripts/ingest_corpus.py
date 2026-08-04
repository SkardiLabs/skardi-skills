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

from _platform import require_supported_platform

DEFAULT_INCLUDE = "*.md,*.markdown,*.txt,*.rst"
DEFAULT_CHUNK_SIZE = 1200
DEFAULT_OVERLAP = 200

# Largest JSON request body skardi-server will accept. Measured against a
# running 0.4.0 server on 2026-08-04: a 2000 KB body returns 200, a 2100 KB
# body returns `413 Payload Too Large`. skardi-server sets no limit of its
# own (no DefaultBodyLimit / body_limit anywhere in crates/), so this is
# axum 0.7's default of 2 MiB.
#
# Worth checking client-side rather than just letting the POST fail, because
# the error the user gets otherwise depends on how far over the line they
# are: slightly over yields the 413 (readable), far over means the server
# closes the connection while we are still writing, and urllib reports
# `Broken pipe` or `Connection reset by peer` with no mention of size. A
# 12 MB markdown file produced exactly that — an unreadable error for an
# entirely understandable input.
SERVER_BODY_LIMIT = 2 * 1024 * 1024

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


def content_hash(text):
    """Stable SHA-256 of the (front-matter-stripped) file body.

    Recorded in the progress manifest so a re-run can tell an unchanged file
    (skip) from one whose content changed since it was ingested (surface it —
    see main()). Without this the manifest only knew ok/err, so an edited file
    that was previously ok was skipped forever and the corpus silently drifted
    out of sync with the source it came from."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
    """Return {source: {"status": str, "hash": str|None}}.

    Back-compat: older manifests stored a bare string per source ("ok" /
    "err: ...") with no content hash. Normalise those to the dict shape with
    hash=None (unknown), so a file ingested by an older version is treated as
    ok-but-unhashed until it is next hashed on this run."""
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError:
        path.rename(path.with_suffix(".json.bak"))
        return {}
    normalised = {}
    for source, val in raw.items():
        if isinstance(val, str):
            normalised[source] = {"status": val, "hash": None}
        elif isinstance(val, dict):
            normalised[source] = {"status": val.get("status", ""), "hash": val.get("hash")}
    return normalised


def save_progress(path, progress):
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(progress))
    tmp.replace(path)


def build_body(doc_id, source, content, chunk_size, overlap):
    """Serialise one document into the JSON request body.

    Built during the work-list pass rather than at POST time so the size can
    be checked against SERVER_BODY_LIMIT before the run starts. The size has
    to be measured on the encoded body, not on the file: json.dumps escapes
    newlines and (with the default ensure_ascii) expands every non-ASCII
    character to a \\uXXXX escape, so a CJK document is roughly twice its
    on-disk size by the time it reaches the wire."""
    return json.dumps({
        "doc_id": doc_id,
        "source": source,
        "content": content,
        "chunk_size": chunk_size,
        "overlap": overlap,
    }).encode("utf-8")


def post_doc(endpoint, body, timeout):
    """POST one already-serialised document to /ingest-chunked/execute.

    The server runs the rendered ingest-chunked pipeline: UNNEST(chunk(
    'markdown', content, chunk_size, overlap)) → embed each chunk → INSERT.
    A success response means every chunk for this document was committed
    in one transaction."""
    req = urllib.request.Request(
        endpoint,
        data=body,
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
        if "UNIQUE constraint failed" in err_body or "duplicate key" in err_body:
            # This one is by design (doc ids are derived from the source path,
            # so a re-ingest collides) but the raw message is a SQLite
            # constraint dump that reads like a corruption. Name the actual
            # situation instead — it is the error a user gets after deleting
            # the manifest to force a re-run.
            return False, (
                "already in the index — the doc id is derived from the source "
                "path, so re-ingesting the same file collides. Delete its rows "
                "first (DELETE FROM <table> WHERE source = '...') or restore "
                "the progress manifest so it is skipped instead."
            )
        return False, f"HTTP {e.code}: {err_body[:300]}"
    except urllib.error.URLError as e:
        return False, f"connection error: {e}"
    except Exception as e:  # noqa: BLE001 — surfaced verbatim into manifest
        return False, f"unexpected: {type(e).__name__}: {e}"


def main():
    require_supported_platform("ingest_corpus.py")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--workspace", required=True, help="Workspace dir from setup_context.py")
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
        die(f"{workspace}/ctx.yaml not found. Did you run setup_context.py?")
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
    #
    # Every file that matched --include has to end up in exactly one bucket.
    # This used to drop empty files silently and let a single unreadable file
    # abort the run with a raw PermissionError traceback, so a corpus of
    # front-matter-only stubs reported "total: 0 / nothing to do" — which
    # reads as success — and one chmod-000 file lost the whole run.
    work = []
    skipped = {"not UTF-8": [], "unreadable": [], "no text content": [],
               "too large for one request": []}
    matched = 0
    for path in iter_files(corpus, args.include):
        matched += 1
        rel = str(path.relative_to(corpus))
        try:
            # utf-8-sig, not utf-8: a UTF-8 BOM would otherwise sit in front of
            # the `---` and defeat front-matter stripping, embedding the YAML
            # header into the indexed text. Files without a BOM decode
            # identically either way.
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            skipped["not UTF-8"].append(rel)
            continue
        except OSError as e:
            # Permission denied, I/O error, symlink loop. One bad file is not
            # a reason to abandon the other N-1.
            skipped["unreadable"].append(f"{rel} ({e.strerror or e})")
            continue
        text = strip_front_matter(text).strip()
        if not text:
            skipped["no text content"].append(rel)
            continue
        body = build_body(stable_doc_id(rel), rel, text,
                          args.chunk_size, args.overlap)
        if len(body) > SERVER_BODY_LIMIT:
            skipped["too large for one request"].append(
                f"{rel} ({len(body) / 1024 / 1024:.1f} MB serialised)")
            continue
        work.append({"source": rel, "body": body, "hash": content_hash(text)})
    if args.limit > 0:
        work = work[: args.limit]

    for reason, names in skipped.items():
        if not names:
            continue
        head = ", ".join(names[:5])
        more = f" (+{len(names) - 5} more)" if len(names) > 5 else ""
        print(f"  skipped {len(names)} {reason}: {head}{more}")
    if skipped["too large for one request"]:
        print(f"  note: skardi-server accepts request bodies up to "
              f"{SERVER_BODY_LIMIT // 1024 // 1024} MiB and one document is one "
              f"request. Split those files into smaller documents; there is no "
              f"client-side splitter (chunking happens server-side, after the "
              f"whole document arrives).")

    if matched == 0:
        # Not "nothing to do" — the user asked for a corpus to be ingested and
        # no file was even a candidate. Almost always a wrong --include or the
        # wrong directory, so say what was searched for.
        present = sum(1 for p in corpus.rglob("*") if p.is_file())
        die(
            f"no files under {corpus} matched --include {args.include!r} "
            f"({present} file(s) are present). Fix the patterns or the path."
        )

    # Split the work list into what to ingest now vs. what changed since it was
    # last ingested. A file is pending when it was never ingested or its last
    # attempt errored. A file whose stored hash differs from its current content
    # is CHANGED — we do NOT auto-ingest it, because its rows still exist under
    # stable ids and a re-POST would collide on the primary key; re-ingesting
    # means deleting the old rows first, which is the user's call (see the
    # warning below). Files ingested by an older version have hash=None; we
    # backfill the hash in place so future edits are detectable, but treat them
    # as ok for this run because we cannot know whether they changed.
    pending = []
    changed = []
    backfilled = 0
    for w in work:
        entry = progress.get(w["source"])
        if entry is None or entry.get("status") != "ok":
            pending.append(w)
            continue
        stored_hash = entry.get("hash")
        if stored_hash is None:
            progress[w["source"]]["hash"] = w["hash"]  # backfill, don't re-ingest
            backfilled += 1
        elif stored_hash != w["hash"]:
            changed.append(w["source"])

    if backfilled:
        # Persist the backfill NOW, not only on the ingest path. A run with
        # nothing to ingest returns early, and without this the upgraded
        # hashes were dropped — so a manifest written by an older version
        # stayed hash-less forever and edits to those files could never be
        # detected, which is the whole point of recording the hash.
        save_progress(progress_path, progress)
        print(f"  backfilled content hashes for {backfilled} file(s) from an "
              f"older manifest (not re-ingested — their content is unknown to "
              f"this version, so edits made before now cannot be detected)")

    if changed:
        save_progress(progress_path, progress)
        head = "\n    ".join(changed[:10])
        more = f"\n    (+{len(changed) - 10} more)" if len(changed) > 10 else ""
        print(
            f"\n  WARNING: {len(changed)} file(s) changed since they were ingested:\n"
            f"    {head}{more}\n"
            f"  These are NOT re-ingested automatically: their chunks still exist\n"
            f"  under stable ids, so a re-POST would fail on the primary key. To\n"
            f"  refresh them, delete their existing rows and drop their manifest\n"
            f"  entries, then re-run this script. For each changed <source>:\n"
            f"    DELETE FROM <table> WHERE source = '<source>';\n"
            f"  (or DELETE the whole set in one WHERE source IN (...)), then remove\n"
            f"  those keys from {progress_path}.\n"
        )

    total_skipped = sum(len(v) for v in skipped.values())
    print(f"  matched: {matched}  ingestable: {len(work)}  skipped: {total_skipped}  "
          f"already ok: {len(work) - len(pending) - len(changed)}  "
          f"changed (see warning): {len(changed)}  to ingest: {len(pending)}")

    if not work:
        # Files matched but none survived. Distinct from "already ingested":
        # nothing is in the index as a result of this run, so exiting 0 here
        # would report success for a corpus that produced no context at all.
        die(f"all {matched} matched file(s) were skipped — see the reasons above. "
            f"Nothing was ingested.")

    if not pending:
        print("  nothing to do (every ingestable file is already ok in the manifest)")
        return

    started = time.time()
    last_save = started
    ok = 0
    failed = []

    def _record(item, success, err):
        nonlocal ok, last_save
        if success:
            progress[item["source"]] = {"status": "ok", "hash": item["hash"]}
            ok += 1
        else:
            progress[item["source"]] = {"status": f"err: {err}", "hash": None}
            failed.append((item["source"], err))
        if time.time() - last_save > 2.0:
            save_progress(progress_path, progress)
            last_save = time.time()
        done = ok + len(failed)
        if done % 5 == 0 or done == len(pending):
            print(f"    {done}/{len(pending)} (ok={ok} failed={len(failed)})")

    if args.concurrency <= 1:
        for item in pending:
            success, err = post_doc(endpoint, item["body"], args.timeout)
            _record(item, success, err)
    else:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {
                pool.submit(post_doc, endpoint, item["body"], args.timeout): item
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
