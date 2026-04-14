# skardi-skills

Agent Skills for working with [Skardi](https://github.com/skardi/skardi) — a backend engine to provide SQL pipelines as http endpoints. These skills give your AI coding agent (Claude Code, Cursor, or any [Agent Skills](https://agentskills.io/)-compatible tool) deep knowledge of Skardi's patterns so you don't have to re-explain them each session.

Check out our demo [here](https://www.youtube.com/watch?v=Cx5jG0OtUuk).

## Available skills

| Directory | Skill name | What it covers |
|---|---|---|
| `skardi_on_sealos/` | `skardi-deploy-and-patterns` | Core Skardi concepts (auth, pipelines, CSRF, DataFusion SQL dialect) + deploying to [Sealos](https://sealos.io/) via kubectl |

## Installation

### Claude Code

Copy the skill into your personal skills directory so it's available across all projects:

```bash
mkdir -p ~/.claude/skills/skardi-deploy-and-patterns
cp skardi_on_sealos/skill_sealos_k8s_deploy.md ~/.claude/skills/skardi-deploy-and-patterns/SKILL.md
cp -r skardi_on_sealos/templates ~/.claude/skills/skardi-deploy-and-patterns/templates
```

Claude Code will automatically load the skill when your request is relevant (e.g. deploying to Sealos, writing pipelines, setting up auth). You can also invoke it directly:

```text
/skardi-deploy-and-patterns
```

### Cursor

Copy the skill into the project-level skills directory:

```bash
mkdir -p .cursor/skills/skardi-deploy-and-patterns
cp skardi_on_sealos/skill_sealos_k8s_deploy.md .cursor/skills/skardi-deploy-and-patterns/SKILL.md
cp -r skardi_on_sealos/templates .cursor/skills/skardi-deploy-and-patterns/templates
```

### Other Agent Skills-compatible tools

The `SKILL.md` files follow the [Agent Skills open standard](https://agentskills.io/) and work with any compatible tool. Place the skill directory wherever your tool resolves personal or project skills.

## Templates

The `skardi_on_sealos/templates/` directory contains ready-to-use files referenced by the skill:

| File | Purpose |
|---|---|
| `skardi-sealos.yaml` | Kubernetes manifest for deploying Skardi on Sealos |
| `nextjs-sealos.yaml` | Kubernetes manifest for a Next.js frontend on Sealos |
| `Dockerfile.nextjs` | Dockerfile for a Next.js app |
| `nextjs-proxy.ts` | Next.js API route that proxies requests to Skardi |
| `docker-compose.yml` | Local development stack |
| `init-db.py` | Database initialisation script |
