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
import os
import re
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "skills" / "auto-context" / "scripts"))

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


# ------------------------------------------------ cross-entry source identity

def _run_corpus(monkeypatch, argv, outcome=("ok", None)):
    """Drive ingest_corpus.py's main() the same way _run_main drives this one."""
    import ingest_corpus
    posted = []

    def fake_post(endpoint, body, timeout):
        posted.append(json.loads(body))
        return outcome

    monkeypatch.setattr(ingest_corpus, "post_doc", fake_post)
    monkeypatch.setattr(sys, "argv", ["ingest_corpus.py"] + argv)
    ingest_corpus.main()
    return posted


def test_table_run_refuses_a_source_that_means_another_document(
        tmp_path, monkeypatch, capsys):
    """Same source string, different content, different raw-material set —
    two genuinely different documents fighting over one doc id."""
    ws = _make_workspace(tmp_path)
    corpus = tmp_path / "docs"
    corpus.mkdir()
    (corpus / "guide.md").write_text("the file's own body")
    _run_corpus(monkeypatch, ["--workspace", str(ws), "--corpus", str(corpus)])

    db = _make_staging(tmp_path, [("guide.md", "guide.md", None, "a different body")])
    with pytest.raises(SystemExit) as e:
        _run_main(monkeypatch, [
            "--workspace", str(ws), "--db", str(db), "--table", "staged_documents",
            "--key-column", "key", "--content-column", "content",
            "--source-column", "source"])
    err = capsys.readouterr().err
    assert e.value.code != 0
    assert "guide.md" in err and "already indexed from" in err
    assert "Nothing was ingested" in err


def test_identical_content_from_another_set_is_not_a_collision(
        tmp_path, monkeypatch, capsys):
    """Byte-identical content under the same source is the SAME document
    reached another way — a moved corpus root, a re-export. Refusing it
    would block ordinary re-runs, which is the trap the label-based version
    fell into."""
    ws = _make_workspace(tmp_path)
    corpus = tmp_path / "docs"
    corpus.mkdir()
    (corpus / "guide.md").write_text("shared body")
    _run_corpus(monkeypatch, ["--workspace", str(ws), "--corpus", str(corpus)])

    db = _make_staging(tmp_path, [("guide.md", "guide.md", None, "shared body")])
    posted = _run_main(monkeypatch, [
        "--workspace", str(ws), "--db", str(db), "--table", "staged_documents",
        "--key-column", "key", "--content-column", "content",
        "--source-column", "source"])
    assert posted == []                        # already indexed, not refused
    assert "nothing to do" in capsys.readouterr().out


def test_folder_run_refuses_a_source_already_held_by_a_table_entry(
        tmp_path, monkeypatch, capsys):
    """Symmetric: the folder entry must refuse too, or the guard is one-way."""
    ws = _make_workspace(tmp_path)
    db = _make_staging(tmp_path, [("guide.md", "guide.md", None, "table body")])
    _run_main(monkeypatch, [
        "--workspace", str(ws), "--db", str(db), "--table", "staged_documents",
        "--key-column", "key", "--content-column", "content",
        "--source-column", "source"])

    corpus = tmp_path / "docs"
    corpus.mkdir()
    (corpus / "guide.md").write_text("file body")
    with pytest.raises(SystemExit) as e:
        _run_corpus(monkeypatch, ["--workspace", str(ws), "--corpus", str(corpus)])
    err = capsys.readouterr().err
    assert e.value.code != 0
    assert "already indexed from" in err and "staging.db::staged_documents" in err


def test_two_entries_coexist_when_sources_are_distinct(tmp_path, monkeypatch):
    """The guard must not punish the normal case — same workspace, two
    entries, different source strings."""
    ws = _make_workspace(tmp_path)
    corpus = tmp_path / "docs"
    corpus.mkdir()
    (corpus / "guide.md").write_text("file body")
    _run_corpus(monkeypatch, ["--workspace", str(ws), "--corpus", str(corpus)])
    db = _make_staging(tmp_path, [("k1", "https://wiki/p1", None, "table body")])
    posted = _run_main(monkeypatch, [
        "--workspace", str(ws), "--db", str(db), "--table", "staged_documents",
        "--key-column", "key", "--content-column", "content",
        "--source-column", "source"])
    assert [p["source"] for p in posted] == ["https://wiki/p1"]
    manifest = json.loads((ws / "ingest_progress.json").read_text())
    assert manifest["guide.md"]["set"] == str(corpus)
    assert manifest["https://wiki/p1"]["set"].endswith("staging.db::staged_documents")


