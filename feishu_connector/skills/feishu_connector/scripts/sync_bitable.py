#!/usr/bin/env python3
"""把飞书多维表格(Bitable)同步成本地 SQLite。v1: 手动一次性同步；特殊字段先按文本存。"""
import argparse, json, subprocess, sqlite3, sys

def lark(args):
    r = subprocess.run(["lark-cli","base"]+args+["--as","user","--json"], capture_output=True, text=True)
    out = "".join(l for l in (r.stdout+r.stderr).splitlines() if not l.startswith("[page"))
    d = json.loads(out)
    if not d.get("ok"): sys.exit(f"lark-cli error: {json.dumps(d,ensure_ascii=False)[:300]}")
    return d["data"]

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

    fl = lark(["+field-list","--base-token",a.base_token,"--table-id",a.table_id])
    ftype = {f.get("field_name") or f["name"]: f["type"] for f in fl.get("items") or fl["fields"]}

    rows, cols, offset = [], None, 0
    while True:
        rec = lark(["+record-list","--base-token",a.base_token,"--table-id",a.table_id,"--limit",str(a.limit),"--offset",str(offset)])
        cols = cols or rec.get("fields") or rec.get("field_names")
        if cols is None: sys.exit(f"no column names in record-list; data keys = {list(rec.keys())}")
        batch = rec.get("data", [])
        rows += batch
        if len(batch) < a.limit: break
        offset += a.limit

    def sqltype(name): return {"number":"REAL","checkbox":"INTEGER"}.get(ftype.get(name),"TEXT")
    conn = sqlite3.connect(a.out); cur = conn.cursor()
    cur.execute(f'DROP TABLE IF EXISTS "{a.table_name}"')
    cur.execute(f'CREATE TABLE "{a.table_name}" (' + ", ".join(f'"{c}" {sqltype(c)}' for c in cols) + ')')
    ph = ",".join("?"*len(cols))
    cur.executemany(f'INSERT INTO "{a.table_name}" VALUES ({ph})', [[norm(v) for v in row] for row in rows])
    conn.commit(); conn.close()
    print(f"OK: synced {len(rows)} rows, {len(cols)} cols -> {a.out} (table '{a.table_name}')")
    print("columns:", cols)

main()
