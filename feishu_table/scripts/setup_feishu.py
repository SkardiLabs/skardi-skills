#!/usr/bin/env python3
"""Render a Skardi workspace that mirrors Feishu (Lark) Bitables / Sheets.

Idempotent: re-running rewrites the rendered files but never destroys the
mirror DB. Schema creation is `CREATE TABLE IF NOT EXISTS` — safe on every
invocation.

Flow:
  1. Verify `lark-cli` is on PATH (the skill shells out to it for every
     read; without it, sync_*.py can't run).
  2. Verify `skardi --version >= 0.4.0` so the kind: semantics overlay
     loads — older binaries silently ignore the file.
  3. Render <workspace>/{ctx.yaml, semantics.yaml, pipelines/*.yaml} from
     ../assets/, substituting the absolute mirror DB path.
  4. Bootstrap the SQLite mirror (feishu_bitable_records,
     feishu_bitable_fields, feishu_sheet_cells, feishu_sync_log) —
     CREATE TABLE IF NOT EXISTS, no destructive DDL.
  5. Run `skardi query --sql "SELECT 1"` to surface an early failure if
     the rendered ctx is malformed or the mirror file isn't readable.
"""
import argparse
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
ASSETS = SKILL_DIR / "assets"

MIN_SKARDI_MAJOR = 0
MIN_SKARDI_MINOR = 4

MIRROR_DDL = """
CREATE TABLE IF NOT EXISTS feishu_bitable_records (
  base_token  TEXT NOT NULL,
  table_id    TEXT NOT NULL,
  record_id   TEXT NOT NULL,
  fields_json TEXT NOT NULL,
  synced_at   TEXT NOT NULL,
  PRIMARY KEY (base_token, table_id, record_id)
);

CREATE INDEX IF NOT EXISTS idx_records_table
  ON feishu_bitable_records(base_token, table_id);

CREATE TABLE IF NOT EXISTS feishu_bitable_fields (
  base_token  TEXT NOT NULL,
  table_id    TEXT NOT NULL,
  field_id    TEXT NOT NULL,
  field_name  TEXT NOT NULL,
  field_type  INTEGER NOT NULL,
  is_primary  INTEGER NOT NULL DEFAULT 0,
  description TEXT,
  PRIMARY KEY (base_token, table_id, field_id)
);

CREATE TABLE IF NOT EXISTS feishu_sheet_cells (
  spreadsheet_token TEXT NOT NULL,
  sheet_id          TEXT NOT NULL,
  row_idx           INTEGER NOT NULL,
  col_idx           INTEGER NOT NULL,
  value_text        TEXT,
  value_number      REAL,
  PRIMARY KEY (spreadsheet_token, sheet_id, row_idx, col_idx)
);

CREATE TABLE IF NOT EXISTS feishu_sync_log (
  source_type    TEXT NOT NULL,
  source_key     TEXT NOT NULL,
  last_synced_at TEXT NOT NULL,
  row_count      INTEGER NOT NULL,
  status         TEXT NOT NULL,
  PRIMARY KEY (source_type, source_key)
);
"""


def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def check_lark_cli():
    if shutil.which("lark-cli") is None:
        die(
            "`lark-cli` not found on PATH. Install with `npm i -g @larksuite/cli` "
            "(needs Node >= 18). Then run `lark-cli config init` to record "
            "your app_id / app_secret in the OS keychain, and "
            "`lark-cli auth login --domain base,sheets,drive` to grant the "
            "scopes the sync scripts need."
        )
    out = subprocess.run(["lark-cli", "--version"], capture_output=True, text=True)
    print(f"  lark-cli: {(out.stdout or out.stderr).strip() or 'unknown'}")


def check_skardi():
    if shutil.which("skardi") is None:
        die(
            "`skardi` CLI not found on PATH. Install >= 0.4.0 with "
            "`cargo install --locked --git https://github.com/SkardiLabs/skardi "
            "--branch main skardi-cli`."
        )
    out = subprocess.run(["skardi", "--version"], capture_output=True, text=True)
    raw = (out.stdout or out.stderr).strip()
    print(f"  skardi:   {raw or 'version unknown'}")
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", raw)
    if not m:
        print(
            "  warning: could not parse version; need >= 0.4.0 for the "
            "kind: semantics overlay.",
            file=sys.stderr,
        )
        return
    major, minor = int(m.group(1)), int(m.group(2))
    if (major, minor) < (MIN_SKARDI_MAJOR, MIN_SKARDI_MINOR):
        die(
            f"Skardi {raw} is too old for this skill (need >= "
            f"{MIN_SKARDI_MAJOR}.{MIN_SKARDI_MINOR}.0)."
        )


def render_template(src: Path, dst: Path, substitutions: dict):
    text = src.read_text()
    for key, value in substitutions.items():
        text = text.replace("{{" + key + "}}", value)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text)
    print(f"  rendered: {dst.relative_to(dst.parent.parent)}")


def bootstrap_mirror(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(MIRROR_DDL)
        conn.commit()
    finally:
        conn.close()
    print(f"  mirror:   {db_path}")


def health_probe(workspace: Path):
    env = os.environ.copy()
    env["SKARDICONFIG"] = str(workspace)
    out = subprocess.run(
        ["skardi", "query", "--sql", "SELECT 1"],
        env=env,
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        die(
            "Health probe `skardi query --sql 'SELECT 1'` failed:\n"
            f"  stdout: {out.stdout}\n  stderr: {out.stderr}\n"
            "Most common cause: the rendered ctx.yaml points at a path the "
            "current shell can't read. Inspect <workspace>/ctx.yaml."
        )
    print("  health:   ok")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--workspace", required=True, help="Workspace dir; created if missing.")
    p.add_argument(
        "--db-path",
        default=None,
        help="Mirror SQLite path. Defaults to <workspace>/feishu.db.",
    )
    args = p.parse_args()

    workspace = Path(args.workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    db_path = Path(args.db_path).resolve() if args.db_path else workspace / "feishu.db"

    print("Checking prerequisites:")
    check_lark_cli()
    check_skardi()

    print("Rendering workspace:")
    subs = {"DB_PATH": str(db_path)}
    render_template(ASSETS / "ctx.yaml.tpl", workspace / "ctx.yaml", subs)
    render_template(ASSETS / "semantics.yaml.tpl", workspace / "semantics.yaml", subs)
    pipelines_dst = workspace / "pipelines"
    for tpl in sorted((ASSETS / "pipelines").glob("*.yaml.tpl")):
        render_template(tpl, pipelines_dst / tpl.name.replace(".tpl", ""), subs)

    print("Bootstrapping mirror:")
    bootstrap_mirror(db_path)

    print("Running health probe:")
    health_probe(workspace)

    print()
    print(f"Workspace ready: {workspace}")
    print(f"Mirror DB:       {db_path}")
    print()
    print("Next steps:")
    print(f"  python {SKILL_DIR}/scripts/sync_bitable.py \\")
    print(f"    --workspace {workspace} \\")
    print("    --base-token <app_xxx> --table-id <tbl_xxx>")
    print()
    print(f"  python {SKILL_DIR}/scripts/sync_sheets.py \\")
    print(f"    --workspace {workspace} \\")
    print('    --url "<spreadsheet share URL>" --range "A1:Z10000"')
    print()
    print("Then query via Skardi:")
    print(f"  SKARDICONFIG={workspace} skardi query --pipeline list-feishu-sources")


if __name__ == "__main__":
    main()