def test_changing_the_label_is_not_a_collision_when_sources_are_a_column(
        tmp_path, monkeypatch, capsys):
    """The guard keys on the ENTRY, not on its parameters.

    Recording the table entry as `table:<label>` looked more precise and was
    wrong: with --source-column the label is not part of any source string,
    so re-running the same table under a different label refused every row —
    and the refusal advised using --source-column, which the run was already
    doing. A parameter change must not read as a document change."""
    ws = _make_workspace(tmp_path)
    db = _make_staging(tmp_path, [("k1", "https://wiki/p1", None, "body one")])
    argv = ["--workspace", str(ws), "--db", str(db), "--table", "staged_documents",
            "--key-column", "key", "--content-column", "content",
            "--source-column", "source"]
    assert len(_run_main(monkeypatch, argv + ["--label", "A"])) == 1
    posted = _run_main(monkeypatch, argv + ["--label", "B"])
    assert posted == []                       # already ingested, not refused
    assert "nothing to do" in capsys.readouterr().out


def test_legacy_entry_blocks_a_table_run_but_not_a_corpus_run(
        tmp_path, monkeypatch, capsys):
    """A manifest predating set ids records neither a set nor a hash, so the
    two documents cannot be proven identical. A table run must stop and ask;
    a corpus run must NOT, or every upgraded workspace breaks on its next
    ordinary run."""
    ws = _make_workspace(tmp_path)
    (ws / "ingest_progress.json").write_text(json.dumps({"guide.md": "ok"}))
    db = _make_staging(tmp_path, [("guide.md", "guide.md", None, "body")])
    with pytest.raises(SystemExit):
        _run_main(monkeypatch, [
            "--workspace", str(ws), "--db", str(db), "--table", "staged_documents",
            "--key-column", "key", "--content-column", "content",
            "--source-column", "source"])
    assert "already indexed from" in capsys.readouterr().err

    # same legacy manifest, ordinary corpus run: proceeds, and upgrades the entry
    corpus = tmp_path / "docs"
    corpus.mkdir()
    (corpus / "guide.md").write_text("body")
    _run_corpus(monkeypatch, ["--workspace", str(ws), "--corpus", str(corpus)])
    manifest = json.loads((ws / "ingest_progress.json").read_text())
    assert manifest["guide.md"]["set"] == str(corpus)
    assert manifest["guide.md"]["hash"] is not None


# ---------------------------------------------------------- --require-complete

def test_require_complete_refuses_a_partially_fetched_table(
        tmp_path, monkeypatch, capsys):
    ws = _make_workspace(tmp_path)
    db = _make_staging(tmp_path, [
        ("k1", "https://wiki/p1", "one", "body one"),
        ("k2", "https://wiki/p2", "two", None),      # listed, never fetched
    ])
    with pytest.raises(SystemExit) as e:
        _run_main(monkeypatch, [
            "--workspace", str(ws), "--db", str(db), "--table", "staged_documents",
            "--key-column", "key", "--content-column", "content",
            "--source-column", "source", "--require-complete"])
    err = capsys.readouterr().err
    assert e.value.code != 0
    assert "no text content: 1" in err
    assert "listed but never fetched" in err
    # and nothing reached the index or the manifest
    assert not (ws / "ingest_progress.json").exists()


def test_require_complete_passes_a_whole_table(tmp_path, monkeypatch):
    ws = _make_workspace(tmp_path)
    db = _make_staging(tmp_path, [
        ("k1", "https://wiki/p1", "one", "body one"),
        ("k2", "https://wiki/p2", "two", "body two"),
    ])
    posted = _run_main(monkeypatch, [
        "--workspace", str(ws), "--db", str(db), "--table", "staged_documents",
        "--key-column", "key", "--content-column", "content",
        "--source-column", "source", "--require-complete"])
    assert len(posted) == 2


