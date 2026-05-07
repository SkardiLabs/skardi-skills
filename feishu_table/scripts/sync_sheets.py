#!/usr/bin/env python3
"""Mirror a Feishu Sheet range into the local SQLite mirror.

`lark-cli sheets +read --url <share-url> --range <A1:Zn> --format ndjson`
streams cells in row-major order. We re-key by (row_idx, col_idx) so the
mirror stays sparse — empty cells aren't written, which keeps the table
small for the typical case where users sync a wide range "just in case"
and most of it is blank.

The skill assumes the user already pinned a specific sheet_id (run
`lark-cli sheets +info --url <url>` once to discover it). Feishu Sheets
allows the same range string ("Sheet1!A1:Z100") to refer to different
tabs in different spreadsheets, so storing sheet_id explicitly is what
keeps the mirror unambiguous.
"""
import argparse
import json
import shlex
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_lark_json(cmd_args):
    cmd = ["lark-cli"] + cmd_args + ["--format", "json"]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        msg = out.stderr.strip() or out.stdout.strip() or "(no output)"
        raise RuntimeError(f"lark-cli failed: `{shlex.join(cmd)}`\n{msg}")
    return json.loads(out.stdout) if out.stdout.strip() else {}


def fetch_sheet(url, sheet_id, cell_range):
    """Return the 2-D values matrix for a range — row-major list of lists."""
    payload = run_lark_json(
        [
            "sheets", "+read",
            "--url", url,
            "--sheet-id", sheet_id,
            "--range", cell_range,
        ]
    )
    # lark-cli's response shape mirrors the v2 spreadsheets API:
    #   { "valueRange": { "values": [[...], [...]] } }
    # Some versions wrap differently; guard against both.
    vr = payload.get("valueRange") or payload.get("data", {}).get("valueRange") or {}
    return vr.get("values") or payload.get("values") or []


def upsert_cells(conn, spreadsheet_token, sheet_id, matrix):
    conn.execute(
        "DELETE FROM feishu_sheet_cells WHERE spreadsheet_token=? AND sheet_id=?",
        (spreadsheet_token, sheet_id),
    )
    rows = []
    for row_idx, row in enumerate(matrix):
        for col_idx, cell in enumerate(row):
            if cell is None or cell == "":
                continue
            value_text = str(cell)
            value_number = None
            if isinstance(cell, (int, float)):
                value_number = float(cell)
            else:
                # Lark sometimes returns numbers as strings — try parsing.
                try:
                    value_number = float(cell)
                except (TypeError, ValueError):
                    value_number = None
            rows.append(
                (spreadsheet_token, sheet_id, row_idx, col_idx, value_text, value_number)
            )
    if rows:
        conn.executemany(
            """
            INSERT INTO feishu_sheet_cells
              (spreadsheet_token, sheet_id, row_idx, col_idx, value_text, value_number)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    return len(rows)


def upsert_log(conn, source_key, status, row_count):
    conn.execute(
        """
        INSERT INTO feishu_sync_log
          (source_type, source_key, last_synced_at, row_count, status)
        VALUES ('sheet', ?, ?, ?, ?)
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
    p.add_argument("--url", required=True, help="Spreadsheet share URL.")
    p.add_argument("--spreadsheet-token", required=True, help="The token portion of the share URL.")
    p.add_argument("--sheet-id", required=True, help="Sheet/tab id (run `sheets +info` to list).")
    p.add_argument("--range", default="A1:Z10000", dest="cell_range")
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

    source_key = f"{args.spreadsheet_token}/{args.sheet_id}"
    conn = sqlite3.connect(str(db_path))
    try:
        print(f"[1/2] sheets +read {source_key} range={args.cell_range}")
        matrix = fetch_sheet(args.url, args.sheet_id, args.cell_range)
        cell_count = upsert_cells(conn, args.spreadsheet_token, args.sheet_id, matrix)
        conn.commit()
        print(f"      {len(matrix)} rows / {cell_count} non-empty cells")

        print("[2/2] sync log")
        upsert_log(conn, source_key, "ok", cell_count)
        conn.commit()
    except Exception as e:
        upsert_log(conn, source_key, f"err: {e}", 0)
        conn.commit()
        die(str(e))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
