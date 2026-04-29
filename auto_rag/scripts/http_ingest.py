#!/usr/bin/env python3
"""POST chunks from an NDJSON file into skardi-server's /ingest/execute.

Each line of the input is a JSON object with `id`, `source`, `chunk_idx`,
`content` (the shape chunk_corpus.py emits). The pipeline rendered by
setup_rag.py expects exactly those four parameters and does the embedding
inline during INSERT.

Resumability — important when corpora get large: every successful POST is
recorded in <workspace>/ingest_progress.json keyed by `id`. On rerun we
skip ids already marked `ok`, so a network blip or a transient server
restart doesn't force re-embedding the whole corpus.

Concurrency — the embedding UDF is the bottleneck so a small concurrency
(4–8) is the sweet spot. The default is 1 because that's the safest
behaviour against unfamiliar Skardi builds; opt in with --concurrency.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Same package, sibling module — embedding lives on the agent side.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from embed import embed_text, parse_breadcrumb  # noqa: E402


def _ensure_localhost_no_proxy(host: str) -> None:
    """Make urllib bypass the user's HTTP proxy *only* when posting to a
    local address.

    The skill's HTTP loop talks to a skardi-server the agent just started on
    the same machine. A surprising number of dev environments have a
    transparent SOCKS / HTTP proxy on 127.0.0.1 (mihomo, clash, corp
    proxies, etc.) and `urllib.request` will helpfully route every request
    through it — including localhost ones the proxy cannot reach. The
    symptom is HTTP 502 on every ingest call. We only want to override the
    proxy for the local hop, not for every request, because some users do
    have real proxies for non-local traffic that they need to keep working.

    Strategy: prepend the local target host to NO_PROXY *for this Python
    process only*. urllib's `getproxies` reads NO_PROXY at request time, so
    setting it before the first urlopen is enough. We don't touch the
    user's shell, don't install a global empty opener, and don't affect
    any other tool the user runs.
    """
    if host not in {"127.0.0.1", "localhost", "::1"}:
        # Not a local target — leave the proxy chain alone.
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


def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def load_progress(path):
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        # Corrupt manifest — start from scratch, but preserve the file
        # for inspection.
        path.rename(path.with_suffix(".json.bak"))
        return {}


def save_progress(path, progress):
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(progress))
    tmp.replace(path)


def post_chunk(endpoint, chunk, embedding, timeout):
    """POST one chunk + its precomputed embedding to /ingest/execute.

    `embedding` is a Float32 list (length matches the schema's vector(N)
    dim). Embedding is computed on the agent side via embed.py — see the
    module docstring there for why."""
    body = {
        "doc_id": chunk["id"],
        "source": chunk["source"],
        "chunk_idx": chunk["chunk_idx"],
        "content": chunk["content"],
        "embedding": embedding,
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
    ap.add_argument("--chunks", required=True, help="NDJSON file from chunk_corpus.py")
    ap.add_argument(
        "--port",
        type=int,
        default=None,
        help=(
            "Server port. Defaults to whatever start_server.py wrote to "
            "<workspace>/server.port; falls back to 8080 if neither is set. "
            "An explicit --port always wins."
        ),
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--concurrency", type=int, default=1, help="Inflight POSTs (default: 1)")
    ap.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="Per-request timeout in seconds (default: 120, generous for slow first embedding load).",
    )
    ap.add_argument("--limit", type=int, default=0, help="Only ingest the first N chunks (0 = all).")
    args = ap.parse_args()

    workspace = Path(args.workspace).expanduser().resolve()
    chunks_path = Path(args.chunks).expanduser().resolve()
    if not chunks_path.is_file():
        die(f"{chunks_path} not found")
    progress_path = workspace / "ingest_progress.json"

    # Resolve the port: explicit flag wins, then <workspace>/server.port,
    # then 8080 as a last-ditch default.
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
    endpoint = f"http://{args.host}:{port}/ingest/execute"
    print(f"  endpoint:    {endpoint}")
    print(f"  chunks:      {chunks_path}")
    print(f"  manifest:    {progress_path}")
    print(f"  concurrency: {args.concurrency}")

    progress = load_progress(progress_path)

    # Stream-read into memory once so we can dispatch concurrently. Files
    # this big (millions of chunks) should use the bulk pipeline / Skardi
    # job path instead — the SKILL.md calls this out.
    chunks = []
    with chunks_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            chunks.append(json.loads(line))
    if args.limit > 0:
        chunks = chunks[: args.limit]

    pending = [c for c in chunks if str(c["id"]) not in progress or progress[str(c["id"])] != "ok"]
    skipped = len(chunks) - len(pending)
    print(f"  total: {len(chunks)}  skipped (already ok): {skipped}  to ingest: {len(pending)}")

    if not pending:
        print("  nothing to do")
        return

    ok = 0
    failed = []
    started = time.time()
    last_save = started

    # Embed all pending chunks first, in one pass, so the host's `skardi
    # query` model cache is hot for every call after the first. Doing
    # this up-front (rather than interleaved with the POSTs) also makes
    # progress reporting cleaner — embedding and ingest become two
    # well-defined phases the user can read separately in the log.
    print(f"  embedding {len(pending)} chunks via host skardi CLI ...")
    breadcrumb = parse_breadcrumb(workspace)
    embed_started = time.time()
    embeddings = {}
    for i, c in enumerate(pending):
        try:
            embeddings[c["id"]] = embed_text(c["content"], breadcrumb, workspace=workspace)
        except Exception as e:  # noqa: BLE001 — surfaced verbatim
            failed.append((c["id"], f"embed: {e}"))
            progress[str(c["id"])] = f"err: embed: {e}"
        if (i + 1) % 25 == 0 or (i + 1) == len(pending):
            print(f"    embedded {i + 1}/{len(pending)}")
    print(f"  embedding done in {time.time() - embed_started:.1f}s")

    pending_with_embed = [c for c in pending if c["id"] in embeddings]
    save_progress(progress_path, progress)

    if not pending_with_embed:
        print("  every chunk failed during embedding; nothing to POST.")
        sys.exit(1)

    print(f"  POSTing {len(pending_with_embed)} chunks at concurrency {args.concurrency} ...")
    if args.concurrency <= 1:
        # Serial — keeps the manifest writes cheap and the failure modes
        # easy to read.
        for c in pending_with_embed:
            success, err = post_chunk(endpoint, c, embeddings[c["id"]], args.timeout)
            key = str(c["id"])
            if success:
                progress[key] = "ok"
                ok += 1
            else:
                progress[key] = f"err: {err}"
                failed.append((c["id"], err))
            if time.time() - last_save > 2.0:
                save_progress(progress_path, progress)
                last_save = time.time()
            done = ok + len(failed)
            if done % 25 == 0 or done == len(pending):
                print(f"    {done}/{len(pending)} (ok={ok} failed={len(failed)})")
    else:
        # Concurrent — batch progress saves to keep IO out of the hot
        # path.
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {
                pool.submit(post_chunk, endpoint, c, embeddings[c["id"]], args.timeout): c
                for c in pending_with_embed
            }
            for fut in as_completed(futures):
                c = futures[fut]
                success, err = fut.result()
                key = str(c["id"])
                if success:
                    progress[key] = "ok"
                    ok += 1
                else:
                    progress[key] = f"err: {err}"
                    failed.append((c["id"], err))
                if time.time() - last_save > 2.0:
                    save_progress(progress_path, progress)
                    last_save = time.time()
                done = ok + len(failed)
                if done % 25 == 0 or done == len(pending):
                    print(f"    {done}/{len(pending)} (ok={ok} failed={len(failed)})")

    save_progress(progress_path, progress)
    elapsed = time.time() - started
    rate = (ok + len(failed)) / elapsed if elapsed > 0 else 0
    print(f"  done in {elapsed:.1f}s ({rate:.1f} chunks/s)  ok={ok}  failed={len(failed)}")

    if failed:
        print("  failures (first 10):")
        for cid, err in failed[:10]:
            print(f"    id={cid}  {err}")
        sys.exit(1)


if __name__ == "__main__":
    main()
