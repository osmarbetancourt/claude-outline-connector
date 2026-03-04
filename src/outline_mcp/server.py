import hashlib
import os
import secrets
import time
from base64 import urlsafe_b64encode
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Config — fail fast if required env vars are missing
# ---------------------------------------------------------------------------

OUTLINE_BASE_URL = os.environ.get("OUTLINE_BASE_URL", "").rstrip("/")
OUTLINE_API_KEY = os.environ["OUTLINE_API_KEY"]  # raises KeyError if unset
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "8000"))

# Public base URL of this server — required for OAuth discovery (e.g. https://mcp.example.com)
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "").rstrip("/")

# OAuth 2.1 credentials — set both to enable auth; leave both empty for local dev (no auth)
OAUTH_CLIENT_ID = os.environ.get("OAUTH_CLIENT_ID", "")
OAUTH_CLIENT_SECRET = os.environ.get("OAUTH_CLIENT_SECRET", "")

if not OUTLINE_BASE_URL:
    raise RuntimeError("OUTLINE_BASE_URL environment variable is required")
if (OAUTH_CLIENT_ID or OAUTH_CLIENT_SECRET) and not (OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET):
    raise RuntimeError("Set both OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET, or neither")
if OAUTH_CLIENT_ID and not MCP_SERVER_URL:
    raise RuntimeError("MCP_SERVER_URL is required when OAuth is enabled")

# ---------------------------------------------------------------------------
# FastMCP instance — stateless HTTP (no session state, plain JSON responses)
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "outline",
    stateless_http=True,
    json_response=True,
)

# ---------------------------------------------------------------------------
# OAuth 2.1 in-memory state — sufficient for a single-user personal server
# ---------------------------------------------------------------------------

# code → {client_id, redirect_uri, code_challenge, expires_at}
_auth_codes: dict[str, dict] = {}

# token → expires_at (unix timestamp)
_access_tokens: dict[str, float] = {}


def _prune_expired() -> None:
    now = time.time()
    expired_codes = [k for k, v in _auth_codes.items() if v["expires_at"] < now]
    for k in expired_codes:
        del _auth_codes[k]
    expired_tokens = [k for k, v in _access_tokens.items() if v < now]
    for k in expired_tokens:
        del _access_tokens[k]


# ---------------------------------------------------------------------------
# FastAPI app — serves OAuth endpoints and mounts the FastMCP ASGI sub-app
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: FastAPI):
    yield


app = FastAPI(lifespan=_lifespan)


@app.middleware("http")
async def _bearer_auth(request: Request, call_next):
    """Protect /mcp with Bearer token auth when OAuth is configured."""
    # OAuth discovery and flow endpoints must be publicly accessible
    path = request.url.path
    if not path.startswith("/mcp"):
        return await call_next(request)

    # No OAuth configured → allow all (local dev / authless connector)
    if not OAUTH_CLIENT_ID:
        return await call_next(request)

    _prune_expired()

    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
    now = time.time()
    if not token or token not in _access_tokens or _access_tokens[token] < now:
        return JSONResponse(
            {"error": "Unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await call_next(request)


# --- OAuth 2.1 discovery endpoints ---


@app.get("/.well-known/oauth-protected-resource")
async def _oauth_protected_resource():
    return {
        "resource": MCP_SERVER_URL,
        "authorization_servers": [MCP_SERVER_URL],
        "bearer_methods_supported": ["header"],
    }


@app.get("/.well-known/oauth-authorization-server")
async def _oauth_server_metadata():
    return {
        "issuer": MCP_SERVER_URL,
        "authorization_endpoint": f"{MCP_SERVER_URL}/oauth/authorize",
        "token_endpoint": f"{MCP_SERVER_URL}/oauth/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post"],
    }


# --- OAuth 2.1 authorize endpoint — auto-approves (personal server) ---


@app.get("/oauth/authorize")
async def _oauth_authorize(
    response_type: str,
    client_id: str,
    redirect_uri: str,
    state: str = "",
    code_challenge: str = "",
    code_challenge_method: str = "S256",
):
    if not OAUTH_CLIENT_ID:
        return JSONResponse({"error": "OAuth not configured on this server"}, status_code=400)
    if client_id != OAUTH_CLIENT_ID:
        return JSONResponse({"error": "unknown_client"}, status_code=400)
    if response_type != "code":
        return JSONResponse({"error": "unsupported_response_type"}, status_code=400)

    code = secrets.token_urlsafe(32)
    _auth_codes[code] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "expires_at": time.time() + 300,  # 5 minutes
    }

    location = f"{redirect_uri}?code={code}"
    if state:
        location += f"&state={state}"
    return RedirectResponse(location, status_code=302)


# --- OAuth 2.1 token endpoint ---


