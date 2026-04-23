#!/usr/bin/env python3
"""Initialize a Skardi-backed knowledge-base workspace.

Idempotent: re-running against an existing workspace recreates the DB (with
--force) but otherwise exits cleanly.

Flow:
  1. Check skardi CLI is on PATH.
  2. Ensure sqlite_vec and (if downloading the model) huggingface_hub are importable.
  3. Resolve or download the embedding model.
  4. Create workspace dir; render ctx.yaml, aliases.yaml, and pipelines/*.yaml
     from templates in ../assets, substituting absolute paths so `skardi`
     works regardless of CWD.
  5. Create kb.db with documents + documents_fts + documents_vec + triggers.

After this runs, the caller should export SKARDICONFIG=<workspace> and proceed
to chunk_corpus.py + bulk_ingest.py.
"""
import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
ASSETS = SKILL_DIR / "assets"

DEFAULT_MODEL_REPO = "BAAI/bge-small-en-v1.5"
DEFAULT_MODEL_FILES = ["model.safetensors", "config.json", "tokenizer.json"]
DEFAULT_EMBEDDING_DIM = 384


def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def check_skardi():
    if shutil.which("skardi") is None:
        die(
            "`skardi` CLI not found on PATH. Install from "
            "https://github.com/SkardiLabs/skardi (cargo install --locked --path "
            "crates/cli --features candle)."
        )
    out = subprocess.run(["skardi", "--version"], capture_output=True, text=True)
    print(f"  found: {out.stdout.strip() or 'skardi (version unknown)'}")


def ensure_pkg(pkg, import_name=None):
    import_name = import_name or pkg.replace("-", "_")
    try:
        __import__(import_name)
        return
    except ImportError:
        pass

    print(f"  installing {pkg} ...")
    # Try a plain --user install first. Homebrew / Debian-style Pythons reject
    # that under PEP 668; if we detect that, retry with --break-system-packages
    # (still --user, so we don't touch the system site-packages). Failing
    # that, suggest a venv.
    attempts = [
        [sys.executable, "-m", "pip", "install", "--user", "--quiet", pkg],
        [sys.executable, "-m", "pip", "install", "--user", "--break-system-packages", "--quiet", pkg],
    ]
    last_err = None
    for cmd in attempts:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0:
            break
        last_err = (proc.stdout or "") + (proc.stderr or "")
        if "externally-managed-environment" not in last_err:
            # Not a PEP 668 issue — no point retrying with --break-system-packages.
            break
    else:
        die(
            f"Failed to install {pkg}. Last error:\n{last_err}\n"
            f"Install it manually (e.g. create a venv, or `pipx install {pkg}`) "
            f"and re-run setup_kb.py."
        )
    # After install, Python's import machinery still has cached state from
    # before the new site-packages path was populated. Refresh it so the
    # newly-installed module is discoverable without a restart.
    import importlib
    import site
    site.main()
    importlib.invalidate_caches()
    try:
        __import__(import_name)
    except ImportError as e:
        die(f"Installed {pkg} but still cannot import {import_name}: {e}")


def resolve_sqlite_vec():
    ensure_pkg("sqlite-vec", "sqlite_vec")
    import sqlite_vec

    # loadable_path() returns an extensionless stem (e.g. .../sqlite_vec/vec0);
    # SQLite's load_extension resolves the platform suffix (.dylib / .so / .dll)
    # automatically. Validate by checking the parent dir has a vec0.* file.
    path = sqlite_vec.loadable_path()
    parent = Path(path).parent
    stem = Path(path).name
    if not any(p.name.startswith(stem + ".") for p in parent.iterdir()):
        die(f"sqlite_vec loadable path missing: no {stem}.* file in {parent}")
    return path


