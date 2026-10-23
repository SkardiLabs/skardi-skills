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


def test_a_line_comment_does_not_swallow_the_next_line(tmp_path):
    # `SELECT 1 -- note` then `+ 1` on the next line returns 2; on one line it
    # returns 1. Different results, so both must reach the person choosing.
    db, index = tmp_path / "audit.db", tmp_path / "index.json"
    audit_db(db, [("SELECT 1 -- note\n+ 1", "two lines"),
                  ("SELECT 1 -- note + 1", "one line")])

    out = seed("--db", str(db), "--index", str(index))

    assert "2 in the log are still unfiled" in out


def test_statements_that_differ_only_in_comments_are_one_statement(tmp_path):
    db, index = tmp_path / "audit.db", tmp_path / "index.json"
    audit_db(db, [("SELECT a FROM t -- first try\nWHERE b = 1", "with a line comment"),
                  ("SELECT a FROM t /* why */ WHERE b = 1", "with a block comment"),
                  ("SELECT a FROM t WHERE b = 1", "no comment")])

    out = seed("--db", str(db), "--index", str(index))

    assert "1 in the log are still unfiled" in out


def test_comment_markers_inside_a_literal_are_data(tmp_path):
    db, index = tmp_path / "audit.db", tmp_path / "index.json"
    audit_db(db, [("SELECT * FROM p WHERE n = 'a -- b'", "dashes in a value"),
                  ("SELECT * FROM p WHERE n = 'a -- c'", "different value")])

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
    e["SKARDI_SERVER_URL"] = url.rsplit("/query", 1)[0]   # the CLI points where the index does
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


def run_block(tmp_path, contexts, token=None, view=""):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    skardi = bindir / "skardi"
    skardi.write_text("#!/bin/sh\n"
                      'if [ "$1 $2" = "config view" ]; then cat "$FAKE_VIEW"; exit 0; fi\n'
                      'if [ "$1" = config ]; then cat "$FAKE_CONTEXTS"; exit 0; fi\n'
                      'echo "--purpose --session-id --task"\n')
    skardi.chmod(skardi.stat().st_mode | stat.S_IEXEC)
    fake_ask = tmp_path / "ask.py"
    marker = tmp_path / "asked"
    fake_ask.write_text(f"open({str(marker)!r}, 'w').write('yes')\n")
    ctx = tmp_path / "contexts"
    ctx.write_text(contexts)
    view_file = tmp_path / "view"
    view_file.write_text(view)
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", FAKE_ASK=str(fake_ask),
               FAKE_CONTEXTS=str(ctx), FAKE_VIEW=str(view_file))
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


# What `skardi config view` prints: tokens redacted, but the key is still there.
LOCAL_AND_TEAM = (HEADER
                  + "*  local                    server   -                    http://127.0.0.1:8080\n"
                  + "   team                     cloud    acme                 https://gw.example\n")


def test_token_saved_with_the_current_context_skips_ask(tmp_path):
    view = ("current-context: local\ncontexts:\n"
            "- name: local\n  server: http://127.0.0.1:8080\n  token: (redacted)\n"
            "- name: team\n  server: https://gw.example\n  mode: cloud\n  workspace: acme\n")
    assert not run_block(tmp_path, LOCAL_AND_TEAM, view=view)


def test_token_saved_only_with_another_context_still_checks_the_index(tmp_path):
    view = ("current-context: local\ncontexts:\n"
            "- name: local\n  server: http://127.0.0.1:8080\n"
            "- name: team\n  server: https://gw.example\n  mode: cloud\n  workspace: acme\n"
            "  token: (redacted)\n  token-id: pat_1\n")
    assert run_block(tmp_path, LOCAL_AND_TEAM, view=view)


def test_token_in_a_legacy_spec_file_skips_ask(tmp_path):
    view = "kind: client\nspec:\n  server: http://legacy:8080\n  token: (redacted)\n"
    assert not run_block(tmp_path, "no contexts configured (/x/config.yaml)\n", view=view)


# --- ask.py against a CLI pointed elsewhere -----------------------------------

def fake_cli(tmp_path, current_server):
    bindir = tmp_path / "clibin"
    bindir.mkdir(exist_ok=True)
    cli = bindir / "skardi"
    cli.write_text("#!/bin/sh\n"
                   "echo '   NAME                     MODE     WORKSPACE            SERVER'\n"
                   "echo '   a                        server   -                    http://127.0.0.1:1'\n"
                   f"echo '*  b                        server   -                    {current_server}'\n")
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    e = {k: v for k, v in os.environ.items() if k != "SKARDI_SERVER_URL"}
    e["PATH"] = f"{bindir}:{e['PATH']}"
    return e


