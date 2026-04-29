#!/usr/bin/env python3
"""Render a Skardi-server RAG workspace targeting a USER-SUPPLIED datastore.

Idempotent: re-running rewrites the rendered files but never touches the
user's database. The user's datastore is treated as read-only-from-the-skill's
perspective: this script never runs DDL. If the schema is missing, the
caller has to create it themselves first.

Flow:
  1. Validate `skardi` CLI is on PATH (used for the SELECT 1 health probe).
  2. Resolve the embedding UDF + model path / remote args based on flags.
  3. Render <workspace>/ctx.yaml + <workspace>/pipelines/*.yaml from
     ../assets/postgres/ templates, substituting connection string, table
     name, embedding call, and dim.
  4. Run `skardi query --sql "SELECT 1 FROM <table> LIMIT 1"` against the
     rendered ctx to surface auth / network / table-missing errors at
     setup time. If this fails, print the error and exit non-zero — do not
     leave a half-finished workspace that will fail noisily at ingest time.

Output: <workspace>/{ctx.yaml, pipelines/*.yaml}, and a `.embedding.txt`
breadcrumb so http_ingest.py / start_server.py know what the rendered
pipelines target without re-parsing the YAML.
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
ASSETS = SKILL_DIR / "assets"

DEFAULT_MODEL_FILES = ["model.safetensors", "config.json", "tokenizer.json"]


def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def check_skardi():
    if shutil.which("skardi") is None:
        die(
            "`skardi` CLI not found on PATH. The skill uses it for the "
            "pre-flight `SELECT 1` health probe. Install with "
            "`cargo install --locked --git https://github.com/SkardiLabs/skardi "
            "--branch main skardi-cli --features candle`."
        )


def resolve_candle_model(cli_path, workspace):
    """Return abs path to a candle-compatible model directory.

    If the caller passed --model-path, use it (must contain the three
    canonical files). Otherwise auto-download is *not* performed: candle
    has many compatible models and picking one silently is exactly the
    behaviour the SKILL.md tells us to avoid.
    """
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
    if not cli_path:
        die(
            "--embedding-udf gguf requires --model-path pointing at a "
            "directory that contains the .gguf weights file (and a "
            "tokenizer.json if the model needs one — e.g. embeddinggemma). "
            "The skill does not auto-download GGUF because some are "
            "licence-gated (Gemma) or have multiple quantisations the "
            "user must pick between."
        )
    p = Path(cli_path).expanduser().resolve()
    if not p.is_dir():
        die(f"--model-path {p} is not a directory")
    if not any(f.suffix == ".gguf" for f in p.iterdir() if f.is_file()):
        die(f"gguf model dir {p} contains no .gguf file")
    print(f"  gguf model: {p}")
    return str(p)


def build_embedding_calls(udf, args, model_path):
    """Returns (call_with_content_param, call_with_query_param) — the SQL
    fragments substituted into the ingest pipeline (over the {content}
    parameter) and into the search pipelines (over the {query} parameter).
    """
    if udf == "candle":
        return (
            f"candle('{model_path}', {{content}})",
            f"candle('{model_path}', {{query}})",
        )
    if udf == "gguf":
        return (
            f"gguf('{model_path}', {{content}})",
            f"gguf('{model_path}', {{query}})",
        )
    if udf == "remote_embed":
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
        return (
            f"remote_embed({args}, {{content}})",
            f"remote_embed({args}, {{query}})",
        )
    die(f"Unsupported --embedding-udf: {udf}")


def render_templates(backend, workspace, subs):
    src_dir = ASSETS / backend
    if not src_dir.is_dir():
        die(f"No template directory for backend {backend!r} at {src_dir}")

    # ctx.yaml
    ctx_tpl = src_dir / "ctx.yaml.tpl"
    if not ctx_tpl.is_file():
        die(f"Missing template {ctx_tpl}")
    text = ctx_tpl.read_text()
    for k, v in subs.items():
        text = text.replace(k, v)
    (workspace / "ctx.yaml").write_text(text)

    # pipelines/*.yaml
    pipelines_out = workspace / "pipelines"
    pipelines_out.mkdir(parents=True, exist_ok=True)
    for tpl in (src_dir / "pipelines").glob("*.yaml.tpl"):
        text = tpl.read_text()
        for k, v in subs.items():
            text = text.replace(k, v)
        (pipelines_out / tpl.name[:-4]).write_text(text)


def health_check(workspace, table):
    """Probe the user's datastore via skardi query SELECT 1 FROM <table>.

    Surfaces auth, network, and missing-table errors before we spend time
    starting a server or downloading models. Read-only — never runs DDL.
    """
    env = os.environ.copy()
    env["SKARDICONFIG"] = str(workspace)
    sql = f"SELECT 1 FROM {table} LIMIT 1"
    print(f"  probing user's datastore: {sql}")
    proc = subprocess.run(
        ["skardi", "query", "--sql", sql],
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        die(
            "Pre-flight `SELECT 1 FROM <table>` failed against the "
            "user's datastore. This usually means the connection string "
            "is wrong, PG_USER / PG_PASSWORD aren't exported in this "
            "shell, or the user hasn't run the schema SQL yet (see "
            "SKILL.md § 'Schema the user needs to create'). Fix the "
            "underlying issue and re-run setup_rag.py — the workspace is "
            "left as-is so a second attempt can succeed without "
            "re-rendering."
        )
    print(f"  ok: connection + table reachable")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--workspace", required=True, help="Directory to populate (e.g. ./rag)")
    ap.add_argument("--backend", default="postgres", choices=["postgres"])
    ap.add_argument(
        "--connection-string",
        required=True,
        help="e.g. postgresql://localhost:5432/ragdb?sslmode=disable",
    )
    ap.add_argument("--table", required=True, help="Table name (must already exist)")
    ap.add_argument("--schema", default="public", help="Postgres schema (default: public)")
    ap.add_argument(
        "--embedding-udf",
        required=True,
        choices=["candle", "gguf", "remote_embed"],
        help=(
            "Which Skardi UDF to use for embedding. The Skardi server must "
            "be built with the matching feature flag (--features candle / "
            "gguf / remote-embed)."
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
        "--skip-health-check",
        action="store_true",
        help=(
            "Skip the SELECT 1 probe (e.g. when the server will run on a "
            "different machine than where setup_rag.py runs)."
        ),
    )
    args = ap.parse_args()

    workspace = Path(args.workspace).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    print(f"[1/4] Checking skardi CLI ...")
    check_skardi()

    print(f"[2/4] Resolving embedding UDF + model ...")
    if args.embedding_udf == "candle":
        model_path = resolve_candle_model(args.model_path, workspace)
    elif args.embedding_udf == "gguf":
        model_path = resolve_gguf_model(args.model_path)
    else:
        model_path = ""  # remote_embed has no local model
        print(f"  remote_embed args: {args.embedding_args!r}")
    embed_call_content, embed_call_query = build_embedding_calls(
        args.embedding_udf, args.embedding_args, model_path
    )

    print(f"[3/4] Rendering {args.backend} templates into {workspace} ...")
    subs = {
        "{{CONNECTION_STRING}}": args.connection_string,
        "{{TABLE}}": args.table,
        "{{SCHEMA}}": args.schema,
        "{{EMBED_CALL_CONTENT}}": embed_call_content,
        "{{EMBED_CALL_QUERY}}": embed_call_query,
    }
    render_templates(args.backend, workspace, subs)
    # Breadcrumb: helpful for downstream scripts and for the agent to
    # introspect what was rendered without re-parsing YAML.
    (workspace / ".embedding.txt").write_text(
        f"udf={args.embedding_udf}\n"
        f"model_path={model_path}\n"
        f"embedding_args={args.embedding_args or ''}\n"
        f"dim={args.embedding_dim}\n"
        f"table={args.table}\n"
        f"schema={args.schema}\n"
    )
    print(f"  wrote ctx.yaml, pipelines/{{ingest,search_vector,search_fulltext,search_hybrid}}.yaml")

    print(f"[4/4] Pre-flight connection check ...")
    if args.skip_health_check:
        print("  skipped (--skip-health-check)")
    else:
        health_check(workspace, args.table)

    print()
    print("=" * 72)
    print("Workspace ready. Next steps:")
    print()
    print(f"  # 1. Make sure the embedding feature is built into skardi-server.")
    feature = {"candle": "candle", "gguf": "gguf", "remote_embed": "remote-embed"}[
        args.embedding_udf
    ]
    print(f"  #    e.g. cargo build --release -p skardi-server --features {feature}")
    print()
    print(f"  # 2. Start the server (it will load the embedding model on first call):")
    print(f"  python {SKILL_DIR}/scripts/start_server.py --workspace {workspace} --port 8080")
    print()
    print(f"  # 3. Chunk a corpus and POST it through /ingest/execute:")
    print(f"  python {SKILL_DIR}/scripts/chunk_corpus.py \\")
    print(f"    --corpus <path/to/docs> --out {workspace}/chunks.json")
    print(f"  python {SKILL_DIR}/scripts/http_ingest.py \\")
    print(f"    --workspace {workspace} --chunks {workspace}/chunks.json")
    print()
    print(f"  # 4. Query:")
    print(f"  curl -X POST http://localhost:8080/search-hybrid/execute \\")
    print(f"    -H 'Content-Type: application/json' \\")
    print(f"    -d '{{\"query\":\"...\",\"text_query\":\"...\",\"vector_weight\":0.5,\"text_weight\":0.5,\"limit\":5}}'")
    print("=" * 72)


if __name__ == "__main__":
    main()
