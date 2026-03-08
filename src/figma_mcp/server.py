"""
Figma MCP Server
Remote MCP server that gives Claude read-only access to the Figma REST API.

Tools:
  list_files      — browse files in a Figma team/project
  list_pages      — list all pages in a file
  get_components  — list all components in a file (cached)
  get_styles      — list all design tokens/styles (cached)
  export_node     — export a node as an image URL
  get_comments    — list all comments on a file (cached)
  figma_api       — escape-hatch to call any Figma REST endpoint
"""

import base64
import hashlib
import os
import secrets
import time
from base64 import urlsafe_b64encode

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import Icon
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

# ---------------------------------------------------------------------------
# Config — fail fast if required env vars are missing
# ---------------------------------------------------------------------------

FIGMA_API_KEY = os.environ["FIGMA_API_KEY"]  # raises KeyError if unset
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "8000"))
MCP_SERVER_URL = os.environ.get(
    "FIGMA_MCP_SERVER_URL", os.environ.get("MCP_SERVER_URL", "")
).rstrip("/")
OAUTH_CLIENT_ID = os.environ.get("OAUTH_CLIENT_ID_FIGMA", "")
OAUTH_CLIENT_SECRET = os.environ.get("OAUTH_CLIENT_SECRET_FIGMA", "")
CACHE_TTL = int(os.environ.get("FIGMA_CACHE_TTL", "120"))

if (OAUTH_CLIENT_ID or OAUTH_CLIENT_SECRET) and not (
    OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET
):
    raise RuntimeError("Set both OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET, or neither")
if OAUTH_CLIENT_ID and not MCP_SERVER_URL:
    raise RuntimeError("MCP_SERVER_URL is required when OAuth is enabled")

# ---------------------------------------------------------------------------
# FastMCP instance
# ---------------------------------------------------------------------------

_public_host = (
    MCP_SERVER_URL.removeprefix("https://").removeprefix("http://").rstrip("/")
)

# Figma logo as a small data URI
_FIGMA_ICON = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAA"
    "CXBIWXMAAAsTAAALEwEAmpwYAAABaElEQVQ4y2P4TwRgIEbzfwYGBob/DAwM/xgYGP4z"
    "MDAwMJBiAAMDA8N/BgYGBhD+D8L/GRgY/v9nYGD4D5L7z8DA8B+kFqQGpOf/f5Be"
    "mBqwHJAAyID/IAYYg9T+B2GQGBD+DzYApBZmAEwNSC3cBTAXwFwC0w+TA3MJTA1I"
    "LcwABhgGOQFkKUwOZADIAJAamBqQWrgBDGAMcgnIUJgcyACQATDXgNTC1ID0wAxg"
    "AGOQYSAXgHSD5EAGgAyAuQakFqYG5hoGdA0gQ2AGgJwAMhRGg9TC1MBcwwDDIBeA"
    "LIW5ACQH0g8yAGQAzDUgtTA1ILUwAxjAGOQikKEwGqQWpgbmGgZ0DTA1IENhBoCc"
    "ADIU5gKQHEg/yACQATDXgNTC1IDUwgxgAGOQi0CGwmiQWpgamGsY0DXA1IAMhRkA"
    "cgLIUJgLQHIg/SADQAbAXANSC1MDUgszgAGMQS4CGQqjQWphamCuYcCnBgBfCXSM"
    "3IYAAAAASUVORK5CYII="
)

mcp = FastMCP(
    "figma",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        allowed_hosts=["localhost", "localhost:*", _public_host],
        allowed_origins=[
            "https://" + _public_host,
            "http://localhost",
            "http://localhost:*",
        ],
    ),
    icons=[Icon(src=_FIGMA_ICON, mimeType="image/png")],
)

# ---------------------------------------------------------------------------
# OAuth 2.1 in-memory state
# ---------------------------------------------------------------------------

