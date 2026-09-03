#!/usr/bin/env python3
"""Render a skardi-server workspace for searchable context. Two backends.

  --backend sqlite (default)
      This skill creates and owns <workspace>/kb.db: a `documents` table
      plus FTS5 and sqlite-vec `vec0` mirrors kept in sync by triggers.
      The user supplies nothing about storage. Writing DDL here is fine —
      the file is a workspace artifact, and deleting the workspace undoes
      it completely.

  --backend postgres
      Targets a table the USER created in a database the user runs. This
      script never runs DDL there. If the schema is missing, the caller
      prints the SQL and stops.

Flow:
  1. Resolve the embedding UDF + model path / remote args from flags.
  2. Render <workspace>/{ctx.yaml, semantics.yaml, pipelines/*.yaml} from
     ../assets/<backend>/ templates. Embedding happens server-side inside
     the rendered pipelines (chunk → embed → write in one INSERT for
     ingest-chunked; embed inline for search-{vector,hybrid}), so this
     needs the skardi-server-full image or a server built --features rag.
  3. On sqlite only: create the .db and its schema.

This script never invokes the `skardi` CLI. Two reasons, both structural:

  * There is deliberately NO connectivity pre-flight. The CLI holds no
    engine since skardi PR #170, so it cannot reach a datastore before a
    server runs. Server startup is the check — see the note further down.
  * An earlier version gated setup on `skardi --version`, using the CLI
    version as a stand-in for the server's. That proxy does not hold: the
    v0.5.0 CLI is a thin HTTP client that talks to whatever server it is
    pointed at, so its version says nothing about the server that will
    actually run the pipelines — and requiring it on PATH blocked anyone
    running the server from the published container image. The real check
    is the server refusing to load a pipeline whose UDF it lacks.

Output: <workspace>/{ctx.yaml, semantics.yaml, pipelines/*.yaml}, the
sqlite .db when applicable, plus a `.embedding.txt` breadcrumb so
ingest_corpus.py / start_server.py know what the pipelines target without
re-parsing the YAML.
"""
import argparse
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from _platform import require_supported_platform
from _report import Report

SKILL_DIR = Path(__file__).resolve().parent.parent
ASSETS = SKILL_DIR / "assets"

DEFAULT_MODEL_FILES = ["model.safetensors", "config.json", "tokenizer.json"]


def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def ensure_pkg(pkg, import_name=None):
    """Import pkg, installing it into the user site if missing."""
    import_name = import_name or pkg.replace("-", "_")
    try:
        __import__(import_name)
        return
    except ImportError:
        pass

    print(f"  installing {pkg} ...")
    attempts = [
        [sys.executable, "-m", "pip", "install", "--user", "--quiet", pkg],
        [sys.executable, "-m", "pip", "install", "--user",
         "--break-system-packages", "--quiet", pkg],
    ]
    last_err = None
    for cmd in attempts:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0:
            break
        last_err = (proc.stdout or "") + (proc.stderr or "")
        if "externally-managed-environment" not in last_err:
            break
    else:
        die(
            f"Failed to install {pkg}. Last error:\n{last_err}\n"
            f"Install it manually (e.g. create a venv, or `pipx install {pkg}`) "
            f"and re-run setup_context.py."
        )
    import importlib
    import site
    site.main()
    importlib.invalidate_caches()
    try:
        __import__(import_name)
    except ImportError as e:
        die(f"Installed {pkg} but still cannot import {import_name}: {e}")


def sqlite_vec_extension_present(path):
    """Does this loadable-extension path resolve to a file on disk?

    The path is a STEM, not a filename: `sqlite_vec.loadable_path()` returns
    `.../sqlite_vec/vec0`, and SQLite's `load_extension` appends the platform
    suffix itself (`.dylib` / `.so` / `.dll`). So the check is "is there a
    `vec0.*` next to it", never `Path(p).is_file()` — that is False for every
    correctly resolved path, and a caller that used it refused to start on a
    perfectly good workspace. Both the resolver here and start_server.py's
    re-export go through this one function so the two cannot drift again.

    A path that already carries its suffix is accepted too: users export
    SQLITE_VEC_PATH by hand, and pointing at the actual file is a reasonable
    thing to do — SQLite loads that as-is.
    """
    p = Path(path)
    if p.is_file():
        return True
    parent = p.parent
    if not parent.is_dir():
        return False
    return any(f.name.startswith(p.name + ".") for f in parent.iterdir())


