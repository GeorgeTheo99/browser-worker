from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from auth import AuthConfigError, BearerAuthMiddleware, ClientRegistry, current_caller


def make_registry(tmp_path: Path, *, capabilities: list[str] | None = None) -> tuple[ClientRegistry, str]:
    tmp_path.chmod(0o700)
    token_dir = tmp_path / "tokens"
    token_dir.mkdir(mode=0o700)
    token = "x" * 48
    token_path = token_dir / "client"
    token_path.write_text(token + "\n", encoding="utf-8")
    token_path.chmod(0o600)
    config = tmp_path / "clients.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "clients": [
                    {
                        "id": "test-client",
                        "token_file": "tokens/client",
                        "capabilities": capabilities or ["fetch"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    config.chmod(0o600)
    registry = ClientRegistry(config, tmp_path)
    registry.load()
    return registry, token


def test_registry_authenticates_exact_bearer(tmp_path: Path) -> None:
    registry, token = make_registry(tmp_path)
    caller = registry.authenticate_header(f"Bearer {token}")
    assert caller is not None and caller.id == "test-client"
    assert registry.authenticate_header(None) is None
    assert registry.authenticate_header(f"Bearer {token}x") is None
    assert registry.authenticate_header(f"Basic {token}") is None


def test_registry_rejects_public_or_symlinked_secret(tmp_path: Path) -> None:
    registry, _token = make_registry(tmp_path)
    token_path = tmp_path / "tokens" / "client"
    token_path.chmod(0o644)
    with pytest.raises(AuthConfigError):
        registry.load()
    token_path.unlink()
    real = tmp_path / "real-token"
    real.write_text("y" * 48, encoding="utf-8")
    real.chmod(0o600)
    token_path.symlink_to(real)
    with pytest.raises(AuthConfigError):
        registry.load()


def test_registry_rejects_data_directory_with_group_access(tmp_path: Path) -> None:
    registry, _token = make_registry(tmp_path)
    tmp_path.chmod(0o750)
    with pytest.raises(AuthConfigError):
        registry.load()


@pytest.mark.asyncio
async def test_middleware_binds_caller_only_for_authenticated_mcp(tmp_path: Path) -> None:
    registry, token = make_registry(tmp_path)

    async def endpoint(_request):
        return JSONResponse({"caller": current_caller().id})

    app = Starlette(routes=[Route("/mcp", endpoint, methods=["POST"])])
    app.add_middleware(BearerAuthMiddleware, registry=registry)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        assert (await client.post("/mcp")).status_code == 401
        assert (
            await client.post("/mcp", headers={"Authorization": "Bearer bad"})
        ).status_code == 401
        response = await client.post("/mcp", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        assert response.json() == {"caller": "test-client"}
