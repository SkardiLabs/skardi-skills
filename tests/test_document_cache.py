#!/usr/bin/env python3
"""Tests for skills/document-cache/scripts/doc_cache.py (stdlib + pytest).

Every test points SKARDI_HOME at a temporary directory so the real
~/.skardi is never read or written.

Run: python3 -m pytest tests/test_document_cache.py -q
"""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "skills", "document-cache", "scripts")
SCRIPT = os.path.join(SCRIPTS, "doc_cache.py")
sys.path.insert(0, SCRIPTS)
import doc_cache  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "skardi_home"
    monkeypatch.setenv("SKARDI_HOME", str(h))
    return h


@pytest.fixture
def work(tmp_path):
    w = tmp_path / "work"
    w.mkdir()
    return w


def real(p):
    return os.path.realpath(str(p))


def run(*args, home=None, check_exit=True):
    env = dict(os.environ)
    if home is not None:
        env["SKARDI_HOME"] = str(home)
    r = subprocess.run(
        [sys.executable, SCRIPT, *[str(a) for a in args]],
        capture_output=True, text=True, env=env,
    )
    out = json.loads(r.stdout)
    if check_exit:
        assert r.returncode == 0, r.stderr + r.stdout
    return out, r.returncode


def check(path, home):
    out, _ = run("check", path, home=home)
    return out


def write(path, data=b"hello"):
    path.write_bytes(data)
    return path


def state_path(home):
    return home / "doc-cache.json"


def test_check_reports_hash_size_type(home, work):
    data = b"# title\n\nbody\n" * 10
    f = write(work / "notes.md", data)
    out = check(f, home)
    assert out["abs_path"] == real(f)
    assert out["supported"] is True
    assert out["byte_length"] == len(data)
    assert out["content_type"] == "text/markdown"
    assert out["sha256"] == hashlib.sha256(data).hexdigest()
    assert out["decision"] == "ask"
    assert out["indexed_sha256"] is None


def test_content_types_match_contract(home, work):
    expected = {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".md": "text/markdown",
        ".markdown": "text/markdown",
        ".txt": "text/plain",
        ".PDF": "application/pdf",
    }
    for ext, ctype in expected.items():
        f = write(work / ("a" + ext))
        out = check(f, home)
        assert out["supported"] is True, ext
        assert out["content_type"] == ctype, ext


def test_unsupported_extension(home, work):
    f = write(work / "image.png", b"\x89PNG")
    out, code = run("check", f, home=home)
    assert code == 0
    assert out["supported"] is False
    assert out["content_type"] is None
    assert out["sha256"] is None
    assert out["byte_length"] == 4
    assert out["decision"] == "ask"


def test_small_flag(home, work):
    small = write(work / "small.txt", b"x" * 32767)
    edge = write(work / "edge.txt", b"x" * 32768)
    assert check(small, home)["small"] is True
    assert check(edge, home)["small"] is False


def test_missing_file_is_error(home, work):
    out, code = run("check", work / "nope.md", home=home, check_exit=False)
    assert code == 1
    assert "error" in out


def test_consent_root_is_git_toplevel(home, work):
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    sub = work / "docs" / "deep"
    sub.mkdir(parents=True)
    f = write(sub / "spec.md")
    assert check(f, home)["consent_root"] == real(work)