def test_require_complete_and_limit_are_mutually_exclusive(
        tmp_path, monkeypatch, capsys):
    ws = _make_workspace(tmp_path)
    db = _make_staging(tmp_path, [("k1", None, None, "body")])
    with pytest.raises(SystemExit):
        _run_main(monkeypatch, [
            "--workspace", str(ws), "--db", str(db), "--table", "staged_documents",
            "--key-column", "key", "--content-column", "content",
            "--require-complete", "--limit", "1"])
    assert "contradict each other" in capsys.readouterr().err


# ------------------------------------------------------------------- --limit

def test_limit_keeps_the_accounting_balanced(tmp_path, monkeypatch, capsys):
    """`rows` must still equal ingestable + skipped + limited. Truncating the
    work list silently printed `rows: 3  ingestable: 1  skipped: 0` and exited
    0 — indistinguishable from a complete run over a small table."""
    ws = _make_workspace(tmp_path)
    db = _make_staging(tmp_path, [
        ("k1", None, None, "one"), ("k2", None, None, "two"), ("k3", None, None, "three")])
    posted = _run_main(monkeypatch, [
        "--workspace", str(ws), "--db", str(db), "--table", "staged_documents",
        "--key-column", "key", "--content-column", "content", "--limit", "1"])
    out = capsys.readouterr().out
    assert len(posted) == 1
    assert "rows: 3  ingestable: 1  skipped: 0  limited (--limit, NOT ingested): 2" in out
    assert "INCOMPLETE" in out


def test_corpus_limit_keeps_the_accounting_balanced(tmp_path, monkeypatch, capsys):
    """Same defect, same contract, on the folder entry."""
    ws = _make_workspace(tmp_path)
    corpus = tmp_path / "docs"
    corpus.mkdir()
    for name in ("a.md", "b.md", "c.md"):
        (corpus / name).write_text(f"body of {name}")
    posted = _run_corpus(monkeypatch, [
        "--workspace", str(ws), "--corpus", str(corpus), "--limit", "1"])
    out = capsys.readouterr().out
    assert len(posted) == 1
    assert "matched: 3  ingestable: 1  skipped: 0  limited (--limit, NOT ingested): 2" in out
    assert "INCOMPLETE" in out


# --------------------------------------------- read-only under hostile names

@pytest.mark.parametrize("name", [
    "raw?table.db",     # `?` used to end the path and drop mode=ro entirely
    "raw#frag.db",      # `#` is the same family
    "with space.db",
    "amp&and.db",
    "plain.db",
])
def test_source_db_is_readonly_whatever_the_filename(name, tmp_path, monkeypatch):
    """`mode=ro` has to survive the filename.

    The URI used to be an f-string, so a file called `raw?table.db` produced
    `file:/dir/raw?table.db?mode=ro`: SQLite read the path as `/dir/raw`,
    never parsed mode=ro, CREATED that new database and accepted writes to
    it. The promise "a table handed over as raw material is never modified"
    was silently false for any such name."""
    ws = _make_workspace(tmp_path)
    db = tmp_path / name
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE docs (k TEXT PRIMARY KEY, body TEXT)")
    conn.execute("INSERT INTO docs VALUES ('k1','body one')")
    conn.commit()
    conn.close()

    before_hash = hashlib.sha256(db.read_bytes()).hexdigest()
    before_files = set(p.name for p in tmp_path.iterdir())

    posted = _run_main(monkeypatch, [
        "--workspace", str(ws), "--db", str(db), "--table", "docs",
        "--key-column", "k", "--content-column", "body"])

    assert len(posted) == 1                                   # still readable
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before_hash
    # no stray database, no -wal / -journal, nothing new beside it
    assert set(p.name for p in tmp_path.iterdir()) == before_files


def test_readonly_connection_actually_refuses_writes(tmp_path):
    """Directly pin the guarantee, independent of the ingest path."""
    db = tmp_path / "raw?table.db"
    sqlite3.connect(db).close()
    conn = ingest_table.open_sqlite_readonly(db)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("CREATE TABLE nope (x INT)")
    conn.close()


# ------------------------------------------------------- --accept-missing

def _partial_staging(tmp_path):
    return _make_staging(tmp_path, [
        ("k1", "https://wiki/p1", "one", "body one"),
        ("k2", "https://wiki/p2", "two", None),      # listed, never fetched
    ])


