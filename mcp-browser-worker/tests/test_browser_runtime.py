from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import security
from artifacts import ArtifactStore
from browser_runtime import BrowserManager, WorkerError
from security import PublicResolver, ResolvedTarget


class BrowserFixtureResolver(PublicResolver):
    def __init__(self, port: int) -> None:
        self.port = port

    async def resolve(self, hostname: str, port: int) -> ResolvedTarget:
        if hostname == "public.test" and port == self.port:
            return ResolvedTarget(hostname, port, ("127.0.0.1",))
        return await super().resolve(hostname, port)


@pytest.mark.browser
@pytest.mark.asyncio
async def test_ephemeral_rendered_session_and_private_subresource_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[str] = []

    async def fixture(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.read(65536)
        first = head.split(b"\r\n", 1)[0].decode("latin-1")
        requests.append(first)
        if " /redirect " in first:
            writer.write(
                f"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:{port}/private\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode()
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            return
        if " /private " in first:
            body = b"PRIVATE"
        else:
            body = (
                b"<!doctype html><title>Fixture</title><body><h1>Rendered fixture</h1>"
                b"<input id=value><button id=apply onclick=\"document.querySelector('h1').textContent=document.querySelector('#value').value\">Apply</button>"
                b"<script>document.body.insertAdjacentHTML('beforeend','<p id=dynamic>dynamic text</p>')</script>"
                + b'<img src="http://127.0.0.1:PORT/private">'
                + b"</body>"
            )
        body = body.replace(b"PORT", str(port).encode())
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(fixture, "127.0.0.1", 0)
    port = int((server.sockets or [])[0].getsockname()[1])
    monkeypatch.setattr(security, "ALLOWED_DESTINATION_PORTS", frozenset({80, 443, port}))
    artifacts = ArtifactStore(tmp_path / "artifacts")
    await artifacts.start()
    sessions_dir = tmp_path / "sessions"
    orphan = sessions_dir / "orphan-profile"
    orphan.mkdir(parents=True)
    (orphan / "cookie").write_text("stale", encoding="utf-8")
    manager = BrowserManager(
        artifacts,
        resolver_factory=lambda: BrowserFixtureResolver(port),
        sessions_dir=sessions_dir,
    )
    await manager.start()
    assert not orphan.exists()
    try:
        opened = await manager.open(
            "owner", f"http://public.test:{port}/", wait_until="domcontentloaded", timeout_ms=15_000
        )
        session_id = str(opened["session_id"])
        extracted = await manager.act("owner", session_id, "extract_text", max_chars=5000)
        assert "Rendered fixture" in str(extracted["text"])
        assert "dynamic text" in str(extracted["text"])
        assert not any(" /private " in request for request in requests)

        await manager.act("owner", session_id, "type", selector="#value", text="Changed title")
        await manager.act("owner", session_id, "click", selector="#apply")
        changed = await manager.act("owner", session_id, "extract_text", selector="h1")
        assert changed["text"] == "Changed title"
        screenshot = await manager.act("owner", session_id, "screenshot", full_page=True)
        pdf = await manager.act("owner", session_id, "export_pdf", format="A4")
        assert (await artifacts.read("owner", screenshot["artifact"]["artifact_id"]))[1].startswith(b"\x89PNG")
        assert (await artifacts.read("owner", pdf["artifact"]["artifact_id"]))[1].startswith(b"%PDF")
        tabs = await manager.act(
            "owner", session_id, "open_tab", url=f"http://public.test:{port}/"
        )
        assert len(tabs["tabs"]) == 2
        await manager.act("owner", session_id, "close_tab")
        await manager.act(
            "owner", session_id, "evaluate", script="document.cookie='canary=present; path=/'"
        )
        with pytest.raises(WorkerError, match="not found"):
            await manager.act("other-owner", session_id, "state")
        profile = tmp_path / "sessions" / session_id
        assert profile.exists()
        await manager.close("owner", session_id)
        assert not profile.exists()

        fresh = await manager.open(
            "owner", f"http://public.test:{port}/", wait_until="domcontentloaded", timeout_ms=15_000
        )
        fresh_id = str(fresh["session_id"])
        cookie = await manager.act("owner", fresh_id, "evaluate", script="document.cookie")
        assert cookie["result"] == ""
        await manager.close("owner", fresh_id)

        with pytest.raises(WorkerError, match="public-network policy"):
            await manager.open(
                "owner",
                f"http://public.test:{port}/redirect",
                wait_until="domcontentloaded",
                timeout_ms=15_000,
            )
        assert manager.live_sessions == 0
        assert list((tmp_path / "sessions").iterdir()) == []
        assert not any(" /private " in request for request in requests)
    finally:
        await manager.shutdown()
        await artifacts.close()
        server.close()
        await server.wait_closed()
