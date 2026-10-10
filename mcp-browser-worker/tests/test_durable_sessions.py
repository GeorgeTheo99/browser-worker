from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any

import pytest
from starlette.requests import Request

import browser_runtime
import security
import server
from artifacts import ArtifactStore
from auth import ClientRegistry
from browser_runtime import BrowserManager, WorkerError, safe_download_name
from tests.test_browser_runtime import BrowserFixtureResolver
from tests.test_lifecycle import fake_manager


async def _shutdown_fake(manager: BrowserManager) -> None:
    manager._playwright = None  # the fake runtime has no driver to stop
    await manager.shutdown()


@pytest.mark.asyncio
async def test_durable_profile_reattaches_and_survives_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, artifacts = await fake_manager(tmp_path, monkeypatch)
    first = await manager.open("owner", None, wait_until="domcontentloaded", timeout_ms=1000, profile="default")
    assert first["profile"] == "default"
    assert int(first["idle_expires_in_seconds"]) > browser_runtime.IDLE_TTL_SECONDS
    again = await manager.open("owner", None, wait_until="domcontentloaded", timeout_ms=1000, profile="default")
    assert again["session_id"] == first["session_id"] and again["reattached"] is True
    assert manager.live_sessions == 1

    other = await manager.open("other", None, wait_until="domcontentloaded", timeout_ms=1000, profile="default")
    assert other["session_id"] != first["session_id"]
    owner_dirs = sorted((tmp_path / "profiles").iterdir())
    assert len(owner_dirs) == 2 and all((path / "default").is_dir() for path in owner_dirs)
    assert all(path.stat().st_mode & 0o077 == 0 for path in owner_dirs)

    await manager.close("owner", str(first["session_id"]))
    assert all((path / "default").is_dir() for path in owner_dirs)
    assert list((tmp_path / "sessions").iterdir()) == []
    reopened = await manager.open("owner", None, wait_until="domcontentloaded", timeout_ms=1000, profile="default")
    assert reopened["session_id"] != first["session_id"] and "reattached" not in reopened
    await _shutdown_fake(manager)
    assert all((path / "default").is_dir() for path in owner_dirs)
    await artifacts.close()


@pytest.mark.asyncio
async def test_durable_profile_rejects_scopes_and_unsafe_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, artifacts = await fake_manager(tmp_path, monkeypatch)
    for kwargs in ({"profile": "default", "scope_id": "run"}, {"profile": "../escape"}, {"profile": "Upper"}):
        with pytest.raises(WorkerError):
            await manager.open("owner", None, wait_until="domcontentloaded", timeout_ms=1000, **kwargs)
    assert manager.live_sessions == 0
    assert not (tmp_path / "profiles").exists() or list((tmp_path / "profiles").iterdir()) == []
    await artifacts.close()


@pytest.mark.asyncio
async def test_durable_profile_uses_long_ttl_and_ephemeral_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, artifacts = await fake_manager(tmp_path, monkeypatch)
    durable = await manager.open("owner", None, wait_until="domcontentloaded", timeout_ms=1000, profile="default")
    ephemeral = await manager.open("owner", None, wait_until="domcontentloaded", timeout_ms=1000)
    stale = time.monotonic() - browser_runtime.IDLE_TTL_SECONDS - 1
    for state in (durable, ephemeral):
        manager._sessions[str(state["session_id"])].touched_mono = stale
    await manager.sweep()
    assert set(manager._sessions) == {durable["session_id"]}
    await _shutdown_fake(manager)
    await artifacts.close()


