"""The table entry must account for every row and never touch the source.

ingest_table.py exists so that raw material can be rows in a table instead
of files in a folder. Two promises carry the whole design and both are easy
to break silently, so they are pinned here:

  1. Accounting adds up. Every row that comes in lands in exactly one
     bucket (ingested, or a named skip reason), because the fetch-and-land
     process leans on these counts as its last tripwire — a row that
     vanishes without a bucket is a document nobody knows is missing.
  2. The source is read-only. SKILL.md tells users a table they hand over
     is never modified; that has to stay literally true, journal files
     included.

Run: uvx pytest tests/test_ingest_table.py
"""
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "skills" / "auto_context" / "scripts"))

import ingest_table  # noqa: E402
from ingest_corpus import stable_doc_id  # noqa: E402


def _skipbuckets():
    return {reason: [] for reason in ingest_table.SKIP_REASONS}


def _work_sources(work):
    return [w["source"] for w in work]


# ---------------------------------------------------------------- build_work

def test_every_row_lands_in_exactly_one_bucket():
    rows = [
        (None, "text", None),                  # null key
        ("a", "  good text  ", None),          # ok
        ("b", None, None),                     # no text content
        ("c", "   ", None),                    # no text content (whitespace)
        ("d", 42, None),                       # non-text content
        ("e", "dup", "same://loc"),            # ok
        ("f", "dup2", "same://loc"),           # duplicate source
        ("g", "café".encode(), None),     # ok (bytes, valid UTF-8)
        ("h", b"\xff\xfe\x00bad", None),       # not UTF-8
        ("i", "x" * (ingest_table.SERVER_BODY_LIMIT + 1), None),  # too large
    ]
    skipped = _skipbuckets()
    work, consumed = ingest_table.build_work(rows, "t", 1200, 200, skipped)
    assert consumed == len(rows)
    assert len(work) + sum(len(v) for v in skipped.values()) == len(rows)
    assert len(skipped["null key"]) == 1
    assert skipped["no text content"] == ["t#b", "t#c"]
    assert skipped["non-text content"] == ["t#d (int)"]
    assert skipped["duplicate source"] == ["same://loc"]
    assert skipped["not UTF-8"] == ["t#h"]
    assert len(skipped["too large for one request"]) == 1
    assert _work_sources(work) == ["t#a", "same://loc", "t#g"]


def test_identity_is_the_source_string():
    """doc_id must be derived from the source exactly like the folder form
    derives it from the relative path — that is what makes the shared
    manifest and the shared index coherent across both entries."""
    skipped = _skipbuckets()
    work, _ = ingest_table.build_work(
        [("42", "hello", None), ("7", "world", "https://x/7")],
        "docs", 1200, 200, skipped)
    for w in work:
        body = json.loads(w["body"])
        assert body["doc_id"] == stable_doc_id(w["source"])
    assert _work_sources(work) == ["docs#42", "https://x/7"]


def test_content_is_ingested_as_is_no_front_matter_stripping():
    """Rows are not files: cleanup belongs to the landing step, so a body
    that happens to start with a front-matter fence must survive intact."""
    text = "---\ntitle: kept\n---\nbody"
    skipped = _skipbuckets()
    work, _ = ingest_table.build_work([("k", text, None)], "t", 1200, 200, skipped)
    assert json.loads(work[0]["body"])["content"] == text


# ------------------------------------------------------------------- ndjson

def test_ndjson_malformed_lines_are_counted_not_fatal():
    lines = [
        '{"key": "a", "content": "text a"}',
        "not json at all",
        '{"key": "b"}',
        "",
        '{"key": "c", "content": "text c", "source": "https://x/c"}',
    ]
    skipped = _skipbuckets()
    rows = list(ingest_table.iter_ndjson_rows(iter(lines), skipped))
    assert [r[0] for r in rows] == ["a", "c"]
    assert skipped["not valid JSON"] == ["line 2"]
    assert skipped["missing key or content field"] == ["line 3"]
    # the blank line is a pipeline artifact, not a record — no bucket


# ------------------------------------------------- main(), sqlite end to end

