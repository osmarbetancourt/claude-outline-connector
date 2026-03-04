"""
GitHub MCP Server
Remote MCP server that gives Claude full GitHub access via PAT + shell execution.

Tools:
  help          — describe all tools so Claude doesn't waste context on tools/list
  github_api    — call any GitHub REST API endpoint (GET/POST/PATCH/DELETE)
  execute       — run arbitrary shell/Python in the container (git, gh CLI, scripts)
                  with a 30-second timeout and a denylist blocking secret-leaking commands
"""

import asyncio
import hashlib
import os
import re
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

GITHUB_TOKEN        = os.environ["GITHUB_TOKEN"]                          # PAT — raises KeyError if unset
MCP_HOST            = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT            = int(os.environ.get("MCP_PORT", "8000"))
MCP_SERVER_URL      = os.environ.get("GITHUB_MCP_SERVER_URL",
                      os.environ.get("MCP_SERVER_URL", "")).rstrip("/")
OAUTH_CLIENT_ID     = os.environ.get("OAUTH_CLIENT_ID_GITHUB", "")
OAUTH_CLIENT_SECRET = os.environ.get("OAUTH_CLIENT_SECRET_GITHUB", "")

if (OAUTH_CLIENT_ID or OAUTH_CLIENT_SECRET) and not (OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET):
    raise RuntimeError("Set both OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET, or neither")
if OAUTH_CLIENT_ID and not MCP_SERVER_URL:
    raise RuntimeError("MCP_SERVER_URL is required when OAuth is enabled")

# ---------------------------------------------------------------------------
# FastMCP instance
# ---------------------------------------------------------------------------

_public_host = MCP_SERVER_URL.removeprefix("https://").removeprefix("http://").rstrip("/")

# GitHub's mark-github Octicon embedded as a 16x16 PNG data URI
_GITHUB_ICON = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAGXRFWHRTb2Z0d2FyZQBB"
    "ZG9iZSBJbWFnZVJlYWR5ccllPAAAAyRpVFh0WE1MOmNvbS5hZG9iZS54bXAAAAAAADw/eHBhY2tldCBiZWdpbj0i"
    "77u/IiBpZD0iVzVNME1wQ2VoaUh6cmVTek5UY3prYzlkIj8+IDx4OnhtcG1ldGEgeG1sbnM6eD0iYWRvYmU6bnM6"
    "bWV0YS8iIHg6eG1wdGs9IkFkb2JlIFhNUCBDb3JlIDUuMy1jMDExIDY2LjE0NTY2MSwgMjAxMi8wMi8wNi0xNDo1"
    "NjoyNyAgICAgICAgIj4gPHJkZjpSREYgeG1sbnM6cmRmPSJodHRwOi8vd3d3LnczLm9yZy8xOTk5LzAyLzIyLXJk"
    "Zi1zeW50YXgtbnMjIj4gPHJkZjpEZXNjcmlwdGlvbiByZGY6YWJvdXQ9IiIgeG1sbnM6eG1wPSJodHRwOi8vbnMu"
    "YWRvYmUuY29tL3hhcC8xLjAvIiB4bWxuczp4bXBNTT0iaHR0cDovL25zLmFkb2JlLmNvbS94YXAvMS4wL21tLyIg"
    "eG1sbnM6c3RSZWY9Imh0dHA6Ly9ucy5hZG9iZS5jb20veGFwLzEuMC9zVHlwZS9SZXNvdXJjZVJlZiMiIHhtcDpD"
    "cmVhdG9yVG9vbD0iQWRvYmUgUGhvdG9zaG9wIENTNiAoTWFjaW50b3NoKSIgeG1wTU06SW5zdGFuY2VJRD0ieG1w"
    "LmlpZDpFNTE3OEEyQTk5MjExMUUyOUExNUJDMTA0NkE4OTA0RCIgeG1wTU06RG9jdW1lbnRJRD0ieG1wLmRpZDpF"
    "NTE3OEEyQjk5MjExMUUyOUExNUJDMTA0NkE4OTA0RCI+IDx4bXBNTTpEZXJpdmVkRnJvbSBzdFJlZjppbnN0YW5j"
    "ZUlEPSJ4bXAuaWlkOkU1MTc4QTI4OTkyMTExRTI5QTE1QkMxMDQ2QTg5MDREIiBzdFJlZjpkb2N1bWVudElEPSJ4"
    "bXAuZGlkOkU1MTc4QTI5OTkyMTExRTI5QTE1QkMxMDQ2QTg5MDREIi8+IDwvcmRmOkRlc2NyaXB0aW9uPiA8L3Jk"
    "ZjpSREY+IDwveDp4bXBtZXRhPiA8P3hwYWNrZXQgZW5kPSJyIj8+m6wkogAAAaNJREFUeNqkk79LAmEYx7/35lna"
    "mUGrQxAVQkSguBQNFkFDU5NDTQ3+A0FDRA0NNjkFLUVTQ1ODQ00REeofqCEoSIKGIIgKMqMsk/d93ufrvecrqCCC"
    "B3fP+9zz43mf97kzMMYghMDj+fg8Ho9rmqaVJEk6KcsyGWPkOM6e53kHruvuXNdVrutKcRxz27a5bduc4zj2vmdZ"
    "VhiGhRBCeJ4HABARMzMzAMDM/gBEvAEAJEkCAMzM/gAQkfcDAABERAAAAABERAAAAAAAAEREAAAAAAAAAAAAiAgA"
    "gIiIiIiIiIiIiIgIgAAAAAAAAICIiIiIiIiIiICAAAAAAAAgICIiIiIiIiIiIiIiAgAAAAAAAAICIiIiIiIiIiIA"
    "gAAAAAAAAgICIiIiIiIiIiIiIiAgAAAAAAAAICIiIiIiIiIiIAAAAAAAAAICAgICAgICAgICAgIAAAAAAAACAgICA"
    "gICAgICAgIAAAAAAAAAICAgICAgICAgICAgIAAAAAAAACAgICAgICAgICAgIAAAAAAAAAICAgICAgICAgICAgAAAAA"
    "AAACAQIDBAUGB"
)

