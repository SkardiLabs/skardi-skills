---
name: skardi-deploy-and-patterns
description: >
  End-to-end reference for Skardi: core concepts (auth, SQLite, pipelines, CSRF),
  local Docker development, and deploying to Sealos via kubectl.
  Emphasis on auth setup and cross-origin client patterns applicable to any frontend.
type: feedback
---

# Skardi — Patterns & Sealos Deployment Guide

Templates live in `skardi_on_sealos/templates/` — reference them instead of writing from scratch.

---

## 1. Skardi core concepts

### Pipeline execution

```
POST /<pipeline-name>/execute
Content-Type: application/json
Authorization: Bearer <token>   ← required when auth is enabled

{ "param1": "value", "param2": null }   ← all declared params must be present; null = optional filter
```

Pipeline YAML format:
```yaml
metadata:
  name: pipeline-name   # must match the URL segment
  version: 1.0.0
  description: "..."

query: |
  SELECT col FROM table
  WHERE id = {id}           # named params with {curly braces}
    AND ({filter} IS NULL OR col = {filter})
```

DML (INSERT / UPDATE / DELETE) returns:
```json
{ "success": true, "data": [{ "count": 1 }], "rows": 1 }
```

### SQL dialect

Pipelines execute through **DataFusion**, not the underlying SQLite engine directly:

| SQLite syntax | DataFusion equivalent |
|---|---|
| `datetime('now')` | `CAST(now() AS VARCHAR)` |
| `strftime('%Y-%m-%d', col)` | `date_format(col, '%Y-%m-%d')` |

### Passing multiple pipelines

Pass a **directory** to `--pipeline` — Skardi loads every `.yaml` file it finds there. On Sealos/K8s, use a single ConfigMap with one data key per pipeline file (not individual `subPath` mounts):

```yaml
volumes:
  - name: pipelines
    configMap:
      name: my-app-pipelines
volumeMounts:
  - name: pipelines
    mountPath: /config/pipelines
    readOnly: true
```

---

## 2. SQLite data sources

### ctx.yaml format

Each table is a separate entry — multiple tables in one file are fine:

```yaml
data_sources:
  - name: "items"             # name used in pipeline SQL
    type: "sqlite"
    access_mode: "read_write" # omit or "read_only" for SELECT-only
    path: "/data/app.db"
    options:
      table: "items"
```

### The SQLite file must exist before Skardi starts

Skardi will fail with `"Data source file not found"` if the `.db` file is missing — it does not create the file or schema.

**Local:** create and run `init-db.py` once before `docker compose up` — see `templates/init-db.py` for a starting point (§5).
**Sealos/K8s:** use an init container — see `templates/skardi-auth-sealos.yaml`.

The `IF NOT EXISTS` guards make init idempotent — safe to re-run on every deploy.

### Cross-schema JOINs

When auth is enabled (§3), `auth.users` and `auth.sessions` are virtual tables joinable with your own SQLite tables:

```sql
SELECT t.*, au.email
FROM items t
JOIN auth.users au ON t.user_id = au.id
WHERE t.id = {id}
```

---

## 3. Auth system

### Enabling auth

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `AUTH_MODE` | yes | — | Set to `BETTER_AUTH_DIESEL_SQLITE` to enable |
| `AUTH_SECRET` | yes | — | Session signing secret — **minimum 32 characters** |
| `AUTH_DB_PATH` | no | `skardi_auth.db` | Path for the auth SQLite (**auto-created** by Skardi) |
| `AUTH_BASE_URL` | no | `http://localhost:{PORT}` | Public base URL — used for CSRF origin validation |

On Sealos, store `AUTH_SECRET` as a K8s Secret and reference it in the Deployment:
```bash
kubectl create secret generic skardi-auth-secret \
  --from-literal=AUTH_SECRET='your-secret-at-least-32-chars!' \
  -n <namespace>
```

### Auth endpoints

| Endpoint | Method | Notes |
|---|---|---|
| `/api/auth/sign-up/email` | POST | `{ email, password, name }` — auto-signs-in |
| `/api/auth/sign-in/email` | POST | `{ email, password }` |
| `/api/auth/get-session` | GET | Bearer token in `Authorization` header |
| `/api/auth/sign-out` | POST | Bearer token in `Authorization` header |