_auth_codes: dict[str, dict] = {}
_access_tokens: dict[str, float] = {}


def _prune_expired() -> None:
    now = time.time()
    for k in [k for k, v in _auth_codes.items() if v["expires_at"] < now]:
        del _auth_codes[k]
    for k in [k for k, v in _access_tokens.items() if v < now]:
        del _access_tokens[k]


# ---------------------------------------------------------------------------
# OAuth 2.1 discovery endpoints
# ---------------------------------------------------------------------------


@mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
async def _oauth_protected_resource(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "resource": MCP_SERVER_URL,
            "authorization_servers": [MCP_SERVER_URL],
            "bearer_methods_supported": ["header"],
        }
    )


@mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
async def _oauth_server_metadata(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "issuer": MCP_SERVER_URL,
            "authorization_endpoint": f"{MCP_SERVER_URL}/oauth/authorize",
            "token_endpoint": f"{MCP_SERVER_URL}/oauth/token",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["client_secret_post"],
        }
    )


@mcp.custom_route("/oauth/authorize", methods=["GET"])
async def _oauth_authorize(request: Request) -> JSONResponse | RedirectResponse:
    params = request.query_params
    if not OAUTH_CLIENT_ID:
        return JSONResponse(
            {"error": "OAuth not configured on this server"}, status_code=400
        )
    if params.get("client_id") != OAUTH_CLIENT_ID:
        return JSONResponse({"error": "unknown_client"}, status_code=400)
    if params.get("response_type") != "code":
        return JSONResponse({"error": "unsupported_response_type"}, status_code=400)

    code = secrets.token_urlsafe(32)
    redirect_uri = params.get("redirect_uri", "")
    _auth_codes[code] = {
        "client_id": params.get("client_id"),
        "redirect_uri": redirect_uri,
        "code_challenge": params.get("code_challenge", ""),
        "expires_at": time.time() + 300,
    }
    location = f"{redirect_uri}?code={code}"
    if state := params.get("state", ""):
        location += f"&state={state}"
    return RedirectResponse(location, status_code=302)


