---
name: local-skardi-demo
description: Patterns, gotchas, and workflows for running Skardi locally with Docker, including auth setup, SQLite data sources, pipeline authoring, and wiring a Next.js frontend.
type: feedback
---

# Running Skardi Locally

## 1. Docker setup

Minimal `docker-compose.yml`:

```yaml
services:
  skardi:
    image: ghcr.io/skardilabs/skardi/skardi-server:<IMAGE_TAG>
    platform: linux/amd64   # image is amd64-only; works on Apple Silicon via emulation
    ports:
      - "18080:8080"
    environment:
      AUTH_MODE: BETTER_AUTH_DIESEL_SQLITE
      AUTH_SECRET: <at-least-32-character-secret>
      AUTH_DB_PATH: /data/skardi_auth.db   # Skardi creates this automatically
      AUTH_BASE_URL: http://localhost:18080
      RUST_LOG: info
    volumes:
      - ./skardi/data/expense.db:/data/expense.db   # mount pre-created SQLite file
      - ./skardi/ctx.yaml:/config/ctx.yaml:ro
      - ./skardi/pipelines:/config/pipelines:ro
    command:
      - --ctx
      - /config/ctx.yaml
      - --pipeline
      - /config/pipelines/
      - --port
      - "8080"
```

**Key points:**
- `AUTH_DB_PATH` is Skardi's internal auth database — it is created automatically; do not pre-create it.
- The app SQLite file (e.g. `expense.db`) must **already exist** before Skardi starts. Skardi will fail with "Data source file not found" if it is missing.
- Pipelines are loaded at startup from the directory; no hot-reload — restart the container after changing YAMLs.
- `platform: linux/amd64` avoids Docker choosing the wrong architecture on Apple Silicon.

---

## 2. Pre-creating the SQLite database

Skardi requires the `.db` file to exist before it starts. Create it once with Python (no extra dependencies):

```python
# skardi/init-db.py
import sqlite3, os

db_path = os.path.join(os.path.dirname(__file__), 'data', 'expense.db')
os.makedirs(os.path.dirname(db_path), exist_ok=True)

conn = sqlite3.connect(db_path)
conn.executescript('''
CREATE TABLE IF NOT EXISTS expense_reports (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL,
    ...
);
CREATE TABLE IF NOT EXISTS user_roles (
    user_id TEXT PRIMARY KEY,
    role    TEXT NOT NULL DEFAULT 'employee'
);
''')
conn.commit()
conn.close()
```

Run once before `docker compose up`:
```bash
python3 skardi/init-db.py
docker compose up
```

The `IF NOT EXISTS` guards make it safe to re-run — it will not wipe data.

---

## 3. Auth system

Enable auth with three required env vars:

| Variable | Purpose |
|---|---|
| `AUTH_MODE` | Set to `BETTER_AUTH_DIESEL_SQLITE` |
| `AUTH_SECRET` | Signing secret, **minimum 32 characters** |
| `AUTH_DB_PATH` | Path for the auth SQLite file (auto-created) |

Auth endpoints (all under `/api/auth/`):

| Endpoint | Method | Body |
|---|---|---|
| `/api/auth/sign-up/email` | POST | `{ email, password, name }` |
| `/api/auth/sign-in/email` | POST | `{ email, password }` |
| `/api/auth/get-session` | GET | — (Bearer token in header) |
| `/api/auth/sign-out` | POST | — (Bearer token in header) |

