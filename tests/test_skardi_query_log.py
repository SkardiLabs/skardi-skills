#!/usr/bin/env python3
"""What skardi-query-log must keep true, written from what a user relies on.

- Filing candidates out of the log never loses what the index already held:
  questions, rejected signatures and the pitfalls filed from failures.
- A filed statement is the statement that ran. Whitespace inside a string
  literal is data, so `'A  B'` is filed as `'A  B'` and is a different
  statement from `'A B'`; layout outside quotes does not make two statements.
- A final query sent through `ask.py` carries the task's session id and task
  line, so it sits in the ledger with the peeks that led to it.
- `retrieval` only runs `ask.py` against a self-hosted server without a token;
  on a Skardi Cloud context, or with a token set, the opening block skips it.

Run: python3 -m pytest tests/test_skardi_query_log.py
"""
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "skills" / "skardi-query-log" / "scripts"
SEED = SCRIPTS / "seed_index.py"
ASK = SCRIPTS / "ask.py"
RETRIEVAL = ROOT / "skills" / "retrieval" / "SKILL.md"


def audit_db(path, rows):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE query_audit (sql TEXT, ai_context TEXT, status TEXT, created_at TEXT)")
    for i, (sql, purpose) in enumerate(rows):
        con.execute("INSERT INTO query_audit VALUES (?, ?, 'succeeded', ?)",
                    (sql, json.dumps({"purpose": purpose}), f"2026-10-21T00:00:{i:02d}+00:00"))
    con.commit()
    con.close()


def seed(*args):
    r = subprocess.run([sys.executable, str(SEED), *args], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr + r.stdout
    return r.stdout


# --- seed_index.py -----------------------------------------------------------

def test_keep_preserves_pitfalls_and_existing_entries(tmp_path):
    db, index = tmp_path / "audit.db", tmp_path / "index.json"
    audit_db(db, [("SELECT 1 FROM a", "first"), ("SELECT 2 FROM b", "second")])
    pitfall = {"kind": "table name", "what": "action name used as a table",
               "bad": "SELECT * FROM list_prs", "good": "SELECT * FROM github.prs"}
    existing = {"id": "q001", "question": "old", "sql": "SELECT 0 FROM z", "hits": 3}
    index.write_text(json.dumps({"questions": [existing], "rejected": ["abc"],
                                 "pitfalls": [pitfall]}))

    seed("--db", str(db), "--index", str(index), "--keep", "q001")

    after = json.loads(index.read_text())
    assert after["pitfalls"] == [pitfall]
    assert existing in after["questions"]
    assert [q["question"] for q in after["questions"]] == ["old", "first"]
    assert "abc" in after["rejected"] and len(after["rejected"]) == 2


def test_whitespace_inside_a_string_literal_is_filed_as_written(tmp_path):
    db, index = tmp_path / "audit.db", tmp_path / "index.json"
    sql = "SELECT *\n  FROM people WHERE name = 'A  B'"
    audit_db(db, [(sql, "who is A  B")])

    seed("--db", str(db), "--index", str(index), "--keep", "q001")

    filed = json.loads(index.read_text())["questions"][0]["sql"]
    assert "'A  B'" in filed


def test_literals_that_differ_in_whitespace_are_different_statements(tmp_path):
    db, index = tmp_path / "audit.db", tmp_path / "index.json"
    audit_db(db, [("SELECT * FROM p WHERE n = 'A  B'", "two spaces"),
                  ("SELECT * FROM p WHERE n = 'A B'", "one space"),
                  ("SELECT *   FROM p\nWHERE n = 'A B'", "one space, laid out differently")])

    out = seed("--db", str(db), "--index", str(index))

    assert "2 in the log are still unfiled" in out


# --- ask.py -------------------------------------------------------------------

@pytest.fixture
def server():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            body = json.dumps({"success": True, "data": [], "rows": 0, "execution_time_ms": 1}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}/query", seen
    httpd.shutdown()


def ask(url, index, *args, env=None):
    e = {k: v for k, v in os.environ.items() if not k.startswith("SKARDI_SESSION")}
    e.update(env or {})
    r = subprocess.run([sys.executable, str(ASK), "how many orders", "--sql", "SELECT 1",
                        "--url", url, "--index", str(index), "--config", "/nonexistent", *args],
                       capture_output=True, text=True, env=e)
    assert r.returncode == 0, r.stderr + r.stdout


def test_task_session_and_task_line_reach_the_ledger(tmp_path, server):
    url, seen = server
    index = tmp_path / "index.json"
    ask(url, index, "--session-id", "sess-1", "--task", "Q3 retention review")
    ask(url, index, "--session-id", "sess-1", "--task", "Q3 retention review")
    assert [r["ai_context"] for r in seen] == [
        {"purpose": "how many orders", "session_id": "sess-1", "task": "Q3 retention review"}] * 2


def test_session_falls_back_to_the_one_retrieval_minted(tmp_path, server):
    url, seen = server
    index = tmp_path / "index.json"
    ask(url, index, env={"SKARDI_SESSION": "sess-env"})
    ask(url, index, env={"SKARDI_SESSION": "sess-env"})
    assert [r["ai_context"]["session_id"] for r in seen] == ["sess-env", "sess-env"]
    assert all("task" not in r["ai_context"] for r in seen)


# --- retrieval opening block ---------------------------------------------------

def opening_block():
    text = RETRIEVAL.read_text(encoding="utf-8")
    blocks = re.findall(r"```bash\n(.*?)```", text, re.S)
    block = next(b for b in blocks if 'ASK="' in b)
    return block.replace("<this skill's directory>/../skardi-query-log/scripts/ask.py", "$FAKE_ASK")


def run_block(tmp_path, contexts, token=None):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    skardi = bindir / "skardi"
    skardi.write_text("#!/bin/sh\n"
                      'if [ "$1" = config ]; then cat "$FAKE_CONTEXTS"; exit 0; fi\n'
                      'echo "--purpose --session-id --task"\n')
    skardi.chmod(skardi.stat().st_mode | stat.S_IEXEC)
    fake_ask = tmp_path / "ask.py"
    marker = tmp_path / "asked"
    fake_ask.write_text(f"open({str(marker)!r}, 'w').write('yes')\n")
    ctx = tmp_path / "contexts"
    ctx.write_text(contexts)
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", FAKE_ASK=str(fake_ask),
               FAKE_CONTEXTS=str(ctx))
    env.pop("SKARDI_API_TOKEN", None)
    if token:
        env["SKARDI_API_TOKEN"] = token
    r = subprocess.run(["bash", "-c", opening_block()], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    return marker.exists()


HEADER = "   NAME                     MODE     WORKSPACE            SERVER\n"


def test_self_hosted_without_token_checks_the_index(tmp_path):
    assert run_block(tmp_path, HEADER + "*  local                    server   -                    http://127.0.0.1:8080\n")


def test_no_contexts_file_counts_as_self_hosted(tmp_path):
    assert run_block(tmp_path, "no contexts configured (/x/config.yaml)\n")


def test_cloud_context_skips_ask(tmp_path):
    assert not run_block(tmp_path, HEADER
                         + "   local                    server   -                    http://127.0.0.1:8080\n"
                         + "*  team                     cloud    acme                 https://gw.example\n")


def test_token_in_env_skips_ask(tmp_path):
    assert not run_block(tmp_path, "no contexts configured (/x/config.yaml)\n", token="t")
