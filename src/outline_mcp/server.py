import os

import httpx
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Config — fail fast if required env vars are missing
# ---------------------------------------------------------------------------

OUTLINE_BASE_URL = os.environ.get("OUTLINE_BASE_URL", "").rstrip("/")
OUTLINE_API_KEY = os.environ["OUTLINE_API_KEY"]  # raises KeyError if unset
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "8000"))

if not OUTLINE_BASE_URL:
    raise RuntimeError("OUTLINE_BASE_URL environment variable is required")

# ---------------------------------------------------------------------------
# FastMCP instance — stateless HTTP (no session state, plain JSON responses)
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "outline",
    stateless_http=True,
    json_response=True,
)

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
    mcp.run(
        transport="streamable-http",
        host=MCP_HOST,
        port=MCP_PORT,
    )