# The index and url belong to one server. After `skardi config use-context b`,
# a reused statement would otherwise run against server a and answer from the
# wrong data.
@pytest.mark.parametrize("with_sql", [True, False])
def test_a_cli_switched_to_another_server_stops_ask(tmp_path, server, with_sql):
    url, seen = server
    env = fake_cli(tmp_path, "http://10.9.9.9:8080")
    cmd = [sys.executable, str(ASK), "how many orders", "--url", url,
           "--index", str(tmp_path / "index.json"), "--config", "/nonexistent"]
    if with_sql:
        cmd += ["--sql", "SELECT 1"]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    assert r.returncode == 3, r.stderr + r.stdout
    assert "http://10.9.9.9:8080" in r.stderr
    assert seen == []


def test_localhost_and_127_0_0_1_are_the_same_server(tmp_path, server):
    url, seen = server
    port = url.split(":")[2].split("/")[0]
    env = fake_cli(tmp_path, f"http://localhost:{port}")
    r = subprocess.run([sys.executable, str(ASK), "how many orders", "--sql", "SELECT 1",
                        "--url", url, "--index", str(tmp_path / "index.json"),
                        "--config", "/nonexistent"], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr + r.stdout
    assert len(seen) == 1


# --- add_pipeline.py ----------------------------------------------------------

ADD = SCRIPTS / "add_pipeline.py"


@pytest.fixture
def pipeline_server():
    """/health always answers, as the old process would; /pipelines lists `listed`."""
    listed = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                body = b"{}"
            elif self.path == "/pipelines":
                body = json.dumps({"success": True,
                                   "pipelines": [{"name": n} for n in listed]}).encode()
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_port, listed
    httpd.shutdown()


def add(tmp_path, port, restart_cmd):
    d = tmp_path / "pipelines"
    r = subprocess.run([sys.executable, str(ADD), "--name", "open_prs", "--sql",
                        "SELECT id FROM github.prs WHERE state = {state}", "--dir", str(d),
                        "--port", str(port), "--restart-cmd", restart_cmd],
                       capture_output=True, text=True, timeout=120)
    return r, d / "open_prs.yaml"


def test_a_failed_restart_is_a_failed_install_even_while_the_old_server_answers(tmp_path, pipeline_server):
    port, listed = pipeline_server
    listed.append("open_prs")      # even a listing that looks right does not excuse the exit code
    r, yaml = add(tmp_path, port, "exit 7")
    assert r.returncode != 0, r.stdout
    assert "[ok]" not in r.stdout
    assert not yaml.exists()


def test_a_server_that_comes_back_without_the_pipeline_is_a_failed_install(tmp_path, pipeline_server):
    port, _ = pipeline_server
    r, yaml = add(tmp_path, port, "true")
    assert r.returncode != 0, r.stdout
    assert "[ok]" not in r.stdout
    assert not yaml.exists()


def test_a_restart_that_registers_the_pipeline_succeeds(tmp_path, pipeline_server):
    port, listed = pipeline_server
    listed.append("open_prs")
    r, yaml = add(tmp_path, port, "true")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[ok]" in r.stdout
    assert yaml.exists()


# --- read_failures.py ---------------------------------------------------------

READ_FAILURES = SCRIPTS / "read_failures.py"


def test_a_filed_pitfall_keeps_both_statements_as_they_ran(tmp_path):
    # The working statement is what the next person copies and runs. Flattened,
    # `-- open ones only` swallowed the WHERE on the next line and the copy
    # returned every row instead of the open ones.
    bad = "SELECT id FROM github.prs\nWHERE stat = 'open'"
    good = "SELECT id FROM github.prs -- open ones only\nWHERE state = 'open'"
    db, index = tmp_path / "audit.db", tmp_path / "index.json"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE query_audit (created_at TEXT, sql TEXT, ai_context TEXT, "
                "error TEXT, status TEXT)")
    con.execute("INSERT INTO query_audit VALUES ('2026-10-21T00:00:01+00:00', ?, NULL, "
                "'Schema error: No field named stat', 'failed')", (bad,))
    con.execute("INSERT INTO query_audit VALUES ('2026-10-21T00:00:02+00:00', ?, NULL, NULL, "
                "'succeeded')", (good,))
    con.commit()
    con.close()

    r = subprocess.run([sys.executable, str(READ_FAILURES), "--db", str(db), "--index", str(index),
                        "--keep", "p001"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr + r.stdout
    [pitfall] = json.loads(index.read_text())["pitfalls"]
    assert pitfall["good"] == good
    assert pitfall["bad"] == bad
