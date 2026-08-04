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
  1. Validate `skardi` CLI is on PATH and >= 0.4.0 (the chunk() UDF and the
     `kind: semantics` overlay both landed in 0.4.0; the rendered pipelines
     and the auto-discovered semantics file depend on them).
  2. Resolve the embedding UDF + model path / remote args from flags.
  3. Render <workspace>/{ctx.yaml, semantics.yaml, pipelines/*.yaml} from
     ../assets/<backend>/ templates. Embedding happens server-side inside
     the rendered pipelines (chunk → embed → write in one INSERT for
     ingest-chunked; embed inline for search-{vector,hybrid}), so this
     needs the skardi-server-rag image or a server built --features rag.
  4. On sqlite only: create the .db and its schema.

There is deliberately NO connectivity pre-flight. The CLI holds no engine
since skardi PR #170, so it cannot reach a datastore before a server runs.
Server startup is the check — see the note further down.

Output: <workspace>/{ctx.yaml, semantics.yaml, pipelines/*.yaml}, the
sqlite .db when applicable, plus a `.embedding.txt` breadcrumb so
ingest_corpus.py / start_server.py know what the pipelines target without
re-parsing the YAML.
"""
import argparse
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from _platform import require_supported_platform
from _report import Report

SKILL_DIR = Path(__file__).resolve().parent.parent
ASSETS = SKILL_DIR / "assets"

DEFAULT_MODEL_FILES = ["model.safetensors", "config.json", "tokenizer.json"]

MIN_SKARDI_MAJOR = 0
MIN_SKARDI_MINOR = 4


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


def resolve_sqlite_vec():
    """Absolute path to the sqlite-vec loadable extension (no file suffix)."""
    ensure_pkg("sqlite-vec", "sqlite_vec")
    import sqlite_vec

    path = sqlite_vec.loadable_path()
    parent = Path(path).parent
    stem = Path(path).name
    if not any(p.name.startswith(stem + ".") for p in parent.iterdir()):
        die(f"sqlite_vec loadable path missing: no {stem}.* file in {parent}")
    return path


def create_sqlite_db(db_path, dim, sqlite_vec_path, force=False):
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
            die(
                f"{db_path} already exists. Re-run with --force to recreate "
                f"(this drops every row and re-applies the schema)."
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

CREATE VIRTUAL TABLE documents_fts USING fts5(
    id UNINDEXED, source UNINDEXED, chunk_idx UNINDEXED,
    content
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


def check_skardi():
    """Verify the skardi CLI exists AND is >= 0.4.0.

    Returns None when the version was confirmed, or a short note when it ran
    but could not be verified — the caller turns that into a WARN row rather
    than a clean OK. Ported from PR #21: an unparsed version is exactly the
    case that sails through setup and then dies at ingest with "Invalid
    function 'chunk'", so the report must not claim it passed.

    The CLI is used here only for the SELECT 1 health probe, but the
    server (skardi-server-rag) must also be >= 0.4.0 — we use the CLI
    version as a proxy because most users build / install both binaries
    from the same source. If the user has a mixed install we'll fail
    later at ingest time with "Invalid function 'chunk'", which the
    troubleshooting guide names explicitly."""
    if shutil.which("skardi") is None:
        die(
            "`skardi` CLI not found on PATH. The skill uses it for the "
            "pre-flight `SELECT 1` health probe. Install >= 0.4.0 with "
            "`cargo install --locked --git https://github.com/SkardiLabs/skardi "
            "--branch main skardi-cli --features candle`."
        )
    out = subprocess.run(["skardi", "--version"], capture_output=True, text=True)
    if out.returncode != 0:
        # On PATH but `--version` itself errors → the binary is broken, not
        # just unparseable. Hard FAIL (the CLI is unusable), not a WARN.
        die(
            f"`skardi --version` exited {out.returncode} — the binary is on PATH "
            f"but not runnable, so the install looks broken. Reinstall with "
            f"`cargo install --locked --git https://github.com/SkardiLabs/skardi "
            f"--branch main skardi-cli --features candle`.\n"
            f"  stderr: {(out.stderr or '').strip()[:300]}"
        )
    raw = (out.stdout or out.stderr).strip()
    print(f"  found: {raw or 'skardi (version unknown)'}")
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", raw)
    if not m:
        print(
            "  warning: could not parse version; auto_context needs >= 0.4.0 "
            "for the chunk() UDF and the kind: semantics overlay.",
            file=sys.stderr,
        )
        return "version unverified — needs >= 0.4.0 for chunk()"
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if (major, minor) < (MIN_SKARDI_MAJOR, MIN_SKARDI_MINOR):
        die(
            f"Skardi {major}.{minor}.{patch} is too old for this skill. "
            f"auto_context requires >= {MIN_SKARDI_MAJOR}.{MIN_SKARDI_MINOR}.0 "
            f"because it uses the chunk() UDF (server-side ingest) and "
            f"the kind: semantics overlay (catalog descriptions). "
            f"Reinstall with `cargo install --locked --git "
            f"https://github.com/SkardiLabs/skardi --branch main skardi-cli "
            f"--features candle`."
        )


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

    # semantics.yaml — auto-discovered by skardi-server / skardi query --schema.
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
# setting SKARDICONFIG=<workspace> and running
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
            "skardi-server-rag image (which bundles --features rag = "
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
    # (resolve the extension, create the schema), and an honest 3/3 beats a
    # padded 3/5.
    report = Report(5 if args.backend == "sqlite" else 3, "Setup")

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
                die(
                    f"{db_path} already exists. Re-run with --force to recreate "
                    f"(this drops every ingested row and re-applies the schema). "
                    f"Nothing was changed."
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

    with report.step("Checking skardi CLI", "skardi CLI (>=0.4.0)") as r:
        note = check_skardi()
        if note:
            r.warn(note)

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
        )
        print(f"  wrote ctx.yaml, semantics.yaml, pipelines/{{ingest,ingest_chunked,search_vector,search_fulltext,search_hybrid}}.yaml")

    if args.backend == "sqlite":
        with report.step("Creating the local knowledge-base schema",
                         "kb.db + ext-load"):
            create_sqlite_db(db_path, args.embedding_dim, sqlite_vec_path,
                             force=args.force)
        print(f"  export SQLITE_VEC_PATH={sqlite_vec_path}")
        print("  (the server loads sqlite-vec from that path; start_server.py "
              "checks it)")
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
    print(f"  # 1. Start the server (use skardi-server-rag image — bundles chunk + embedding):")
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
