---
name: skardi-deploy-and-patterns
description: >
  End-to-end reference for Skardi: core concepts (auth, SQLite, pipelines, CSRF),
  local Docker development, and deploying to Sealos via kubectl.
  Emphasis on auth setup and cross-origin client patterns applicable to any frontend.
type: feedback
---

# Skardi — Patterns & Sealos Deployment Guide

---

## 1. Skardi core concepts

### Pipeline execution

All pipelines are invoked via a single endpoint:

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

Pipelines execute through **DataFusion**, not the underlying SQLite engine directly. Use DataFusion's dialect:

| SQLite syntax | DataFusion equivalent |
|---|---|
| `datetime('now')` | `CAST(now() AS VARCHAR)` |
| `strftime('%Y-%m-%d', col)` | `date_format(col, '%Y-%m-%d')` |

### Passing multiple pipelines

Pass a **directory** to `--pipeline` — Skardi loads every `.yaml` file it finds there:

```yaml
args:
  - --pipeline
  - /config/pipelines/    # loads all .yaml files in the folder
```

On Sealos/K8s, mount all pipeline files into a directory using a single ConfigMap (not individual `subPath` mounts for each file):

```yaml
volumes:
  - name: pipelines
    configMap:
      name: my-app-pipelines   # ConfigMap has one data key per pipeline file
volumeMounts:
  - name: pipelines
    mountPath: /config/pipelines
    readOnly: true
```

---

## 2. SQLite data sources

### ctx.yaml format

Each table must be registered as a separate data source entry:

```yaml
data_sources:
  - name: "items"             # name used in pipeline SQL
    type: "sqlite"
    access_mode: "read_write" # omit or "read_only" for SELECT-only
    path: "/data/app.db"
    description: "Items table"
    options:
      table: "items"          # actual SQLite table name

  - name: "tags"
    type: "sqlite"
    access_mode: "read_write"
    path: "/data/app.db"      # same file — fine to repeat
    options:
      table: "tags"
```

### The SQLite file must exist before Skardi starts

Skardi registers data sources at boot and will fail with `"Data source file not found"` if the `.db` file is missing. It does **not** create the file or schema automatically.

**Local (Docker):** create the file once before `docker compose up`:
```python
# init-db.py
import sqlite3, os
db = os.path.join(os.path.dirname(__file__), 'data', 'app.db')
os.makedirs(os.path.dirname(db), exist_ok=True)
conn = sqlite3.connect(db)
conn.executescript('''
CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY AUTOINCREMENT, ...);
CREATE TABLE IF NOT EXISTS tags  (id INTEGER PRIMARY KEY AUTOINCREMENT, ...);
''')
conn.commit(); conn.close()
```

**Sealos/K8s (PVC):** use an init container that runs before Skardi:
```yaml
initContainers:
  - name: db-init
    image: python:3.12-slim
    command:
      - python3
      - -c
      - |
        import sqlite3
        conn = sqlite3.connect('/data/app.db')
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS items (...);
        """)
        conn.commit(); conn.close()
    volumeMounts:
      - name: data
        mountPath: /data
    securityContext:
      allowPrivilegeEscalation: false
      runAsNonRoot: true
      capabilities:
        drop: ["ALL"]
      seccompProfile:
        type: RuntimeDefault
```

The `IF NOT EXISTS` guards make both approaches idempotent — safe to re-run on every deploy.

### Cross-schema JOINs

When auth is enabled (see §3), `auth.users` and `auth.sessions` are registered as virtual tables. They can be freely joined with your own SQLite tables in pipeline queries because all sources share the same DataFusion session context:

```sql
SELECT t.*, au.email
FROM items t
JOIN auth.users au ON t.user_id = au.id
WHERE t.id = {id}
```

---

## 3. Auth system

### Enabling auth

Auth is opt-in, controlled entirely by environment variables:

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `AUTH_MODE` | yes | — | Set to `BETTER_AUTH_DIESEL_SQLITE` to enable |
| `AUTH_SECRET` | yes | — | Session signing secret — **minimum 32 characters** |
| `AUTH_DB_PATH` | no | `skardi_auth.db` | Path for the auth SQLite (auto-created by Skardi) |
| `AUTH_BASE_URL` | no | `http://localhost:{PORT}` | Public base URL — used for CSRF origin validation |

On Sealos, store `AUTH_SECRET` as a K8s Secret:
```bash
kubectl create secret generic skardi-auth-secret \
  --from-literal=AUTH_SECRET='your-secret-at-least-32-chars!' \
  -n <namespace>
```
Reference it in the Deployment:
```yaml
env:
  - name: AUTH_SECRET
    valueFrom:
      secretKeyRef:
        name: skardi-auth-secret
        key: AUTH_SECRET
```