def resolve_model(cli_path, workspace):
    """Returns an absolute path to a local bge-style model dir with the three
    required files, downloading it if necessary."""
    if cli_path:
        p = Path(cli_path).expanduser().resolve()
        if not p.is_dir():
            die(f"--model-path {p} is not a directory")
        missing = [f for f in DEFAULT_MODEL_FILES if not (p / f).exists()]
        if missing:
            die(f"model dir {p} missing required files: {missing}")
        print(f"  using model at {p}")
        return str(p)

    # No explicit path — download into <workspace>/models/<repo-basename>/
    ensure_pkg("huggingface_hub")
    from huggingface_hub import hf_hub_download

    target = workspace / "models" / DEFAULT_MODEL_REPO.split("/")[-1]
    target.mkdir(parents=True, exist_ok=True)
    missing = [f for f in DEFAULT_MODEL_FILES if not (target / f).exists()]
    if missing:
        print(f"  downloading {DEFAULT_MODEL_REPO} -> {target} ...")
        for f in DEFAULT_MODEL_FILES:
            if not (target / f).exists():
                hf_hub_download(DEFAULT_MODEL_REPO, f, local_dir=str(target))
    print(f"  using model at {target}")
    return str(target.resolve())


def build_embedding_calls(udf, args, model_path):
    """Returns (ingest_call, query_call) strings embedded in pipeline SQL.

    ingest_call is evaluated over the `content` column during INSERT.
    query_call is evaluated over the pipeline parameter `{query}` at search
    time.
    """
    if udf == "candle":
        # candle(model_dir, text) — local HuggingFace SafeTensors.
        return (
            f"candle('{model_path}', content)",
            f"candle('{model_path}', {{query}})",
        )
    if udf == "gguf":
        # gguf(model_dir, text) — local llama.cpp-format quantised model.
        # Same signature as candle; --model-path points at a dir containing
        # the .gguf file (or the file itself, depending on the Skardi build).
        return (
            f"gguf('{model_path}', content)",
            f"gguf('{model_path}', {{query}})",
        )
    if udf == "remote_embed":
        # remote_embed(provider, model, text) — args is the provider/model head,
        # e.g. "'openai','text-embedding-3-small'" or "'voyage','voyage-3'".
        if not args:
            die(
                "--embedding-udf remote_embed requires --embedding-args "
                "(e.g. \"'openai','text-embedding-3-small'\", "
                "\"'voyage','voyage-3'\", \"'gemini','text-embedding-004'\", "
                "or \"'mistral','mistral-embed'\")"
            )
        return (
            f"remote_embed({args}, content)",
            f"remote_embed({args}, {{query}})",
        )
    die(f"Unsupported --embedding-udf: {udf}")


def render_templates(workspace, db_abs_path, ingest_call, query_call):
    def render_file(src, dst, subs):
        text = src.read_text()
        for k, v in subs.items():
            text = text.replace(k, v)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(text)

    ctx_subs = {"{{DB_PATH}}": db_abs_path}
    render_file(ASSETS / "ctx.yaml.tpl", workspace / "ctx.yaml", ctx_subs)
    render_file(ASSETS / "aliases.yaml.tpl", workspace / "aliases.yaml", {})

    pipeline_subs = {
        "{{EMBEDDING_CALL}}": ingest_call,
        "{{EMBEDDING_CALL_QUERY}}": query_call,
    }
    for tpl in (ASSETS / "pipelines").glob("*.yaml.tpl"):
        render_file(tpl, workspace / "pipelines" / tpl.name[:-4], pipeline_subs)


