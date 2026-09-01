#!/usr/bin/env python3
"""Walk a corpus directory and ingest every file into skardi-server.

Replaces the old chunk_corpus.py + embed.py + http_ingest.py trio. With
Skardi 0.4.0's chunk() UDF and the skardi-server-full image (which bundles
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


def die(msg, code=1, reason="error"):
    """Exit with a reason, and emit the verdict line first.

    Every exit prints a RESULT line, including refusals — a consumer that
    reads the verdict must never have to fall back to guessing from an
    absent line. The reason names the refusal so the caller can tell a
    genuine failure from a deliberate gate."""
    print_result_line(False, reason=reason)
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


# Identity of the raw-material SET a source string came from — the corpus
# root, the db file plus table, or the NDJSON label. Recorded per document
# so a collision can be told apart from an edit.
#
# Two earlier attempts were both too coarse. "corpus" / "table" only caught
# collisions ACROSS entries, so the commonest case slipped through: two
# corpus roots each holding a README.md landed on one source string, and the
# second one was reported as a *changed file* and never indexed — two
# different documents, one silently lost, with a warning telling the user to
# delete rows and re-run. Adding the table's --label instead mistook a
# parameter change for a document change. The set identity fixes both: it is
# specific enough to tell two corpora apart, and collisions are judged
# together with the content hash so a re-run under a new path or label is
# recognised as the same document rather than refused.
SET_LEGACY_CORPUS = "corpus"
KIND_CORPUS = "corpus"
KIND_TABLE = "table"


def corpus_set_id(corpus_root):
    return str(corpus_root)


def sqlite_set_id(db_path, table):
    return f"{db_path}::{table}"


def ndjson_set_id(label):
    return f"ndjson:{label}"


def load_progress(path):
    """Return {source: {"status", "hash", "set"}}.

    Back-compat, three generations deep. Older manifests stored a bare
    string per source ("ok" / "err: ...") with no hash; then a dict with a
    hash; then a dict carrying `origin` ("corpus" / "table"). Anything
    without a `set` is normalised to its `origin`, defaulting to "corpus" —
    exact for the oldest generation, because ingest_corpus.py was the only
    writer before the table entry existed."""
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
            normalised[source] = {"status": val, "hash": None,
                                  "set": SET_LEGACY_CORPUS}
        elif isinstance(val, dict):
            normalised[source] = {
                "status": val.get("status", ""),
                "hash": val.get("hash"),
                "set": val.get("set") or val.get("origin") or SET_LEGACY_CORPUS,
            }
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
    in one transaction.

    Returns one of three outcomes, not two:

      ("ok", None)          committed by this POST
      ("present", None)     the rows were already there (primary-key collision)
      ("fail", "<reason>")  anything else

    `present` is separate from `fail` because it is the normal aftermath of an
    interrupted run, not an error. The manifest is flushed at most every two
    seconds, so a Ctrl-C drops the last few successes from it while their rows
    stay committed server-side. Folding that into `fail` made those files
    permanently unrecoverable: the manifest marked them `err:`, the next run
    treated `err:` as pending, re-POSTed, collided again, and wrote `err:`
    again. They could never leave that state without hand-deleting rows —
    while `resuming after a pause loses no work` sat in the troubleshooting
    table. Caller decides what to record; see main().
    """
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
                return "fail", f"server returned success=false: {payload.get('error')}"
            return "ok", None
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8")
        except Exception:
            err_body = ""
        if "UNIQUE constraint failed" in err_body or "duplicate key" in err_body:
            return "present", None
        return "fail", f"HTTP {e.code}: {err_body[:300]}"
    except urllib.error.URLError as e:
        return "fail", f"connection error: {e}"
    except Exception as e:  # noqa: BLE001 — surfaced verbatim into manifest
        return "fail", f"unexpected: {type(e).__name__}: {e}"


