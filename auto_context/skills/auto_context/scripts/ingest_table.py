#!/usr/bin/env python3
"""Ingest documents that already live as rows in a table.

This is the second raw-material entry (see "Where the raw material comes
from" in SKILL.md): one row = one document. Two ways in:

  --db staging.db --table docs --key-column id --content-column body
      Read rows straight out of a SQLite file. The file is opened
      read-only (URI mode=ro), so pointing this at a table someone else
      owns cannot write to it, alter it, or leave journal files behind.

  --ndjson -  (or a file path)
      Read one JSON object per line: {"key": ..., "content": ...,
      "source": ...} ("source" optional). Produced by whatever client
      already talks to the datastore the rows live in — psql with
      json_build_object, mongoexport, or a five-line script. This is how
      a table in a datastore this script has no driver for still gets
      ingested with the same accounting, without this skill growing a
      driver per database.

Everything downstream is identical to ingest_corpus.py — same endpoint,
same manifest (<workspace>/ingest_progress.json), same resume / skip /
accounting semantics — because a document's origin stops mattering the
moment it is serialised into {doc_id, source, content}.

Identity: a document IS its source string. doc_id is derived from it the
same way ingest_corpus.py derives ids from relative paths, so source
strings must be unique within the run and stable across runs. By default
that is "<label>#<key>" (label defaults to the table name); pass
--source-column when rows already carry a real locator (a URL, a path) —
those make better citations. Changing the label or the source scheme
between runs re-ingests everything under new ids NEXT TO the old rows,
the same way moving a corpus root does on the folder path.

Rows are ingested as-is: no front-matter stripping, no cleanup. The
landing step (references/fetch_and_land.md) is where boilerplate gets
removed — by the time text is in a table it is what gets indexed.
"""
import argparse
import json
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from _platform import require_supported_platform
from ingest_corpus import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_OVERLAP,
    SERVER_BODY_LIMIT,
    _ensure_localhost_no_proxy,
    build_body,
    content_hash,
    die,
    load_progress,
    post_doc,
    save_progress,
    stable_doc_id,
)

SKIP_REASONS = (
    "null key",
    "not valid JSON",
    "missing key or content field",
    "duplicate source",
    "no text content",
    "not UTF-8",
    "non-text content",
    "too large for one request",
)


def open_sqlite_readonly(db_path):
    """Open a SQLite file with no ability to write to it.

    mode=ro is enforced by SQLite itself, not by politeness: INSERTs on
    this connection fail with "attempt to write a readonly database" and
    no -wal / -journal side files are created. That is what lets SKILL.md
    promise that a table handed over as raw material is never modified."""
    if not db_path.is_file():
        die(f"--db {db_path} is not a file")
    uri = f"file:{db_path}?mode=ro"
    try:
        return sqlite3.connect(uri, uri=True)
    except sqlite3.Error as e:
        die(f"could not open {db_path} read-only: {e}")


def quote_ident(name):
    """SQLite identifier quoting: wrap in double quotes, double any inside."""
    return '"' + name.replace('"', '""') + '"'


