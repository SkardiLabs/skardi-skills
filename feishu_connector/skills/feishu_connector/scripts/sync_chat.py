#!/usr/bin/env python3
"""把飞书聊天消息同步成本地 SQLite，一条消息一行(群聊或单聊)。
v1（路线 A）：走 lark-cli `im --as user`——读你自己所在的会话，无需把 bot 拉进去。
agent 用 SQL 按发送人/关键词/时间筛消息，再读 content 自己总结。

安全/完整性:
- 本地快照不继承飞书 ACL——见 SKILL.md 的边界说明;db 文件收紧为仅当前用户可读(0600)。
- 原子替换:先写临时表、成功后才换掉正式表;同步失败保留旧数据。
- 完整性校验:收集到的条数须等于服务端 total,否则(限流/丢页)明确失败、不覆盖旧表。
- --chat-name 只用来定位;匹配不唯一就退出、要求 --chat-id(聊天敏感,拒绝猜)。
lark-cli 1.0.68 脾气:--order desc 翻页(asc 会空);消息已富化(sender.name/格式化时间);--no-reactions 减负。"""
import argparse, json, subprocess, sqlite3, sys, time, os, re
from datetime import datetime, timedelta

def lark(args):
    """跑 lark-cli --as user（im/docs/wiki/api 默认吐 JSON，无需 --json）。失败即报错。"""
    p = subprocess.run(["lark-cli", *args, "--as", "user"], capture_output=True, text=True)
    out = "".join(l for l in p.stdout.splitlines() if not l.startswith("[page"))  # 只解析 stdout
    if p.returncode != 0:
        raise RuntimeError(f"lark-cli {' '.join(args[:2])}… failed: {(p.stderr or out)[:200]}")
    try:
        d = json.loads(out, strict=False)   # strict=False: 容忍消息正文里的裸控制字符
    except json.JSONDecodeError:
        raise RuntimeError(f"lark-cli {' '.join(args[:2])}… non-JSON: {out[:200]}")
    if d.get("code") not in (0, None) or d.get("ok") is False:
        raise RuntimeError(f"lark-cli error: {json.dumps(d, ensure_ascii=False)[:200]}")
    return d.get("data", {})

def lark_retry(args, tries=4):
    """im 端口在快速连调时会限流——异常时退避重试。"""
    last = None
    for i in range(tries):
        try:
            return lark(args)
        except Exception as e:
            last = e
            if i == tries - 1:
                raise
            time.sleep(1.5 * (i + 1))  # 1.5s, 3s, 4.5s
    raise last

def list_all_chats():
    """翻页列出当前用户的全部会话——群聊 + 单聊(chat-list 默认只给群，需 --types 才含 p2p)。"""
    chats, page = [], None
    while True:
        args = ["im", "+chat-list", "--types", "p2p,group"]
        if page:
            args += ["--page-token", page]
        d = lark_retry(args)
        chats += d.get("chats") or d.get("items") or []
        page = d.get("page_token")
        if not d.get("has_more") or not page:
            break
    return chats

def resolve_chat(chat_id, chat_name):
    """→ (chat_id, chat_name)。--chat-id 直接用并反查名。--chat-name 只用于定位:
    唯一命中才继续,否则列出候选并退出、要求 --chat-id(聊天敏感,绝不猜哪个)。"""
    chats = list_all_chats()
    if chat_id:
        name = next((c.get("name") for c in chats if c.get("chat_id") == chat_id), None)
        return chat_id, name or chat_id
    # 先精确匹配,无精确再模糊;本地列表没有再走 chat-search
    exact = [c for c in chats if c.get("name") == chat_name]
    cands = exact or [c for c in chats if chat_name in (c.get("name") or "")]
    if not cands:
        sr = lark_retry(["im", "+chat-search", "--query", chat_name])
        found = sr.get("chats") or sr.get("items") or []
        ex = [c for c in found if c.get("name") == chat_name]
        cands = ex or found
    uniq = {c.get("chat_id"): c for c in cands if c.get("chat_id")}
    if not uniq:
        sys.exit(f"chat not found: {chat_name}")
    if len(uniq) > 1:
        lines = "\n".join(f"    {c.get('chat_id')}  {c.get('name')}  [{c.get('chat_mode')}]"
                          for c in uniq.values())
        sys.exit(f"'{chat_name}' 匹配到多个会话，请改用 --chat-id 指定其一:\n{lines}")
    c = next(iter(uniq.values()))
    return c.get("chat_id"), c.get("name") or chat_name