def detect_source_collisions(progress, work, set_id, entry_kind):
    """Sources that mean a DIFFERENT document than the one already recorded.

    A document IS its source string: doc_id is derived from it, and the
    manifest and index are per-workspace. So a source arriving from another
    raw-material set is only safe when the content is identical; when the
    content differs too, the two are genuinely different documents fighting
    over one id, and the failure is silent in both directions — identical
    content is written off as `already ok`, differing content is reported as
    a changed document and never re-ingested, because its ids are taken.

    Judged on (set, hash) together, not set alone: re-running the same
    material from a moved directory or under a new label yields a different
    set with an unchanged hash, and refusing that would block an ordinary
    re-run. Anything the hashes cannot prove identical is refused, including
    an entry whose hash is unknown.

    `entry_kind` ("corpus" / "table") exists only for the legacy sentinel.
    A manifest written before set ids carries `corpus` in place of a real
    set, with no hash — unknowable, but definitely written by the corpus
    entry, because nothing else could write then. Treating that as a
    different set would refuse the next ordinary run of every upgraded
    workspace, so it counts as the same set for a corpus run and as a
    different one for a table run. Entries get real set ids on that run, so
    the blind spot lasts exactly one pass.

    Returns [(source, recorded_set)]."""
    collisions = []
    for w in work:
        entry = progress.get(w["source"])
        if entry is None:
            continue
        recorded = entry.get("set") or SET_LEGACY_CORPUS
        if recorded == SET_LEGACY_CORPUS:
            same_set = entry_kind == KIND_CORPUS
        else:
            same_set = recorded == set_id
        if not same_set and entry.get("hash") != w["hash"]:
            collisions.append((w["source"], recorded))
    return collisions


def die_on_collisions(collisions, set_id, progress_path):
    if not collisions:
        return
    head = "\n    ".join(f"{src}\n        already indexed from: {rec}"
                          for src, rec in collisions[:10])
    more = f"\n    (+{len(collisions) - 10} more)" if len(collisions) > 10 else ""
    die(
        f"{len(collisions)} source string(s) in this run already name a "
        f"DIFFERENT document in this workspace:\n    {head}{more}\n"
        f"  This run is: {set_id}\n"
        f"  Source strings are document identity here — same string means "
        f"same doc id — so ingesting these would either be skipped as\n"
        f"  'already ok' or reported as a changed document and never indexed. "
        f"Nothing was ingested.\n"
        f"  Fix by making the two sets of source strings distinct: ingest "
        f"each corpus root into its own workspace, or give the table run\n"
        f"  a --label that cannot collide, or point --source-column at real "
        f"locators (URLs).\n"
        f"  A recorded set of just \"corpus\" means a manifest written before "
        f"set ids existed: its content hash is unknown, so the two cannot be\n"
        f"  proven to be the same document either way.\n"
        f"  Manifest: {progress_path}"
    )


def print_result_line(complete, reason=None):
    """One machine-readable line, always printed, on every exit path that
    reaches the end of a run.

    The human-facing INCOMPLETE banner is unambiguous to a reader but
    invisible to a pipeline reading only the exit code, which stays 0 for a
    deliberate trial run — so a --limit smoke test could be consumed as a
    finished ingest. This gives a consumer something uniform to test
    (`complete=true` / `complete=false`) without inventing an exit code that
    would make an intentional trial look like a failure."""
    tail = f" reason={reason}" if reason else ""
    print(f"RESULT complete={'true' if complete else 'false'}{tail}")


# Verdict reasons, most severe first. A run is complete only when nothing
# was held back, dropped, or left stale — anything else is reported with the
# reason that most needs the reader's attention.
VERDICT_ORDER = (
    ("failed-posts", "documents the server refused"),
    ("limit", "--limit held rows back"),
    ("accepted-shortfall", "a shortfall was explicitly accepted"),
    ("skipped", "unusable input was skipped"),
    ("stale", "content changed since it was indexed and was not refreshed"),
)


def compute_verdict(failed=0, limited=0, accepted_shortfall=0, skipped=0,
                    changed=0):
    """(complete, reason) for the whole run.

    Computed from corpus completeness, not from what this particular
    invocation happened to do, because the two diverge exactly where it
    matters: a re-run over a table whose shortfall was accepted earlier has
    nothing left to ingest, and reporting that as complete flipped the
    verdict to true while the corpus was still missing a document. Skips and
    unrefreshed edits count the same way — the index does not hold what the
    source holds, whichever run failed to put it there."""
    counts = {
        "failed-posts": failed,
        "limit": limited,
        "accepted-shortfall": accepted_shortfall,
        "skipped": skipped,
        "stale": changed,
    }
    for reason, _ in VERDICT_ORDER:
        if counts[reason]:
            return False, reason
    return True, None


def print_verdict(**kwargs):
    complete, reason = compute_verdict(**kwargs)
    print_result_line(complete, reason=reason)
    return complete