def _gate_argv(ws, db):
    return ["--workspace", str(ws), "--db", str(db), "--table", "staged_documents",
            "--key-column", "key", "--content-column", "content",
            "--source-column", "source", "--require-complete"]


def _token_from_refusal(err):
    m = re.search(r"--accept-missing (\d+:[0-9a-f]{6})", err)
    assert m, f"no override token in refusal:\n{err}"
    return m.group(1)


def test_the_gate_names_the_override_instead_of_dead_ending(
        tmp_path, monkeypatch, capsys):
    """Stage C allows an explicitly accepted shortfall, so the gate must
    offer a way through — otherwise the documented process contradicts the
    tool and the only way past is to drop the flag, which is exactly the
    silent partial ingest the gate exists to stop."""
    ws = _make_workspace(tmp_path)
    with pytest.raises(SystemExit):
        _run_main(monkeypatch, _gate_argv(ws, _partial_staging(tmp_path)))
    assert _token_from_refusal(capsys.readouterr().err).startswith("1:")


def test_accept_missing_lets_an_explicit_shortfall_through(
        tmp_path, monkeypatch, capsys):
    ws = _make_workspace(tmp_path)
    db = _partial_staging(tmp_path)
    with pytest.raises(SystemExit):
        _run_main(monkeypatch, _gate_argv(ws, db))
    token = _token_from_refusal(capsys.readouterr().err)

    posted = _run_main(monkeypatch, _gate_argv(ws, db)
                       + ["--accept-missing", token])
    out = capsys.readouterr().out
    assert len(posted) == 1
    assert "ACCEPTED SHORTFALL" in out
    assert "RESULT complete=false reason=accepted-shortfall" in out


def test_an_accepted_shortfall_still_reports_incomplete_on_a_rerun(
        tmp_path, monkeypatch, capsys):
    """The verdict must describe the CORPUS, not this invocation.

    After the shortfall was accepted once, a re-run has nothing left to
    ingest — and reporting that as complete flipped the machine-readable
    verdict to true while the corpus was still missing a document, which is
    precisely what a pipeline would act on."""
    ws = _make_workspace(tmp_path)
    db = _partial_staging(tmp_path)
    with pytest.raises(SystemExit):
        _run_main(monkeypatch, _gate_argv(ws, db))
    token = _token_from_refusal(capsys.readouterr().err)
    _run_main(monkeypatch, _gate_argv(ws, db) + ["--accept-missing", token])
    capsys.readouterr()

    _run_main(monkeypatch, _gate_argv(ws, db) + ["--accept-missing", token])
    out = capsys.readouterr().out
    assert "nothing to do" in out
    assert "RESULT complete=false" in out
    assert "RESULT complete=true" not in out


def test_the_override_is_bound_to_the_set_not_the_count(
        tmp_path, monkeypatch, capsys):
    """Swap which document is missing, keep the count — the override written
    for the first set must not wave the second one through."""
    ws = _make_workspace(tmp_path)
    db = _make_staging(tmp_path, [
        ("k1", "https://wiki/p1", "one", "body one"),
        ("k2", "https://wiki/p2", "two", None)])
    with pytest.raises(SystemExit):
        _run_main(monkeypatch, _gate_argv(ws, db))
    token = _token_from_refusal(capsys.readouterr().err)

    conn = sqlite3.connect(db)                 # now p1 is the missing one
    conn.execute("UPDATE staged_documents SET content = NULL WHERE key = 'k1'")
    conn.execute("UPDATE staged_documents SET content = 'body two' WHERE key = 'k2'")
    conn.commit()
    conn.close()
    with pytest.raises(SystemExit) as e:
        _run_main(monkeypatch, _gate_argv(ws, db) + ["--accept-missing", token])
    err = capsys.readouterr().err
    assert e.value.code != 0
    assert "does not match the shortfall this run found" in err


def test_only_unfetched_rows_can_be_waived(tmp_path, monkeypatch, capsys):
    """A malformed export is a defect the operator can fix, not a document
    the source cannot give you — so it must never pass under the flag whose
    documented purpose is a source-side shortfall."""
    ws = _make_workspace(tmp_path)
    nd = tmp_path / "rows.ndjson"
    nd.write_text('{"key": "a", "content": "good"}\nbroken line\n')
    with pytest.raises(SystemExit) as e:
        _run_main(monkeypatch, [
            "--workspace", str(ws), "--ndjson", str(nd), "--label", "nd",
            "--require-complete", "--accept-missing", "1:000000"])
    err = capsys.readouterr().err
    assert e.value.code != 0
    assert "not a source-side shortfall" in err
    assert "not valid JSON" in err


