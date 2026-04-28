# Runtime selection: where skardi-server actually runs

`start_server.py --runtime` picks one of three execution targets for
skardi-server. The three are functionally equivalent at the HTTP level
— same `/ingest/execute`, same `/search-*/execute` — but they have very
different operational shapes and the right one depends on where the
agent that will *call* the RAG service ultimately lives.

| Runtime | When to pick it | What it gives you | What it costs |
|---|---|---|---|
| `local-process` (default) | Single-laptop dev, single-agent loops, prototyping | Zero infra: a host process started with `nohup`, logs at `<workspace>/server.log`, killed by sending SIGTERM to a pid file | Skardi binary must be on PATH (`cargo install --features candle/gguf/remote-embed`) or pass `--skardi-source` so we can fall back to `cargo run`. Tied to whatever Python/OpenSSL/libsqlite the host has. |
| `docker` | Shipping RAG to a teammate, isolating the server from the host's library versions, "production single-box" | One pulled image (`ghcr.io/skardilabs/skardi/skardi-server-embedding:latest`), no Rust toolchain needed on the host, full container isolation, identical lifecycle on macOS / Linux | Docker Desktop / engine on the host. Connection-string gotcha: when PG runs on the host, use `host.docker.internal` (auto-mapped by `--add-host=host.docker.internal:host-gateway` in start_server.py, so it works on Linux too). |
| `kubernetes` | Agent itself already runs in a cluster, multi-replica retrieval, shared service across teams | Deployment + Service + ConfigMap + Secret rendered into `<workspace>/k8s/`, optional `--apply` and `--port-forward`, in-cluster service DNS for collocated agents | A reachable cluster (kubectl context). PG must be reachable from inside the cluster — you handle that via Service / Endpoints / external IPs. |

## How embedding works in all three runtimes

**Embedding always happens client-side, on the host running this skill,
via the `skardi` CLI** ([scripts/embed.py](../scripts/embed.py) wraps it).
This is a deliberate decoupling: the published `skardi-server-embedding`
images currently expose `pg_knn` / `pg_fts` (the storage-side table
functions) but do NOT expose the `candle` / `gguf` / `remote_embed`
scalar UDFs at runtime, despite the `embedding` build feature being
enabled. By computing vectors on the agent side — where the user's
locally-installed `skardi` CLI does have the matching feature flags —
the same templates run unchanged across every runtime. As an
architecture choice it has its own merits: the embedder is swappable
without rebuilding the server, and multiple agents can point at one
server while choosing their own embedders.

This means: every ingest call posts `{doc_id, source, chunk_idx,
content, embedding: [...]}` (vector pre-computed); every search call
posts `{query_vec: [...], ...}` after a separate embed step.

`http_ingest.py` does the embed-then-POST automatically. For ad-hoc
search, the agent can do:

```bash
QVEC=$(python SKILL_DIR/scripts/embed.py --workspace ./rag --text "your question")
curl -X POST http://localhost:8080/search-hybrid/execute \
  -H 'Content-Type: application/json' \
  -d "{\"query_vec\": $QVEC, \"text_query\": \"keywords\", \"vector_weight\":0.5, \"text_weight\":0.5, \"limit\":5}"
```

## Local-process runtime

```bash
python SKILL_DIR/scripts/start_server.py \
  --workspace ./rag --runtime local-process --port 8080
```

`start_server.py` prefers a `skardi-server` binary on PATH. If the
binary is missing, pass `--skardi-source <path-to-skardi-clone>` and we
fall back to `cargo run --release --bin skardi-server --features
<udf>`. Logs land at `<workspace>/server.log`; the pid is in
`<workspace>/server.pid` and gets killed by `stop_server.py`.

Important: the published `skardi-server` release images don't expose
the `candle`/`gguf`/`remote_embed` UDFs. For server-side embedding you'd
need to build the binary yourself. Since we embed client-side, a
**release binary built with no embedding features is fine** for the
server — the user only needs the embedding features compiled into the
`skardi` CLI on their host.

## Docker runtime

```bash
python SKILL_DIR/scripts/start_server.py \
  --workspace ./rag --runtime docker --port 8080
```

Pulls and runs `ghcr.io/skardilabs/skardi/skardi-server-embedding:latest`
under a name like `skardi-rag-<workspace-hash>`. Mounts the rendered
workspace at the same absolute path inside the container so paths in
ctx.yaml / pipelines resolve unchanged. Forwards `PG_USER`,
`PG_PASSWORD`, and (for `remote_embed`) the relevant API keys. Adds
`--add-host=host.docker.internal:host-gateway` so connection strings
that say `host.docker.internal:5432` work on Linux as they do on macOS.