Sign-in and sign-up both return the session token in the response body:
```json
{ "token": "<session-token>", "user": { "id": "...", "email": "..." } }
```

Pass it as `Authorization: Bearer <token>` for all pipeline calls. A missing/expired token returns `401`.

### auth.users and auth.sessions virtual tables

```sql
-- auth.users columns: id, name, email, email_verified, username, role, banned, created_at, updated_at
-- auth.sessions columns: id, token, user_id, expires_at, created_at, ip_address, user_agent
SELECT id, email FROM auth.users WHERE id = {user_id};
```

### Role management (AdminPlugin not loaded)

The current image only enables `EmailPasswordPlugin`. `auth.users.role` is read-only from pipelines.

**Pattern:** maintain a separate `user_roles` table and JOIN it with `auth.users`:

```sql
-- Assign role on signup — call this pipeline immediately after sign-up
INSERT INTO user_roles (user_id, role)
SELECT {user_id},
  CASE WHEN (SELECT COUNT(*) FROM auth.users) = 1 THEN 'admin' ELSE 'user' END;

-- Read user + role
SELECT au.id, au.email, COALESCE(ur.role, 'user') AS role
FROM auth.users au
LEFT JOIN user_roles ur ON au.id = ur.user_id
WHERE au.id = {user_id};
```

---

## 4. CSRF and cross-origin clients

### What happens

Skardi validates the `Origin` (or `Referer`) header on all state-changing requests. Only the origin matching `AUTH_BASE_URL` is trusted. A mismatched origin returns:

```json
{ "code": "CSRF_ERROR", "message": "Cross-site request blocked" }
```

This affects any frontend on a different origin — `localhost:3000` calling Skardi on `localhost:18080`, or separate subdomains on Sealos.

### Why rewrites don't help

Next.js `rewrites()`, nginx `proxy_pass`, etc. forward the browser-injected `Origin` header unchanged. Skardi still sees the original browser origin and rejects it.

### The fix: server-side proxy that strips Origin

The CSRF middleware allows requests with **no** `Origin` header (treats them as same-origin / non-browser clients). A server-side proxy that strips `origin`, `referer`, and `host` before forwarding solves it cleanly.

**→ Template: `templates/nextjs-proxy.ts`** — drop-in Next.js Route Handler at `src/app/api/skardi/[...path]/route.ts`.

The same principle applies to any framework — Express, Fastify, Go, etc.

Env vars:
```
NEXT_PUBLIC_SKARDI_URL=https://<app-domain>/api/skardi   # browser uses this (hits the proxy)
SKARDI_UPSTREAM_URL=http://<skardi-service>:8080          # server proxy uses this (never exposed)
```

On Sealos, set `SKARDI_UPSTREAM_URL` to the **internal K8s service URL** — avoids the ingress round-trip:
```yaml
- name: SKARDI_UPSTREAM_URL
  value: http://skardi:8080   # K8s Service name + port
```

---

## 5. Local development with Docker

**→ Template: `templates/docker-compose.yml`**

Key points:
- `AUTH_DB_PATH` is **auto-created** by Skardi — do not pre-create it.
- Your app `.db` file must **exist before** `docker compose up`. **You must write an `init-db.py` tailored to your own schema** — generate it on the fly based on the tables in your `ctx.yaml`. `templates/init-db.py` is only a structural example; do not use it as-is. Adapt `DB_PATH` and the `executescript()` to your actual tables, then run it once:
  ```bash
  python3 init-db.py
  ```
- Pipelines are loaded at startup only — restart after editing YAMLs.
- `platform: linux/amd64` avoids silent architecture mismatches on ARM hosts.

---

## 6. Setting up kubectl with Sealos

Download kubeconfig from the Sealos dashboard (Account → kubeconfig):

```bash
cp ~/Downloads/kubeconfig.yaml ~/.kube/sealos-config.yaml
export KUBECONFIG=~/.kube/sealos-config.yaml
```

Your namespace is embedded in the kubeconfig:
```bash
kubectl config view --minify -o jsonpath='{.contexts[0].context.namespace}'
# e.g. ns-bg7m761t
```

`export KUBECONFIG=...` does **not** persist across Bash tool calls — use `KUBECONFIG=~/.kube/sealos-config.yaml kubectl ...` inline or re-export each call.

---

## 7. Sealos ingress and domain rules