def acquire_workspace_lock(workspace):
    """Refuse to run twice against one workspace at the same time.

    Reading the manifest, checking for collisions and writing `inflight` is
    three steps, not one, so two concurrent runs could both pass the check
    and then overwrite each other's state — landing silently on
    `already-present` with no sign that anything was lost. A lock is cheap
    next to that. A lock left behind by a killed process is detected (its
    pid is gone) and taken over with a note, so a crash does not need manual
    cleanup."""
    lock = workspace / "ingest.lock"
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        try:
            holder = int(lock.read_text().strip())
        except (ValueError, OSError):
            holder = None
        if holder is not None:
            try:
                os.kill(holder, 0)
            except ProcessLookupError:
                print(f"  note: removing a stale lock left by pid {holder} "
                      f"(no longer running)")
                lock.unlink(missing_ok=True)
                return acquire_workspace_lock(workspace)
            except PermissionError:
                pass  # alive, owned by another user
            die(f"another ingest is already running against {workspace} "
                f"(pid {holder}). Concurrent runs on one workspace corrupt the "
                f"progress manifest, so this one stopped without touching "
                f"anything. Wait for it, or delete {lock} if you are certain "
                f"that process is gone.", reason="locked")
        die(f"{lock} exists but is unreadable. Delete it if no ingest is "
            f"running.", reason="locked")
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return lock


def release_workspace_lock(lock):
    if lock is not None:
        Path(lock).unlink(missing_ok=True)


def detect_stale_inflight(progress, work):
    """Documents interrupted mid-ingest whose source has changed since.

    `inflight` means the POST was sent and the answer never recorded, so the
    server may already hold that document. If the content has changed since,
    a resumed run POSTs the new text, the server rejects it as a duplicate,
    and the collision is filed as `already-present` — which records the NEW
    hash against rows holding the OLD text. The manifest then claims the
    current version is indexed when it is not, and nothing ever says
    otherwise. Refuse instead: this is one of the few states that genuinely
    needs a human to decide what the index holds."""
    stale = []
    for w in work:
        entry = progress.get(w["source"])
        if entry is None:
            continue
        if entry.get("status") == "inflight" and entry.get("hash") not in (None, w["hash"]):
            stale.append(w["source"])
    return stale


def die_on_stale_inflight(stale, progress_path):
    if not stale:
        return
    head = "\n    ".join(stale[:10])
    more = f"\n    (+{len(stale) - 10} more)" if len(stale) > 10 else ""
    die(
        f"{len(stale)} document(s) were interrupted mid-ingest and have changed "
        f"since:\n    {head}{more}\n"
        f"  The server may already hold the older text under the same id. "
        f"Re-sending the new text would collide, be recorded as\n"
        f"  'already-present', and stamp the NEW hash onto rows holding the OLD "
        f"content — leaving the manifest claiming a version the index\n"
        f"  does not have. Decide explicitly: delete those rows "
        f"(DELETE FROM <table> WHERE source = '...') and drop their entries from\n"
        f"  {progress_path}, then re-run. Nothing was ingested.",
        reason="stale-inflight")


def mark_inflight(progress, pending, set_id, progress_path):
    """Record every pending source BEFORE its POST goes out.

    Without this the manifest only learns about a document after the server
    answers, and it is flushed at most every two seconds — so an interrupt
    leaves rows committed server-side that the manifest has never heard of.
    A later run from a different raw-material set then finds no entry, the
    collision check has nothing to compare against, and the newcomer
    collides on the primary key and is filed as `already ok`: the exact
    silent loss the check exists to prevent. One write up front closes the
    window; `inflight` is not `ok`, so these are retried normally."""
    for item in pending:
        progress[item["source"]] = {"status": "inflight", "hash": item["hash"],
                                    "set": set_id}
    save_progress(progress_path, progress)


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
    ap.add_argument(
        "--limit",
        type=int,
        default=0,
        help=(
            "Trial run: ingest only the first N files (0 = all). The run is "
            "INCOMPLETE by construction — the rest are neither ingested nor "
            "skipped, they are counted apart as `limited` — so its result "
            "cannot stand as a finished ingest."
        ),
    )
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

    lock = acquire_workspace_lock(workspace)
    try:
        run(args, workspace, corpus, progress_path, endpoint)
    finally:
        release_workspace_lock(lock)