def _make_workspace(tmp_path):
    ws = tmp_path / "context"
    ws.mkdir()
    (ws / "ctx.yaml").write_text("kind: context\n")
    (ws / "server.port").write_text("8099")
    return ws


def _make_staging(tmp_path, rows):
    db = tmp_path / "staging.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE staged_documents "
                 "(key TEXT PRIMARY KEY, source TEXT, title TEXT, content TEXT)")
    conn.executemany("INSERT INTO staged_documents VALUES (?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return db


def _run_main(monkeypatch, argv, outcome=("ok", None)):
    posted = []

    def fake_post(endpoint, body, timeout):
        posted.append(json.loads(body))
        return outcome

    monkeypatch.setattr(ingest_table, "post_doc", fake_post)
    monkeypatch.setattr(sys, "argv", ["ingest_table.py"] + argv)
    ingest_table.main()
    return posted


def test_sqlite_run_ingests_and_accounts(tmp_path, monkeypatch, capsys):
    ws = _make_workspace(tmp_path)
    db = _make_staging(tmp_path, [
        ("k1", "https://wiki/p1", "one", "body one"),
        ("k2", "https://wiki/p2", "two", "body two"),
        ("k3", "https://wiki/p3", "three", None),   # listed, never fetched
    ])
    posted = _run_main(monkeypatch, [
        "--workspace", str(ws), "--db", str(db), "--table", "staged_documents",
        "--key-column", "key", "--content-column", "content",
        "--source-column", "source",
    ])
    out = capsys.readouterr().out
    assert [p["source"] for p in posted] == ["https://wiki/p1", "https://wiki/p2"]
    assert "rows: 3  ingestable: 2  skipped: 1" in out
    # the tripwire: an empty row on a fetched table is a missing document
    assert "listed but never landed" in out
    manifest = json.loads((ws / "ingest_progress.json").read_text())
    assert manifest["https://wiki/p1"]["status"] == "ok"


def test_second_run_is_a_no_op(tmp_path, monkeypatch, capsys):
    ws = _make_workspace(tmp_path)
    db = _make_staging(tmp_path, [("k1", None, None, "body one")])
    argv = ["--workspace", str(ws), "--db", str(db), "--table", "staged_documents",
            "--key-column", "key", "--content-column", "content"]
    assert len(_run_main(monkeypatch, argv)) == 1
    posted = _run_main(monkeypatch, argv)
    assert posted == []
    assert "nothing to do" in capsys.readouterr().out


def test_changed_rows_are_surfaced_not_reingested(tmp_path, monkeypatch, capsys):
    ws = _make_workspace(tmp_path)
    db = _make_staging(tmp_path, [("k1", None, None, "original")])
    argv = ["--workspace", str(ws), "--db", str(db), "--table", "staged_documents",
            "--key-column", "key", "--content-column", "content"]
    _run_main(monkeypatch, argv)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE staged_documents SET content = 'edited' WHERE key = 'k1'")
    conn.commit()
    conn.close()
    posted = _run_main(monkeypatch, argv)
    out = capsys.readouterr().out
    assert posted == []       # stable ids would collide; user deletes rows first
    assert "changed since they were ingested" in out


def test_source_db_is_untouched(tmp_path, monkeypatch):
    """The read-only promise, checked at the byte level: same file hash
    before and after, and no -wal / -journal siblings appear."""
    ws = _make_workspace(tmp_path)
    db = _make_staging(tmp_path, [("k1", None, None, "body one")])
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    _run_main(monkeypatch, [
        "--workspace", str(ws), "--db", str(db), "--table", "staged_documents",
        "--key-column", "key", "--content-column", "content"])
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert list(tmp_path.glob("staging.db-*")) == []


def test_refuses_the_workspaces_own_kb_db(tmp_path, monkeypatch, capsys):
    ws = _make_workspace(tmp_path)
    conn = sqlite3.connect(ws / "kb.db")
    conn.execute("CREATE TABLE documents (id, source, chunk_idx, content, embedding)")
    conn.close()
    with pytest.raises(SystemExit):
        _run_main(monkeypatch, [
            "--workspace", str(ws), "--db", str(ws / "kb.db"), "--table", "documents",
            "--key-column", "id", "--content-column", "content"])
    assert "kb.db" in capsys.readouterr().err


def test_zero_rows_is_a_failure_not_a_success(tmp_path, monkeypatch, capsys):
    """An empty table means the listing stage never ran — exiting 0 would
    report success for a corpus that produced no context at all."""
    ws = _make_workspace(tmp_path)
    db = _make_staging(tmp_path, [])
    with pytest.raises(SystemExit) as e:
        _run_main(monkeypatch, [
            "--workspace", str(ws), "--db", str(db), "--table", "staged_documents",
            "--key-column", "key", "--content-column", "content"])
    assert e.value.code != 0
    assert "listing stage never ran" in capsys.readouterr().err


def test_all_rows_skipped_is_a_failure(tmp_path, monkeypatch, capsys):
    ws = _make_workspace(tmp_path)
    db = _make_staging(tmp_path, [("k1", None, None, None), ("k2", None, None, "  ")])
    with pytest.raises(SystemExit) as e:
        _run_main(monkeypatch, [
            "--workspace", str(ws), "--db", str(db), "--table", "staged_documents",
            "--key-column", "key", "--content-column", "content"])
    assert e.value.code != 0
    assert "Nothing was ingested" in capsys.readouterr().err


def test_wrong_column_dies_naming_what_exists(tmp_path, monkeypatch, capsys):
    ws = _make_workspace(tmp_path)
    db = _make_staging(tmp_path, [("k1", None, None, "text")])
    with pytest.raises(SystemExit):
        _run_main(monkeypatch, [
            "--workspace", str(ws), "--db", str(db), "--table", "staged_documents",
            "--key-column", "key", "--content-column", "body"])
    err = capsys.readouterr().err
    assert "'body'" in err and "content" in err


def test_failed_posts_exit_nonzero_and_are_recorded(tmp_path, monkeypatch, capsys):
    ws = _make_workspace(tmp_path)
    db = _make_staging(tmp_path, [("k1", None, None, "body one")])
    with pytest.raises(SystemExit) as e:
        _run_main(monkeypatch, [
            "--workspace", str(ws), "--db", str(db), "--table", "staged_documents",
            "--key-column", "key", "--content-column", "content"],
            outcome=("fail", "HTTP 500: boom"))
    assert e.value.code == 1
    manifest = json.loads((ws / "ingest_progress.json").read_text())
    assert manifest["staged_documents#k1"]["status"].startswith("err:")


# ------------------------------------------------- main(), ndjson end to end

def test_ndjson_file_run_accounts_for_parse_failures(tmp_path, monkeypatch, capsys):
    ws = _make_workspace(tmp_path)
    nd = tmp_path / "rows.ndjson"
    nd.write_text('{"key": "a", "content": "text a"}\n'
                  'broken\n'
                  '{"key": "b", "content": "text b", "source": "https://x/b"}\n')
    posted = _run_main(monkeypatch, [
        "--workspace", str(ws), "--ndjson", str(nd), "--label", "kb_docs"])
    out = capsys.readouterr().out
    assert [p["source"] for p in posted] == ["kb_docs#a", "https://x/b"]
    assert "rows: 3  ingestable: 2  skipped: 1" in out
    assert "not valid JSON" in out


def test_ndjson_requires_a_label(tmp_path, monkeypatch, capsys):
    ws = _make_workspace(tmp_path)
    nd = tmp_path / "rows.ndjson"
    nd.write_text('{"key": "a", "content": "x"}\n')
    with pytest.raises(SystemExit):
        _run_main(monkeypatch, ["--workspace", str(ws), "--ndjson", str(nd)])
    assert "--label is required" in capsys.readouterr().err


def test_ndjson_rejects_db_only_flags(tmp_path, monkeypatch, capsys):
    ws = _make_workspace(tmp_path)
    nd = tmp_path / "rows.ndjson"
    nd.write_text('{"key": "a", "content": "x"}\n')
    with pytest.raises(SystemExit):
        _run_main(monkeypatch, [
            "--workspace", str(ws), "--ndjson", str(nd), "--label", "l",
            "--table", "t"])
    assert "only applies to --db mode" in capsys.readouterr().err
