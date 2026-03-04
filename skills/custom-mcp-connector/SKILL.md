---
name: custom-mcp-connector
description: >
  Build a production-ready remote MCP server that connects Claude to any internal tool, API, or service.
  Use this skill whenever the user wants to: create a Claude connector, build a Claude integration with
  an internal tool or API, expose their own service as MCP tools, make Claude talk to their wiki /
  database / backend / SaaS, scaffold an MCP server from scratch, or plug anything into Claude.ai's
  connector settings. Also triggers on: "Claude connector", "MCP server", "custom integration",
  "make Claude connect to X", "expose X to Claude", "I want Claude to be able to use X", "build an MCP",
  or any request to give Claude access to an internal system. This skill produces a complete, deployable
  Python MCP server with OAuth 2.1 + PKCE, Docker, and Caddy — ready to paste into
  Claude.ai → Settings → Connectors. ALWAYS use this skill for any MCP server scaffolding request,
  even if the user only mentions it casually.
---

# Custom MCP Connector Skill

Scaffold a **complete, deployable remote MCP server** in Python that makes any API or tool available to
Claude — via Claude.ai (browser/desktop/mobile) and Claude Code CLI.

The canonical reference is **`claude-outline-connector`** — a working production server that connects
Claude to a self-hosted Outline wiki. Every pattern in this skill comes directly from that codebase.

The full annotated server template is at `references/server-template.py`. Read it when writing code.

---

## How it works

```
Claude (browser / CLI / mobile)
        │  Streamable HTTP — JSON-RPC over HTTP
        ▼
  Your MCP server  ◄──── SERVICE_API_KEY
  (this skill builds it)
        │  HTTPS + Bearer token
        ▼
  Target service  (Outline, Notion, your API, anything)
```

The server is a **thin async Python proxy**: it exposes MCP tools that Claude calls, translates them
into API requests to the target service, and returns JSON. No AI logic lives here.

---

## Stack (exact versions from production)

| Layer | Choice |
|---|---|
| MCP framework | `FastMCP` via `mcp[cli] >= 1.9.0` |
| HTTP runtime | `uvicorn >= 0.30.0` + `starlette >= 0.41.0` |
| HTTP client | `httpx >= 0.27.0` (async) |
| Auth | OAuth 2.1 + PKCE — fully in-memory, no database |
| Packaging | Docker multi-stage + `uv` from `ghcr.io/astral-sh/uv` |
| Reverse proxy | Caddy on a shared `caddy_net` Docker network |

---

## Step 1 — Gather requirements

Ask the user before writing code:

1. **What service are you wrapping?** (REST API, database, internal tool, SaaS?)
2. **How does it authenticate?** (Bearer token, API key in header, basic auth?)
3. **What should Claude be able to do?** (list the specific operations: read, write, search, etc.)
4. **Where will it be deployed?** (VPS + Caddy, Render/Railway, local only?)
5. **Claude.ai web, Claude Code CLI, or both?**

> **Important**: Claude.ai web connectors **require** a public HTTPS URL + OAuth 2.1.  
> Claude Code CLI alone works with local HTTP and a `--header "Authorization: Bearer <token>"` flag — no OAuth needed.

---

## Step 2 — Project structure

```
my-connector/
├── src/
│   └── my_connector/
│       ├── __init__.py        ← empty file
│       └── server.py          ← ALL logic lives here (single file)
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── .dockerignore
└── README.md
```

**Single-file rule:** keep everything in `server.py`. The reference implementation is ~230 lines.
Don't split into modules unless tool count exceeds ~20.

---

## Step 3 — `pyproject.toml`

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "my-connector"
version = "0.1.0"
description = "Remote MCP server proxying Claude to <your service>"
readme = "README.md"
license = { file = "LICENSE" }
requires-python = ">=3.11"
dependencies = [
    "mcp[cli]>=1.9.0",
    "httpx>=0.27.0",
    "uvicorn>=0.30.0",
    "starlette>=0.41.0",
]

