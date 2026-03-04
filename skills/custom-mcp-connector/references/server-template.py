"""
MCP Connector Server Template
Based on the production `claude-outline-connector` implementation.

Replace every ← REPLACE comment with your actual service details.
Search for the string "REPLACE" to find all customization points.
"""

import hashlib
import os
import secrets
import time
from base64 import urlsafe_b64encode

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

# ---------------------------------------------------------------------------
# Config — fail fast if required env vars are missing
# ---------------------------------------------------------------------------

SERVICE_BASE_URL    = os.environ.get("SERVICE_BASE_URL", "").rstrip("/")   # ← REPLACE var name
SERVICE_API_KEY     = os.environ["SERVICE_API_KEY"]                        # ← REPLACE var name (KeyError = crash if unset)
MCP_HOST            = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT            = int(os.environ.get("MCP_PORT", "8000"))
MCP_SERVER_URL      = os.environ.get("MCP_SERVER_URL", "").rstrip("/")
OAUTH_CLIENT_ID     = os.environ.get("OAUTH_CLIENT_ID", "")
OAUTH_CLIENT_SECRET = os.environ.get("OAUTH_CLIENT_SECRET", "")

if not SERVICE_BASE_URL:
    raise RuntimeError("SERVICE_BASE_URL environment variable is required")  # ← REPLACE var name
if (OAUTH_CLIENT_ID or OAUTH_CLIENT_SECRET) and not (OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET):
    raise RuntimeError("Set both OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET, or neither")
if OAUTH_CLIENT_ID and not MCP_SERVER_URL:
    raise RuntimeError("MCP_SERVER_URL is required when OAuth is enabled")

# ---------------------------------------------------------------------------
# FastMCP instance — stateless HTTP, plain JSON responses
# ---------------------------------------------------------------------------

_public_host = MCP_SERVER_URL.removeprefix("https://").removeprefix("http://").rstrip("/")

mcp = FastMCP(
    "my-connector",   # ← REPLACE with your connector name
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        allowed_hosts=["localhost", "localhost:*", _public_host],
        allowed_origins=["https://" + _public_host, "http://localhost", "http://localhost:*"],
    ),
)

# ---------------------------------------------------------------------------
# OAuth 2.1 in-memory state — sufficient for a single-user personal server
# ---------------------------------------------------------------------------

_auth_codes:    dict[str, dict]  = {}   # code  → {client_id, redirect_uri, code_challenge, expires_at}
_access_tokens: dict[str, float] = {}   # token → expires_at (unix timestamp)


def _prune_expired() -> None:
    now = time.time()
    for k in [k for k, v in _auth_codes.items()    if v["expires_at"] < now]: del _auth_codes[k]
    for k in [k for k, v in _access_tokens.items() if v < now]:               del _access_tokens[k]


# ---------------------------------------------------------------------------
# OAuth 2.1 discovery endpoints — required by Claude.ai
# ---------------------------------------------------------------------------

@mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
async def _oauth_protected_resource(request: Request) -> JSONResponse:
    return JSONResponse({
        "resource": MCP_SERVER_URL,
        "authorization_servers": [MCP_SERVER_URL],
        "bearer_methods_supported": ["header"],
    })


@mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
async def _oauth_server_metadata(request: Request) -> JSONResponse:
    return JSONResponse({
        "issuer": MCP_SERVER_URL,
        "authorization_endpoint": f"{MCP_SERVER_URL}/oauth/authorize",
        "token_endpoint":         f"{MCP_SERVER_URL}/oauth/token",
        "response_types_supported":              ["code"],
        "grant_types_supported":                 ["authorization_code"],
        "code_challenge_methods_supported":      ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post"],
    })


@mcp.custom_route("/oauth/authorize", methods=["GET"])
async def _oauth_authorize(request: Request) -> JSONResponse | RedirectResponse:
    params = request.query_params
    if not OAUTH_CLIENT_ID:
        return JSONResponse({"error": "OAuth not configured on this server"}, status_code=400)
    if params.get("client_id") != OAUTH_CLIENT_ID:
        return JSONResponse({"error": "unknown_client"}, status_code=400)
    if params.get("response_type") != "code":
        return JSONResponse({"error": "unsupported_response_type"}, status_code=400)

    code = secrets.token_urlsafe(32)
    redirect_uri = params.get("redirect_uri", "")
    _auth_codes[code] = {
        "client_id":      params.get("client_id"),
        "redirect_uri":   redirect_uri,
        "code_challenge": params.get("code_challenge", ""),
        "expires_at":     time.time() + 300,   # 5-minute code expiry
    }
    location = f"{redirect_uri}?code={code}"
    if state := params.get("state", ""):
        location += f"&state={state}"
    return RedirectResponse(location, status_code=302)


