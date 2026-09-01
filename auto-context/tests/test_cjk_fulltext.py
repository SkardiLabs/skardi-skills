"""Full-text search must find CJK terms that are plainly in the corpus.

FTS5's default tokenizer (unicode61) splits on whitespace and punctuation,
neither of which occurs inside a run of Chinese, Japanese or Korean
characters. A CJK corpus therefore indexes as a handful of giant tokens and
MATCH returns nothing — while reporting success, which is what made this
survive: the documented default path is hybrid search, whose vector half
still returns sensible rows, so the full-text half can be dead without
anyone noticing (skardi-skills#26).

These tests pin the two halves of the fix: the index is built with
tokenize='trigram', and queries below the 3-character trigram window fall
back to a substring scan instead of silently returning nothing.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS = (Path(__file__).resolve().parent.parent
           / "skills" / "auto-context" / "scripts")
sys.path.insert(0, str(SCRIPTS))

import ingest_corpus  # noqa: E402
import setup_context  # noqa: E402

ASSETS = (Path(__file__).resolve().parent.parent
          / "skills" / "auto-context" / "assets" / "sqlite")

CORPUS = [
    "预跑一遍再定 schema，上下文这块用 Agent 来做检索。",
    "Skardi 的 auto_context 负责把文档变成可查询的上下文。",
]


def _build(tokenize):
    """An in-memory documents/documents_fts pair, indexed as given."""
    db = sqlite3.connect(":memory:")
    clause = f", tokenize='{tokenize}'" if tokenize else ""
    db.execute("CREATE TABLE documents(id INTEGER PRIMARY KEY, content TEXT)")
    db.execute(
        f"CREATE VIRTUAL TABLE documents_fts USING fts5(id UNINDEXED, content{clause})"
    )
    for i, text in enumerate(CORPUS, 1):
        db.execute("INSERT INTO documents VALUES (?, ?)", (i, text))
        db.execute("INSERT INTO documents_fts(id, content) VALUES (?, ?)", (i, text))
    return db


def _search(db, query):
    """The rendered pipeline's shape: MATCH at >= 3 chars, LIKE below."""
    if len(query) >= 3:
        rows = db.execute(
            "SELECT id FROM documents_fts WHERE documents_fts MATCH ?", (query,)
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT id FROM documents WHERE content LIKE ?", (f"%{query}%",)
        ).fetchall()
    return sorted(r[0] for r in rows)


def test_default_tokenizer_is_the_bug():
    """Pins the failure this fix exists for, so a revert is loud."""
    db = _build(None)
    for term in ["上下文", "检索", "预跑"]:
        assert _search(db, term) == [] or len(term) < 3, (
            f"{term!r} unexpectedly matched under unicode61"
        )
    assert _search(db, "Skardi") == [2], "ASCII was never broken"


@pytest.mark.parametrize("term,expected", [
    ("上下文", [1, 2]),
    ("auto_context", [2]),
    ("可查询", [2]),
])
def test_trigram_finds_cjk_terms_of_three_or_more(term, expected):
    assert _search(_build("trigram"), term) == expected


@pytest.mark.parametrize("term,expected", [
    ("预跑", [1]),
    ("检索", [1]),
    ("文档", [2]),
])
def test_short_queries_fall_back_instead_of_returning_nothing(term, expected):
    """Below the trigram window MATCH can never hit; LIKE must answer."""
    db = _build("trigram")
    assert db.execute(
        "SELECT count(*) FROM documents_fts WHERE documents_fts MATCH ?", (term,)
    ).fetchone()[0] == 0, "precondition: MATCH cannot serve a 2-char query"
    assert _search(db, term) == expected


def test_absent_term_returns_nothing_on_both_paths():
    db = _build("trigram")
    assert _search(db, "量子隧穿") == []
    assert _search(db, "没有") == []


def test_created_schema_takes_the_tokenizer_from_the_flag():
    """The DDL is parameterised, and the default stays English-safe."""
    src = (SCRIPTS / "setup_context.py").read_text(encoding="utf-8")
    fts = src.split("CREATE VIRTUAL TABLE documents_fts")[1].split(";")[0]
    assert "tokenize='{fts_tokenizer}'" in fts
    assert 'fts_tokenizer="unicode61"' in src, "default must not change English behaviour"


def test_trigram_is_a_trade_off_not_a_free_win():
    """Pins the cost, so nobody later makes trigram the default by accident."""
    db = _build("trigram")
    for text in ["the cat sat on the mat", "concatenate the two strings"]:
        db.execute("INSERT INTO documents VALUES (NULL, ?)", (text,))
        db.execute(
            "INSERT INTO documents_fts(id, content) "
            "SELECT id, content FROM documents WHERE content = ?", (text,)
        )
    hits = db.execute(
        "SELECT count(*) FROM documents_fts WHERE documents_fts MATCH 'cat'"
    ).fetchone()[0]
    assert hits == 2, "trigram matches 'cat' inside 'concatenate' — that is the cost"


