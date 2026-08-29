from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.requests import Request

import server
from artifacts import ArtifactStore
from auth import ClientRegistry


def request(artifact_id: str, token: str | None) -> Request:
    headers = []
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/artifacts/{artifact_id}",
            "headers": headers,
            "path_params": {"artifact_id": artifact_id},
        }
    )


@pytest.mark.asyncio
async def test_authenticated_artifact_route_headers_and_owner_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path.chmod(0o700)
    tokens = tmp_path / "tokens"
    tokens.mkdir(mode=0o700)
    token = "z" * 48
    token_path = tokens / "client"
    token_path.write_text(token + "\n", encoding="utf-8")
    token_path.chmod(0o600)
    config = tmp_path / "clients.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "clients": [
                    {
                        "id": "owner",
                        "token_file": "tokens/client",
                        "capabilities": ["inspect.artifact"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    config.chmod(0o600)
    registry = ClientRegistry(config, tmp_path)
    registry.load()
    store = ArtifactStore(tmp_path / "artifacts")
    await store.start()
    record = await store.create(
        "owner", b"%PDF-test", suffix="pdf", mime_type="application/pdf", max_bytes=100
    )
    monkeypatch.setattr(server, "registry", registry)
    monkeypatch.setattr(server, "artifacts", store)

    assert (await server.artifact(request(record.id, None))).status_code == 401
    assert (await server.artifact(request(record.id, "bad"))).status_code == 401
    response = await server.artifact(request(record.id, token))
    assert response.status_code == 200
    assert response.body == b"%PDF-test"
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"].startswith("attachment;")
    await store.close()