def resolve_sqlite_vec():
    """Absolute path to the sqlite-vec loadable extension (no file suffix)."""
    ensure_pkg("sqlite-vec", "sqlite_vec")
    import sqlite_vec

    path = sqlite_vec.loadable_path()
    if not sqlite_vec_extension_present(path):
        die(f"sqlite_vec loadable path missing: no {Path(path).name}.* file "
            f"in {Path(path).parent}")
    return path


def fts_tokenizer_of(db_path):
    """Which tokenizer an existing documents_fts was built with, or None.

    None means the question does not apply — no file, no FTS table, or an
    unreadable file. Callers stay silent then, rather than describing a
    database they could not inspect.
    """
    if not db_path.exists():
        return None
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = db.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='documents_fts'"
            ).fetchone()
        finally:
            db.close()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    return "trigram" if "trigram" in row[0].lower() else "unicode61"


def create_sqlite_db(db_path, dim, sqlite_vec_path, force=False,
                     fts_tokenizer="unicode61"):
    """Create the local knowledge-base schema this skill owns.

    Only ever runs on the sqlite backend, against a .db inside the workspace
    that this skill created. It is NOT the same act as writing DDL into a
    datastore the user owns — that stays forbidden.

    Layout: `documents` holds the canonical rows; `documents_fts` (FTS5) and
    `documents_vec` (sqlite-vec vec0) are mirrors kept in sync by AFTER
    INSERT/UPDATE/DELETE triggers, so one INSERT updates all three signals.
    """
    if db_path.exists():
        if not force:
            tok = fts_tokenizer_of(db_path)
            hint = f" Its full-text index uses the {tok} tokenizer." if tok else ""
            die(
                f"{db_path} already exists. Re-run with --force to recreate "
                f"(this drops every row and re-applies the schema).{hint}"
            )
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    schema = f"""
CREATE TABLE documents (
    id         INTEGER PRIMARY KEY,
    source     TEXT NOT NULL,
    chunk_idx  INTEGER NOT NULL,
    content    TEXT NOT NULL,
    embedding  BLOB NOT NULL
);

-- The tokenizer is a corpus-language trade-off, fixed at CREATE TABLE time.
--   unicode61 (FTS5's default): splits on whitespace and punctuation, so
--     English word search is exact — but a run of Han characters contains
--     neither, indexes as one giant token, and MATCH finds nothing while
--     still reporting success.
--   trigram: indexes every 3-character window, which makes CJK searchable —
--     but English word search becomes substring search (measured: a query
--     for 'cat' also matches 'concatenate'), and queries below the 3-char
--     window can never match at all (search-fulltext falls back to LIKE
--     for those).
-- Neither is right for both, so --fts-tokenizer picks by corpus.
CREATE VIRTUAL TABLE documents_fts USING fts5(
    id UNINDEXED, source UNINDEXED, chunk_idx UNINDEXED,
    content,
    tokenize='{fts_tokenizer}'
);

CREATE VIRTUAL TABLE documents_vec USING vec0(
    id        INTEGER PRIMARY KEY,
    embedding float[{dim}]
);

CREATE TRIGGER documents_ai AFTER INSERT ON documents BEGIN
    INSERT INTO documents_fts(id, source, chunk_idx, content)
        VALUES (NEW.id, NEW.source, NEW.chunk_idx, NEW.content);
    INSERT INTO documents_vec(id, embedding)
        VALUES (NEW.id, NEW.embedding);
END;

CREATE TRIGGER documents_au AFTER UPDATE ON documents BEGIN
    DELETE FROM documents_fts WHERE id = OLD.id;
    INSERT INTO documents_fts(id, source, chunk_idx, content)
        VALUES (NEW.id, NEW.source, NEW.chunk_idx, NEW.content);
    DELETE FROM documents_vec WHERE id = OLD.id;
    INSERT INTO documents_vec(id, embedding)
        VALUES (NEW.id, NEW.embedding);
END;

CREATE TRIGGER documents_ad AFTER DELETE ON documents BEGIN
    DELETE FROM documents_fts WHERE id = OLD.id;
    DELETE FROM documents_vec WHERE id = OLD.id;
END;
"""

    db = sqlite3.connect(str(db_path))
    if not hasattr(db, "enable_load_extension"):
        db.close()
        die(
            "This Python build cannot load SQLite extensions, which sqlite-vec needs.\n"
            "  This is the default on macOS system Python (/usr/bin/python3).\n"
            "  Fix: use a Python compiled with extension support, e.g.\n"
            "    brew install python   # then run with /opt/homebrew/bin/python3\n"
            "  Verify any Python with:\n"
            "    <python> -c \"import sqlite3; print(hasattr(sqlite3.connect(':memory:'), 'enable_load_extension'))\"   # must print True"
        )
    db.enable_load_extension(True)
    db.load_extension(sqlite_vec_path)
    db.enable_load_extension(False)
    db.executescript(schema)
    db.commit()
    db.close()
    print(f"  created {db_path} with documents/documents_fts/documents_vec (dim={dim})")