**Connection-string gotcha**: when Postgres runs on the host (the most
common dev setup), the container can't reach `localhost:5432` — that's
the container's own loopback. Either:

- Re-render the workspace with `host.docker.internal:<port>` in the
  connection string (cleanest), or
- Run Docker with `--network host` (Linux only; doesn't work on macOS
  Desktop)

The skill prints a clear error if PG is unreachable from inside the
container — surface it to the user rather than guessing the right
hostname for their topology.

`stop_server.py` knows about the docker runtime via
`<workspace>/server.runtime` and calls `docker rm -f <container_name>`
on stop. Logs are `docker logs -f` streamed into `<workspace>/server.log`.

## Kubernetes runtime

```bash
# Render manifests only (no cluster changes)
python SKILL_DIR/scripts/start_server.py \
  --workspace ./rag --runtime kubernetes \
  --port 8080 --k8s-namespace skardi-rag

# After review, apply + port-forward in one shot
python SKILL_DIR/scripts/start_server.py \
  --workspace ./rag --runtime kubernetes \
  --port 8080 --k8s-namespace skardi-rag \
  --apply --port-forward
```

The skill never auto-applies cluster changes without `--apply`. The
two-step "render then apply" matches the way most teams treat
Kubernetes — review the YAML, then deploy. Without `--apply`, the
manifests are written to `<workspace>/k8s/` and you can `kubectl apply
-f` yourself, paste them into a Helm chart, or open a PR.

### Manifests rendered

Five files, applied in this numeric order:

| File | Purpose |
|---|---|
| `00-namespace.yaml` | The namespace (defaults to `skardi-rag`; override with `--k8s-namespace`). |
| `10-configmap.yaml` | Holds `ctx.yaml` and all four pipeline YAMLs as keys. The Deployment mounts each one at `/config/<name>`. |
| `20-secret.yaml` | Carries the credentials this skill needs at runtime (`PG_USER`, `PG_PASSWORD`, and any embedding-API keys when remote_embed is in use). Built from `os.environ` at render time — make sure those vars are exported before running `setup_rag.py` / `start_server.py`. |
| `30-deployment.yaml` | Single-replica deployment using `ghcr.io/skardilabs/skardi/skardi-server-embedding:latest`, `envFrom: secretRef`, readiness/liveness on `/health`, conservative 512 Mi / 200 m requests. |
| `40-service.yaml` | ClusterIP Service exposing port 8080. |

### Connectivity expectations

- **PG reachability from cluster**: the skill does not touch your data
  source. Make sure the connection string in `ctx.yaml` (which gets
  baked into the ConfigMap) resolves and is reachable from cluster
  pods. Common setups: PG as a sibling Deployment + Service in the same
  namespace; an Endpoints object pointing at an external IP; a
  ServiceEntry / mesh policy if you're on Istio. The skill's blast
  radius rule applies as much in k8s as on a laptop — we won't
  provision your database.

- **Agent reachability to the service**: if your agent already runs in
  the cluster, it talks to `skardi-rag.skardi-rag.svc.cluster.local:8080`
  in-cluster. If your agent is on a laptop and you want to debug
  against a live cluster, pass `--port-forward` and the script wires up
  `kubectl port-forward svc/skardi-rag <local>:8080` for you (pid in
  `server.pid`, killed by `stop_server.py`).

### Embedding-on-agent caveat

Client-side embedding means the agent — wherever it runs — calls the
host `skardi` CLI to compute vectors. If the agent is in-cluster, you
have a choice: run the agent in a container that has the `skardi` CLI
binary baked in (with `--features candle`), or use `remote_embed` with
an API key in the agent's environment so no local model files are
needed. The skill leans on whatever the agent's environment offers —
we don't push embeddings into the server pod.

### Cleanup

```bash
# Stop the local port-forward, leave cluster objects in place
python SKILL_DIR/scripts/stop_server.py --workspace ./rag

# Stop port-forward AND kubectl delete -f <workspace>/k8s/
python SKILL_DIR/scripts/stop_server.py --workspace ./rag --delete
```

`--delete` is opt-in because you may want the deployment to keep
running for other agents; stopping the agent's port-forward shouldn't
implicitly tear down a service the team relies on.

## Picking a runtime, by user signal

The user's words are usually enough:

- **"set up RAG locally"**, **"on my laptop"**, **"for my dev loop"** → `local-process`.
- **"docker"**, **"production single-box"**, **"can you ship me an image"** → `docker`.
- **"on our cluster"**, **"in kubernetes"**, **"deploy to k8s"**, **"alongside our other services"** → `kubernetes`.

When ambiguous, ask: *"Where will the agent calling this RAG service ultimately run?"* If they say "on my machine", local-process. If they say "in our infra", lean kubernetes (`docker` is rarely the right end state — usually a stepping stone).
