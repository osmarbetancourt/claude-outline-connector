# claude-outline-connector

A remote MCP server that exposes your self-hosted [Outline](https://www.getoutline.com/) wiki to Claude. Once deployed, Claude can search, read, create, and update your wiki documents — from Claude Code, Claude.ai, or Claude mobile.

The Outline instance stays fully on-premises. Only this small proxy needs a public HTTPS URL.

---

## How it works

```
Claude Code / Claude.ai
        │  (Streamable HTTP / MCP protocol)
        ▼
  outline-mcp  ◄──── OUTLINE_API_KEY
  (this server)
        │  (HTTPS POST + Bearer token)
        ▼
  Your Outline instance  (on-prem)
```

---

## Prerequisites

- A self-hosted Outline instance (any recent version)
- An Outline API key — create one at `https://your-outline/settings/tokens`
- Docker **or** Python 3.11+ with [uv](https://docs.astral.sh/uv/)
- A public HTTPS URL for the server (required for Claude.ai connectors)

---

## Quick start — Docker Compose (recommended)

```bash
git clone https://github.com/osmarbetancourt/claude-outline-connector.git
cd claude-outline-connector

cp .env.example .env
$EDITOR .env   # set OUTLINE_BASE_URL and OUTLINE_API_KEY

# Create the external network once on the host (shared with Caddy)
docker network create caddy_net

docker compose up -d --build
```

Verify it's running:

```bash
curl -s -X POST http://localhost:8765/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | jq '.result.tools[].name'
```

The container exposes port **8765** on the host for direct access / testing.
Caddy reaches the container at `outline-mcp:8000` over the shared `caddy_net` network.

---

## Quick start — local dev (uv)

```bash
uv sync
cp .env.example .env && $EDITOR .env
set -a && source .env && set +a
uv run python -m outline_mcp.server
```

---

## Connect to Claude

### Option A — Claude.ai connector (recommended)

Works in Claude.ai browser/desktop/mobile **and automatically syncs to Claude Code** when you're logged in.

1. Go to `claude.ai` → **Settings → Connectors → Add custom connector**
2. Paste your public HTTPS URL: `https://your-server.com/mcp`
3. Click **Add** — no OAuth setup needed

> **HTTPS is required** for Claude.ai connectors. See [Deployment](#deployment) below.

### Option B — Claude Code CLI (local or remote)

```bash
# Local dev
claude mcp add --transport http outline http://localhost:8000/mcp

# Remote server
claude mcp add --transport http outline https://your-server.com/mcp

# With scope flags
claude mcp add --transport http outline --scope user https://your-server.com/mcp
```

Verify: `claude mcp list`

---

## Available tools

| Tool | Description | Key parameters |
|---|---|---|
| `outline_api` | **Call any Outline API endpoint directly** — full API freedom | `endpoint`, `payload` |
| `outline_search` | Search documents by keyword | `query`, `collection_id?` |
| `outline_get_document` | Get full document content | `id` |
| `outline_list_collections` | List all collections | — |
| `outline_list_documents` | List documents in a collection | `collection_id` |
| `outline_create_document` | Create and publish a new document | `title`, `text`, `collection_id`, `parent_document_id?` |
| `outline_update_document` | Update title and/or content | `id`, `title?`, `text?` |
| `outline_delete_document` | Delete a document permanently | `id` |

`outline_api` is the power tool — it lets Claude call any of Outline's 100+ API endpoints (`documents.star`, `users.list`, `groups.list`, `fileOperations.list`, etc.) without needing a server update. See the [Outline API reference](https://www.getoutline.com/developers).

**Example usage in Claude:**
```
"Search my Outline wiki for anything about deployment"
"Create a new document titled 'Meeting Notes 2026-03-04' in the Engineering collection"
"List all my Outline collections"
"Star the document with id abc-123 using outline_api"
```

---

## Deployment

Claude.ai connectors require a public HTTPS URL. Here are two paths:

### Render / Railway (easiest)

1. Push this repo to GitHub
2. Create a new **Web Service** on [Render](https://render.com) or [Railway](https://railway.app)
3. Point it at your repo, select **Docker** as the runtime
4. Add `OUTLINE_BASE_URL` and `OUTLINE_API_KEY` as environment variables in the dashboard
5. Use the generated `https://your-app.onrender.com/mcp` URL as the connector URL

### VPS + Caddy (docker-compose + external network)

This repo is designed to run alongside a separate Caddy container/service that
shares the `caddy_net` Docker network.

**Step 1 — create the shared network (once per host)**

```bash
docker network create caddy_net
```

**Step 2 — wire Caddy to the same network**

In your Caddy repo's `docker-compose.yml`, attach Caddy to `caddy_net`:

```yaml
services:
  caddy:
    image: caddy:latest
    networks:
      - caddy_net
    # ... rest of your Caddy config

networks:
  caddy_net:
    external: true
```

**Step 3 — add a block in your Caddyfile**

```
wiki-mcp.yourdomain.com {
    reverse_proxy outline-mcp:8000
}
```

Caddy resolves `outline-mcp` by container name on the shared network and handles
HTTPS automatically. No port binding needed on Caddy's side.

---

## Testing with MCP Inspector

```bash
npx @modelcontextprotocol/inspector http://localhost:8000/mcp
```

Opens a browser UI where you can call each tool interactively and inspect request/response payloads.

---

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `OUTLINE_BASE_URL` | Yes | — | Base URL of your Outline instance, e.g. `https://wiki.example.com` |
| `OUTLINE_API_KEY` | Yes | — | Outline API key from Settings → API |
| `MCP_HOST` | No | `0.0.0.0` | Host the MCP server binds to |
| `MCP_PORT` | No | `8000` | Port the MCP server listens on |

---

## Known issue — Claude Desktop / Cowork

There is an [open bug](https://github.com/anthropics/claude-code/issues/23736) where Streamable HTTP-only MCP servers can fail silently when added via the Claude Desktop or Cowork connector UI. The same server works correctly via:

- Claude.ai **Settings → Connectors** (browser)
- Claude Code CLI: `claude mcp add --transport http ...`

Use one of those two methods until the Desktop UI issue is resolved.

---

## Troubleshooting

**`401 Unauthorized` from Outline**
Check that `OUTLINE_API_KEY` is correct. Generate a new key at `https://your-outline/settings/tokens`.

**`Connection refused` or `Name resolution failed`**
Check `OUTLINE_BASE_URL` — it must be reachable from the server where outline-mcp is running, with no trailing slash.

**`KeyError: 'OUTLINE_API_KEY'` on startup**
The env var is not set. Use `--env-file .env` with Docker or `source .env` locally.

**Claude.ai says "Could not connect"**
The server must be accessible over public HTTPS. Test with `curl https://your-server.com/mcp` from outside your network.

---

## License

MIT — see [LICENSE](LICENSE).
