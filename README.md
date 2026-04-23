# skardi-skills

Agent Skills for working with [Skardi](https://github.com/skardi/skardi) — a backend engine to provide SQL pipelines as http endpoints. These skills give your AI coding agent (Claude Code, Cursor, or any [Agent Skills](https://agentskills.io/)-compatible tool) deep knowledge of Skardi's patterns so you don't have to re-explain them each session.

Check out our demo [here](https://www.youtube.com/watch?v=Cx5jG0OtUuk).

## Available skills

| Directory | Skill name | What it covers |
|---|---|---|
| `skardi_on_sealos/` | `skardi-deploy-and-patterns` | Core Skardi concepts (auth, pipelines, CSRF, DataFusion SQL dialect) + deploying to [Sealos](https://sealos.io/) via kubectl |
| `auto_knowledge_base/` | `auto_knowledge_base` | Agent-autonomous knowledge-base construction — turn any directory of text/markdown into a queryable KB using Skardi CLI + SQLite + sqlite-vec + FTS5 (Postgres/pgvector and Lance supported as overrides). Handles prereq detection, model download, chunking, ingest, and hybrid (vector + full-text + RRF) retrieval end-to-end. Supports `candle`, `gguf`, and `remote_embed` UDFs. |

## Installation

### Claude Code

Copy the skill(s) into your personal skills directory so they're available across all projects:

```bash
# skardi-deploy-and-patterns (deployment + core concepts)
mkdir -p ~/.claude/skills/skardi-deploy-and-patterns
cp skardi_on_sealos/skill_sealos_k8s_deploy.md ~/.claude/skills/skardi-deploy-and-patterns/SKILL.md
cp -r skardi_on_sealos/templates ~/.claude/skills/skardi-deploy-and-patterns/templates

# auto_knowledge_base (agent-autonomous KB construction)
cp -r auto_knowledge_base ~/.claude/skills/auto_knowledge_base
```

Claude Code will automatically load the relevant skill when your request matches it — e.g. deployment/auth/pipelines for `skardi-deploy-and-patterns`, or "index these docs", "build a RAG", "make this folder searchable" for `auto_knowledge_base`. You can also invoke either directly:

```text
/skardi-deploy-and-patterns
/auto_knowledge_base
```

### Cursor

Copy the skill(s) into the project-level skills directory:

```bash
# skardi-deploy-and-patterns
mkdir -p .cursor/skills/skardi-deploy-and-patterns
cp skardi_on_sealos/skill_sealos_k8s_deploy.md .cursor/skills/skardi-deploy-and-patterns/SKILL.md
cp -r skardi_on_sealos/templates .cursor/skills/skardi-deploy-and-patterns/templates

# auto_knowledge_base
cp -r auto_knowledge_base .cursor/skills/auto_knowledge_base
```

### Other Agent Skills-compatible tools

The `SKILL.md` files follow the [Agent Skills open standard](https://agentskills.io/) and work with any compatible tool. Place the skill directory wherever your tool resolves personal or project skills.

## Bundled resources per skill

### `skardi_on_sealos/templates/`

Ready-to-use files referenced by `skardi-deploy-and-patterns`:

| File | Purpose |
|---|---|
| `skardi-sealos.yaml` | Kubernetes manifest for deploying Skardi on Sealos |
| `nextjs-sealos.yaml` | Kubernetes manifest for a Next.js frontend on Sealos |
| `Dockerfile.nextjs` | Dockerfile for a Next.js app |
| `nextjs-proxy.ts` | Next.js API route that proxies requests to Skardi |
| `docker-compose.yml` | Local development stack |
| `init-db.py` | Database initialisation script |

### `auto_knowledge_base/`

Executable scripts, YAML templates, and reference docs the skill invokes:

| Path | Purpose |
|---|---|
| `scripts/setup_kb.py` | Creates a KB workspace — checks prereqs, installs missing Python deps, resolves/downloads the embedding model, renders the ctx + pipeline YAMLs with absolute paths, and initialises the SQLite schema with FTS5 + `vec0` mirrors and triggers |
| `scripts/chunk_corpus.py` | Walks a corpus directory and emits NDJSON chunks (markdown-aware heading splitting with paragraph-packed overlap; falls back to plain-text paragraph packing) |
| `scripts/bulk_ingest.py` | Embeds and inserts every chunk in a single `skardi query` — reuses the rendered ingest pipeline so `candle` / `gguf` / `remote_embed` all work without per-row overhead |
| `assets/ctx.yaml.tpl`, `assets/aliases.yaml.tpl` | Skardi v0.3 ctx + aliases templates (rendered with absolute DB path at setup) |
| `assets/pipelines/*.yaml.tpl` | The four pipelines: `ingest`, `search_vector`, `search_fulltext`, `search_hybrid` (RRF over sqlite_knn + sqlite_fts) |
| `references/backends.md` | Trade-offs and migration notes for Postgres + pgvector and Lance overrides |
| `references/pipeline_patterns.md` | The exact SQL the skill generates, with commentary on RRF, the DataFusion INSERT-VALUES quirk, and how to extend the pipelines (metadata filters, updates, deletes) |
| `references/troubleshooting.md` | Symptom → fix lookup for common failures (missing features, FTS5 syntax errors, trigger mismatches, dim mismatches, remote-API issues) |