@app.post("/oauth/token")
async def _oauth_token(request: Request):
    form = await request.form()
    grant_type = str(form.get("grant_type", ""))
    code = str(form.get("code", ""))
    client_id = str(form.get("client_id", ""))
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

    # Verify PKCE S256
    if code_data["code_challenge"]:
        verifier_hash = (
            urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        if verifier_hash != code_data["code_challenge"]:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

    token = secrets.token_urlsafe(32)
    expires_in = 86400 * 30  # 30 days
    _access_tokens[token] = time.time() + expires_in
    return {"access_token": token, "token_type": "bearer", "expires_in": expires_in}


# Mount FastMCP's ASGI app at /mcp (Claude.ai hits POST /mcp and GET /mcp)
app.mount("/mcp", mcp.streamable_http_app())

# ---------------------------------------------------------------------------
# Outline API client — single private helper, all calls go through here
# ---------------------------------------------------------------------------


async def _outline_post(endpoint: str, payload: dict) -> dict:
    """POST to the Outline API and return the unwrapped data payload."""
    url = f"{OUTLINE_BASE_URL}/api/{endpoint}"
    headers = {
        "Authorization": f"Bearer {OUTLINE_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()  # HTTPStatusError surfaces as a tool error in Claude
        body = response.json()
        # Outline wraps results in {"ok": true, "data": {...}, "pagination": {...}}
        return body.get("data", body)


# ---------------------------------------------------------------------------
# Generic passthrough — gives the agent access to the entire Outline API
# ---------------------------------------------------------------------------


@mcp.tool()
async def outline_api(endpoint: str, payload: dict) -> dict:
    """
    Call any Outline API endpoint directly. Use this for operations not covered
    by the named helpers below, or when you need fine-grained control.

    Args:
        endpoint: Outline API method name, e.g. 'documents.search',
                  'collections.list', 'documents.star', 'users.list'.
                  See https://www.getoutline.com/developers for all endpoints.
        payload:  JSON body to send with the request.

    Returns:
        The 'data' field from Outline's response envelope, or the full
        response body if no 'data' key is present.
    """
    return await _outline_post(endpoint, payload)


# ---------------------------------------------------------------------------
# Named helpers — common operations with typed parameters for ergonomics
# ---------------------------------------------------------------------------


@mcp.tool()
async def outline_search(query: str, collection_id: str | None = None) -> dict:
    """
    Search documents in Outline by keyword.

    Args:
        query:         Full-text search string.
        collection_id: Optional collection UUID to scope the search.

    Returns:
        List of matching documents with titles, excerpts, and IDs.
    """
    payload: dict = {"query": query}
    if collection_id is not None:
        payload["collectionId"] = collection_id
    return await _outline_post("documents.search", payload)


@mcp.tool()
async def outline_get_document(id: str) -> dict:
    """
    Retrieve the full content of a document by its ID.

    Args:
        id: Document UUID or urlId.

    Returns:
        Document object including title, text (Markdown), and metadata.
    """
    return await _outline_post("documents.info", {"id": id})


@mcp.tool()
async def outline_list_collections() -> dict:
    """
    List all collections in the Outline workspace.

    Returns:
        Array of collection objects with IDs, names, and descriptions.
    """
    return await _outline_post("collections.list", {})


@mcp.tool()
async def outline_list_documents(collection_id: str) -> dict:
    """
    List all documents inside a specific collection.

    Args:
        collection_id: Collection UUID to list documents from.

    Returns:
        Array of document objects with IDs, titles, and metadata.
    """
    return await _outline_post("documents.list", {"collectionId": collection_id})


@mcp.tool()
async def outline_create_document(
    title: str,
    text: str,
    collection_id: str,
    parent_document_id: str | None = None,
) -> dict:
    """
    Create a new document in Outline and publish it immediately.

    Args:
        title:              Document title.
        text:               Document body in Markdown format.
        collection_id:      UUID of the collection to create the document in.
        parent_document_id: Optional UUID of a parent document (for nesting).

    Returns:
        The newly created document object.
    """
    payload: dict = {
        "title": title,
        "text": text,
        "collectionId": collection_id,
        "publish": True,
    }
    if parent_document_id is not None:
        payload["parentDocumentId"] = parent_document_id
    return await _outline_post("documents.create", payload)


@mcp.tool()
async def outline_update_document(
    id: str,
    title: str | None = None,
    text: str | None = None,
) -> dict:
    """
    Update an existing document's title and/or content.

    Args:
        id:    Document UUID.
        title: New title (omit to keep the current title).
        text:  New body in Markdown format (omit to keep the current content).

    Returns:
        The updated document object.
    """
    payload: dict = {"id": id, "publish": True}
    if title is not None:
        payload["title"] = title
    if text is not None:
        payload["text"] = text
    return await _outline_post("documents.update", payload)


@mcp.tool()
async def outline_delete_document(id: str) -> dict:
    """
    Permanently delete a document by its ID.

    Args:
        id: Document UUID.

    Returns:
        Confirmation object from Outline.
    """
    return await _outline_post("documents.delete", {"id": id})


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)
