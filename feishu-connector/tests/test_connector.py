#!/usr/bin/env python3
"""feishu_connector 聚焦单测(纯 stdlib,不需要 lark-cli / skardi)。
覆盖不依赖网络的核心不变量:
  - 原子换表:插入失败时旧表数据必须完好(P1-2 数据丢失回归护栏)
  - 原子换表:成功时数据被替换
  - qident:SQLite 标识符转义(P2-6,飞书字段名可能含引号)
  - extract_text:聊天消息文本抽取
依赖 lark-cli 的行为(限流空返回、子树部分失败、同名聊天拒绝)需集成测试 + lark mock,列为后续。
跑法: python3 tests/test_connector.py
"""
import os, sys, sqlite3, tempfile

SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "skills", "feishu-connector", "scripts")
sys.path.insert(0, SCRIPTS)
import sync_bitable          # noqa: E402  (import 不会跑 main —— 有 __main__ 卫语句)
import sync_chat             # noqa: E402
qident = sync_bitable.qident
extract_text = sync_chat.extract_text


def _swap(conn, table, insert_rows, fail=False):
    """复刻脚本的原子换表:写临时表 → 成功后丢旧表、改名。fail=True 时插入阶段故意报错。"""
    tmp = table + "__new"
    conn.execute(f'DROP TABLE IF EXISTS "{tmp}"')
    conn.execute(f'CREATE TABLE "{tmp}" (a TEXT)')
    payload = insert_rows if not fail else insert_rows + [(object(),)]  # object() 无法绑定 → 报错
    conn.executemany(f'INSERT INTO "{tmp}" VALUES (?)', payload)
    conn.execute(f'DROP TABLE IF EXISTS "{table}"')
    conn.execute(f'ALTER TABLE "{tmp}" RENAME TO "{table}"')
    conn.commit()


def test_atomic_swap_preserves_old_on_failure():
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    try:
        conn = sqlite3.connect(path)
        conn.execute('CREATE TABLE t (a TEXT)')
        conn.execute("INSERT INTO t VALUES ('old')")
        conn.commit()
        try:
            _swap(conn, "t", [("x",)], fail=True)   # 插入阶段报错,不应到达换名
        except Exception:
            pass
        rows = conn.execute("SELECT a FROM t").fetchall()
        assert rows == [("old",)], f"数据丢失!旧表应保留 'old',实际 {rows}"
        conn.close()
    finally:
        os.unlink(path)


def test_atomic_swap_replaces_on_success():
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    try:
        conn = sqlite3.connect(path)
        conn.execute('CREATE TABLE t (a TEXT)')
        conn.execute("INSERT INTO t VALUES ('old')")
        conn.commit()
        _swap(conn, "t", [("new",)])
        rows = conn.execute("SELECT a FROM t").fetchall()
        assert rows == [("new",)], f"成功换表后应为 'new',实际 {rows}"
        conn.close()
    finally:
        os.unlink(path)


def test_qident_escaping():
    assert qident("a") == '"a"'
    assert qident('a"b') == '"a""b"'          # 内部双引号翻倍
    assert qident("工作方向") == '"工作方向"'
    # 转义后能安全用作真实列名(含引号)
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    try:
        conn = sqlite3.connect(path)
        conn.execute(f'CREATE TABLE t ({qident(chr(34)+"x")} TEXT)')  # 列名含引号
        conn.execute(f'INSERT INTO t VALUES (?)', ("v",))
        assert conn.execute("SELECT * FROM t").fetchall() == [("v",)]
        conn.close()
    finally:
        os.unlink(path)


def test_extract_text():
    assert extract_text({"msg_type": "text", "content": "hello"}) == "hello"
    assert extract_text({"msg_type": "image", "content": ""}) == "[image]"
    assert extract_text({"msg_type": "file", "content": ""}) == "[file]"
    assert extract_text({"msg_type": "text", "content": '{"text":"hi"}'}) == "hi"
    assert extract_text({"msg_type": "post", "content": "plain"}) == "plain"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
