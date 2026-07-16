"""Transport auth tests for Streamable HTTP (task 6.1).

Verifies the auth middleware behavior in isolation:
- Unauthenticated request → 401
- Wrong Bearer token → 401
- Correct Bearer token passes through (GET/POST/DELETE)
- All paths are gated, not just /mcp

We build a minimal FastAPI app that replicates the exact middleware from
server.py, avoiding the module-level swagger fetch that requires a live
JumpServer.
"""

import pytest
from fastapi import FastAPI
from fastapi.responses import Response
from starlette.requests import Request


def _build_app(api_key: str = "test-key") -> FastAPI:
    """Create a minimal app with the same auth middleware as server.py."""
    app = FastAPI()

    @app.middleware("http")
    async def check_api_key(request: Request, call_next) -> Response:
        if api_key:
            auth = request.headers.get("Authorization")
            if (
                not auth
                or not auth.startswith("Bearer ")
                or auth != f"Bearer {api_key}"
            ):
                return Response(status_code=401, content="Unauthorized: Invalid API token")
        return await call_next(request)

    @app.api_route("/mcp", methods=["GET", "POST", "DELETE"])
    async def mcp_endpoint(request: Request):
        return Response(status_code=200, content="ok")

    @app.api_route("/other", methods=["GET"])
    async def other_endpoint(request: Request):
        return Response(status_code=200, content="ok")

    return app


@pytest.fixture()
async def client():
    """AsyncClient against the minimal app."""
    from httpx import ASGITransport, AsyncClient

    app = _build_app(api_key="test-key")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture()
async def open_client():
    """AsyncClient with no API key configured (open access)."""
    from httpx import ASGITransport, AsyncClient

    app = _build_app(api_key="")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# --- Rejection cases ----------------------------------------------------------


@pytest.mark.asyncio
async def test_no_auth_returns_401(client):
    """Request without Authorization header is rejected."""
    resp = await client.get("/mcp")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_wrong_key_returns_401(client):
    """Request with wrong Bearer token is rejected."""
    resp = await client.get("/mcp", headers={"Authorization": "Bearer wrong-key"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_missing_bearer_prefix_returns_401(client):
    """Token without 'Bearer ' prefix is rejected."""
    resp = await client.get("/mcp", headers={"Authorization": "test-key"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_non_mcp_path_also_gated(client):
    """Auth middleware covers all paths, not just /mcp."""
    resp = await client.get("/other")
    assert resp.status_code == 401


# --- Pass-through cases -------------------------------------------------------


@pytest.mark.asyncio
async def test_get_with_valid_key(client):
    """GET /mcp with correct Bearer passes through."""
    resp = await client.get("/mcp", headers={"Authorization": "Bearer test-key"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_post_with_valid_key(client):
    """POST /mcp with correct Bearer passes through."""
    resp = await client.post(
        "/mcp",
        headers={"Authorization": "Bearer test-key"},
        content="{}",
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_delete_with_valid_key(client):
    """DELETE /mcp with correct Bearer passes through."""
    resp = await client.delete("/mcp", headers={"Authorization": "Bearer test-key"})
    assert resp.status_code == 200


# --- Open-access mode (api_key empty) ----------------------------------------


@pytest.mark.asyncio
async def test_no_key_configured_allows_all(open_client):
    """When api_key is empty, all requests pass without auth."""
    resp = await open_client.get("/mcp")
    assert resp.status_code == 200
