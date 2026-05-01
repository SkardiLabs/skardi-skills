# Runtime selection: where skardi-server actually runs

`start_server.py --runtime` picks one of three execution targets for skardi-server. The three are functionally equivalent at the HTTP level — same `/ingest-chunked/execute`, same `/search-*/execute` — but they have very different operational shapes and the right one depends on where the agent that will *call* the RAG service ultimately lives.

| Runtime | When to pick it | What it gives you | What it costs |
|---|---|---|---|
| `local-process` (default) | Single-laptop dev, single-agent loops, prototyping | Zero infra: a host process started with `nohup`, logs at `<workspace>/server.log`, killed by sending SIGTERM to a pid file | Skardi binary must be on PATH (build with `--features rag`, which bundles chunk + candle) or pass `--skardi-source` so we can fall back to `cargo run`. Tied to whatever Python/OpenSSL/libsqlite the host has. |
| `docker` | Shipping RAG to a teammate, isolating the server from the host's library versions, "production single-box" | One pulled image (`ghcr.io/skardilabs/skardi/skardi-server-rag:latest` — chunk + candle + gguf + remote-embed bundled), no Rust toolchain needed on the host, full container isolation, identical lifecycle on macOS / Linux | Docker Desktop / engine on the host. Connection-string gotcha: when PG runs on the host, use `host.docker.internal` (auto-mapped by `--add-host=host.docker.internal:host-gateway` in start_server.py, so it works on Linux too). |
| `kubernetes` | Agent itself already runs in a cluster, multi-replica retrieval, shared service across teams | Deployment + Service + ConfigMap + Secret rendered into `<workspace>/k8s/`, optional `--apply` and `--port-forward`, in-cluster service DNS for collocated agents | A reachable cluster (kubectl context). PG must be reachable from inside the cluster — you handle that via Service / Endpoints / external IPs. |

## Embedding and chunking happen on the server (Skardi 0.4.0+)

This is a change from earlier versions of the skill: as of Skardi 0.4.0 + the `skardi-server-rag` image, both `chunk()` and the embedding UDFs are registered server-side. The rendered pipelines call them inline:

- `/ingest-chunked/execute` takes raw document content and runs `INSERT ... SELECT ... UNNEST(chunk('markdown', {content}, ...)) ... candle(...)` in one statement.
- `/search-vector/execute` and `/search-hybrid/execute` take the question as plain text and run `pg_knn(..., candle('<model>', {query}), ...)` inline.

So the agent's HTTP body for ingest is just `{doc_id, source, content, chunk_size, overlap}`, and for search just `{query, ...}`. No client-side chunker, no client-side embedder, no precomputed `query_vec` parameter.