def test_download_names_are_sanitized() -> None:
    assert safe_download_name("../../etc/passwd") == "passwd"
    assert safe_download_name('report "Q3".pdf') == "report _Q3_.pdf"
    assert safe_download_name("..") == "download"
    assert len(safe_download_name("a" * 500 + ".zip")) == 200


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"type": "click", "x": 10, "y": 20}, {"type": "click", "x": 10.0, "y": 20.0, "button": "left", "click_count": 1}),
        ({"type": "wheel", "x": 1, "y": 1, "dy": 400}, {"type": "wheel", "x": 1.0, "y": 1.0, "dx": 0.0, "dy": 400.0}),
        ({"type": "key", "key": "Control+a"}, {"type": "key", "key": "Control+a"}),
        ({"type": "key", "key": "Enter"}, {"type": "key", "key": "Enter"}),
        ({"type": "text", "text": "hello"}, {"type": "text", "text": "hello"}),
        ({"type": "navigate", "url": "https://example.com/"}, {"type": "navigate", "url": "https://example.com/"}),
        ({"type": "back"}, {"type": "back"}),
    ],
)
def test_live_input_accepts_bounded_events(body: dict[str, Any], expected: dict[str, Any]) -> None:
    assert server.parse_live_input(body) == expected


@pytest.mark.parametrize(
    "body",
    [
        None,
        {"type": "click", "x": -1, "y": 0},
        {"type": "click", "x": 0, "y": 99_999},
        {"type": "click", "x": float("nan"), "y": 0},
        {"type": "click", "x": True, "y": 0},
        {"type": "click", "x": 0, "y": 0, "selector": "#a"},
        {"type": "key", "key": "a b"},
        {"type": "text", "text": ""},
        {"type": "navigate", "url": "http://127.0.0.1/"},
        {"type": "navigate", "url": "file:///etc/passwd"},
        {"type": "evaluate"},
    ],
)
def test_live_input_rejects_invalid_events(body: Any) -> None:
    with pytest.raises(WorkerError):
        server.parse_live_input(body)


def _route_request(path: str, token: str | None, *, method: str = "GET", session_id: str | None = None) -> Request:
    headers = [] if token is None else [(b"authorization", f"Bearer {token}".encode())]
    scope: dict[str, Any] = {"type": "http", "method": method, "path": path, "headers": headers, "path_params": {}}
    if session_id is not None:
        scope["path_params"] = {"session_id": session_id}
    return Request(scope)