[tool.hatch.build.targets.wheel]
packages = ["src/my_connector"]
```

---

## Step 4 — `server.py` — exact structure to follow

Copy `references/server-template.py` and replace every `← REPLACE` comment.
The file has seven sections in order:

### 4.1 — Config block (top of file — fail fast)

```python
SERVICE_BASE_URL    = os.environ.get("SERVICE_BASE_URL", "").rstrip("/")
SERVICE_API_KEY     = os.environ["SERVICE_API_KEY"]   # raises KeyError if unset
MCP_HOST            = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT            = int(os.environ.get("MCP_PORT", "8000"))
MCP_SERVER_URL      = os.environ.get("MCP_SERVER_URL", "").rstrip("/")
OAUTH_CLIENT_ID     = os.environ.get("OAUTH_CLIENT_ID", "")
OAUTH_CLIENT_SECRET = os.environ.get("OAUTH_CLIENT_SECRET", "")
```

Three validations immediately after:
- `SERVICE_BASE_URL` must not be empty
- `OAUTH_CLIENT_ID` and `OAUTH_CLIENT_SECRET` must both be set or both empty
- `MCP_SERVER_URL` is required when OAuth is enabled

### 4.2 — FastMCP instance

```python
_public_host = MCP_SERVER_URL.removeprefix("https://").removeprefix("http://").rstrip("/")

mcp = FastMCP(
    "my-connector",
    stateless_http=True,    # ← required for Claude.ai
    json_response=True,     # ← required for Claude.ai
    transport_security=TransportSecuritySettings(
        allowed_hosts=["localhost", "localhost:*", _public_host],
        allowed_origins=["https://" + _public_host, "http://localhost", "http://localhost:*"],
    ),
)
```

Both `stateless_http=True` and `json_response=True` are **required** — Claude.ai fails without them.

### 4.3 — In-memory OAuth 2.1 state

```python
_auth_codes:    dict[str, dict]  = {}   # code  → {client_id, redirect_uri, code_challenge, expires_at}
_access_tokens: dict[str, float] = {}   # token → expires_at (unix timestamp)
```

Four custom routes registered with `@mcp.custom_route(...)`:

| Path | Method | Role |
|---|---|---|
| `/.well-known/oauth-protected-resource` | GET | Resource metadata — Claude hits this first |
| `/.well-known/oauth-authorization-server` | GET | Server metadata (endpoints, supported methods) |
| `/oauth/authorize` | GET | Issues auth code; auto-approves; redirects to `redirect_uri?code=…` |
| `/oauth/token` | POST | Exchanges code for 30-day bearer token; validates PKCE S256 |

Auth codes expire in **5 minutes**. Tokens expire in **30 days**.
Always call `_prune_expired()` at the start of auth-gated requests.

**The authorize endpoint auto-approves** — no consent UI needed for a personal server.
The PKCE S256 check in `/oauth/token`:
```python
verifier_hash = urlsafe_b64encode(
    hashlib.sha256(code_verifier.encode()).digest()
).rstrip(b"=").decode()
if verifier_hash != code_data["code_challenge"]:
    return JSONResponse({"error": "invalid_grant"}, status_code=400)
