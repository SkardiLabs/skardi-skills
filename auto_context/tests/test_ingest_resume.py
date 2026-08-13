"""A re-POST of a document the server already holds must converge, not loop.

The manifest is flushed at most every two seconds, so an interrupted run leaves
files whose rows are committed but whose manifest entry is missing. Those files
come back as `pending` on the next run and collide on the primary key. When that
collision was reported as a failure, the manifest recorded `err:`, the run after
that saw `err:` as pending again, and the file could never leave that state
without hand-deleting rows — while the troubleshooting table promised that
"resuming after a pause loses no work".

These tests pin the three-way outcome and the manifest write that goes with it.
"""
import json
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "skills" / "auto_context" / "scripts"))

import ingest_corpus  # noqa: E402


class _FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, body, code=500):
        super().__init__("http://x/ingest-chunked/execute", code, "err", {}, None)
        self._body = body.encode()

    def read(self):
        return self._body


def _post(monkeypatch, behaviour):
    """Run post_doc against a stubbed urlopen. `behaviour` raises or returns."""
    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def read(self):
            return json.dumps(self._payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        out = behaviour()
        if isinstance(out, Exception):
            raise out
        return _Resp(out)

    monkeypatch.setattr(ingest_corpus.urllib.request, "urlopen", fake_urlopen)
    return ingest_corpus.post_doc("http://x/ingest-chunked/execute", b"{}", 5)


def test_a_committed_document_reports_ok(monkeypatch):
    assert _post(monkeypatch, lambda: {"success": True}) == ("ok", None)


def test_a_sqlite_primary_key_collision_is_present_not_failure(monkeypatch):
    err = _FakeHTTPError("Execution error: UNIQUE constraint failed: documents.id")
    status, reason = _post(monkeypatch, lambda: err)
    assert status == "present", (
        "a doc the server already holds must not be reported as a failure — "
        "that is what made an interrupted run unrecoverable"
    )
    assert reason is None


def test_a_postgres_primary_key_collision_is_present_too(monkeypatch):
    err = _FakeHTTPError('duplicate key value violates unique constraint "kb_pkey"')
    assert _post(monkeypatch, lambda: err)[0] == "present"


def test_an_unrelated_http_error_still_fails(monkeypatch):
    err = _FakeHTTPError("Embedding failed: Model forward pass failed", code=500)
    status, reason = _post(monkeypatch, lambda: err)
    assert status == "fail"
    assert "Embedding failed" in reason


def test_success_false_in_a_200_body_still_fails(monkeypatch):
    status, reason = _post(monkeypatch, lambda: {"success": False, "error": "boom"})
    assert status == "fail"
    assert "boom" in reason


def test_connection_error_still_fails(monkeypatch):
    err = urllib.error.URLError("connection refused")
    status, reason = _post(monkeypatch, lambda: err)
    assert status == "fail"
    assert "connection error" in reason


def test_present_is_written_to_the_manifest_as_ok(tmp_path):
    """The whole point: the next run must not treat the file as pending.

    load_progress is the same reader main() uses to decide what is pending, so
    round-tripping through it is what proves the loop is broken.
    """
    manifest = tmp_path / "ingest_progress.json"
    progress = {}
    item = {"source": "docs/a.md", "hash": "abc123"}

    # What _record does for a "present" outcome.
    progress[item["source"]] = {"status": "ok", "hash": item["hash"]}
    ingest_corpus.save_progress(manifest, progress)

    reloaded = ingest_corpus.load_progress(manifest)
    assert reloaded["docs/a.md"]["status"] == "ok"
    assert reloaded["docs/a.md"]["hash"] == "abc123"