### Auth endpoints

All auth routes live under `/api/auth/`:

| Endpoint | Method | Body / Notes |
|---|---|---|
| `/api/auth/sign-up/email` | POST | `{ email, password, name }` |
| `/api/auth/sign-in/email` | POST | `{ email, password }` |
| `/api/auth/get-session` | GET | Bearer token in `Authorization` header |
| `/api/auth/sign-out` | POST | Bearer token in `Authorization` header |

**Sign-in and sign-up both return the session token in the response body** — read it from there rather than parsing cookies:
```json
{ "token": "<session-token>", "user": { "id": "...", "email": "..." } }
```

Sign-up auto-signs-in by default (`auto_sign_in: true`), so `token` is always present unless explicitly disabled.

### Calling authenticated pipelines

Pass the token as a Bearer header:
```
Authorization: Bearer <session-token>
```

Skardi also accepts a session cookie as fallback, but Bearer is simpler for any cross-origin or non-browser client.

When auth is enabled, **all** pipeline endpoints require a valid session. A missing or expired token returns `401`.

### auth.users and auth.sessions virtual tables

Two read-only virtual tables are automatically available in pipeline queries when auth is enabled:

```sql
-- auth.users columns:
-- id, name, email, email_verified, username, role, banned, created_at, updated_at
SELECT id, email, role FROM auth.users WHERE id = {user_id};

-- auth.sessions columns:
-- id, token, user_id, expires_at, created_at, ip_address, user_agent
SELECT user_id FROM auth.sessions WHERE token = {token};
```

These reflect live state on every query — no caching.

### Role management (AdminPlugin not loaded)

The current Skardi image only enables `EmailPasswordPlugin`. The `AdminPlugin` (which would expose `/api/auth/admin/set-role`) is **not** loaded. `auth.users.role` is read-only from pipelines.

**Pattern:** maintain a separate `user_roles` table in your own SQLite and JOIN it with `auth.users`:

```sql
-- Schema
CREATE TABLE user_roles (user_id TEXT PRIMARY KEY, role TEXT NOT NULL DEFAULT 'user');

-- Assign role on signup (pipeline: call immediately after sign-up)
INSERT INTO user_roles (user_id, role)
SELECT {user_id},
  CASE WHEN (SELECT COUNT(*) FROM auth.users) = 1 THEN 'admin' ELSE 'user' END;

-- Read user + role
SELECT au.id, au.email, COALESCE(ur.role, 'user') AS role
FROM auth.users au
LEFT JOIN user_roles ur ON au.id = ur.user_id
WHERE au.id = {user_id};

-- Update role (admin only — enforce on client)
UPDATE user_roles SET role = {role} WHERE user_id = {user_id};
```

---

## 4. CSRF and cross-origin clients

### What happens

Skardi's auth middleware validates the `Origin` (or `Referer`) header on all state-changing requests (POST/PUT/DELETE/PATCH). Only the origin matching `AUTH_BASE_URL` is trusted by default. Any browser making cross-origin requests — e.g. a frontend on `localhost:3000` calling Skardi on `localhost:18080`, or a frontend on `app.sealos.app` calling Skardi on `skardi.sealos.app` — will receive:

```json
{ "code": "CSRF_ERROR", "message": "Cross-site request blocked" }
```

### Why rewrites don't help

Framework-level proxy rewrites (Next.js `rewrites()`, nginx `proxy_pass`, etc.) forward the request **including** the browser-injected `Origin` header unchanged. Skardi still sees the original browser origin and rejects it.

### The fix: strip Origin server-side

The CSRF middleware explicitly allows requests with **no** `Origin` header:
> *"If no Origin/Referer header is present, allow the request. This handles same-origin requests from older browsers and non-browser clients (curl, SDKs, etc.)."*

So the solution is a server-side proxy that strips `Origin` and `Referer` before forwarding. The browser talks to the frontend's own origin; the frontend's server process calls Skardi without those headers.

**Next.js Route Handler** (`src/app/api/skardi/[...path]/route.ts`):
```ts
const SKARDI_URL = process.env.SKARDI_UPSTREAM_URL ?? 'http://localhost:18080';
const STRIP = new Set(['origin', 'referer', 'host']);

async function proxy(req: NextRequest, path: string[]) {
  const headers = new Headers();
  req.headers.forEach((v, k) => {
    if (!STRIP.has(k.toLowerCase())) headers.set(k, v);
  });
  const res = await fetch(`${SKARDI_URL}/${path.join('/')}`, {
    method: req.method,
    headers,
    body: req.method === 'GET' ? undefined : req.body,
    duplex: 'half',
  } as RequestInit);
  return new NextResponse(res.body, { status: res.status, headers: res.headers });
}

export const GET = (req, { params }) => params.then(p => proxy(req, p.path));
export const POST = (req, { params }) => params.then(p => proxy(req, p.path));
export const PUT = (req, { params }) => params.then(p => proxy(req, p.path));
export const DELETE = (req, { params }) => params.then(p => proxy(req, p.path));
export const OPTIONS = (req, { params }) => params.then(p => proxy(req, p.path));
```