mcp = FastMCP(
    "github",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        allowed_hosts=["localhost", "localhost:*", _public_host],
        allowed_origins=["https://" + _public_host, "http://localhost", "http://localhost:*"],
    ),
    icons=[Icon(src=_GITHUB_ICON, mimeType="image/png")],
)

# ---------------------------------------------------------------------------
# OAuth 2.1 in-memory state
# ---------------------------------------------------------------------------

_auth_codes:    dict[str, dict]  = {}
_access_tokens: dict[str, float] = {}


def _prune_expired() -> None:
    now = time.time()
    for k in [k for k, v in _auth_codes.items()    if v["expires_at"] < now]: del _auth_codes[k]
    for k in [k for k, v in _access_tokens.items() if v < now]:               del _access_tokens[k]


# ---------------------------------------------------------------------------
# OAuth 2.1 discovery endpoints
# ---------------------------------------------------------------------------

@mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
async def _oauth_protected_resource(request: Request) -> JSONResponse:
    return JSONResponse({
        "resource":               MCP_SERVER_URL,
        "authorization_servers":  [MCP_SERVER_URL],
        "bearer_methods_supported": ["header"],
    })


@mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
async def _oauth_server_metadata(request: Request) -> JSONResponse:
    return JSONResponse({
        "issuer":                                MCP_SERVER_URL,
        "authorization_endpoint":                f"{MCP_SERVER_URL}/oauth/authorize",
        "token_endpoint":                        f"{MCP_SERVER_URL}/oauth/token",
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

    code         = secrets.token_urlsafe(32)
    redirect_uri = params.get("redirect_uri", "")
    _auth_codes[code] = {
        "client_id":      params.get("client_id"),
        "redirect_uri":   redirect_uri,
        "code_challenge": params.get("code_challenge", ""),
        "expires_at":     time.time() + 300,
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

    token      = secrets.token_urlsafe(32)
    expires_in = 86400 * 30
    _access_tokens[token] = time.time() + expires_in
    return JSONResponse({"access_token": token, "token_type": "bearer", "expires_in": expires_in})


# ---------------------------------------------------------------------------
# GitHub REST API client
# ---------------------------------------------------------------------------

_GH_BASE = "https://api.github.com"
_GH_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept":        "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


async def _gh_request(method: str, path: str, body: dict | None = None, params: dict | None = None) -> dict:
    url = f"{_GH_BASE}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.request(method.upper(), url, headers=_GH_HEADERS, json=body, params=params)
        r.raise_for_status()
        if r.status_code == 204 or not r.content:
            return {"status": "ok"}
        return r.json()


# ---------------------------------------------------------------------------
# execute() — denylist of patterns that leak secrets or destroy the container
# ---------------------------------------------------------------------------

# These patterns are blocked regardless of context.
_EXEC_DENYLIST = re.compile(
    r"""
    \bprintenv\b          |   # dumps all env vars including GITHUB_TOKEN
    \benv\b\s*$           |   # bare `env` with no args — same issue
    /proc/[0-9]+/environ  |   # /proc/<pid>/environ — raw env dump
    \bcat\s+/proc/        |   # cat /proc/... variants
    (?<!\w)rm\s+-[a-z]*r[a-z]*f   # rm -rf / rm -fr etc.
    """,
    re.VERBOSE,
)

_EXEC_TIMEOUT = 30  # seconds


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def help() -> dict:
    """
    Return a description of all available tools and usage examples.
    Call this first in any session to understand what this server can do.

    Returns:
        Dict with tool names, descriptions, and example invocations.
    """
    return {
        "tools": {
            "help": "This tool — returns available tools and examples.",
            "github_api": (
                "Call any GitHub REST API endpoint. "
                "Args: method (GET/POST/PATCH/DELETE), path (e.g. 'repos/owner/repo/issues'), "
                "body (optional JSON dict), params (optional query params dict). "
                "Examples: list issues → method='GET' path='repos/osmar/myrepo/issues'; "
                "create PR → method='POST' path='repos/osmar/myrepo/pulls' body={...}"
            ),
            "execute": (
                "Run arbitrary shell or Python commands inside the container. "
                "git, gh CLI, python3, curl are all available. GITHUB_TOKEN is pre-configured "
                "in the environment so `gh` works without extra auth. "
                "Args: command (shell string), workdir (optional working directory). "
                "Timeout: 30 seconds. "
                "Examples: clone a repo → 'git clone https://github.com/osmar/myrepo /tmp/myrepo'; "
                "read a file → 'cat /tmp/myrepo/src/main.py'; "
                "patch and push → 'cd /tmp/myrepo && sed -i s/foo/bar/ file.py && git add . && "
                "git commit -m fix && git push'"
            ),
        },
        "notes": [
            "GITHUB_TOKEN is a PAT with repo + workflow + read:org scopes.",
            "execute() has a 30s timeout — chain long operations with && or write a script.",
            "execute() blocks: printenv, env (bare), /proc/*/environ, rm -rf.",
            "Repos cloned in /tmp are ephemeral — they disappear when the container restarts.",
        ],
    }


@mcp.tool()
async def github_api(
    method: str,
    path: str,
    body: dict | None = None,
    params: dict | None = None,
) -> dict:
    """
    Call any GitHub REST API endpoint directly.

    Args:
        method: HTTP method — GET, POST, PATCH, PUT, or DELETE.
        path:   API path without the base URL, e.g. 'repos/owner/repo/issues'
                or 'user/repos'. See https://docs.github.com/en/rest for all endpoints.
        body:   Optional JSON request body (for POST/PATCH/PUT).
        params: Optional query parameters dict (for GET filtering/pagination).

    Returns:
        Parsed JSON response from the GitHub API.
    """
    return await _gh_request(method, path, body=body, params=params)


@mcp.tool()
async def execute(command: str, workdir: str | None = None) -> dict:
    """
    Run a shell command inside the container and return stdout, stderr, and exit code.

    git, gh CLI, python3, curl, jq are available. GITHUB_TOKEN is already set
    in the environment so gh commands work without additional auth setup.
    Cloned repos persist in /tmp for the container's lifetime.

    Args:
        command: Shell command string. Use && to chain multiple commands.
                 Example: 'git clone https://github.com/owner/repo /tmp/repo && cat /tmp/repo/README.md'
        workdir: Optional working directory for the command (default: /tmp).

    Returns:
        Dict with keys: stdout (str), stderr (str), exit_code (int), blocked (bool).
    """
    if _EXEC_DENYLIST.search(command):
        return {
            "stdout":    "",
            "stderr":    "Command blocked by security policy.",
            "exit_code": 1,
            "blocked":   True,
        }

    env = os.environ.copy()
    env["GITHUB_TOKEN"] = GITHUB_TOKEN
    env["GH_TOKEN"]     = GITHUB_TOKEN   # gh CLI reads GH_TOKEN

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir or "/tmp",
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_EXEC_TIMEOUT)
        return {
            "stdout":    stdout.decode(errors="replace"),
            "stderr":    stderr.decode(errors="replace"),
            "exit_code": proc.returncode,
            "blocked":   False,
        }
    except asyncio.TimeoutError:
        proc.kill()
        return {
            "stdout":    "",
            "stderr":    f"Command timed out after {_EXEC_TIMEOUT}s.",
            "exit_code": 124,
            "blocked":   False,
        }


# ---------------------------------------------------------------------------
# Bearer auth middleware
# ---------------------------------------------------------------------------

class _BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith("/mcp") or not OAUTH_CLIENT_ID:
            return await call_next(request)

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
# App assembly
# ---------------------------------------------------------------------------

_base_app = mcp.streamable_http_app()
_base_app.add_middleware(_BearerAuthMiddleware)
app = _base_app

if __name__ == "__main__":
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)