@pytest.mark.asyncio
async def test_live_routes_require_live_capability_and_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path.chmod(0o700)
    tokens = tmp_path / "tokens"
    tokens.mkdir(mode=0o700)
    clients = []
    for name, capabilities in (("viewer", ["inspect.read", "inspect.live"]), ("reader", ["inspect.read"])):
        path = tokens / name
        path.write_text(name[0] * 48, encoding="utf-8")
        path.chmod(0o600)
        clients.append({"id": name, "token_file": f"tokens/{name}", "capabilities": capabilities})
    config = tmp_path / "clients.json"
    config.write_text(json.dumps({"version": 1, "clients": clients}), encoding="utf-8")
    config.chmod(0o600)
    registry = ClientRegistry(config, tmp_path)
    registry.load()
    manager, artifacts = await fake_manager(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "registry", registry)
    monkeypatch.setattr(server, "manager", manager)
    state = await manager.open("viewer", None, wait_until="domcontentloaded", timeout_ms=1000, profile="default")
    session_id = str(state["session_id"])

    assert (await server.list_sessions(_route_request("/sessions", None))).status_code == 401
    assert (await server.list_sessions(_route_request("/sessions", "r" * 48))).status_code == 403
    listed = await server.list_sessions(_route_request("/sessions", "v" * 48))
    assert json.loads(listed.body)["sessions"][0]["session_id"] == session_id
    assert json.loads(listed.body)["sessions"][0]["profile"] == "default"
    missing = await server.live_view(_route_request("/x", "v" * 48, session_id="not-a-session"))
    assert missing.status_code == 404
    denied = await server.live_view(_route_request("/x", "r" * 48, session_id=session_id))
    assert denied.status_code == 403
    await _shutdown_fake(manager)
    await artifacts.close()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_durable_profile_downloads_and_live_takeover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"quarterly,report\n1,2\n"

    async def fixture(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.read(65536)
        first = head.split(b"\r\n", 1)[0].decode("latin-1")
        if " /file.csv " in first:
            headers = b"Content-Type: text/csv\r\nContent-Disposition: attachment; filename=\"q3 report.csv\"\r\n"
            body = payload
        else:
            headers = b"Content-Type: text/html\r\n"
            body = (
                b"<!doctype html><title>Durable</title><body style='margin:0'>"
                b"<button id=b style='position:fixed;left:0;top:0;width:200px;height:80px' "
                b"onclick=\"document.title='clicked';document.querySelector('#i').focus()\">Go</button>"
                b"<input id=i style='position:fixed;left:0;top:100px'>"
                b"<a id=dl href='/file.csv' style='position:fixed;left:0;top:200px'>download</a></body>"
            )
        writer.write(
            b"HTTP/1.1 200 OK\r\n" + headers + b"Content-Length: " + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n" + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    fixture_server = await asyncio.start_server(fixture, "127.0.0.1", 0)
    port = int((fixture_server.sockets or [])[0].getsockname()[1])
    monkeypatch.setattr(security, "ALLOWED_DESTINATION_PORTS", frozenset({80, 443, port}))
    artifacts = ArtifactStore(tmp_path / "artifacts")
    await artifacts.start()
    manager = BrowserManager(
        artifacts, resolver_factory=lambda: BrowserFixtureResolver(port), sessions_dir=tmp_path / "sessions"
    )
    await manager.start()
    url = f"http://public.test:{port}/"
    try:
        opened = await manager.open(
            "owner", url, wait_until="domcontentloaded", timeout_ms=15_000, profile="default", downloads=True
        )
        session_id = str(opened["session_id"])
        await manager.act(
            "owner", session_id, "evaluate", script="document.cookie='canary=present; path=/; max-age=3600'"
        )

        events = manager.live_events("owner", session_id)
        seen: dict[str, dict[str, object]] = {}
        async with asyncio.timeout(15):
            while "frame" not in seen or "state" not in seen:
                event = await anext(events)
                seen[str(event["event"])] = event
        assert seen["state"]["url"] == url
        assert base64.b64decode(str(seen["frame"]["data"])).startswith(b"\xff\xd8")
        assert seen["frame"]["width"] == browser_runtime.VIEWPORT_WIDTH

        await manager.human_input("owner", session_id, {"type": "click", "x": 50.0, "y": 30.0, "button": "left", "click_count": 1})
        await manager.human_input("owner", session_id, {"type": "text", "text": "typed by a person"})
        title = await manager.act("owner", session_id, "evaluate", script="[document.title, document.querySelector('#i').value]")
        assert title["result"] == ["clicked", "typed by a person"]
        await events.aclose()

        await manager.act("owner", session_id, "click", selector="#dl")
        async with asyncio.timeout(15):
            while True:
                listed = await manager.act("owner", session_id, "downloads")
                rows = listed["downloads"]
                if rows and rows[0]["status"] != "in_progress":
                    break
                await asyncio.sleep(0.1)
        assert rows[0]["status"] == "completed", rows
        assert rows[0]["filename"] == "q3 report.csv"
        assert rows[0]["url"] == f"{url}file.csv"
        record, data = await artifacts.read("owner", rows[0]["artifact"]["artifact_id"])
        assert data == payload and record.mime_type == "text/csv" and record.name == "q3 report.csv"

        await manager.close("owner", session_id)
        reopened = await manager.open(
            "owner", url, wait_until="domcontentloaded", timeout_ms=15_000, profile="default"
        )
        cookie = await manager.act("owner", str(reopened["session_id"]), "evaluate", script="document.cookie")
        assert "canary=present" in cookie["result"]
        assert list((tmp_path / "sessions").iterdir()) == []
    finally:
        await manager.shutdown()
        await artifacts.close()
        fixture_server.close()
        await fixture_server.wait_closed()