Environment variables:
```
NEXT_PUBLIC_SKARDI_URL=https://<app-domain>/api/skardi   # browser uses this
SKARDI_UPSTREAM_URL=http://<skardi-service>:8080          # server proxy uses this
```

The same principle applies to any framework — Express, Fastify, Go, etc.: create a thin server-side proxy that forwards everything except `origin`, `referer`, and `host`.

### On Sealos

Set `SKARDI_UPSTREAM_URL` to the **internal K8s service URL**, not the public ingress. This avoids the ingress round-trip and keeps traffic within the cluster:

```yaml
# In the Next.js deployment
- name: SKARDI_UPSTREAM_URL
  value: http://skardi-service-name:8080   # K8s Service name + port
- name: NEXT_PUBLIC_SKARDI_URL
  value: https://<nextjs-subdomain>.<cloud-domain>/api/skardi
```

---

## 5. Local development with Docker

Minimal `docker-compose.yml`:

```yaml
services:
  skardi:
    image: ghcr.io/skardilabs/skardi/skardi-server:<IMAGE_TAG>
    platform: linux/amd64   # image is amd64-only; Docker emulates on Apple Silicon
    ports:
      - "18080:8080"
    environment:
      AUTH_MODE: BETTER_AUTH_DIESEL_SQLITE
      AUTH_SECRET: your-local-dev-secret-must-be-32-chars-min!
      AUTH_DB_PATH: /data/skardi_auth.db
      AUTH_BASE_URL: http://localhost:18080
      RUST_LOG: info
    volumes:
      - ./data/app.db:/data/app.db      # pre-created; Skardi will not create it
      - ./ctx.yaml:/config/ctx.yaml:ro
      - ./pipelines:/config/pipelines:ro
    command:
      - --ctx
      - /config/ctx.yaml
      - --pipeline
      - /config/pipelines/
      - --port
      - "8080"
```

Key points:
- `AUTH_DB_PATH` (the auth store) is **auto-created** by Skardi — do not pre-create it.
- Your app `.db` file must **exist before** `docker compose up` — run your `init-db.py` once first.
- Pipelines are loaded at startup only — restart the container after editing YAMLs.
- `platform: linux/amd64` avoids silent architecture mismatches on ARM hosts.

---

## 6. Setting up kubectl with Sealos

Download kubeconfig from the Sealos dashboard (Account → kubeconfig), then:

```bash
cp ~/Downloads/kubeconfig.yaml ~/.kube/sealos-config.yaml
export KUBECONFIG=~/.kube/sealos-config.yaml
```

If the file is not found, ask the user to copy-paste their kubeconfig.yaml into the current terminal directory.

Your namespace is embedded in the kubeconfig:
```bash
kubectl config view --minify -o jsonpath='{.contexts[0].context.namespace}'
# e.g. ns-bg7m761t
```

Always use `KUBECONFIG=~/.kube/sealos-config.yaml kubectl ...` inline or `export KUBECONFIG=...` first — `export` does **not** persist across Bash tool calls.

---

## 7. Sealos ingress and domain rules

- `*.usw-1.sealos.io` — **forbidden** for user-created ingresses (system only)
- `*.usw-1.sealos.app` — **allowed** for user apps; enable a custom subdomain from the Sealos dashboard first
- TLS secret `wildcard-cert` works for both — no namespace-local TLS secret needed
- Use `spec.ingressClassName: nginx` — the old `kubernetes.io/ingress.class` annotation is deprecated

Resolve `CLOUD_DOMAIN` from existing ingresses:
```bash
NS=$(KUBECONFIG=~/.kube/sealos-config.yaml kubectl config view --minify -o jsonpath='{.contexts[0].context.namespace}')
CLOUD_DOMAIN=$(KUBECONFIG=~/.kube/sealos-config.yaml kubectl get ingress -n $NS \
  -o jsonpath='{.items[0].spec.rules[0].host}' 2>/dev/null | cut -d. -f2-)
# If namespace is fresh (no ingresses yet), derive from cluster server URL:
# CLOUD_DOMAIN=$(... kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}' \
#   | sed 's|https://||; s|:.*||; s|sealos\.io|sealos.app|')
```