def run(args, workspace, corpus, progress_path, endpoint):
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

    # Held back by --limit. Counted on its own line rather than folded into
    # the work list, because `matched = ingestable + skipped` is a promise the
    # counts line makes: truncating silently broke it (matched: 3,
    # ingestable: 1, skipped: 0) while still exiting 0, which reads exactly
    # like a complete run over a small corpus.
    limited = 0
    if args.limit > 0 and len(work) > args.limit:
        limited = len(work) - args.limit
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
    set_id = corpus_set_id(corpus)
    die_on_collisions(
        detect_source_collisions(progress, work, set_id, KIND_CORPUS),
        set_id, progress_path)
    die_on_stale_inflight(detect_stale_inflight(progress, work), progress_path)

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
            progress[w["source"]]["set"] = set_id
            backfilled += 1
        elif stored_hash != w["hash"]:
            changed.append(w["source"])
        elif entry.get("set") != set_id:
            # Same source, same bytes, different set — the collision check
            # already cleared it as the same document reached another way
            # (a moved corpus root). Record where it is served from now.
            progress[w["source"]]["set"] = set_id

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
    limited_note = f"  limited (--limit, NOT ingested): {limited}" if limited else ""
    print(f"  matched: {matched}  ingestable: {len(work)}  skipped: {total_skipped}"
          f"{limited_note}  "
          f"already ok: {len(work) - len(pending) - len(changed)}  "
          f"changed (see warning): {len(changed)}  to ingest: {len(pending)}")
    if limited:
        print(f"  INCOMPLETE: --limit held back {limited} ingestable file(s). This is a "
              f"trial run over part of the corpus;\n"
              f"              do not report it as a finished ingest. Re-run without "
              f"--limit for the rest.")

    if not work:
        # Files matched but none survived. Distinct from "already ingested":
        # nothing is in the index as a result of this run, so exiting 0 here
        # would report success for a corpus that produced no context at all.
        die(f"all {matched} matched file(s) were skipped — see the reasons above. "
            f"Nothing was ingested.")

    if not pending:
        print("  nothing to do (every ingestable file is already ok in the manifest)")
        print_verdict(limited=limited, skipped=total_skipped, changed=len(changed))
        return

    mark_inflight(progress, pending, set_id, progress_path)

    started = time.time()
    last_save = started
    ok = 0
    already_present = []
    failed = []

    def _record(item, status, err):
        nonlocal ok, last_save
        if status == "ok":
            progress[item["source"]] = {"status": "ok", "hash": item["hash"],
                                        "set": set_id}
            ok += 1
        elif status == "present":
            # The rows are in the index; the manifest just did not know. Record
            # ok so the file stops being retried forever, and remember it for
            # the caveat printed below — we cannot compare our hash against
            # what is actually indexed, so "present" is not proof of "current".
            progress[item["source"]] = {"status": "ok", "hash": item["hash"],
                                        "set": set_id}
            already_present.append(item["source"])
        else:
            progress[item["source"]] = {"status": f"err: {err}", "hash": None,
                                        "set": set_id}
            failed.append((item["source"], err))
        if time.time() - last_save > 2.0:
            save_progress(progress_path, progress)
            last_save = time.time()
        done = ok + len(already_present) + len(failed)
        if done % 5 == 0 or done == len(pending):
            print(f"    {done}/{len(pending)} (ok={ok} "
                  f"already-present={len(already_present)} failed={len(failed)})")

    if args.concurrency <= 1:
        for item in pending:
            status, err = post_doc(endpoint, item["body"], args.timeout)
            _record(item, status, err)
    else:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {
                pool.submit(post_doc, endpoint, item["body"], args.timeout): item
                for item in pending
            }
            for fut in as_completed(futures):
                item = futures[fut]
                status, err = fut.result()
                _record(item, status, err)

    save_progress(progress_path, progress)
    elapsed = time.time() - started
    done = ok + len(already_present) + len(failed)
    rate = done / elapsed if elapsed > 0 else 0
    print(f"  done in {elapsed:.1f}s ({rate:.2f} files/s)  ok={ok}  "
          f"already-present={len(already_present)}  failed={len(failed)}")

    if already_present:
        head = ", ".join(already_present[:5])
        more = (f" (+{len(already_present) - 5} more)"
                if len(already_present) > 5 else "")
        print(
            f"\n  note: {len(already_present)} file(s) were already in the index "
            f"and have been\n"
            f"        marked ok in the manifest: {head}{more}\n"
            f"        Normal after an interrupted run — the manifest is flushed "
            f"every two\n"
            f"        seconds, so the last few successes before a Ctrl-C are "
            f"missing from it\n"
            f"        while their rows are committed. If instead you deleted the "
            f"manifest to\n"
            f"        force a refresh, note that this did NOT re-index them: the "
            f"rows already\n"
            f"        there are whatever was ingested last time. Delete those "
            f"rows and their\n"
            f"        manifest entries to actually refresh.\n"
        )

    if failed:
        print_verdict(failed=len(failed))
        print("  failures (first 10):")
        for src, err in failed[:10]:
            print(f"    {src}  {err}")
        sys.exit(1)

    print_verdict(limited=limited, skipped=total_skipped, changed=len(changed))


if __name__ == "__main__":
    main()