def extract_text(m):
    """把一条消息抽成可读文本。text→纯文本；富文本/JSON→尽量取 text 字段，否则保留 JSON；
    图片/文件等→占位标签。"""
    mt = m.get("msg_type")
    c = m.get("content")
    if mt in ("image", "file", "audio", "media", "sticker"):
        return f"[{mt}]"
    if not c:
        return f"[{mt}]" if mt and mt != "text" else ""
    if isinstance(c, dict):
        return c.get("text") or json.dumps(c, ensure_ascii=False)
    s = str(c).strip()
    if s.startswith("{"):
        try:
            obj = json.loads(s)
            if isinstance(obj, dict) and "text" in obj:
                return obj["text"]
            return json.dumps(obj, ensure_ascii=False)
        except json.JSONDecodeError:
            return s
    return s

def main():
    ap = argparse.ArgumentParser(description="Sync a Feishu chat's messages into a local SQLite table (one message per row).")
    ap.add_argument("--chat-id", help="chat id (oc_...)")
    ap.add_argument("--chat-name", help="chat name — used only to locate; must match exactly one chat, else it exits and asks for --chat-id")
    ap.add_argument("--days", type=int, default=30, help="only messages from the last N days (default 30; 0 = all)")
    ap.add_argument("--out", default="feishu.db")
    ap.add_argument("--table-name", default="feishu_chat")
    a = ap.parse_args()
    if not a.chat_id and not a.chat_name:
        sys.exit("give --chat-id or --chat-name")
    tbl = a.table_name
    if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', tbl):
        sys.exit("--table-name must be a simple identifier (letters/digits/underscore)")

    chat_id, chat_name = resolve_chat(a.chat_id, a.chat_name)
    start_iso = None
    if a.days and a.days > 0:
        start_iso = (datetime.now() - timedelta(days=a.days)).replace(microsecond=0).isoformat()

    # 翻页拉取(desc；--no-reactions 减负;限流→退避重试)
    msgs, page, total = [], None, None
    while True:
        args = ["im", "+chat-messages-list", "--chat-id", chat_id,
                "--order", "desc", "--page-size", "50", "--no-reactions"]
        if start_iso:
            args += ["--start", start_iso]
        if page:
            args += ["--page-token", page]
        d = lark_retry(args)
        msgs += d.get("messages") or d.get("items") or []   # chat-messages-list 返回 data.messages
        if total is None:
            total = d.get("total")
        page = d.get("page_token")
        if not d.get("has_more") or not page:
            break

    # 完整性校验:收集数应等于服务端 total,否则(限流空返回/丢页)明确失败、不覆盖旧表
    if isinstance(total, int) and len(msgs) < total:
        sys.exit(f"incomplete sync: collected {len(msgs)} messages but server total={total} "
                 f"(likely rate-limited or a dropped page). Aborting to keep the previous table intact; retry later.")

    rows = []
    for m in msgs:
        if m.get("deleted"):
            continue
        sender = m.get("sender") or {}
        sname = sender.get("name") or sender.get("id") or ""
        rows.append((chat_id, chat_name, m.get("message_id"), sname,
                     m.get("create_time"), m.get("msg_type"), extract_text(m)))

    # 原子替换:写临时表 → 成功后丢旧表、改名(任何一步失败,旧表完好保留)
    tmp = tbl + "__new"
    conn = sqlite3.connect(a.out); cur = conn.cursor()
    cur.execute(f'DROP TABLE IF EXISTS "{tmp}"')
    cur.execute(f'CREATE TABLE "{tmp}" '
                '(chat_id TEXT, chat_name TEXT, message_id TEXT PRIMARY KEY, sender TEXT, '
                'send_time TEXT, msg_type TEXT, content TEXT)')
    cur.executemany(f'INSERT OR REPLACE INTO "{tmp}" VALUES (?,?,?,?,?,?,?)', rows)
    cur.execute(f'DROP TABLE IF EXISTS "{tbl}"')
    cur.execute(f'ALTER TABLE "{tmp}" RENAME TO "{tbl}"')
    conn.commit(); conn.close()
    try:
        os.chmod(a.out, 0o600)   # 本地快照含私密聊天:收紧为仅当前用户可读
    except OSError:
        pass
    print(f"OK: synced {len(rows)} messages from '{chat_name}' -> {a.out} (table '{tbl}')")

if __name__ == "__main__":
    main()
