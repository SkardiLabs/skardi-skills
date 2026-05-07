#!/usr/bin/env python3
"""Mirror a Feishu Bitable (one base + table_id) into the local SQLite mirror.

Drives `lark-cli` serially — the Bitable record-list endpoint forbids
concurrent calls and rate-limits at 20 req/s per app, so this script
deliberately walks one page at a time. The mirror is a small upsert per
record: schema-flexible Bitables map onto the `feishu_bitable_records`
table without DDL changes (the whole record is stashed as JSON; columns
in `feishu_bitable_fields` describe the shape).

The contract: a successful run leaves `feishu_sync_log` with status='ok'
and the new row count. A failed run records 'err: <msg>' but leaves the
previously-mirrored rows in place so an agent can still query stale data
while the operator investigates.
"""
import argparse
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PAGE = 100  # max --limit for Bitable record-list per Lark Open Platform
SLEEP_BETWEEN_PAGES = 0.1  # cushion under the 20 req/s ceiling


def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_lark(cmd_args, ndjson=False):
    """Invoke lark-cli, return parsed JSON (or list of NDJSON dicts)."""
    cmd = ["lark-cli"] + cmd_args + ["--format", "ndjson" if ndjson else "json"]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        msg = out.stderr.strip() or out.stdout.strip() or "(no output)"
        raise RuntimeError(f"lark-cli failed: `{shlex.join(cmd)}`\n{msg}")
    if ndjson:
        rows = []
        for line in out.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
        return rows
    return json.loads(out.stdout) if out.stdout.strip() else {}


def fetch_fields(base_token, table_id):
    """Return [{field_id, field_name, field_type, is_primary, description}, ...]."""
    items = []
    offset = 0
    while True:
        page = run_lark(
            [
                "base", "+field-list",
                "--base-token", base_token,
                "--table-id", table_id,
                "--offset", str(offset),
                "--limit", str(DEFAULT_PAGE),
            ]
        )
        chunk = page.get("items") or []
        items.extend(chunk)
        total = page.get("total")
        offset += len(chunk)
        if not chunk or (total is not None and offset >= total):
            break
        time.sleep(SLEEP_BETWEEN_PAGES)
    return [
        {
            "field_id": f.get("field_id") or f.get("id"),
            "field_name": f.get("field_name") or f.get("name") or "",
            "field_type": int(f.get("type") or f.get("field_type") or 0),
            "is_primary": 1 if f.get("is_primary") else 0,
            "description": (f.get("description") or {}).get("text")
            if isinstance(f.get("description"), dict)
            else f.get("description"),
        }
        for f in items
        if (f.get("field_id") or f.get("id"))
    ]


def fetch_records(base_token, table_id, view_id=None):
    """Yield record dicts page by page. Walks --offset until exhausted."""
    offset = 0
    page_size = 500  # max --limit for record-list
    while True:
        cmd = [
            "base", "+record-list",
            "--base-token", base_token,
            "--table-id", table_id,
            "--offset", str(offset),
            "--limit", str(page_size),
        ]
        if view_id:
            cmd += ["--view-id", view_id]
        page = run_lark(cmd)
        chunk = page.get("items") or []
        for rec in chunk:
            yield rec
        total = page.get("total")
        offset += len(chunk)
        if not chunk or (total is not None and offset >= total):
            break
        time.sleep(SLEEP_BETWEEN_PAGES)


def upsert_fields(conn, base_token, table_id, fields):
    conn.execute(
        "DELETE FROM feishu_bitable_fields WHERE base_token=? AND table_id=?",
        (base_token, table_id),
    )
    conn.executemany(
        """
        INSERT INTO feishu_bitable_fields
          (base_token, table_id, field_id, field_name, field_type, is_primary, description)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                base_token,
                table_id,
                f["field_id"],
                f["field_name"],
                f["field_type"],
                f["is_primary"],
                f.get("description"),
            )
            for f in fields
        ],
    )


def upsert_records(conn, base_token, table_id, records):
    synced_at = now_iso()
    rows = []
    for rec in records:
        record_id = rec.get("record_id") or rec.get("id")
        if not record_id:
            continue
        fields_json = json.dumps(rec.get("fields") or rec, ensure_ascii=False)
        rows.append((base_token, table_id, record_id, fields_json, synced_at))
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT INTO feishu_bitable_records
          (base_token, table_id, record_id, fields_json, synced_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(base_token, table_id, record_id)
        DO UPDATE SET fields_json=excluded.fields_json, synced_at=excluded.synced_at
        """,
        rows,
    )
    return len(rows)


def upsert_log(conn, source_key, status, row_count):
    conn.execute(
        """
        INSERT INTO feishu_sync_log
          (source_type, source_key, last_synced_at, row_count, status)
        VALUES ('bitable', ?, ?, ?, ?)
        ON CONFLICT(source_type, source_key)
        DO UPDATE SET last_synced_at=excluded.last_synced_at,
                      row_count=excluded.row_count,
                      status=excluded.status
        """,
        (source_key, now_iso(), row_count, status),
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--workspace", required=True)
    p.add_argument("--base-token", required=True, help="app_xxx Bitable id.")
    p.add_argument("--table-id", required=True, help="tbl_xxx within the Base.")
    p.add_argument("--view-id", default=None, help="Optional Lark view filter (vew_xxx).")
    p.add_argument(
        "--full-refresh",
        action="store_true",
        help="Delete prior rows for this (base, table) before insert. Default upserts in place.",
    )
    p.add_argument(
        "--db-path",
        default=None,
        help="Override mirror path (defaults to <workspace>/feishu.db).",
    )
    args = p.parse_args()

    workspace = Path(args.workspace).resolve()
    db_path = Path(args.db_path).resolve() if args.db_path else workspace / "feishu.db"
    if not db_path.exists():
        die(f"Mirror DB not found at {db_path}. Run setup_feishu.py first.")

    source_key = f"{args.base_token}/{args.table_id}"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        # Schema first — describes will fail loudly on a wrong table_id.
        print(f"[1/3] field-list  {source_key}")
        fields = fetch_fields(args.base_token, args.table_id)
        if not fields:
            die("field-list returned no rows — bad base_token / table_id, or scopes missing.")
        upsert_fields(conn, args.base_token, args.table_id, fields)
        conn.commit()
        print(f"      {len(fields)} fields")

        print(f"[2/3] record-list {source_key} (serial; rate-limited)")
        if args.full_refresh:
            conn.execute(
                "DELETE FROM feishu_bitable_records WHERE base_token=? AND table_id=?",
                (args.base_token, args.table_id),
            )
            conn.commit()
        total = 0
        batch = []
        for rec in fetch_records(args.base_token, args.table_id, args.view_id):
            batch.append(rec)
            if len(batch) >= 500:
                total += upsert_records(conn, args.base_token, args.table_id, batch)
                conn.commit()
                batch = []
                print(f"      ... {total} rows", flush=True)
        if batch:
            total += upsert_records(conn, args.base_token, args.table_id, batch)
            conn.commit()
        print(f"      {total} records")

        print("[3/3] sync log")
        upsert_log(conn, source_key, "ok", total)
        conn.commit()
    except Exception as e:
        upsert_log(conn, source_key, f"err: {e}", 0)
        conn.commit()
        die(str(e))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