```

### 4.4 — Service API client (replace with your actual API)

**POST-based APIs** (Outline pattern — all calls are `POST /api/method.name`):
```python
async def _api_post(endpoint: str, payload: dict) -> dict:
    url = f"{SERVICE_BASE_URL}/api/{endpoint}"
    headers = {
        "Authorization": f"Bearer {SERVICE_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(url, json=payload, headers=headers)
        r.raise_for_status()
        body = r.json()
        return body.get("data", body)   # unwrap envelope
```

**REST-style APIs** (GET to resource paths):
```python
async def _api_get(path: str, params: dict | None = None) -> dict:
    url = f"{SERVICE_BASE_URL}/{path}"
    headers = {"Authorization": f"Bearer {SERVICE_API_KEY}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url, params=params, headers=headers)
        r.raise_for_status()
        return r.json()
```

### 4.5 — MCP tools

Rules for every tool:
- Decorate with `@mcp.tool()`
- One-line docstring (becomes tool description Claude sees)
- All params typed; optional params typed as `str | None = None` (not `Optional[str]`)
- Full `Args:` / `Returns:` docstring

**Always add an escape-hatch tool** — lets Claude call any raw API endpoint without a server
deploy. This is the single most important tool to include:

```python
@mcp.tool()
async def my_service_api(endpoint: str, payload: dict) -> dict:
    """
    Call any <service> API endpoint directly.
    Use for operations not covered by the named tools, or fine-grained control.

    Args:
        endpoint: API method path, e.g. 'documents.search', 'users/list'
        payload:  JSON body to send with the request.

    Returns:
        The 'data' field from the response envelope, or the full body.
    """
    return await _api_post(endpoint, payload)
```

### 4.6 — Bearer auth middleware

```python
from starlette.middleware.base import BaseHTTPMiddleware

class _BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith("/mcp") or not OAUTH_CLIENT_ID:
            return await call_next(request)   # public paths and local dev pass through
        _prune_expired()
        auth  = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
        if not token or token not in _access_tokens or _access_tokens[token] < time.time():
            return JSONResponse(
                {"error": "Unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)
```

### 4.7 — App assembly (bottom of file — ORDER MATTERS)

```python
# 1. Build the base ASGI app from the mcp instance
_base_app = mcp.streamable_http_app()

# 2. Add middleware AFTER (not before) streamable_http_app()
_base_app.add_middleware(_BearerAuthMiddleware)
app = _base_app

# 3. Entrypoint
if __name__ == "__main__":
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)
```

⚠️ The reference uses `_base_app` as an intermediate variable and then assigns `app = _base_app`.
This is intentional — it makes the `app` symbol point to the fully-configured instance.

---

## Step 5 — Docker

### `Dockerfile`

```dockerfile
# ---------- build stage ----------
FROM python:3.11-slim AS builder
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY pyproject.toml LICENSE README.md ./
COPY src/ src/
RUN uv sync --no-dev --no-editable

# ---------- runtime stage ----------
FROM python:3.11-slim
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src   /app/src
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8765
CMD ["python", "-m", "my_connector.server"]
```

### `docker-compose.yml`

```yaml
services:
  my-connector:
    build: .
    container_name: my-connector
    restart: unless-stopped
    env_file: .env
    environment:
      MCP_PORT: "8765"     # override default — matches EXPOSE above
    ports:
      - "8765:8765"        # host:container — for local testing / direct curl
    networks:
      - caddy_net

networks:
  caddy_net:
    external: true         # create once: docker network create caddy_net
```

**Port note from the reference:** `MCP_PORT: "8765"` is set in `environment:` (overriding the `8000`
default from the config block). The host binding `8765:8765` lets you curl locally. Caddy reaches the
container at `my-connector:8765` over the shared network without needing the host binding.

### `.dockerignore`

```
.env
.venv
__pycache__
*.egg-info
*.pyc
.git
.github
```

### `.env.example`

```bash
# Required
SERVICE_BASE_URL=https://your-service.example.com
SERVICE_API_KEY=your_api_key_here

# Server bind settings
MCP_HOST=0.0.0.0
MCP_PORT=8765

# OAuth 2.1 — required for Claude.ai connector; leave all empty for local dev
MCP_SERVER_URL=https://mcp.yourdomain.com
OAUTH_CLIENT_ID=my-connector
OAUTH_CLIENT_SECRET=   # python -c "import secrets; print(secrets.token_urlsafe(32))"
```

---

## Step 6 — Deploy

### Option A — VPS + Caddy (self-hosted, production)

```bash
# 1. Create shared Docker network (once per host)
docker network create caddy_net

# 2. Configure .env
cp .env.example .env && nano .env

# 3. Build and start
docker compose up -d --build

# 4. Caddyfile entry — Caddy resolves container by name on shared network
# mcp.yourdomain.com {
#     reverse_proxy my-connector:8765
# }
```

### Option B — Render / Railway (zero-ops)

1. Push repo to GitHub
2. Create **Web Service** → Docker runtime
3. Add env vars in the dashboard
4. Use the generated `https://your-app.onrender.com` as `MCP_SERVER_URL`

### Option C — Local dev only

```bash
uv sync
cp .env.example .env && nano .env
set -a && source .env && set +a
uv run python -m my_connector.server
```

### CI/CD — GitHub Actions + sshpass (from `deploy-production.yml`)

```yaml
- name: Create .env from GitHub secrets
  run: |
    cat <<EOF > .env
    SERVICE_BASE_URL=${{ secrets.SERVICE_BASE_URL }}
    SERVICE_API_KEY=${{ secrets.SERVICE_API_KEY }}
    MCP_HOST=0.0.0.0
    MCP_PORT=8765
    MCP_SERVER_URL=${{ secrets.MCP_SERVER_URL }}
    OAUTH_CLIENT_ID=${{ secrets.OAUTH_CLIENT_ID }}
    OAUTH_CLIENT_SECRET=${{ secrets.OAUTH_CLIENT_SECRET }}
    EOF

- name: Deploy via rsync + ssh
  run: |
    sshpass -p "${{ secrets.SERVER_PASSWORD }}" \
      rsync -az --delete --include='.env' -e "ssh -o StrictHostKeyChecking=no" \
      ./ ${{ secrets.SERVER_USER }}@${{ secrets.SERVER_HOST }}:${{ secrets.PROJECT_PATH }}
    sshpass -p "${{ secrets.SERVER_PASSWORD }}" \
      ssh -o StrictHostKeyChecking=no \
      ${{ secrets.SERVER_USER }}@${{ secrets.SERVER_HOST }} \
      "cd ${{ secrets.PROJECT_PATH }} && docker compose pull && docker compose up -d --build"
```

Use `upload-github-secrets.sh` (from the reference repo) to bulk-upload your `.env` to GitHub Secrets.

---

## Step 7 — Connect to Claude

### Claude.ai (browser / desktop / mobile)

1. **Settings → Connectors → Add custom connector**
2. **Server URL:** your `MCP_SERVER_URL` — base URL only, **not** `/mcp`
3. **OAuth Client ID:** value of `OAUTH_CLIENT_ID`
4. **OAuth Client Secret:** value of `OAUTH_CLIENT_SECRET`
5. Click **Add** — Claude performs the OAuth flow automatically

> Connectors added via Claude.ai **auto-sync to Claude Code CLI** for the same account.  
> One setup → all clients.

### Claude Code CLI

```bash
# Local dev (no auth)
claude mcp add --transport http my-connector http://localhost:8765/mcp

# Remote with bearer token
claude mcp add --transport http my-connector https://mcp.yourdomain.com/mcp \
  --header "Authorization: Bearer <access_token>"
```

---

## Step 8 — Verify

```bash
# List tools (no auth required — hits /mcp directly)
curl -s -X POST http://localhost:8765/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | jq '.result.tools[].name'

# Interactive — MCP Inspector
npx @modelcontextprotocol/inspector http://localhost:8765/mcp
```

---

## Auth scheme variations

### API key in custom header
```python
headers = {"X-API-Key": SERVICE_API_KEY, "Accept": "application/json"}
```

### Basic auth
```python
auth = httpx.BasicAuth(username=API_USER, password=API_PASSWORD)
async with httpx.AsyncClient(timeout=30.0, auth=auth) as client: ...
```

### API key in query param
```python
params = {**(params or {}), "api_key": SERVICE_API_KEY}
```

### Multiple credentials
```python
DB_HOST     = os.environ["DB_HOST"]
DB_USER     = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]
```

---

## Security checklist

Before going live:

- [ ] `OAUTH_CLIENT_SECRET` is a strong random string — `python -c "import secrets; print(secrets.token_urlsafe(32))"`
- [ ] `MCP_SERVER_URL` uses `https://`
- [ ] `.env` is in both `.gitignore` and `.dockerignore`
- [ ] In production, Caddy is the only public entry point — the Docker host port binding is for local testing only
- [ ] `_BearerAuthMiddleware` returns `401` for unauthenticated `/mcp` requests when `OAUTH_CLIENT_ID` is set
- [ ] OAuth discovery paths (`/.well-known/*`, `/oauth/*`) are **not** guarded by auth — they must remain public for the OAuth flow to work

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `KeyError: 'SERVICE_API_KEY'` on startup | Env var not set | Check `.env` and `env_file:` in `docker-compose.yml` |
| `RuntimeError: Set both OAUTH_CLIENT_ID…` | Only one OAuth var set | Set both or leave both empty |
| Claude.ai "Could not connect" | OAuth discovery unreachable | `curl https://your-server/.well-known/oauth-protected-resource` |
| `401` on `/mcp` | No or expired bearer token | Re-add connector in Claude.ai Settings |
| Tools empty after connecting | Server connected but tools/list empty | `curl -X POST …/mcp -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'` |
| "Authentication successful, but still requires auth" (Claude Code) | Token not picked up | Restart `claude` session |
| Claude Desktop / Cowork silent failure | [Known upstream bug](https://github.com/anthropics/claude-code/issues/23736) with Streamable HTTP | Use Claude.ai Settings → Connectors or `claude mcp add --transport http` CLI |

### Get a token manually (for Claude Code `--header` use)

```bash
# Step 1 — get auth code (SHA256("") = 47DEQpj8… for testing with empty verifier)
curl -v "https://your-server.com/oauth/authorize?\
response_type=code&client_id=YOUR_CLIENT_ID\
&redirect_uri=https://example.com/cb\
&code_challenge=47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU\
&code_challenge_method=S256"
# → grab `code=` from the Location redirect header

# Step 2 — exchange for token (code_verifier="" matches SHA256("") challenge)
curl -s -X POST https://your-server.com/oauth/token \
  -d "grant_type=authorization_code&code=CODE\
&client_id=YOUR_CLIENT_ID&client_secret=YOUR_SECRET&code_verifier="
# → {"access_token":"…","token_type":"bearer","expires_in":2592000}
```

---

## Reference files

- `references/server-template.py` — complete annotated `server.py` ready to copy and adapt; all
  customization points marked with `← REPLACE`