def validate_sqlite_shape(conn, table, columns):
    """Confirm the table and every named column exist; return nothing.

    Identifier names get interpolated into the SELECT below, so they are
    checked against sqlite_master / PRAGMA table_info first — a name that
    is not literally a table or column in this file never reaches SQL.
    This doubles as the friendly error: a typo dies here naming what the
    file actually contains, instead of surfacing as a syntax error."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
        (table,),
    ).fetchone()
    if row is None:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name")]
        die(f"table {table!r} not found. This file contains: {', '.join(names) or '(nothing)'}")
    have = {r[1] for r in conn.execute(f"PRAGMA table_info({quote_ident(table)})")}
    missing = [c for c in columns if c is not None and c not in have]
    if missing:
        die(f"column(s) {', '.join(repr(c) for c in missing)} not in {table!r}. "
            f"It has: {', '.join(sorted(have))}")


def iter_sqlite_rows(conn, table, key_col, content_col, source_col):
    """Yield (key, content, source_or_None) per row. Names are pre-validated."""
    cols = [quote_ident(key_col), quote_ident(content_col)]
    if source_col:
        cols.append(quote_ident(source_col))
    for row in conn.execute(f"SELECT {', '.join(cols)} FROM {quote_ident(table)}"):
        yield row[0], row[1], (row[2] if source_col else None)


def iter_ndjson_rows(stream, skipped):
    """Yield (key, content, source_or_None) per NDJSON line.

    Malformed lines are accounted, not fatal: one bad line in an export of
    thousands should cost one skip entry, not the run. Blank lines are a
    normal artifact of shell pipelines and are not records at all."""
    for lineno, line in enumerate(stream, 1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            skipped["not valid JSON"].append(f"line {lineno}")
            continue
        if not isinstance(obj, dict) or "key" not in obj or "content" not in obj:
            skipped["missing key or content field"].append(f"line {lineno}")
            continue
        yield obj["key"], obj["content"], obj.get("source")


def build_work(rows, label, chunk_size, overlap, skipped):
    """Map raw (key, content, source) rows onto the ingest work list.

    Returns (work, consumed) where consumed counts every row that reached
    this function. Every one of them lands in exactly one bucket — the work
    list or a named skip reason — mirroring the accounting contract
    ingest_corpus.py established for files. The row count printed later only
    adds up because nothing can fall through silently here; NDJSON lines
    that failed to parse never reach this function and are added back into
    the total by the caller from their own skip buckets."""
    work = []
    consumed = 0
    seen_sources = set()
    for key, content, source in rows:
        consumed += 1
        if key is None:
            skipped["null key"].append("(row with NULL key)")
            continue
        key = str(key)
        source = str(source) if source not in (None, "") else f"{label}#{key}"
        if source in seen_sources:
            skipped["duplicate source"].append(source)
            continue
        if isinstance(content, bytes):
            try:
                content = content.decode("utf-8-sig")
            except UnicodeDecodeError:
                skipped["not UTF-8"].append(source)
                continue
        if content is not None and not isinstance(content, str):
            # An int or a float in the content column is almost always the
            # wrong column named, not a document. Indexing str(42) would
            # hide that; a named skip surfaces it.
            skipped["non-text content"].append(f"{source} ({type(content).__name__})")
            continue
        text = (content or "").strip()
        if not text:
            skipped["no text content"].append(source)
            continue
        seen_sources.add(source)
        body = build_body(stable_doc_id(source), source, text, chunk_size, overlap)
        if len(body) > SERVER_BODY_LIMIT:
            skipped["too large for one request"].append(
                f"{source} ({len(body) / 1024 / 1024:.1f} MB serialised)")
            continue
        work.append({"source": source, "body": body, "hash": content_hash(text)})
    return work, consumed


def main():
    require_supported_platform("ingest_table.py")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--workspace", required=True, help="Workspace dir from setup_context.py")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--db", help="SQLite file holding the raw-material table (opened read-only)")
    src.add_argument("--ndjson", help="NDJSON file of {key, content, source?} objects, or '-' for stdin")
    ap.add_argument("--table", help="Table (or view) name inside --db")
    ap.add_argument("--key-column", help="Column with a stable unique id per document")
    ap.add_argument("--content-column", help="Column holding the document text")
    ap.add_argument("--source-column",
                    help="Column with a real locator (URL, path) to cite instead of "
                         "the default '<label>#<key>'")
    ap.add_argument("--label",
                    help="Namespace for default source strings. Defaults to the table "
                         "name in --db mode; required with --ndjson. Keep it stable "
                         "across runs — changing it re-ingests everything under new ids.")
    ap.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE,
                    help=f"Target max chunk length in characters (default: {DEFAULT_CHUNK_SIZE}).")
    ap.add_argument("--overlap", type=int, default=DEFAULT_OVERLAP,
                    help=f"Characters of overlap between adjacent chunks (default: {DEFAULT_OVERLAP}).")
    ap.add_argument("--port", type=int, default=None,
                    help="Server port. Defaults to <workspace>/server.port, then 8080.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--concurrency", type=int, default=1, help="Inflight POSTs (default: 1)")
    ap.add_argument("--timeout", type=float, default=300.0,
                    help="Per-request timeout in seconds (default: 300). One POST chunks "
                         "+ embeds an entire document; keep this generous.")
    ap.add_argument("--limit", type=int, default=0, help="Only ingest the first N rows (0 = all).")
    args = ap.parse_args()

    workspace = Path(args.workspace).expanduser().resolve()
    if not (workspace / "ctx.yaml").is_file():
        die(f"{workspace}/ctx.yaml not found. Did you run setup_context.py?")
    if args.overlap >= args.chunk_size:
        die(f"--overlap ({args.overlap}) must be strictly less than --chunk-size ({args.chunk_size})")

    if args.db:
        for flag, val in (("--table", args.table), ("--key-column", args.key_column),
                          ("--content-column", args.content_column)):
            if not val:
                die(f"{flag} is required with --db")
        db_path = Path(args.db).expanduser().resolve()
        if db_path == (workspace / "kb.db").resolve():
            die("--db points at the workspace's own kb.db — that file is the INDEX "
                "the server owns, not raw material. Land documents in a separate "
                "staging file (see references/fetch_and_land.md) and ingest from that.")
        label = args.label or args.table
    else:
        for flag, val in (("--table", args.table), ("--key-column", args.key_column),
                          ("--content-column", args.content_column),
                          ("--source-column", args.source_column)):
            if val:
                die(f"{flag} only applies to --db mode; with --ndjson, name the fields "
                    f"inside each JSON object instead")
        if not args.label:
            die("--label is required with --ndjson (it namespaces doc ids and default "
                "source strings; keep it stable across runs)")
        label = args.label

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
    if args.db:
        print(f"  raw table:   {args.table} in {db_path} (read-only)")
    else:
        print(f"  ndjson:      {'stdin' if args.ndjson == '-' else args.ndjson}")
    print(f"  label:       {label}")
    print(f"  manifest:    {progress_path}")
    print(f"  concurrency: {args.concurrency}")
    print(f"  chunk_size:  {args.chunk_size}  overlap: {args.overlap}")

    progress = load_progress(progress_path)

    skipped = {reason: [] for reason in SKIP_REASONS}

    if args.db:
        conn = open_sqlite_readonly(db_path)
        try:
            validate_sqlite_shape(conn, args.table,
                                  [args.key_column, args.content_column, args.source_column])
            work, consumed = build_work(
                iter_sqlite_rows(conn, args.table, args.key_column,
                                 args.content_column, args.source_column),
                label, args.chunk_size, args.overlap, skipped)
        finally:
            conn.close()
    elif args.ndjson == "-":
        work, consumed = build_work(iter_ndjson_rows(sys.stdin, skipped),
                                    label, args.chunk_size, args.overlap, skipped)
    else:
        ndjson_path = Path(args.ndjson).expanduser()
        if not ndjson_path.is_file():
            die(f"--ndjson {ndjson_path} is not a file")
        with ndjson_path.open(encoding="utf-8") as fh:
            work, consumed = build_work(iter_ndjson_rows(fh, skipped),
                                        label, args.chunk_size, args.overlap, skipped)

    # Lines that failed to parse never reached build_work; they are still rows
    # someone exported, so they belong in the denominator.
    rows = consumed + len(skipped["not valid JSON"]) + len(skipped["missing key or content field"])

    if args.limit > 0:
        work = work[: args.limit]

    for reason, names in skipped.items():
        if not names:
            continue
        head = ", ".join(names[:5])
        more = f" (+{len(names) - 5} more)" if len(names) > 5 else ""
        print(f"  skipped {len(names)} {reason}: {head}{more}")
    if skipped["no text content"] and args.db:
        print(
            f"  note: rows with an empty {args.content_column!r} are documents that were\n"
            f"        listed but never landed. If this table was filled by a fetch-and-land\n"
            f"        run, do not trust search results until the reconciliation in\n"
            f"        references/fetch_and_land.md passes — the index builds fine without\n"
            f"        them, and nothing downstream will mention they are missing."
        )

    if rows == 0:
        die(
            "the raw material holds 0 rows — nothing to ingest. If a fetch process was "
            "supposed to land documents here, its listing stage never ran (or the wrong "
            "table or file was named)."
        )

    # Same three-way split as ingest_corpus.py: pending (never ingested or
    # last attempt errored), changed (hash differs from what was ingested —
    # surfaced, never auto-re-ingested), and ok. Hashes recorded by an older
    # run of either script are honoured; the manifest is shared because the
    # index is shared, and identity is the source string in both.
    pending = []
    changed = []
    for w in work:
        entry = progress.get(w["source"])
        if entry is None or entry.get("status") != "ok":
            pending.append(w)
            continue
        stored_hash = entry.get("hash")
        if stored_hash is None:
            progress[w["source"]]["hash"] = w["hash"]  # backfill, don't re-ingest
        elif stored_hash != w["hash"]:
            changed.append(w["source"])

    if changed:
        save_progress(progress_path, progress)
        head = "\n    ".join(changed[:10])
        more = f"\n    (+{len(changed) - 10} more)" if len(changed) > 10 else ""
        print(
            f"\n  WARNING: {len(changed)} row(s) changed since they were ingested:\n"
            f"    {head}{more}\n"
            f"  These are NOT re-ingested automatically: their chunks still exist\n"
            f"  under stable ids, so a re-POST would fail on the primary key. To\n"
            f"  refresh them, delete their existing index rows and drop their\n"
            f"  manifest entries, then re-run this script. For each changed <source>:\n"
            f"    DELETE FROM <index table> WHERE source = '<source>';\n"
            f"  then remove those keys from {progress_path}.\n"
        )

    total_skipped = sum(len(v) for v in skipped.values())
    print(f"  rows: {rows}  ingestable: {len(work)}  skipped: {total_skipped}  "
          f"already ok: {len(work) - len(pending) - len(changed)}  "
          f"changed (see warning): {len(changed)}  to ingest: {len(pending)}")

    if not work:
        die(f"all {rows} row(s) were skipped — see the reasons above. Nothing was ingested.")

    if not pending:
        save_progress(progress_path, progress)  # persist any hash backfill
        print("  nothing to do (every ingestable row is already ok in the manifest)")
        return

    started = time.time()
    last_save = started
    ok = 0
    already_present = []
    failed = []

    def _record(item, status, err):
        nonlocal ok, last_save
        if status == "ok":
            progress[item["source"]] = {"status": "ok", "hash": item["hash"]}
            ok += 1
        elif status == "present":
            progress[item["source"]] = {"status": "ok", "hash": item["hash"]}
            already_present.append(item["source"])
        else:
            progress[item["source"]] = {"status": f"err: {err}", "hash": None}
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
    print(f"  done in {elapsed:.1f}s ({rate:.2f} rows/s)  ok={ok}  "
          f"already-present={len(already_present)}  failed={len(failed)}")

    if already_present:
        head = ", ".join(already_present[:5])
        more = (f" (+{len(already_present) - 5} more)"
                if len(already_present) > 5 else "")
        print(
            f"\n  note: {len(already_present)} row(s) were already in the index and have\n"
            f"        been marked ok in the manifest: {head}{more}\n"
            f"        Normal after an interrupted run. This did NOT re-index them: the\n"
            f"        rows already there are whatever was ingested last time. Delete\n"
            f"        those index rows and their manifest entries to actually refresh.\n"
        )

    if failed:
        print("  failures (first 10):")
        for src, err in failed[:10]:
            print(f"    {src}  {err}")
        sys.exit(1)


if __name__ == "__main__":
    main()
