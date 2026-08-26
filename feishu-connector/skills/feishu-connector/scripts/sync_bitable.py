#!/usr/bin/env python3
"""把飞书多维表格(Bitable)同步成本地 SQLite。v1: 手动一次性同步；特殊字段先按文本存。
安全/完整性:本地快照不继承飞书 ACL(见 SKILL.md);db 收紧为 0600;原子替换(写临时表→成功才换),
同步失败保留旧表;列名/表名做 SQLite 标识符转义。"""
import argparse, json, subprocess, sqlite3, sys, re, os

def qident(name):
    """安全的 SQLite 标识符:双引号包裹 + 内部双引号转义(飞书字段名可能含引号/特殊字符)。"""
    return '"' + str(name).replace('"', '""') + '"'

def lark(args):
    r = subprocess.run(["lark-cli", "base"] + args + ["--as", "user", "--json"], capture_output=True, text=True)
    out = "".join(l for l in r.stdout.splitlines() if not l.startswith("[page"))  # 只解析 stdout
    if r.returncode != 0:
        sys.exit(f"lark-cli base {args[0] if args else ''}… failed: {(r.stderr or out)[:200]}")
    try:
        d = json.loads(out, strict=False)   # strict=False: 容忍单元格里的裸控制字符
    except json.JSONDecodeError:
        sys.exit(f"lark-cli base… non-JSON: {out[:200]}")
    if not d.get("ok"):
        sys.exit(f"lark-cli error: {json.dumps(d, ensure_ascii=False)[:300]}")
    return d.get("data", {})

def norm(v):                      # 值 → sqlite 标量（v1：数组/多选 → 文本）
    if isinstance(v, bool): return 1 if v else 0
    if isinstance(v, list): return " / ".join(str(x) for x in v)
    if isinstance(v, dict): return json.dumps(v, ensure_ascii=False)
    return v

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-token", required=True)
    ap.add_argument("--table-id", required=True)
    ap.add_argument("--out", default="feishu.db")
    ap.add_argument("--table-name", default="feishu_table")
    ap.add_argument("--limit", type=int, default=100)
    a = ap.parse_args()
    tbl = a.table_name
    if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', tbl):
        sys.exit("--table-name must be a simple identifier (letters/digits/underscore)")

    fl = lark(["+field-list", "--base-token", a.base_token, "--table-id", a.table_id])
    ftype = {f.get("field_name") or f["name"]: f["type"] for f in fl.get("items") or fl["fields"]}

    rows, cols, offset = [], None, 0
    while True:
        rec = lark(["+record-list", "--base-token", a.base_token, "--table-id", a.table_id,
                    "--limit", str(a.limit), "--offset", str(offset)])
        cols = cols or rec.get("fields") or rec.get("field_names")
        if cols is None:
            sys.exit(f"no column names in record-list; data keys = {list(rec.keys())}")
        batch = rec.get("data", [])
        rows += batch
        if len(batch) < a.limit:
            break
        offset += a.limit

    def sqltype(name): return {"number": "REAL", "checkbox": "INTEGER"}.get(ftype.get(name), "TEXT")

    # 原子替换:写临时表 → 成功后丢旧表、改名(任何一步失败,旧表完好保留)
    tmp = tbl + "__new"
    conn = sqlite3.connect(a.out); cur = conn.cursor()
    cur.execute(f'DROP TABLE IF EXISTS {qident(tmp)}')
    cur.execute(f'CREATE TABLE {qident(tmp)} (' + ", ".join(f'{qident(c)} {sqltype(c)}' for c in cols) + ')')
    ph = ",".join("?" * len(cols))
    cur.executemany(f'INSERT INTO {qident(tmp)} VALUES ({ph})', [[norm(v) for v in row] for row in rows])
    cur.execute(f'DROP TABLE IF EXISTS {qident(tbl)}')
    cur.execute(f'ALTER TABLE {qident(tmp)} RENAME TO {qident(tbl)}')
    conn.commit(); conn.close()
    try:
        os.chmod(a.out, 0o600)   # 本地快照:收紧为仅当前用户可读
    except OSError:
        pass
    print(f"OK: synced {len(rows)} rows, {len(cols)} cols -> {a.out} (table '{tbl}')")
    print("columns:", cols)

if __name__ == "__main__":
    main()