def resolve_candle_model(cli_path, workspace):
    if not cli_path:
        die(
            "--embedding-udf candle requires --model-path. The skill does "
            "not pick a default model — see SKILL.md § 'Choosing the "
            "embedding backend' for guidance, then download the chosen "
            "HuggingFace repo (model.safetensors + config.json + "
            "tokenizer.json) and pass its absolute path here."
        )
    p = Path(cli_path).expanduser().resolve()
    if not p.is_dir():
        die(f"--model-path {p} is not a directory")
    missing = [f for f in DEFAULT_MODEL_FILES if not (p / f).exists()]
    if missing:
        die(
            f"candle model dir {p} is missing required files: {missing}. "
            f"A candle-compatible HuggingFace model needs all three of "
            f"model.safetensors, config.json, tokenizer.json."
        )
    print(f"  candle model: {p}")
    return str(p)


def resolve_gguf_model(cli_path):
    """Validate a GGUF model directory against what the server will accept.

    The server's rule (crates/skardi/src/model/gguf/embed.rs, `find_gguf_file`)
    is *exactly one* `.gguf` file in the directory: zero and two are both hard
    errors. This check has to match it. Accepting a directory with several
    quantisations — the normal result of cloning a GGUF repo, which ships
    Q4_K_M / Q8_0 / f16 side by side — let setup report success and then failed
    every single embed call at ingest time, which is the failure shape this
    skill's whole report design exists to avoid.

    Sidecar files are fine: the server filters by extension, and the tokenizer
    comes from the GGUF's own `tokenizer.ggml.*` metadata, not from a
    tokenizer.json next to it.
    """
    if not cli_path:
        die(
            "--embedding-udf gguf requires --model-path pointing at a "
            "directory that contains exactly one .gguf weights file. The "
            "skill does not auto-download GGUF because some are licence-gated "
            "(Gemma) or have multiple quantisations the user must pick between."
        )
    p = Path(cli_path).expanduser().resolve()
    if not p.is_dir():
        die(
            f"--model-path {p} is not a directory. Point at the directory "
            f"holding the .gguf file, not at the file itself — the server "
            f"takes a directory and finds the weights inside it."
        )
    found = sorted(f.name for f in p.iterdir() if f.is_file() and f.suffix == ".gguf")
    if not found:
        die(f"gguf model dir {p} contains no .gguf file")
    if len(found) > 1:
        die(
            f"gguf model dir {p} contains {len(found)} .gguf files: "
            f"{', '.join(found)}.\n"
            f"  The server loads a GGUF model by scanning the directory and "
            f"requires exactly one, so it cannot tell which quantisation you "
            f"meant. Move the one you want into a directory of its own and "
            f"point --model-path there."
        )
    print(f"  gguf model: {p} ({found[0]})")
    return str(p)


def build_embedding_calls(udf, args, model_path):
    """Return a dict of SQL fragments keyed by which column / parameter
    the call wraps.

    The same UDF gets called over three different column references in
    the rendered pipelines, so we render three variants up front rather
    than parameterising the column name at call time:

      - `content`     — used by `ingest` (caller-supplied chunk text)
      - `chunk_text`  — used by `ingest-chunked` (UNNEST(chunk(...)) output)
      - `{query}`     — used by `search-vector` / `search-hybrid` (pipeline param)
    """
    if udf == "candle":
        head = f"candle('{model_path}',"
    elif udf == "gguf":
        head = f"gguf('{model_path}',"
    elif udf == "remote_embed":
        if not args:
            die(
                "--embedding-udf remote_embed requires --embedding-args. "
                "Examples: \"'openai','text-embedding-3-small'\", "
                "\"'voyage','voyage-3'\", \"'voyage','voyage-code-3'\", "
                "\"'gemini','text-embedding-004'\", "
                "\"'mistral','mistral-embed'\". The relevant API key "
                "(OPENAI_API_KEY / VOYAGE_API_KEY / GEMINI_API_KEY / "
                "MISTRAL_API_KEY) must be in the server's environment "
                "when it starts."
            )
        head = f"remote_embed({args},"
    else:
        die(f"Unsupported --embedding-udf: {udf}")
    return {
        "content":    f"{head} content)",
        "chunk_text": f"{head} chunk_text)",
        "query":      f"{head} {{query}})",
    }


