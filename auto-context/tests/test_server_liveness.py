#!/usr/bin/env python3
"""Liveness guards in start_server.py, exercised against real resources.

These cover the failure that start_server.py used to report as success: a
/health 200 that came from somebody else's server while ours never started.
The unit under test is not the whole launch path — it is the two guards that
decide "is this port already someone else's" and "is the thing we launched
still alive", plus wait_for_health's use of the second one.

Real sockets and a real Docker daemon on purpose: the previous round tested
container_gone() with stubbed return values, which cannot catch a wrong
`docker inspect` format string or a changed "No such object" message. Docker
tests skip themselves when no daemon is reachable.

Run: python3 tests/test_server_liveness.py
"""
import http.server
import os
import socket
import subprocess
import sys
import threading
import time
import uuid

SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "skills", "auto-context", "scripts")
sys.path.insert(0, SCRIPTS)
import start_server  # noqa: E402

port_is_taken = start_server.port_is_taken
container_gone = start_server.container_gone
wait_for_health = start_server.wait_for_health


# -- helpers ----------------------------------------------------------------

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Health(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"healthy"}')

    def log_message(self, *a):
        pass


class impostor:
    """An HTTP server answering /health 200 — stands in for 'someone else'."""

    def __init__(self, port):
        self.port = port

    def __enter__(self):
        self.httpd = http.server.HTTPServer(("127.0.0.1", self.port), _Health)
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.t.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def docker_available():
    try:
        return subprocess.run(["docker", "info"], capture_output=True,
                              timeout=10).returncode == 0
    except Exception:
        return False


DOCKER = docker_available()
SKIPPED = []


# -- port_is_taken ----------------------------------------------------------

def test_free_port_is_not_taken():
    assert port_is_taken("127.0.0.1", free_port()) is False


def test_a_real_listener_is_seen_as_taken():
    p = free_port()
    with impostor(p):
        assert port_is_taken("127.0.0.1", p) is True


def test_port_is_free_again_after_the_listener_stops():
    p = free_port()
    with impostor(p):
        pass
    # The connect-test must not report TIME_WAIT as occupied — that false
    # alarm is why this is a connect test and not a bind test.
    assert port_is_taken("127.0.0.1", p) is False


# -- wait_for_health: the regression this all exists for --------------------

def test_a_200_from_someone_else_is_not_our_server():
    """The original bug: ours died, theirs answered, we reported success."""
    p = free_port()
    with impostor(p):
        got = wait_for_health("127.0.0.1", p, timeout_s=5, kind="test",
                              is_alive=lambda: "pid 1 exited with code 1")
    assert got is False


def test_a_200_counts_when_our_process_is_alive():
    p = free_port()
    with impostor(p):
        got = wait_for_health("127.0.0.1", p, timeout_s=5, kind="test",
                              is_alive=lambda: None)
    assert got is True


def test_a_dead_launch_ends_the_wait_immediately():
    """No listener at all: a dead child must not sit out the full timeout."""
    p = free_port()
    started = time.time()
    got = wait_for_health("127.0.0.1", p, timeout_s=30, kind="test",
                          is_alive=lambda: "pid 1 exited with code 1")
    assert got is False
    assert time.time() - started < 5, "should not wait out the timeout"


def test_without_is_alive_the_old_permissive_behaviour_is_kept():
    """Callers that pass no liveness check still just believe the 200."""
    p = free_port()
    with impostor(p):
        assert wait_for_health("127.0.0.1", p, timeout_s=5, kind="test") is True


# -- container_gone: real docker --------------------------------------------

def test_running_container_reads_as_alive():
    if not DOCKER:
        SKIPPED.append("test_running_container_reads_as_alive"); return
    name = f"auto-context-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "run", "-d", "--name", name,
                    "alpine", "sleep", "30"], capture_output=True, check=True)
    try:
        assert container_gone(name) is None
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def test_exited_container_reports_its_exit_code():
    if not DOCKER:
        SKIPPED.append("test_exited_container_reports_its_exit_code"); return
    name = f"auto-context-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "run", "--name", name, "alpine", "sh", "-c",
                    "exit 3"], capture_output=True)
    try:
        msg = container_gone(name)
        assert msg is not None and "exited with code 3" in msg, msg
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def test_rm_container_that_removed_itself_is_reported_as_gone():
    """The shape start_docker actually produces: --rm plus a crash."""
    if not DOCKER:
        SKIPPED.append("test_rm_container_that_removed_itself_is_reported_as_gone"); return
    name = f"auto-context-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "run", "--rm", "--name", name, "alpine", "sh",
                    "-c", "exit 1"], capture_output=True)
    msg = container_gone(name)
    assert msg is not None and "no longer exists" in msg, msg


def test_unknown_container_name_is_reported_as_gone():
    if not DOCKER:
        SKIPPED.append("test_unknown_container_name_is_reported_as_gone"); return
    msg = container_gone(f"auto-context-never-created-{uuid.uuid4().hex[:8]}")
    assert msg is not None and "no longer exists" in msg, msg


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ok    {name}" if name not in SKIPPED else f"  skip  {name}")
        except Exception as e:
            failures += 1
            print(f"  FAIL  {name}: {type(e).__name__}: {e}")
    if SKIPPED:
        print(f"\n  ({len(SKIPPED)} docker test(s) skipped — no daemon)")
    print(f"\nall passed (0 failure(s))" if not failures
          else f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