def create_db(db_path, dim, sqlite_vec_path, force=False):
    if db_path.exists():
        if not force:
            die(
                f"{db_path} already exists. Re-run with --force to recreate (this "
                f"drops every row and re-applies the schema)."
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
    db.enable_load_extension(True)
    db.load_extension(sqlite_vec_path)
    db.enable_load_extension(False)
    db.executescript(schema)
    db.commit()
    db.close()
    print(f"  created {db_path} with documents/documents_fts/documents_vec (dim={dim})")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--workspace", required=True, help="Directory to create (e.g. ./kb)")
    ap.add_argument(
        "--model-path",
        default=None,
        help=(
            "Absolute path to an existing bge-small-style model dir. If omitted, "
            "downloads BAAI/bge-small-en-v1.5 into <workspace>/models/."
        ),
    )
    ap.add_argument(
        "--embedding-udf",
        default="candle",
        choices=["candle", "gguf", "remote_embed"],
        help=(
            "Which Skardi UDF to use for embedding (default: candle). "
            "candle = local HF SafeTensors (bge/e5/nomic/etc.); "
            "gguf = local llama.cpp-format quantised model; "
            "remote_embed = hosted API (openai/voyage/gemini/mistral). "
            "Skardi must be built with the matching feature (candle/gguf/remote-embed)."
        ),
    )
    ap.add_argument(
        "--embedding-args",
        default=None,
        help=(
            "Extra args for the UDF. For remote_embed, e.g. \"'openai','text-embedding-3-small'\". "
            "Ignored for candle."
        ),
    )
    ap.add_argument(
        "--embedding-dim",
        type=int,
        default=DEFAULT_EMBEDDING_DIM,
        help="Dimension of the embedding vector (default: 384 for bge-small).",
    )
    ap.add_argument("--force", action="store_true", help="Overwrite existing kb.db.")
    args = ap.parse_args()

    workspace = Path(args.workspace).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    db_path = workspace / "kb.db"

    print(f"[1/5] Checking skardi CLI ...")
    check_skardi()

    print(f"[2/5] Resolving sqlite-vec ...")
    sqlite_vec_path = resolve_sqlite_vec()
    print(f"  sqlite_vec loadable at {sqlite_vec_path}")

    print(f"[3/5] Resolving embedding model ...")
    if args.embedding_udf == "candle":
        model_path = resolve_model(args.model_path, workspace)
    elif args.embedding_udf == "gguf":
        if not args.model_path:
            die(
                "--embedding-udf gguf requires --model-path pointing at a "
                "local .gguf file or a directory containing one. The skill "
                "does not auto-download GGUF models — pick one from "
                "HuggingFace (search for 'gguf' quantisations of your target "
                "embedding model) and pass its absolute path."
            )
        p = Path(args.model_path).expanduser().resolve()
        if not (p.is_file() or p.is_dir()):
            die(f"--model-path {p} does not exist")
        model_path = str(p)
        print(f"  using gguf model at {model_path}")
    else:
        model_path = ""  # unused for remote_embed
        print(f"  using remote_embed UDF with args {args.embedding_args!r}")

    print(f"[4/5] Rendering templates into {workspace} ...")
    ingest_call, query_call = build_embedding_calls(
        args.embedding_udf, args.embedding_args, model_path
    )
    render_templates(workspace, str(db_path), ingest_call, query_call)
    print(f"  wrote ctx.yaml, aliases.yaml, pipelines/*.yaml")

    print(f"[5/5] Creating {db_path} (dim={args.embedding_dim}) ...")
    create_db(db_path, args.embedding_dim, sqlite_vec_path, force=args.force)

    print()
    print("=" * 72)
    print("Workspace ready. Next steps:")
    print()
    print(f"  export SKARDICONFIG={workspace}")
    print(f"  export SQLITE_VEC_PATH={sqlite_vec_path}")
    print()
    print(f"  python {SKILL_DIR}/scripts/chunk_corpus.py \\")
    print(f"    --corpus <path/to/docs> --out {workspace}/chunks.csv")
    print()
    print(f"  python {SKILL_DIR}/scripts/bulk_ingest.py \\")
    print(f"    --workspace {workspace} --chunks {workspace}/chunks.csv")
    print()
    print(f"  skardi grep \"your question\" --limit=5")
    print("=" * 72)


if __name__ == "__main__":
    main()
