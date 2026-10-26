from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

import pytest

import auth
import security
import server
from artifacts import ArtifactStore
from auth import Caller
from browser_runtime import MAX_UPLOAD_BYTES, BrowserManager, WorkerError
from server import UploadFile, browser_inspect
from tests.test_browser_runtime import BrowserFixtureResolver

PHOTO = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
FILES = [{"name": "../photo.png", "mime_type": "image/png", "data": base64.b64encode(PHOTO).decode()}]
PAGE = (
    b"<!doctype html><title>Upload</title><body>"
    b"<input id=direct type=file>"
    b"<label id=lab for=labelled>Choose</label><input id=labelled type=file style='display:none'>"
    b"<input id=hidden type=file style='display:none'>"
    b"<button id=picker onclick=\"document.querySelector('#hidden').click()\">Add photo</button>"
    b"<button id=nothing>Nothing</button>"
    b"<iframe id=frame srcdoc=\"<input id=inner type=file>\"></iframe></body>"
)
READ = "sel => { const f = document.querySelector(sel).files[0]; return f && [f.name, f.size, f.type]; }"


@pytest.mark.asyncio
async def test_upload_requires_its_capability_and_files(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def act(owner: str, session_id: str, action: str, **params: Any) -> dict[str, Any]:
        calls.append((action, params))
        return {"status": "ok"}

    monkeypatch.setattr(server.manager, "act", act)
    files = [UploadFile(**FILES[0])]
    token = auth._current_caller.set(Caller("interact", frozenset({"inspect.read", "inspect.interact"})))
    try:
        result = await browser_inspect("upload", session_id="s", selector="#f", files=files)
        assert result.structured_content["code"] == "capability_denied"
    finally:
        auth._current_caller.reset(token)
    token = auth._current_caller.set(Caller("uploader", frozenset({"inspect.read", "inspect.upload"})))
    try:
        for fields in [
            {"selector": "#f"},  # no files
            {"files": files},  # no target
            {"selector": "#f", "files": [UploadFile(name="a", mime_type="image/png", data="not base64!")]},
            {"selector": "#f", "files": [UploadFile(**FILES[0])] * 6},
        ]:
            result = await browser_inspect("upload", session_id="s", **fields)
            assert result.structured_content["code"] == "invalid_request", fields
        big = base64.b64encode(b"x" * (MAX_UPLOAD_BYTES // 2 + 1)).decode()
        two = [UploadFile(name="a", mime_type="image/png", data=big)] * 2
        assert (await browser_inspect("upload", session_id="s", selector="#f", files=two)).structured_content["code"] == "invalid_request"
        assert (await browser_inspect("type", session_id="s", selector="#f", text="x", files=files)).structured_content["code"] == "invalid_request"
        assert calls == []
        assert (await browser_inspect("upload", session_id="s", ref="e3", files=files)).structured_content["status"] == "ok"
        action, params = calls[0]
        assert action == "upload" and params["ref"] == "e3"
        assert params["files"] == [{"name": "photo.png", "mimeType": "image/png", "buffer": PHOTO}]
    finally:
        auth._current_caller.reset(token)


@pytest.mark.browser
@pytest.mark.asyncio
async def test_upload_fills_inputs_labels_pickers_and_frames(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fixture(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(65536)
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: " + str(len(PAGE)).encode()
            + b"\r\nConnection: close\r\n\r\n" + PAGE
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    fixture_server = await asyncio.start_server(fixture, "127.0.0.1", 0)
    port = int((fixture_server.sockets or [])[0].getsockname()[1])
    monkeypatch.setattr(security, "ALLOWED_DESTINATION_PORTS", frozenset({80, 443, port}))
    artifacts = ArtifactStore(tmp_path / "artifacts")
    await artifacts.start()
    manager = BrowserManager(artifacts, resolver_factory=lambda: BrowserFixtureResolver(port), sessions_dir=tmp_path / "sessions")
    await manager.start()
    payload = [{"name": "photo.png", "mimeType": "image/png", "buffer": PHOTO}]
    try:
        opened = await manager.open("owner", f"http://public.test:{port}/", wait_until="load", timeout_ms=15_000)
        sid = str(opened["session_id"])

        async def files_of(selector: str, frame_id: str | None = None) -> Any:
            extra = {"frame_id": frame_id} if frame_id else {}
            return (await manager.act("owner", sid, "evaluate", script=READ, arg=selector, **extra))["result"]

        for target, field in [("#direct", "#direct"), ("#lab", "#labelled"), ("#picker", "#hidden")]:
            result = await manager.act("owner", sid, "upload", selector=target, files=payload, timeout_ms=5_000)
            assert result["files"] == 1
            assert await files_of(field) == ["photo.png", len(PHOTO), "image/png"]

        with pytest.raises(WorkerError, match="no file picker"):
            await manager.act("owner", sid, "upload", selector="#nothing", files=payload, timeout_ms=1_000)
        with pytest.raises(WorkerError, match="upload failed"):
            await manager.act("owner", sid, "upload", selector="#direct", files=payload * 2, timeout_ms=5_000)

        frames = (await manager.act("owner", sid, "frames"))["frames"]
        child = next(row["frame_id"] for row in frames if row["parent_frame_id"])
        await manager.act("owner", sid, "upload", selector="#inner", frame_id=child, files=payload, timeout_ms=5_000)
        assert await files_of("#inner", child) == ["photo.png", len(PHOTO), "image/png"]
    finally:
        await manager.shutdown()
        await artifacts.close()
        fixture_server.close()
        await fixture_server.wait_closed()