def test_fulltext_pipeline_has_a_short_query_branch():
    tpl = (ASSETS / "pipelines" / "search_fulltext.yaml.tpl").read_text(encoding="utf-8")
    assert "length({query}) >= 3" in tpl
    assert "length({query}) < 3" in tpl
    assert "LIKE" in tpl


def test_index_tokenizer_is_readable_from_an_existing_db(tmp_path):
    """ingest must be able to tell which index it is about to feed."""
    old = tmp_path / "old.db"
    db = sqlite3.connect(str(old))
    db.execute("CREATE VIRTUAL TABLE documents_fts USING fts5(id UNINDEXED, content)")
    db.close()
    assert ingest_corpus.fts_tokenizer_of(old) == "unicode61"

    new = tmp_path / "new.db"
    db = sqlite3.connect(str(new))
    db.execute(
        "CREATE VIRTUAL TABLE documents_fts USING fts5(id UNINDEXED, content, tokenize='trigram')"
    )
    db.close()
    assert ingest_corpus.fts_tokenizer_of(new) == "trigram"


def test_unknown_workspaces_stay_silent(tmp_path):
    """No file, or no FTS table, is not evidence of a mismatch."""
    assert ingest_corpus.fts_tokenizer_of(tmp_path / "missing.db") is None
    empty = tmp_path / "empty.db"
    sqlite3.connect(str(empty)).close()
    assert ingest_corpus.fts_tokenizer_of(empty) is None


def test_corpus_language_is_detected(tmp_path):
    """The warning only fires when the corpus really is CJK."""
    cjk = tmp_path / "cjk"
    cjk.mkdir()
    (cjk / "a.md").write_text("预跑一遍再定 schema，上下文这块用 Agent 来做检索。", encoding="utf-8")
    assert ingest_corpus.corpus_is_mostly_cjk(cjk) is True

    en = tmp_path / "en"
    en.mkdir()
    (en / "a.md").write_text("the cat sat on the mat and nothing else happened", encoding="utf-8")
    assert ingest_corpus.corpus_is_mostly_cjk(en) is False

    empty = tmp_path / "empty"
    empty.mkdir()
    assert ingest_corpus.corpus_is_mostly_cjk(empty) is None
    assert ingest_corpus.corpus_is_mostly_cjk(tmp_path / "missing") is None


# --- chunk size vs the embedding model's token cap (skardi-skills#14) -------

def test_chunk_guard_constants_are_below_the_cap():
    """The suggested ceiling must fit the tightest measured tokenizer.

    bge turned 1200 characters of Chinese into 1202 tokens — 1:1 — so at
    roughly one token per character the suggestion has to sit under 512, not
    at it, to leave room for the special tokens the model adds.
    """
    assert ingest_corpus.EMBED_TOKEN_CAP == 512
    assert ingest_corpus.CJK_SAFE_CHUNK_CHARS < ingest_corpus.EMBED_TOKEN_CAP
    assert ingest_corpus.DEFAULT_OVERLAP < ingest_corpus.CJK_SAFE_CHUNK_CHARS, (
        "the default overlap must still be usable at the CJK ceiling"
    )


def test_default_chunk_size_would_overflow_cjk():
    """Pins the bug: the shipped default is unsafe for CJK, which is why the
    guard exists rather than the default simply being lowered for everyone."""
    assert ingest_corpus.DEFAULT_CHUNK_SIZE > ingest_corpus.CJK_SAFE_CHUNK_CHARS
    # ~1 token per character in CJK, so the default is over twice the cap.
    assert ingest_corpus.DEFAULT_CHUNK_SIZE > 2 * ingest_corpus.EMBED_TOKEN_CAP * 0.9


def _guard_fires(chunk_size, corpus_dir):
    """The guard's condition, kept in one place so the test pins the rule
    rather than a copy of it."""
    return (chunk_size > ingest_corpus.CJK_SAFE_CHUNK_CHARS
            and bool(ingest_corpus.corpus_is_mostly_cjk(corpus_dir)))


def test_guard_fires_only_for_oversized_cjk(tmp_path):
    cjk = tmp_path / "cjk"
    cjk.mkdir()
    (cjk / "a.md").write_text("预跑一遍再定 schema，上下文这块用 Agent 来做检索。" * 10,
                              encoding="utf-8")
    en = tmp_path / "en"
    en.mkdir()
    (en / "a.md").write_text("the cat sat on the mat and nothing else happened. " * 20,
                             encoding="utf-8")

    assert _guard_fires(1200, cjk) is True, "CJK at the default must be refused"
    assert _guard_fires(500, cjk) is False, "CJK within the ceiling must pass"
    assert _guard_fires(1200, en) is False, "Latin script is unaffected"
    assert _guard_fires(4000, en) is False, "still unaffected at any size"


def test_guard_stays_silent_when_the_corpus_cannot_be_sampled(tmp_path):
    """An empty or missing corpus is not evidence of anything."""
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _guard_fires(1200, empty) is False
    assert _guard_fires(1200, tmp_path / "missing") is False