def test_accept_missing_must_match_the_actual_shortfall(
        tmp_path, monkeypatch, capsys):
    """The token is exact so the override cannot be written once and left in
    place: a shortfall that later changes stops the run again."""
    ws = _make_workspace(tmp_path)
    with pytest.raises(SystemExit) as e:
        _run_main(monkeypatch,
                  _gate_argv(ws, _partial_staging(tmp_path))
                  + ["--accept-missing", "5:abcdef"])
    err = capsys.readouterr().err
    assert e.value.code != 0
    assert "does not match the shortfall this run found" in err


def test_a_malformed_override_token_is_refused(tmp_path, monkeypatch, capsys):
    ws = _make_workspace(tmp_path)
    with pytest.raises(SystemExit):
        _run_main(monkeypatch,
                  _gate_argv(ws, _partial_staging(tmp_path))
                  + ["--accept-missing", "1"])
    assert "`<count>:<digest>` token" in capsys.readouterr().err


def test_accept_missing_needs_the_gate(tmp_path, monkeypatch, capsys):
    ws = _make_workspace(tmp_path)
    db = _partial_staging(tmp_path)
    with pytest.raises(SystemExit):
        _run_main(monkeypatch, [
            "--workspace", str(ws), "--db", str(db), "--table", "staged_documents",
            "--key-column", "key", "--content-column", "content",
            "--accept-missing", "1"])
    assert "only means something with --require-complete" in capsys.readouterr().err


# ------------------------------------------------- machine-readable verdict

def test_result_line_marks_a_complete_run(tmp_path, monkeypatch, capsys):
    ws = _make_workspace(tmp_path)
    db = _make_staging(tmp_path, [("k1", None, None, "body")])
    _run_main(monkeypatch, [
        "--workspace", str(ws), "--db", str(db), "--table", "staged_documents",
        "--key-column", "key", "--content-column", "content"])
    assert "RESULT complete=true" in capsys.readouterr().out


def test_result_line_marks_a_limited_run(tmp_path, monkeypatch, capsys):
    """A pipeline reading only the exit code cannot tell a trial run from a
    finished one, because a deliberate trial exits 0. The verdict line is
    what makes it machine-checkable."""
    ws = _make_workspace(tmp_path)
    db = _make_staging(tmp_path, [
        ("k1", None, None, "one"), ("k2", None, None, "two")])
    _run_main(monkeypatch, [
        "--workspace", str(ws), "--db", str(db), "--table", "staged_documents",
        "--key-column", "key", "--content-column", "content", "--limit", "1"])
    assert "RESULT complete=false reason=limit" in capsys.readouterr().out


# ------------------------------------------------------- interrupt window

def test_a_pending_source_is_recorded_before_its_post(tmp_path, monkeypatch):
    """An interrupt used to leave rows committed server-side that the
    manifest had never heard of — and with no entry, a later run from a
    different set had nothing to compare against, so the collision check
    could not fire and the newcomer was filed as `already ok`."""
    ws = _make_workspace(tmp_path)
    db = _make_staging(tmp_path, [("k1", "https://wiki/p1", None, "body one")])
    seen = {}

    def fake_post(endpoint, body, timeout):
        # what the manifest holds at the moment the POST goes out
        seen["manifest"] = json.loads((ws / "ingest_progress.json").read_text())
        return ("ok", None)

    monkeypatch.setattr(ingest_table, "post_doc", fake_post)
    monkeypatch.setattr(sys, "argv", ["ingest_table.py"] + [
        "--workspace", str(ws), "--db", str(db), "--table", "staged_documents",
        "--key-column", "key", "--content-column", "content",
        "--source-column", "source"])
    ingest_table.main()

    entry = seen["manifest"]["https://wiki/p1"]
    assert entry["status"] == "inflight"
    assert entry["set"].endswith("staging.db::staged_documents")


# ------------------------------------------------------ concurrency & resume

