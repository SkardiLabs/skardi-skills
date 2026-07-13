#!/usr/bin/env python3
"""把飞书云文档(docx)同步成本地 SQLite,一篇一行(整篇正文按 markdown 存)。
v1(路线 A):agent 用 SQL 按标题/关键词筛出相关文档,再读 content_md 自己抽内容。
不做切块/向量——少量文档时 agent 本身就是语义层;大规模语义检索请用 auto_knowledge_base。"""
import argparse, json, subprocess, sqlite3, sys, re
from datetime import datetime

def lark(args):
    """跑 lark-cli --as user，返回解析后的 data；失败即报错。
    注意:--json 是 base 插件专属 flag，wiki/docs/api 子命令不加(默认就吐 JSON)。"""
    p = subprocess.run(["lark-cli", *args, "--as", "user"], capture_output=True, text=True)
    out = "".join(l for l in p.stdout.splitlines() if not l.startswith("[page"))  # 只解析 stdout
    if p.returncode != 0:
        sys.exit(f"lark-cli {' '.join(args[:3])}… failed: {(p.stderr or out)[:200]}")
    try:
        d = json.loads(out, strict=False)   # strict=False: 容忍正文里的裸控制字符
    except json.JSONDecodeError:
        sys.exit(f"lark-cli {' '.join(args[:3])}… non-JSON: {out[:200]}")
    if d.get("code") not in (0, None) or d.get("ok") is False:   # 兼容 code/ok 两种信封
        sys.exit(f"lark-cli error: {json.dumps(d, ensure_ascii=False)[:300]}")
    return d.get("data", {})

def lark_try(args):
    """非致命版:失败(旧版拒绝 flag / 非 JSON / 报错)返回 None，用于版本探测回退。"""
    p = subprocess.run(["lark-cli", *args, "--as", "user"], capture_output=True, text=True)
    out = "".join(l for l in p.stdout.splitlines() if not l.startswith("[page"))  # 只解析 stdout
    if p.returncode != 0:
        return None
    try:
        d = json.loads(out, strict=False)   # strict=False: 容忍正文里的裸控制字符
    except json.JSONDecodeError:
        return None
    if d.get("code") not in (0, None) or d.get("ok") is False:
        return None
    return d.get("data", {})

def resolve_doc(token_or_url):
    """URL/token → (docx_document_id, title)。处理 /wiki/、/docx/、/docs/ 和裸 token。"""
    m = re.search(r'/(?:wiki|docx|docs)/([A-Za-z0-9]+)', token_or_url)
    tok = m.group(1) if m else token_or_url
    if "/wiki/" in token_or_url:            # wiki 节点要解析到底层 docx obj_token
        d = lark_try(["wiki", "spaces", "get_node", "--params", json.dumps({"token": tok})])  # 非致命:失败则当裸 token
        node = (d or {}).get("node")
        if node and node.get("obj_type") in ("docx", "doc") and node.get("obj_token"):
            return node["obj_token"], node.get("title") or tok
    return tok, None                         # 当作已是 docx token

def list_children(space, node):
    items, page = [], None
    while True:                                  # 翻页,避免子节点 >50 被静默截断
        params = {"parent_node_token": node, "page_size": 50}
        if page:
            params["page_token"] = page
        d = lark_try(["api", "GET", f"/open-apis/wiki/v2/spaces/{space}/nodes",
                      "--params", json.dumps(params)])  # 非致命:某页失败则停在已拿到的
        if not d:
            break
        items += d.get("items") or []
        page = d.get("page_token")
        if not d.get("has_more") or not page:
            break
    return items

def collect_from_node(space, node_token, acc):
    """递归收集 wiki 子树里所有 docx 节点 → [(doc_id, title, wiki_url)]。单节点失败则跳过、不中断整树。"""
    if not node_token:
        return
    d = lark_try(["wiki", "spaces", "get_node", "--params", json.dumps({"token": node_token})])
    node = (d or {}).get("node") or {}
    if node.get("obj_type") in ("docx", "doc") and node.get("obj_token"):
        acc.append((node["obj_token"], node.get("title"), f"wiki/{node_token}"))
    if node.get("has_child"):
        for ch in list_children(space, node_token):
            collect_from_node(space, ch.get("node_token"), acc)

def fetch_content(doc_id):
    """取整篇正文 markdown。目标 lark-cli 版本 = 最新(用户 npm 装到的即最新):
    需 --doc-format markdown，正文在 data.document.content。"""
    d = lark(["docs", "+fetch", "--doc", doc_id, "--doc-format", "markdown"])
    return (d.get("document") or {}).get("content", "")

def fetch_title(doc_id):
    """标题走 docx 元数据 API —— 两个版本都稳(fetch 的 title 字段在 1.0.68 已消失)。"""
    d = lark_try(["api", "GET", f"/open-apis/docx/v1/documents/{doc_id}"])
    return ((d or {}).get("document") or {}).get("title")

def main():
    ap = argparse.ArgumentParser(description="Sync Feishu cloud docs (docx) into a local SQLite table (one doc per row).")
    ap.add_argument("--doc", action="append", default=[], help="doc URL or docx token (repeatable)")
    ap.add_argument("--node", help="wiki node token: pull ALL docx under this subtree")
    ap.add_argument("--space", help="wiki space id (required with --node)")
    ap.add_argument("--out", default="feishu.db")
    ap.add_argument("--table-name", default="feishu_docs")
    a = ap.parse_args()
    if a.node and not a.space:
        sys.exit("--node requires --space")
    if not a.doc and not a.node:
        sys.exit("give at least one --doc or a --node/--space subtree")

    # 1) 汇总要同步的文档清单 (doc_id, wiki_title, source_url)
    targets = []
    for d in a.doc:
        doc_id, title = resolve_doc(d)
        targets.append((doc_id, title, d))
    if a.node:
        collect_from_node(a.space, a.node, targets)
    # 去重(同一 docx 可能既在列表又在子树)
    seen, uniq = set(), []
    for t in targets:
        if t[0] not in seen:
            seen.add(t[0]); uniq.append(t)

    # 2) 逐篇取正文
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows, fail = [], 0
    for doc_id, wiki_title, url in uniq:
        try:
            md = fetch_content(doc_id)
        except SystemExit as e:
            print(f"  SKIP {doc_id}: {e}", file=sys.stderr); fail += 1; continue
        title = wiki_title or fetch_title(doc_id) or doc_id
        rows.append((doc_id, title, url, md, now))

    # 3) 写 SQLite
    conn = sqlite3.connect(a.out); cur = conn.cursor()
    cur.execute(f'DROP TABLE IF EXISTS "{a.table_name}"')
    cur.execute(f'CREATE TABLE "{a.table_name}" '
                '(doc_id TEXT PRIMARY KEY, title TEXT, url TEXT, content_md TEXT, synced_at TEXT)')
    cur.executemany(f'INSERT OR REPLACE INTO "{a.table_name}" VALUES (?,?,?,?,?)', rows)
    conn.commit(); conn.close()
    print(f"OK: synced {len(rows)} docs" + (f" ({fail} failed)" if fail else "") +
          f" -> {a.out} (table '{a.table_name}')")
    for r in rows:
        print(f"  · {r[1]}  ({len(r[3])} chars)")

main()