@mcp.custom_route("/oauth/token", methods=["POST"])
async def _oauth_token(request: Request) -> JSONResponse:
    form          = await request.form()
    grant_type    = str(form.get("grant_type", ""))
    code          = str(form.get("code", ""))
    client_id     = str(form.get("client_id", ""))
    client_secret = str(form.get("client_secret", ""))
    code_verifier = str(form.get("code_verifier", ""))

    if grant_type != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
    if not OAUTH_CLIENT_ID:
        return JSONResponse({"error": "OAuth not configured"}, status_code=400)
    if client_id != OAUTH_CLIENT_ID or client_secret != OAUTH_CLIENT_SECRET:
        return JSONResponse({"error": "invalid_client"}, status_code=401)

    code_data = _auth_codes.pop(code, None)
    if not code_data or code_data["expires_at"] < time.time():
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    if code_data["code_challenge"]:
        verifier_hash = (
            urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        if verifier_hash != code_data["code_challenge"]:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

    token = secrets.token_urlsafe(32)
    expires_in = 86400 * 30   # 30-day tokens
    _access_tokens[token] = time.time() + expires_in
    return JSONResponse({"access_token": token, "token_type": "bearer", "expires_in": expires_in})


# ---------------------------------------------------------------------------
# Service API client — ← REPLACE these helpers with your actual API calls
# ---------------------------------------------------------------------------

async def _api_post(endpoint: str, payload: dict) -> dict:
    """POST to your service and return the unwrapped response body."""
    url = f"{SERVICE_BASE_URL}/api/{endpoint}"           # ← REPLACE path structure if needed
    headers = {
        "Authorization": f"Bearer {SERVICE_API_KEY}",   # ← REPLACE auth scheme if needed
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(url, json=payload, headers=headers)
        r.raise_for_status()
        body = r.json()
        return body.get("data", body)   # ← REPLACE envelope key if needed


async def _api_get(path: str, params: dict | None = None) -> dict:
    """GET from your service (for REST-style APIs)."""
    url     = f"{SERVICE_BASE_URL}/{path}"
    headers = {"Authorization": f"Bearer {SERVICE_API_KEY}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url, params=params, headers=headers)
        r.raise_for_status()
        body = r.json()
        return body.get("data", body)


# ---------------------------------------------------------------------------
# MCP tools — ← REPLACE / ADD / REMOVE to match your service
# ---------------------------------------------------------------------------

@mcp.tool()
async def my_service_api(endpoint: str, payload: dict) -> dict:
    """
    Call any <your service> API endpoint directly.        ← REPLACE description
    Use for operations not covered by the named tools below, or for fine-grained control.

    Args:
        endpoint: API method path, e.g. 'documents.search' or 'users/list'
        payload:  JSON body to send with the request.

    Returns:
        The 'data' field from the response envelope, or the full body.
    """
    return await _api_post(endpoint, payload)


@mcp.tool()
async def search_items(query: str, collection_id: str | None = None) -> dict:
    """
    Search items in <your service>.                       ← REPLACE

    Args:
        query:         Full-text search string.
        collection_id: Optional scope/collection filter.

    Returns:
        List of matching items with titles, excerpts, and IDs.
    """
    payload: dict = {"query": query}
    if collection_id is not None:
        payload["collectionId"] = collection_id
    return await _api_post("items.search", payload)      # ← REPLACE endpoint name


@mcp.tool()
async def get_item(id: str) -> dict:
    """
    Get a single item by ID.                              ← REPLACE

    Args:
        id: Item UUID or identifier.

    Returns:
        Full item object including content and metadata.
    """
    return await _api_post("items.info", {"id": id})     # ← REPLACE endpoint name


@mcp.tool()
async def create_item(
    title: str,
    text: str,
    collection_id: str,
    parent_id: str | None = None,
) -> dict:
    """
    Create a new item.                                    ← REPLACE

    Args:
        title:         Item title.
        text:          Item body in Markdown.
        collection_id: UUID of the collection to create the item in.
        parent_id:     Optional parent item UUID (for nested structures).

    Returns:
        The newly created item.
    """
    payload: dict = {"title": title, "text": text, "collectionId": collection_id, "publish": True}
    if parent_id is not None:
        payload["parentId"] = parent_id
    return await _api_post("items.create", payload)      # ← REPLACE endpoint name


@mcp.tool()
async def update_item(
    id: str,
    title: str | None = None,
    text: str | None = None,
) -> dict:
    """
    Update an existing item's title and/or content.      ← REPLACE

    Args:
        id:    Item UUID.
        title: New title (omit to keep current).
        text:  New body in Markdown (omit to keep current).

    Returns:
        The updated item.
    """
    payload: dict = {"id": id, "publish": True}
    if title is not None: payload["title"] = title
    if text  is not None: payload["text"]  = text
    return await _api_post("items.update", payload)      # ← REPLACE endpoint name


@mcp.tool()
async def delete_item(id: str) -> dict:
    """
    Delete an item permanently.                           ← REPLACE

    Args:
        id: Item UUID.

    Returns:
        Confirmation object.
    """
    return await _api_post("items.delete", {"id": id})   # ← REPLACE endpoint name


# ---------------------------------------------------------------------------
# Bearer auth middleware — guards /mcp path when OAuth is enabled
# NOTE: Must be added AFTER mcp.streamable_http_app() is called (see below)
# ---------------------------------------------------------------------------

class _BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith("/mcp") or not OAUTH_CLIENT_ID:
            return await call_next(request)   # public paths + local dev pass through

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


# ---------------------------------------------------------------------------
# Build the final ASGI app — middleware is added AFTER streamable_http_app()
# ---------------------------------------------------------------------------

_base_app = mcp.streamable_http_app()
_base_app.add_middleware(_BearerAuthMiddleware)
app = _base_app   # 'app' is what uvicorn / Docker CMD points to

# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)