def test_a_second_run_on_one_workspace_is_refused(tmp_path, monkeypatch, capsys):
    """Read manifest → check collisions → write inflight is three steps, so
    two concurrent runs could both pass the check and then overwrite each
    other's state, landing silently on `already-present`."""
    ws = _make_workspace(tmp_path)
    db = _make_staging(tmp_path, [("k1", None, None, "body")])
    (ws / "ingest.lock").write_text(str(os.getpid()))     # a live holder
    with pytest.raises(SystemExit) as e:
        _run_main(monkeypatch, [
            "--workspace", str(ws), "--db", str(db), "--table", "staged_documents",
            "--key-column", "key", "--content-column", "content"])
    err = capsys.readouterr().err
    assert e.value.code != 0
    assert "another ingest is already running" in err


def test_a_stale_lock_is_taken_over(tmp_path, monkeypatch, capsys):
    """A crash must not need manual cleanup."""
    ws = _make_workspace(tmp_path)
    db = _make_staging(tmp_path, [("k1", None, None, "body")])
    (ws / "ingest.lock").write_text("999999")             # nobody
    posted = _run_main(monkeypatch, [
        "--workspace", str(ws), "--db", str(db), "--table", "staged_documents",
        "--key-column", "key", "--content-column", "content"])
    assert len(posted) == 1
    assert "stale lock" in capsys.readouterr().out


def test_the_lock_is_released_after_a_run(tmp_path, monkeypatch):
    ws = _make_workspace(tmp_path)
    db = _make_staging(tmp_path, [("k1", None, None, "body")])
    argv = ["--workspace", str(ws), "--db", str(db), "--table", "staged_documents",
            "--key-column", "key", "--content-column", "content"]
    _run_main(monkeypatch, argv)
    assert not (ws / "ingest.lock").exists()
    _run_main(monkeypatch, argv)          # and a second run still works


def test_an_interrupted_document_that_changed_since_is_refused(
        tmp_path, monkeypatch, capsys):
    """`inflight` means the POST went out and the answer was never recorded,
    so the server may already hold that document. If the content changed
    since, resuming re-POSTs the new text, the server rejects it as a
    duplicate, and the collision is filed as `already-present` — stamping
    the NEW hash onto rows holding the OLD content. The manifest then claims
    a version the index does not have, and nothing says otherwise."""
    ws = _make_workspace(tmp_path)
    db = _make_staging(tmp_path, [("k1", "https://wiki/p1", None, "new body")])
    (ws / "ingest_progress.json").write_text(json.dumps({
        "https://wiki/p1": {"status": "inflight",
                            "hash": hashlib.sha256(b"old body").hexdigest(),
                            "set": f"{db}::staged_documents"}}))
    with pytest.raises(SystemExit) as e:
        _run_main(monkeypatch, [
            "--workspace", str(ws), "--db", str(db), "--table", "staged_documents",
            "--key-column", "key", "--content-column", "content",
            "--source-column", "source"])
    captured = capsys.readouterr()
    assert e.value.code != 0
    assert "interrupted mid-ingest and have changed since" in captured.err
    assert "RESULT complete=false reason=stale-inflight" in captured.out


def test_every_exit_path_prints_a_verdict(tmp_path, monkeypatch, capsys):
    """A consumer reading the verdict must never have to interpret its
    absence — refusals emit it too, with the reason."""
    ws = _make_workspace(tmp_path)
    db = _make_staging(tmp_path, [])          # zero rows: a die() path
    with pytest.raises(SystemExit):
        _run_main(monkeypatch, [
            "--workspace", str(ws), "--db", str(db), "--table", "staged_documents",
            "--key-column", "key", "--content-column", "content"])
    assert "RESULT complete=false" in capsys.readouterr().out


def test_ordinary_skips_make_the_verdict_incomplete(tmp_path, monkeypatch, capsys):
    """Without --require-complete a skip is tolerated, but the corpus still
    does not hold what the source holds, so the verdict must say so."""
    ws = _make_workspace(tmp_path)
    db = _make_staging(tmp_path, [
        ("k1", None, None, "body one"), ("k2", None, None, None)])
    _run_main(monkeypatch, [
        "--workspace", str(ws), "--db", str(db), "--table", "staged_documents",
        "--key-column", "key", "--content-column", "content"])
    assert "RESULT complete=false reason=skipped" in capsys.readouterr().out