def render_templates(backend, workspace, subs):
    src_dir = ASSETS / backend
    if not src_dir.is_dir():
        die(f"No template directory for backend {backend!r} at {src_dir}")

    def _render(src, dst):
        text = src.read_text()
        for k, v in subs.items():
            text = text.replace(k, v)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(text)

    # ctx.yaml
    ctx_tpl = src_dir / "ctx.yaml.tpl"
    if not ctx_tpl.is_file():
        die(f"Missing template {ctx_tpl}")
    _render(ctx_tpl, workspace / "ctx.yaml")

    # semantics.yaml — auto-discovered by skardi-server; surfaces in `skardi schema`.
    sem_tpl = src_dir / "semantics.yaml.tpl"
    if sem_tpl.is_file():
        _render(sem_tpl, workspace / "semantics.yaml")

    # pipelines/*.yaml
    pipelines_out = workspace / "pipelines"
    pipelines_out.mkdir(parents=True, exist_ok=True)
    for tpl in (src_dir / "pipelines").glob("*.yaml.tpl"):
        _render(tpl, pipelines_out / tpl.name[:-4])


# NOTE ON THE REMOVED PRE-FLIGHT CHECK
#
# This script used to probe the datastore before starting anything, by
# setting SKARDICONFIG=<workspace> — a variable that no longer exists — and running
#   skardi query --sql "SELECT 1 FROM <table>"
# so the CLI would connect directly and surface auth / network /
# missing-table errors early.
#
# That is no longer possible. The CLI holds no engine: SKARDICONFIG was
# removed and `skardi query` now POSTs SQL to a running skardi-server.
# There is no server yet at this point in setup, so the probe cannot run.
#
# Connectivity is now checked by starting the server: skardi-server loads
# ctx.yaml and fails at startup when a source is unreachable, and its
# error names the source it could not open. start_server.py surfaces
# that. Do not reintroduce a CLI-side probe here.