@mcp.custom_route("/oauth/token", methods=["POST"])
async def _oauth_token(request: Request) -> JSONResponse:
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

    if code_data["code_challenge"]:
        verifier_hash = (
            urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        if verifier_hash != code_data["code_challenge"]:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

    token = secrets.token_urlsafe(32)
    expires_in = 86400 * 30
    _access_tokens[token] = time.time() + expires_in
    return JSONResponse(
        {"access_token": token, "token_type": "bearer", "expires_in": expires_in}
    )


# ---------------------------------------------------------------------------
# Figma REST API client
# ---------------------------------------------------------------------------

_FIGMA_BASE = "https://api.figma.com"
_FIGMA_HEADERS = {
    "X-Figma-Token": FIGMA_API_KEY,
    "Accept": "application/json",
}


async def _figma_get(path: str, params: dict | None = None) -> dict:
    """GET a Figma REST API endpoint and return the parsed JSON."""
    url = f"{_FIGMA_BASE}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url, headers=_FIGMA_HEADERS, params=params)
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------
# In-memory cache — keyed by (endpoint, file_key)
# ---------------------------------------------------------------------------

_cache: dict[str, dict] = {}  # key → {"data": ..., "expires_at": float}


def _cache_get(key: str) -> dict | None:
    entry = _cache.get(key)
    if entry and entry["expires_at"] > time.time():
        return entry["data"]
    if entry:
        del _cache[key]
    return None


def _cache_set(key: str, data: dict) -> None:
    _cache[key] = {"data": data, "expires_at": time.time() + CACHE_TTL}


async def _figma_get_cached(path: str, cache_key: str, params: dict | None = None) -> dict:
    """GET with TTL cache."""
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    data = await _figma_get(path, params)
    _cache_set(cache_key, data)
    return data


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_files(team_id: str) -> dict:
    """
    List all projects and files in a Figma team.

    Args:
        team_id: Figma team ID. Find it in the URL when you open your team page
                 (e.g. https://www.figma.com/files/team/1234567890).

    Returns:
        Dict with projects, each containing a list of files with keys and names.
    """
    projects = await _figma_get(f"v1/teams/{team_id}/projects")
    result = []
    for project in projects.get("projects", []):
        files = await _figma_get(f"v1/projects/{project['id']}/files")
        result.append(
            {
                "project_id": project["id"],
                "project_name": project["name"],
                "files": [
                    {"file_key": f["key"], "name": f["name"], "last_modified": f.get("last_modified")}
                    for f in files.get("files", [])
                ],
            }
        )
    return {"teams": result}


@mcp.tool()
async def list_pages(file_key: str) -> dict:
    """
    List all pages (top-level canvases) in a Figma file.
    Useful for discovering what's in a file before diving into specific nodes.

    Args:
        file_key: The file key from the Figma URL
                  (e.g. from figma.com/design/XXXXX/..., the file_key is XXXXX).

    Returns:
        List of pages with their node IDs and names.
    """
    data = await _figma_get_cached(
        f"v1/files/{file_key}", f"file:{file_key}:depth1", params={"depth": 1}
    )
    document = data.get("document", {})
    pages = []
    for child in document.get("children", []):
        pages.append(
            {
                "node_id": child.get("id"),
                "name": child.get("name"),
                "type": child.get("type"),
                "child_count": len(child.get("children", [])),
            }
        )
    return {
        "file_name": data.get("name"),
        "last_modified": data.get("lastModified"),
        "pages": pages,
    }


@mcp.tool()
async def get_components(file_key: str) -> dict:
    """
    List all components in a Figma file.
    Components are reusable design elements (buttons, cards, icons, etc.).

    Args:
        file_key: The file key from the Figma URL.

    Returns:
        List of components with IDs, names, descriptions, and containing frames.
    """
    data = await _figma_get_cached(
        f"v1/files/{file_key}/components", f"components:{file_key}"
    )
    components = []
    for meta in data.get("meta", {}).get("components", []):
        components.append(
            {
                "node_id": meta.get("node_id"),
                "name": meta.get("name"),
                "description": meta.get("description"),
                "containing_frame": meta.get("containing_frame", {}).get("name"),
                "file_key": meta.get("file_key", file_key),
            }
        )
    return {"components": components, "count": len(components)}


@mcp.tool()
async def get_styles(file_key: str) -> dict:
    """
    List all published styles (design tokens) in a Figma file.
    Styles include colors, text styles, effects, and grids.

    Args:
        file_key: The file key from the Figma URL.

    Returns:
        List of styles with IDs, names, types (FILL, TEXT, EFFECT, GRID),
        and descriptions.
    """
    data = await _figma_get_cached(
        f"v1/files/{file_key}/styles", f"styles:{file_key}"
    )
    styles = []
    for meta in data.get("meta", {}).get("styles", []):
        styles.append(
            {
                "node_id": meta.get("node_id"),
                "name": meta.get("name"),
                "style_type": meta.get("style_type"),
                "description": meta.get("description"),
                "sort_position": meta.get("sort_position"),
            }
        )
    return {"styles": styles, "count": len(styles)}


# Max image size to embed as base64 (750 KB). Larger images return URL only.
_MAX_EMBED_BYTES = 750_000


@mcp.tool()
async def export_node(
    file_key: str,
    node_id: str,
    format: str = "png",
    scale: float = 1,
    thumbnail: bool = False,
    url_only: bool = False,
) -> dict:
    """
    Export a node as an image. By default fetches the image server-side and
    returns it as base64 so Claude can actually see and analyze the design.

    Args:
        file_key:  The file key from the Figma URL.
        node_id:   Node ID to export (e.g. '123:456'). Find node IDs via
                   list_pages or get_components.
        format:    Image format — 'png', 'jpg', 'svg', or 'pdf'. Default: 'png'.
        scale:     Scale factor (0.01 to 4). Default: 1 (1x — good balance of
                   detail vs size). Use 0.5 for large pages, 2 for small components.
        thumbnail: If True, forces scale=0.25 for a quick low-res overview.
                   Great for scanning all pages without blowing up context.
        url_only:  If True, skips the server-side fetch and returns just the
                   CDN URL (useful when you only need the link, not the pixels).

    Returns:
        Dict with image_url (always), image_data (base64, when possible),
        and size_bytes.
    """
    if thumbnail:
        scale = 0.25

    data = await _figma_get(
        f"v1/images/{file_key}",
        params={"ids": node_id, "format": format, "scale": scale},
    )
    image_url = data.get("images", {}).get(node_id)

    result: dict = {
        "node_id": node_id,
        "format": format,
        "scale": scale,
        "image_url": image_url,
    }

    if not image_url or url_only or format in ("svg", "pdf"):
        return result

    # Fetch the image server-side and embed as base64
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            img_resp = await client.get(image_url)
            img_resp.raise_for_status()
            img_bytes = img_resp.content

        result["size_bytes"] = len(img_bytes)

        if len(img_bytes) <= _MAX_EMBED_BYTES:
            b64 = base64.b64encode(img_bytes).decode()
            mime = "image/jpeg" if format == "jpg" else f"image/{format}"
            result["image_data"] = f"data:{mime};base64,{b64}"
        else:
            result["image_data"] = None
            result["note"] = (
                f"Image is {len(img_bytes) // 1024}KB — too large to embed. "
                f"Use a lower scale or thumbnail=True, or open image_url directly."
            )
    except httpx.HTTPError:
        result["image_data"] = None
        result["note"] = "Could not fetch image from Figma CDN. Use image_url directly."

    return result


@mcp.tool()
async def get_comments(file_key: str) -> dict:
    """
    List all comments on a Figma file.
    Useful for reviewing design feedback and discussions.

    Args:
        file_key: The file key from the Figma URL.

    Returns:
        List of comments with author, message, timestamps, and resolved status.
    """
    data = await _figma_get_cached(
        f"v1/files/{file_key}/comments", f"comments:{file_key}"
    )
    comments = []
    for c in data.get("comments", []):
        comments.append(
            {
                "id": c.get("id"),
                "message": c.get("message"),
                "author": c.get("user", {}).get("handle"),
                "created_at": c.get("created_at"),
                "resolved_at": c.get("resolved_at"),
                "order_id": c.get("order_id"),
            }
        )
    return {"comments": comments, "count": len(comments)}


@mcp.tool()
async def figma_api(
    path: str,
    params: dict | None = None,
) -> dict:
    """
    Call any Figma REST API endpoint directly (GET only, read-only token).

    Args:
        path:   API path without the base URL, e.g. 'v1/files/XXXXX'
                or 'v1/teams/1234/projects'.
                See https://www.figma.com/developers/api for all endpoints.
        params: Optional query parameters dict.

    Returns:
        Parsed JSON response from the Figma API.
    """
    return await _figma_get(path, params=params)


# ---------------------------------------------------------------------------
# Bearer auth middleware
# ---------------------------------------------------------------------------


class _BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith("/mcp") or not OAUTH_CLIENT_ID:
            return await call_next(request)

        _prune_expired()
        auth = request.headers.get("Authorization", "")
        token = (
            auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
        )
        if not token or token not in _access_tokens or _access_tokens[token] < time.time():
            return JSONResponse(
                {"error": "Unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)


# ---------------------------------------------------------------------------
# App assembly
# ---------------------------------------------------------------------------

_base_app = mcp.streamable_http_app()
_base_app.add_middleware(_BearerAuthMiddleware)
app = _base_app

if __name__ == "__main__":
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)