If you need to run on the older `skardi-server-embedding` image (which doesn't register the UDFs server-side) or pre-0.4.0 Skardi, see the fallback note in `SKILL.md` — the gist is to copy the pre-0.4.0 templates from git history and reintroduce a client-side embed step.

## Local-process runtime

```bash
python SKILL_DIR/scripts/start_server.py \
  --workspace ./rag --runtime local-process --port 8080
```

`start_server.py` prefers a `skardi-server` binary on PATH. If the binary is missing, pass `--skardi-source <path-to-skardi-clone>` and we fall back to `cargo run --release --bin skardi-server --features rag`. Logs land at `<workspace>/server.log`; the pid is in `<workspace>/server.pid` and gets killed by `stop_server.py`.

The release binary must be built with the matching feature flags. `--features rag` is the easiest umbrella — it bundles chunking + candle + gguf + remote-embed (well, the embedding UDFs you compiled in). For à la carte builds, at minimum you need `--features chunking` plus the embedding feature you chose.

## Docker runtime

```bash
python SKILL_DIR/scripts/start_server.py \
  --workspace ./rag --runtime docker --port 8080
```

Pulls and runs `ghcr.io/skardilabs/skardi/skardi-server-rag:latest` under a name like `skardi-rag-<workspace-hash>`. Mounts the rendered workspace at the same absolute path inside the container so paths in ctx.yaml / pipelines resolve unchanged. Forwards `PG_USER`, `PG_PASSWORD`, and (for `remote_embed`) the relevant API keys. Adds `--add-host=host.docker.internal:host-gateway` so connection strings that say `host.docker.internal:5432` work on Linux as they do on macOS.

For `candle` / `gguf`, the container also bind-mounts the model directory at its host absolute path so `candle('<host-path>', ...)` resolves identically in-container.

**Connection-string gotcha**: when Postgres runs on the host (the most common dev setup), the container can't reach `localhost:5432` — that's the container's own loopback. Either:

- Re-render the workspace with `host.docker.internal:<port>` in the connection string (cleanest), or
- Run Docker with `--network host` (Linux only; doesn't work on macOS Desktop)

The skill prints a clear error if PG is unreachable from inside the container — surface it to the user rather than guessing the right hostname for their topology.

`stop_server.py` knows about the docker runtime via `<workspace>/server.runtime` and calls `docker rm -f <container_name>` on stop. Logs are `docker logs -f` streamed into `<workspace>/server.log`.

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

The skill never auto-applies cluster changes without `--apply`. The two-step "render then apply" matches the way most teams treat Kubernetes — review the YAML, then deploy. Without `--apply`, the manifests are written to `<workspace>/k8s/` and you can `kubectl apply -f` yourself, paste them into a Helm chart, or open a PR.

### Manifests rendered

Five files, applied in this numeric order:

| File | Purpose |
|---|---|
| `00-namespace.yaml` | The namespace (defaults to `skardi-rag`; override with `--k8s-namespace`). |
| `10-configmap.yaml` | Holds `ctx.yaml` and all five pipeline YAMLs (`ingest`, `ingest_chunked`, `search_vector`, `search_fulltext`, `search_hybrid`) as keys. The Deployment mounts each one at `/config/<name>`. The auto-discovered `semantics.yaml` would also live here if you choose to mount it. |
| `20-secret.yaml` | Carries the credentials this skill needs at runtime (`PG_USER`, `PG_PASSWORD`, and any embedding-API keys when remote_embed is in use). Built from `os.environ` at render time — make sure those vars are exported before running `setup_rag.py` / `start_server.py`. |
| `30-deployment.yaml` | Single-replica deployment using `ghcr.io/skardilabs/skardi/skardi-server-rag:latest`, `envFrom: secretRef`, readiness/liveness on `/health`, conservative 512 Mi / 200 m requests. |
| `40-service.yaml` | ClusterIP Service exposing port 8080. |

### Connectivity expectations

- **PG reachability from cluster**: the skill does not touch your data source. Make sure the connection string in `ctx.yaml` (which gets baked into the ConfigMap) resolves and is reachable from cluster pods. Common setups: PG as a sibling Deployment + Service in the same namespace; an Endpoints object pointing at an external IP; a ServiceEntry / mesh policy if you're on Istio. The skill's blast radius rule applies as much in k8s as on a laptop — we won't provision your database.

- **Agent reachability to the service**: if your agent already runs in the cluster, it talks to `skardi-rag.skardi-rag.svc.cluster.local:8080` in-cluster. If your agent is on a laptop and you want to debug against a live cluster, pass `--port-forward` and the script wires up `kubectl port-forward svc/skardi-rag <local>:8080` for you (pid in `server.pid`, killed by `stop_server.py`).

### Model files in-cluster

For `--embedding-udf candle` or `gguf`, the rendered ctx.yaml references the model dir at its **host** absolute path — which won't exist inside a pod. Two options:

1. **Switch to `remote_embed`** (the skill's recommended k8s default). No model files; the relevant API key flows through the rendered Secret.
2. **Build a derived image** that copies the model dir in at the same absolute path. The base `skardi-server-rag` image plus a single `COPY` instruction is enough; reference it via `--docker-image ghcr.io/<your-org>/skardi-server-rag-with-bge:<tag>`.

`start_server.py` warns about this when you choose `--runtime kubernetes` with a local embedding UDF — read the warning rather than dismissing it.

### Cleanup

```bash
# Stop the local port-forward, leave cluster objects in place
python SKILL_DIR/scripts/stop_server.py --workspace ./rag

# Stop port-forward AND kubectl delete -f <workspace>/k8s/
python SKILL_DIR/scripts/stop_server.py --workspace ./rag --delete
```

`--delete` is opt-in because you may want the deployment to keep running for other agents; stopping the agent's port-forward shouldn't implicitly tear down a service the team relies on.

## Picking a runtime, by user signal

The user's words are usually enough:

- **"set up RAG locally"**, **"on my laptop"**, **"for my dev loop"** → `local-process`.
- **"docker"**, **"production single-box"**, **"can you ship me an image"** → `docker`.
- **"on our cluster"**, **"in kubernetes"**, **"deploy to k8s"**, **"alongside our other services"** → `kubernetes`.

When ambiguous, ask: *"Where will the agent calling this RAG service ultimately run?"* If they say "on my machine", local-process. If they say "in our infra", lean kubernetes (`docker` is rarely the right end state — usually a stepping stone).