def main():
    require_supported_platform("setup_context.py")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--workspace", required=True, help="Directory to populate (e.g. ./context)")
    ap.add_argument(
        "--backend",
        default="sqlite",
        choices=["sqlite", "postgres"],
        help=(
            "Where the rows live. Default 'sqlite': this skill creates and "
            "owns a local .db file inside the workspace, so the user supplies "
            "nothing but a corpus. Use 'postgres' only when the user asks to "
            "put the data in a database they already run — then "
            "--connection-string and --table become required and refer to "
            "objects the user created themselves."
        ),
    )
    ap.add_argument(
        "--connection-string",
        default=None,
        help=(
            "postgres only, e.g. postgresql://localhost:5432/ragdb?sslmode=disable. "
            "Ignored for sqlite, where the path is derived from --workspace."
        ),
    )
    ap.add_argument(
        "--table",
        default=None,
        help="postgres only: table name (must already exist). Ignored for sqlite.",
    )
    ap.add_argument("--schema", default="public", help="postgres only (default: public)")
    ap.add_argument(
        "--embedding-udf",
        required=True,
        choices=["candle", "gguf", "remote_embed"],
        help=(
            "Which Skardi UDF to use for embedding. The Skardi server must "
            "be built with the matching feature flag — most users want the "
            "skardi-server-full image (which bundles --features rag = "
            "chunking + embedding) plus an additional --features for the "
            "specific UDF if it's not already in the rag bundle."
        ),
    )
    ap.add_argument(
        "--model-path",
        default=None,
        help=(
            "Absolute path to a local model directory. Required for candle "
            "and gguf. Ignored for remote_embed."
        ),
    )
    ap.add_argument(
        "--embedding-args",
        default=None,
        help=(
            "Required for remote_embed. The provider/model head, e.g. "
            "\"'openai','text-embedding-3-small'\"."
        ),
    )
    ap.add_argument(
        "--embedding-dim",
        type=int,
        required=True,
        help=(
            "Output dimension of the chosen embedding model. Must match "
            "the vector(N) the user reserved in the schema."
        ),
    )
    ap.add_argument(
        "--chunk-mode",
        default="markdown",
        choices=["markdown", "character"],
        help=(
            "Splitter mode baked into the ingest pipelines and recorded in the "
            "workspace breadcrumb. 'markdown' prefers heading / paragraph / "
            "code-block boundaries; 'character' is a generic recursive "
            "splitter for unstructured prose. Default: markdown."
        ),
    )
    ap.add_argument(
        "--fts-tokenizer",
        choices=["unicode61", "trigram"],
        default="unicode61",
        help=(
            "sqlite only: how documents_fts splits text. unicode61 (default) "
            "is word-accurate for English but finds NOTHING in Chinese, "
            "Japanese or Korean text; trigram makes CJK searchable at the "
            "cost of turning English word search into substring search (a "
            "query for 'cat' starts matching 'concatenate'). Pass trigram for "
            "a CJK corpus. Fixed at CREATE TABLE time, so changing it later "
            "needs --force; ingest_corpus.py warns when the corpus and the "
            "index disagree."
        ),
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help=(
            "sqlite only: recreate <workspace>/kb.db even if it exists. This "
            "drops every ingested row and re-applies the schema."
        ),
    )
    ap.add_argument(
        "--skip-health-check",
        action="store_true",
        # Accepted but ignored. This refers to the REMOVED CLI connectivity
        # probe (see the module docstring), not to the step report printed at
        # the end of a run — there is no flag to turn that off, and it costs
        # nothing to print.
        help=argparse.SUPPRESS,
    )
    args = ap.parse_args()

    # Step report (ported from PR #21). Built before any work happens so that
    # every exit path prints the table — a bare ERROR tells the user what broke
    # but not which step they got to or how long the run had already taken.
    # The denominator is per-backend: sqlite does two things postgres does not
    # (resolve the extension, create the schema), and an honest 2/2 beats a
    # padded 2/4. Dropped from 5/3 when the `skardi --version` step was
    # removed — the CLI is never invoked by this skill, so gating on it
    # measured nothing.
    report = Report(4 if args.backend == "sqlite" else 2, "Setup")

    with report.guard("workspace pre-flight"):
        workspace = Path(args.workspace).expanduser().resolve()
        try:
            workspace.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            # A raw filesystem error (permission denied, path is a file) should
            # read as an ERROR with a cause, not a bare traceback.
            die(f"could not create workspace {workspace}: {e}")

        # Backend-specific argument handling. sqlite is the zero-question
        # default: this skill creates and owns the .db, so the user supplies
        # only a corpus and nothing about storage. postgres means the user
        # already runs the database and created the table, so both must be
        # named explicitly — we never invent a connection string or run DDL
        # against someone's database.
        if args.backend == "sqlite":
            db_path = workspace / "kb.db"
            table = "documents"
            # Refuse an existing .db HERE, not at the create step (ported from
            # PR #21). A re-run that is going to be refused should be refused
            # before it costs the user a model download, and before the render
            # step rewrites ctx.yaml / .embedding.txt with a dim that may not
            # match the schema already in the file. create_sqlite_db repeats
            # the check as a safety net.
            if db_path.exists() and not args.force:
                tok = fts_tokenizer_of(db_path)
                hint = f" Its full-text index uses the {tok} tokenizer." if tok else ""
                die(
                    f"{db_path} already exists. Re-run with --force to recreate "
                    f"(this drops every ingested row and re-applies the schema). "
                    f"Nothing was changed.{hint}"
                )
            for flag, value in (("--connection-string", args.connection_string),
                                ("--table", args.table)):
                if value:
                    print(f"  note: {flag} is ignored for --backend sqlite")
        else:
            missing = [f for f, v in (("--connection-string", args.connection_string),
                                      ("--table", args.table)) if not v]
            if missing:
                die(
                    f"--backend {args.backend} requires {' and '.join(missing)}. "
                    "These name objects the user already created; this skill does "
                    "not create schema in a database you own."
                )
            db_path = None
            table = args.table

    sqlite_vec_path = ""
    if args.backend == "sqlite":
        with report.step("Resolving sqlite-vec", "sqlite-vec extension"):
            sqlite_vec_path = resolve_sqlite_vec()
            print(f"  sqlite_vec loadable at {sqlite_vec_path}")

    with report.step("Resolving embedding UDF + model", "embedding model"):
        if args.embedding_udf == "candle":
            model_path = resolve_candle_model(args.model_path, workspace)
        elif args.embedding_udf == "gguf":
            model_path = resolve_gguf_model(args.model_path)
        else:
            model_path = ""  # remote_embed has no local model
            print(f"  remote_embed args: {args.embedding_args!r}")
        embed_calls = build_embedding_calls(args.embedding_udf, args.embedding_args, model_path)

    with report.step(f"Rendering {args.backend} templates into {workspace}",
                     "workspace files"):
        subs = {
            "{{CONNECTION_STRING}}":             args.connection_string or "",
            "{{TABLE}}":                         table,
            "{{SCHEMA}}":                        args.schema,
            "{{DB_PATH}}":                       str(db_path) if db_path else "",
            "{{CHUNK_MODE}}":                    args.chunk_mode,
            "{{EMBED_CALL_OVER_CONTENT}}":       embed_calls["content"],
            "{{EMBED_CALL_OVER_CHUNK_TEXT}}":    embed_calls["chunk_text"],
            "{{EMBED_CALL_OVER_QUERY}}":         embed_calls["query"],
        }
        render_templates(args.backend, workspace, subs)
        # Breadcrumb read back by ingest_corpus.py. chunk_mode is recorded here
        # so a mode chosen at setup actually takes effect at bulk ingest instead
        # of silently reverting to the default — see ingest_corpus.py.
        #
        # sqlite_vec_path is recorded for start_server.py, which re-exports it
        # when the environment does not already carry it. Without the key it
        # could not: setup and start usually run in different shells, and an
        # unset SQLITE_VEC_PATH does not stop the server — it starts clean,
        # registers all five pipelines, and then fails every vector query.
        (workspace / ".embedding.txt").write_text(
            f"udf={args.embedding_udf}\n"
            f"model_path={model_path}\n"
            f"embedding_args={args.embedding_args or ''}\n"
            f"dim={args.embedding_dim}\n"
            f"backend={args.backend}\n"
            f"table={table}\n"
            f"schema={args.schema}\n"
            f"db_path={db_path or ''}\n"
            f"chunk_mode={args.chunk_mode}\n"
            f"sqlite_vec_path={sqlite_vec_path}\n"
        )
        print(f"  wrote ctx.yaml, semantics.yaml, pipelines/{{ingest,ingest_chunked,search_vector,search_fulltext,search_hybrid}}.yaml")

    if args.backend == "sqlite":
        with report.step("Creating the local knowledge-base schema",
                         "kb.db + ext-load"):
            create_sqlite_db(db_path, args.embedding_dim, sqlite_vec_path,
                             force=args.force,
                             fts_tokenizer=args.fts_tokenizer)
        print(f"  export SQLITE_VEC_PATH={sqlite_vec_path}")
        print("  (the server loads sqlite-vec from that path. The path is also "
              "recorded in")
        print("   .embedding.txt, so start_server.py re-exports it for you when "
              "your shell")
        print("   does not already have it — the export above only matters for "
              "commands")
        print("   you run yourself against the server.)")
    else:
        # Deliberately NOT a step row: nothing is checked here. Claiming a
        # passed "connectivity check" the CLI cannot perform is the kind of
        # over-promise this report exists to avoid.
        print("  note: connectivity is not checked at setup — the CLI can no")
        print("  longer reach a datastore on its own, so skardi-server loading")
        print("  ctx.yaml IS the check. start_server.py names the source that")
        print("  failed.")

    report.finish()
    print()
    print("=" * 72)
    print("Workspace ready. Next steps:")
    print()
    print(f"  # 1. Start the server (use skardi-server-full image — bundles chunk + embedding):")
    print(f"  python {SKILL_DIR}/scripts/start_server.py --workspace {workspace} --port 8080")
    print()
    print(f"  # 2. Ingest the corpus end-to-end (server chunks + embeds inline):")
    print(f"  python {SKILL_DIR}/scripts/ingest_corpus.py \\")
    print(f"    --workspace {workspace} --corpus <path/to/docs>")
    print()
    print(f"  # 3. Query (server embeds the question inline; pass plain text):")
    print(f"  curl -X POST http://localhost:8080/search-hybrid/execute \\")
    print(f"    -H 'Content-Type: application/json' \\")
    print(f"    -d '{{\"query\":\"...\",\"text_query\":\"...\",\"vector_weight\":0.5,\"text_weight\":0.5,\"limit\":5}}'")
    print("=" * 72)


if __name__ == "__main__":
    main()