---

## 8. Deploying Skardi to Sealos

### Step 1 — resolve the image tag

```bash
IMAGE_TAG=$(gh release view --repo SkardiLabs/skardi --json tagName -q '.tagName | ltrimstr("v")')
# fallback without gh:
IMAGE_TAG=$(curl -s https://api.github.com/repos/SkardiLabs/skardi/releases/latest \
  | grep '"tag_name"' | head -1 | sed 's/.*"v\([^"]*\)".*/\1/')
```

### Step 2 — fill placeholders and apply

```bash
SUBDOMAIN=my-skardi

sed \
  -e "s/<IMAGE_TAG>/$IMAGE_TAG/g" \
  -e "s/<YOUR_NAMESPACE>/$NS/g" \
  -e "s/<YOUR_SUBDOMAIN>/$SUBDOMAIN/g" \
  -e "s/<SEALOS_CLOUD_DOMAIN>/$CLOUD_DOMAIN/g" \
  skardi-deploy.yaml | KUBECONFIG=~/.kube/sealos-config.yaml kubectl apply -f -
```

### Step 3 — verify

```bash
KUBECONFIG=~/.kube/sealos-config.yaml kubectl rollout status deployment/skardi -n $NS
curl https://$SUBDOMAIN.$CLOUD_DOMAIN/health   # expect 200 OK
```

### Deploying with auth + SQLite on Sealos

Add a PVC for persistent storage, an init container for schema setup, and auth env vars. Condensed pattern:

```yaml
# PVC
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: skardi-data
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 1Gi
---
# Deployment (relevant sections only)
spec:
  template:
    spec:
      initContainers:
        - name: db-init
          image: python:3.12-slim
          command: [python3, -c, "import sqlite3; conn=sqlite3.connect('/data/app.db'); conn.executescript('CREATE TABLE IF NOT EXISTS ...'); conn.commit()"]
          volumeMounts:
            - name: data
              mountPath: /data
      containers:
        - name: skardi
          env:
            - name: AUTH_MODE
              value: BETTER_AUTH_DIESEL_SQLITE
            - name: AUTH_SECRET
              valueFrom:
                secretKeyRef:
                  name: skardi-auth-secret
                  key: AUTH_SECRET
            - name: AUTH_DB_PATH
              value: /data/skardi_auth.db
            - name: AUTH_BASE_URL
              value: https://<subdomain>.<cloud-domain>
          volumeMounts:
            - name: data
              mountPath: /data
            - name: ctx
              mountPath: /config/ctx.yaml
              subPath: ctx.yaml
            - name: pipelines
              mountPath: /config/pipelines
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: skardi-data
        - name: ctx
          configMap:
            name: skardi-ctx
        - name: pipelines
          configMap:
            name: skardi-pipelines   # all pipeline files as separate data keys
```

**PodSecurity gotcha:** Sealos enforces `restricted:v1.25` (warn mode — apply succeeds but prints warnings). Apply this `securityContext` to each container to silence warnings:
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

For frontends with server-side logic (API routes, SSR) — use a containerised Node.js image.

**Dockerfile** (Next.js standalone example):
```dockerfile
FROM node:20-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
RUN addgroup --system --gid 1001 nodejs \
 && adduser  --system --uid 1001 nextjs
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static    ./.next/static
COPY --from=builder /app/public ./public
USER nextjs
EXPOSE 3000
ENV PORT=3000 HOSTNAME=0.0.0.0
CMD ["node", "server.js"]
```

Requires `output: 'standalone'` in `next.config.ts`.

Build and push:
```bash
docker build -t ghcr.io/<you>/<app>:latest .
docker push ghcr.io/<you>/<app>:latest
```

K8s Deployment env vars:
```yaml
env:
  - name: NEXT_PUBLIC_SKARDI_URL
    value: https://<app-subdomain>.<cloud-domain>/api/skardi
  - name: SKARDI_UPSTREAM_URL
    value: http://<skardi-k8s-service>:8080   # internal — no ingress hop
```

---

## 10. Deploying a static frontend via ConfigMaps + nginx

For pure SPAs (no server-side logic). Use `public.ecr.aws/nginx/nginx:alpine` (avoids Docker Hub rate limits).

```yaml
containers:
  - image: public.ecr.aws/nginx/nginx:alpine
    volumeMounts:
      - name: html
        mountPath: /usr/share/nginx/html/index.html
        subPath: index.html
      - name: assets
        mountPath: /usr/share/nginx/html/assets/<hashed>.js
        subPath: <hashed>.js
      - name: nginx-conf
        mountPath: /etc/nginx/conf.d/default.conf
        subPath: default.conf
```

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