- `*.usw-1.sealos.io` — **forbidden** for user-created ingresses (system only)
- `*.usw-1.sealos.app` — **allowed**; enable the subdomain from the Sealos dashboard first
- TLS secret `wildcard-cert` works for both — no namespace-local TLS secret needed
- Use `spec.ingressClassName: nginx` — the old annotation is deprecated

Resolve `CLOUD_DOMAIN` from existing ingresses:
```bash
CLOUD_DOMAIN=$(kubectl get ingress -n $NS \
  -o jsonpath='{.items[0].spec.rules[0].host}' 2>/dev/null | cut -d. -f2-)
```

---

## 8. Deploying Skardi to Sealos

**→ Template: `templates/skardi-sealos.yaml`** — includes PVC, init container, auth env vars, pipelines ConfigMap, Service, and Ingress.

Step 1 — create the auth secret:
```bash
kubectl create secret generic skardi-auth-secret \
  --from-literal=AUTH_SECRET='<32+-char-secret>' -n $NS
```

Step 2 — fill placeholders and apply (current image tag: `main-test-img-20260408184213`):
```bash
sed \
  -e "s/<IMAGE_TAG>/$IMAGE_TAG/g" \
  -e "s/<YOUR_NAMESPACE>/$NS/g" \
  -e "s/<YOUR_SUBDOMAIN>/$SUBDOMAIN/g" \
  -e "s/<SEALOS_CLOUD_DOMAIN>/$CLOUD_DOMAIN/g" \
  templates/skardi-sealos.yaml | kubectl apply -f -
```

Step 3 — verify:
```bash
kubectl rollout status deployment/skardi -n $NS
curl https://$SUBDOMAIN.$CLOUD_DOMAIN/health   # expect 200 OK
```

**PodSecurity gotcha:** Sealos enforces `restricted:v1.25`. Apply this to each container to silence warnings:
```yaml
securityContext:
  allowPrivilegeEscalation: false
  runAsNonRoot: true
  capabilities:
    drop: ["ALL"]
  seccompProfile:
    type: RuntimeDefault
```

---

## 9. Deploying a Node.js frontend to Sealos

**→ Templates: `templates/Dockerfile.nextjs`** and **`templates/nextjs-sealos.yaml`**

Requires `output: 'standalone'` in `next.config.ts`. Build and push:
```bash
docker build -t ghcr.io/<you>/<app>:latest .
docker push ghcr.io/<you>/<app>:latest
```

The K8s manifest sets `NEXT_PUBLIC_SKARDI_URL` (browser → proxy on app domain) and `SKARDI_UPSTREAM_URL` (server → internal K8s service). The Route Handler proxy in §4 handles the CSRF stripping.

---

## 10. Deploying a static frontend via ConfigMaps + nginx

For pure SPAs (no server-side logic). Use `public.ecr.aws/nginx/nginx:alpine` (avoids Docker Hub rate limits).

nginx config (listens on 8080 for unprivileged):
```nginx
server {
    listen 8080;
    root /usr/share/nginx/html;
    index index.html;
    location / { try_files $uri $uri/ /index.html; }
}
```

Updating after a new build (hash changes):
```bash
kubectl create configmap <name>-assets \
  --from-file=<new-hash>.js=dist/assets/<new-hash>.js \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl patch deployment <name> --type=json \
  -p='[{"op":"replace","path":"/spec/template/spec/containers/0/volumeMounts/1/mountPath",
        "value":"/usr/share/nginx/html/assets/<new-hash>.js"},
       {"op":"replace","path":"/spec/template/spec/containers/0/volumeMounts/1/subPath",
        "value":"<new-hash>.js"}]'
```

---

## 11. Deploying a Sealos Template manually

Sealos template variables (`${{ defaults.app_name }}`, `${{ SEALOS_CLOUD_DOMAIN }}`, etc.) can be resolved with `sed` when bypassing the template engine (see §7 for resolving `CLOUD_DOMAIN`).

---

## 12. Patching a ConfigMap and restarting

```bash
kubectl patch configmap <name> -n <ns> \
  --type=merge \
  -p='{"data":{"pipeline.yaml":"...new content..."}}'

# ConfigMap file mounts are cached — must restart pod to pick up changes
kubectl rollout restart deployment/<name> -n <ns>
```