def test_consent_root_falls_back_to_parent(home, work):
    # No repo anywhere above tmp_path in practice; also force git to be absent.
    f = write(work / "plain.txt")
    env = dict(os.environ, SKARDI_HOME=str(home), PATH="")
    r = subprocess.run([sys.executable, SCRIPT, "check", str(f)],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["consent_root"] == real(work)


def test_consent_root_falls_back_when_not_a_repo(home, tmp_path):
    # GIT_CEILING_DIRECTORIES stops git walking up into any enclosing repo.
    d = tmp_path / "norepo"
    d.mkdir()
    f = write(d / "x.md")
    env = dict(os.environ, SKARDI_HOME=str(home), GIT_CEILING_DIRECTORIES=str(tmp_path))
    r = subprocess.run([sys.executable, SCRIPT, "check", str(f)],
                       capture_output=True, text=True, env=env)
    assert json.loads(r.stdout)["consent_root"] == real(d)


def test_consent_records_and_check_reads(home, work):
    f = write(work / "a.md")
    out, _ = run("consent", f, "always", home=home)
    assert out == {"consent_root": real(work), "decision": "always"}
    assert check(f, home)["decision"] == "always"
    run("consent", f, "never", home=home)
    assert check(f, home)["decision"] == "never"


def test_consent_rejects_bad_decision(home, work):
    f = write(work / "a.md")
    out, code = run("consent", f, "maybe", home=home, check_exit=False)
    assert code == 1
    assert "error" in out


def test_nearest_root_wins(home, tmp_path):
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    f = write(inner / "a.md")
    state = {"version": 1, "consent": {real(outer): "always", real(inner): "never"}, "index": {}}
    home.mkdir(parents=True)
    state_path(home).write_text(json.dumps(state))
    assert check(f, home)["decision"] == "never"
    g = write(outer / "b.md")
    assert check(g, home)["decision"] == "always"


def test_prefix_is_component_wise(home, tmp_path):
    ab = tmp_path / "a" / "b"
    abc = tmp_path / "a" / "bc"
    ab.mkdir(parents=True)
    abc.mkdir(parents=True)
    f = write(abc / "doc.md")
    home.mkdir(parents=True)
    state_path(home).write_text(json.dumps(
        {"version": 1, "consent": {real(ab): "always"}, "index": {}}))
    assert check(f, home)["decision"] == "ask"
    g = write(ab / "doc.md")
    assert check(g, home)["decision"] == "always"


def test_missing_state_is_ask(home, work):
    f = write(work / "a.md")
    assert not state_path(home).exists()
    out = check(f, home)
    assert out["decision"] == "ask"
    assert out["indexed_sha256"] is None
    assert not state_path(home).exists()


def test_corrupt_state_is_ask_and_not_overwritten_by_check(home, work):
    f = write(work / "a.md")
    home.mkdir(parents=True)
    state_path(home).write_text("{not json")
    out = check(f, home)
    assert out["decision"] == "ask"
    assert state_path(home).read_text() == "{not json"


def test_corrupt_state_is_replaced_by_consent(home, work):
    f = write(work / "a.md")
    home.mkdir(parents=True)
    state_path(home).write_text("[]")
    run("consent", f, "always", home=home)
    assert check(f, home)["decision"] == "always"


def test_concurrent_consent_writes_keep_both_roots(home, tmp_path):
    root_a = tmp_path / "root_a"
    root_b = tmp_path / "root_b"
    root_a.mkdir()
    root_b.mkdir()
    fa = write(root_a / "a.md")
    fb = write(root_b / "b.md")
    env = dict(os.environ, SKARDI_HOME=str(home))
    code = (
        "import subprocess, sys\n"
        "script, path, n = sys.argv[1], sys.argv[2], int(sys.argv[3])\n"
        "for i in range(n):\n"
        "    d = 'always' if i % 2 == 0 else 'never'\n"
        "    r = subprocess.run([sys.executable, script, 'consent', path, d],\n"
        "                       capture_output=True)\n"
        "    assert r.returncode == 0, r.stderr\n"
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", code, SCRIPT, str(p), "50"], env=env)
        for p in (fa, fb)
    ]
    assert [p.wait(timeout=120) for p in procs] == [0, 0]
    state = json.loads(state_path(home).read_text())
    assert real(root_a) in state["consent"]
    assert real(root_b) in state["consent"]


def test_concurrent_consent_tight_loop_loses_no_update(home, tmp_path):
    """Two in-process writers, 50 distinct roots each, back to back.

    The subprocess-per-write test above removes the contention (start-up
    dominates) and only checks each writer's *last* write, which a lost update
    in the middle never touches. Here every write adds a new root, so any
    lost update leaves a root missing at the end."""
    env = dict(os.environ, SKARDI_HOME=str(home), GIT_CEILING_DIRECTORIES=str(tmp_path))
    code = (
        "import json, os, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import doc_cache\n"
        "base, tag = sys.argv[2], sys.argv[3]\n"
        "roots = []\n"
        "for i in range(50):\n"
        "    d = os.path.join(base, tag + str(i))\n"
        "    os.makedirs(d)\n"
        "    f = os.path.join(d, 'x.md')\n"
        "    open(f, 'w').write('x')\n"
        "    roots.append(doc_cache.consent(f, 'always')['consent_root'])\n"
        "print(json.dumps(roots))\n"
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", code, SCRIPTS, str(tmp_path), tag],
                         env=env, stdout=subprocess.PIPE, text=True)
        for tag in ("a", "b")
    ]
    expected = set()
    for p in procs:
        out, _ = p.communicate(timeout=120)
        assert p.returncode == 0
        expected.update(json.loads(out))
    assert len(expected) == 100
    state = json.loads(state_path(home).read_text())
    assert expected <= set(state["consent"])


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_state_file_mode_0600(home, work):
    f = write(work / "a.md")
    run("consent", f, "always", home=home)
    assert stat.S_IMODE(os.stat(state_path(home)).st_mode) == 0o600
    run("record", f, "ab" * 32, home=home)
    assert stat.S_IMODE(os.stat(state_path(home)).st_mode) == 0o600


def test_record_then_check_reports_indexed_sha(home, work):
    f = write(work / "a.md")
    sha = hashlib.sha256(b"hello").hexdigest()
    out, _ = run("record", f, sha, home=home)
    assert out == {"abs_path": real(f), "sha256": sha}
    assert check(f, home)["indexed_sha256"] == sha
    state = json.loads(state_path(home).read_text())
    assert state["version"] == 1
    assert state["index"] == {real(f): sha}
    assert state["consent"] == {}


# --- upload ---------------------------------------------------------------