**Sign-in response body** (use `token` field — it's in the body, not just the cookie):
```json
{ "token": "<session-token>", "user": { "id": "...", "email": "..." }, "redirect": false }
```

**Sign-up response body**:
```json
{ "token": "<session-token>", "user": { "id": "...", "email": "..." } }
```

Token is `null` if `auto_sign_in` is disabled (it defaults to `true`).

**Authenticating pipeline calls** — pass the token as Bearer:
```
Authorization: Bearer <session-token>
```

Skardi also accepts the session cookie (`auth.session`) as a fallback, but Bearer is simpler for cross-origin clients.

### auth.users virtual table

When auth is enabled, two read-only virtual tables are available in all pipeline queries:

```sql
SELECT id, email, role, created_at FROM auth.users;
SELECT token, user_id, expires_at FROM auth.sessions;
```

`auth.users` columns: `id, name, email, email_verified, username, role, banned, created_at, updated_at`

These mirror the live auth store — every scan hits the underlying SQLite.

---

## 4. Context file (ctx.yaml)

Each SQLite table must be registered separately. Use `access_mode: read_write` to allow DML.

```yaml
data_sources:
  - name: "expense_reports"       # how you reference it in pipeline SQL
    type: "sqlite"
    access_mode: "read_write"     # omit or set "read_only" for SELECT-only
    path: "/data/expense.db"
    description: "Expense reports table"
    options:
      table: "expense_reports"    # actual table name in the .db file

  - name: "user_roles"
    type: "sqlite"
    access_mode: "read_write"
    path: "/data/expense.db"
    options:
      table: "user_roles"
```

Multiple tables from the same `.db` file are fine — just register them each with the same `path`.

---

## 5. Pipeline YAML format

```yaml
metadata:
  name: pipeline-name       # must match the URL: POST /pipeline-name/execute
  version: 1.0.0
  description: "..."

query: |
  SELECT ...
  FROM table
  WHERE column = {param}    # named parameters with curly braces
```

**Execution:**
```bash
curl -X POST http://localhost:18080/pipeline-name/execute \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <token>" \
  -d '{"param": "value"}'
```

**DML response** (INSERT / UPDATE / DELETE):
```json
{ "success": true, "data": [{ "count": 1 }], "rows": 1 }
```

### Cross-schema JOINs

Queries can freely join `auth.users` (the auth schema) with your own SQLite tables (default schema):

```sql
SELECT er.*, au.email AS user_email
FROM expense_reports er
JOIN auth.users au ON er.user_id = au.id
```

DataFusion resolves both schemas in the same session context — this works.

### SQL dialect gotchas

Pipelines run through **DataFusion**, not SQLite directly. DataFusion has its own SQL dialect:

| SQLite syntax | DataFusion equivalent |
|---|---|
| `datetime('now')` | `CAST(now() AS VARCHAR)` |
| `strftime(...)` | `date_format(...)` |

Use `CAST(now() AS VARCHAR)` for inserting current timestamps into TEXT columns.

---

## 6. Role management pattern (no AdminPlugin)

The current image only enables `EmailPasswordPlugin` — the `AdminPlugin` (which provides `/api/auth/admin/set-role`) is not loaded. `auth.users` is read-only from pipeline queries.

**Workaround**: maintain a separate `user_roles` table in your own SQLite:

```sql
CREATE TABLE user_roles (
    user_id TEXT PRIMARY KEY,
    role    TEXT NOT NULL DEFAULT 'employee'
);
```

**Assign role on signup** (first user → admin, rest → employee):
```yaml
query: |
  INSERT INTO user_roles (user_id, role)
  SELECT {user_id},
    CASE WHEN (SELECT COUNT(*) FROM auth.users) = 1 THEN 'admin' ELSE 'employee' END
```

**Read role** (join auth.users with user_roles):
```yaml
query: |
  SELECT au.id, au.email, COALESCE(ur.role, 'employee') AS role
  FROM auth.users au
  LEFT JOIN user_roles ur ON au.id = ur.user_id
  WHERE au.id = {user_id}
```

**Update role** (admin only — enforced client-side):
```yaml
query: |
  UPDATE user_roles SET role = {role} WHERE user_id = {user_id}
```

---

## 7. CSRF and cross-origin frontend

Skardi's built-in CSRF middleware blocks any POST/PUT/DELETE/PATCH request that carries an `Origin` header pointing to a different origin than `AUTH_BASE_URL`. This affects all browser clients running on a different port.

**The fix (Next.js)**: add a catch-all API Route Handler that strips `Origin` and `Referer` before forwarding to Skardi. Server-to-server requests have no `Origin` header, so CSRF passes.

```ts
// src/app/api/skardi/[...path]/route.ts
const SKARDI_URL = process.env.SKARDI_UPSTREAM_URL ?? 'http://localhost:18080';
const STRIP = new Set(['origin', 'referer', 'host']);

async function proxy(req: NextRequest, path: string[]) {
  const headers = new Headers();
  req.headers.forEach((v, k) => { if (!STRIP.has(k.toLowerCase())) headers.set(k, v); });

  const res = await fetch(`${SKARDI_URL}/${path.join('/')}`, {
    method: req.method,
    headers,
    body: req.method === 'GET' ? undefined : req.body,
    duplex: 'half',
  } as RequestInit);

  return new NextResponse(res.body, { status: res.status, headers: res.headers });
}
```

Set env vars:
```
NEXT_PUBLIC_SKARDI_URL=http://localhost:3000/api/skardi  # used by browser
SKARDI_UPSTREAM_URL=http://localhost:18080                # used by server proxy
```

All Skardi calls from the browser go to `localhost:3000/api/skardi/...` → proxy → `localhost:18080/...` with no `Origin` header.

> **Note**: Next.js `rewrites()` do NOT fix this — they forward the browser's `Origin` header unchanged. Only a Route Handler gives you control over which headers are forwarded.

---

## 8. Expense management demo app

Built as a reference implementation during this session. Located at `expense-app/`.

**Stack**: Skardi (backend + auth) · SQLite (data) · Next.js 15 App Router · Tailwind CSS

**Roles**: admin (first signup), employee (default), reviewer (admin-promoted)

**Pipelines**:

| Pipeline | Operation |
|---|---|
| `assign-initial-role` | INSERT into user_roles after signup |
| `get-user-info` | SELECT user + role (auth.users JOIN user_roles) |
| `list-users` | SELECT all users + roles (admin only) |
| `update-user-role` | UPDATE user_roles (admin only) |
| `create-report` | INSERT draft expense report |
| `update-report` | UPDATE draft fields |
| `submit-report` | UPDATE status draft → submitted |
| `review-report` | UPDATE status submitted → approved/rejected |
| `get-my-reports` | SELECT reports by user_id |
| `get-all-reports` | SELECT all reports + submitter email (reviewer/admin) |
| `get-report` | SELECT single report by id |
| `delete-report` | DELETE draft report |
| `dashboard-summary` | Aggregated approved + pending spend per employee/reviewer |

**Start**:
```bash
# Terminal 1
python3 skardi/init-db.py   # once
docker compose up

# Terminal 2
cd nextjs-app && npm install && npm run dev
# → http://localhost:3000
```