class _Server:
    def __init__(self, status, reply=b"{}"):
        outer = self
        self.received = None
        self.headers = None
        self.method = None

        class H(BaseHTTPRequestHandler):
            def do_PUT(self):
                n = int(self.headers.get("Content-Length", "0"))
                outer.received = self.rfile.read(n)
                outer.headers = dict(self.headers)
                outer.method = "PUT"
                self.send_response(status)
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d/documents/cache/upload/t0ken" % self.httpd.server_port
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server_factory():
    made = []

    def make(status, reply=b"{}"):
        s = _Server(status, reply)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


def test_upload_ok_201(home, work, server_factory):
    data = b"payload-bytes" * 1000
    f = write(work / "a.pdf", data)
    srv = server_factory(201, b'{"ok":true}')
    out, code = run("upload", f, srv.url, home=home)
    assert code == 0
    assert out == {"status": "ok", "code": 201, "body": '{"ok":true}'}
    assert srv.method == "PUT"
    assert srv.received == data
    assert srv.headers["Content-Length"] == str(len(data))


def test_upload_refused_410(home, work, server_factory):
    f = write(work / "a.md")
    srv = server_factory(410, b"ticket spent")
    out, code = run("upload", f, srv.url, home=home)
    assert code == 0
    assert out == {"status": "refused", "code": 410, "body": "ticket spent"}


def test_upload_connection_refused(home, work):
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    f = write(work / "a.md")
    out, code = run("upload", f, "http://127.0.0.1:%d/x" % port, home=home)
    assert code == 0
    assert out["status"] == "refused"
    assert out["code"] == 0
    assert isinstance(out["body"], str)


def test_upload_streams_file_object(monkeypatch, tmp_path):
    """The request body must be the open file, not bytes read into memory."""
    f = tmp_path / "a.txt"
    f.write_bytes(b"abc")
    seen = {}

    class FakeResp:
        status = 201

        def read(self):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen["data_type"] = type(req.data)
        seen["cl"] = req.get_header("Content-length")
        seen["method"] = req.get_method()
        return FakeResp()

    monkeypatch.setattr(doc_cache.urllib.request, "urlopen", fake_urlopen)
    out = doc_cache.upload(str(f), "http://example.invalid/x")
    assert out["status"] == "ok"
    assert not issubclass(seen["data_type"], (bytes, bytearray))
    assert seen["cl"] == "3"
    assert seen["method"] == "PUT"


# --- hardening (review of Task 5.1) ------------------------------------------


def test_upload_incomplete_read_is_refused_code_0(monkeypatch, tmp_path):
    """http.client.HTTPException is not an OSError; it must not escape."""
    import http.client
    f = tmp_path / "a.txt"
    f.write_bytes(b"abc")

    def boom(req, timeout=None):
        raise http.client.IncompleteRead(b"par", 10)

    monkeypatch.setattr(doc_cache.urllib.request, "urlopen", boom)
    out = doc_cache.upload(str(f), "http://example.invalid/x")
    assert out["status"] == "refused"
    assert out["code"] == 0
    assert isinstance(out["body"], str) and out["body"]


def test_upload_server_hangs_up_mid_reply_is_refused(home, work):
    """A real socket: Content-Length promises more than is sent, then close."""
    import socket
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        conn, _ = srv.accept()
        conn.recv(65536)
        conn.sendall(b"HTTP/1.1 201 Created\r\nContent-Length: 100\r\n\r\nshort")
        conn.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    f = write(work / "a.md")
    try:
        out, code = run("upload", f, "http://127.0.0.1:%d/x" % port, home=home)
    finally:
        srv.close()
    assert code == 0
    assert out["status"] == "refused"
    assert out["code"] == 0


def test_main_catch_all_keeps_one_json_object(monkeypatch, capsys):
    def boom(path):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(doc_cache, "check", boom)
    rc = doc_cache.main(["check", "whatever"])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out == {"error": "RuntimeError: unexpected"}


def test_upload_content_length_comes_from_the_open_handle(monkeypatch, tmp_path):
    """getsize before open can disagree with what the handle then streams."""
    f = tmp_path / "a.txt"
    f.write_bytes(b"abcdef")
    seen = {}

    class FakeResp:
        status = 201

        def read(self):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen["cl"] = req.get_header("Content-length")
        return FakeResp()

    monkeypatch.setattr(doc_cache.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(doc_cache.os.path, "getsize", lambda p: 999)
    doc_cache.upload(str(f), "http://example.invalid/x")
    assert seen["cl"] == "6"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_state_directory_mode_0700(home, work):
    f = write(work / "a.md")
    run("consent", f, "always", home=home)
    assert stat.S_IMODE(os.stat(home).st_mode) == 0o700


def test_skardi_home_is_user_expanded(tmp_path, work):
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    f = write(work / "a.md")
    env = dict(os.environ, HOME=str(fake_home), SKARDI_HOME="~/custom-skardi")
    r = subprocess.run(
        [sys.executable, SCRIPT, "consent", str(f), "always"],
        capture_output=True, text=True, env=env, cwd=str(tmp_path),
    )
    assert r.returncode == 0, r.stderr + r.stdout
    assert (fake_home / "custom-skardi" / "doc-cache.json").is_file()
    assert not (tmp_path / "~").exists()
