from __future__ import annotations

import asyncio
import plistlib
from pathlib import Path

import pytest

import browser_runtime
import security
from artifacts import ArtifactStore
from browser_runtime import (
    BrowserManager,
    WorkerError,
    regular_user_agent,
)
from security import PublicResolver, ResolvedTarget


class BrowserFixtureResolver(PublicResolver):
    def __init__(self, port: int) -> None:
        self.port = port

    async def resolve(self, hostname: str, port: int) -> ResolvedTarget:
        if hostname in {"public.test", "other-public.test"} and port == self.port:
            return ResolvedTarget(hostname, port, ("127.0.0.1",))
        return await super().resolve(hostname, port)


@pytest.mark.browser
@pytest.mark.asyncio
async def test_ephemeral_rendered_session_and_private_subresource_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[str] = []
    slow_started = asyncio.Event()
    release_slow = asyncio.Event()

    async def fixture(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.read(65536)
        first = head.split(b"\r\n", 1)[0].decode("latin-1")
        requests.append(first)
        if " /slow " in first:
            slow_started.set()
            await release_slow.wait()
            writer.close()
            await writer.wait_closed()
            return
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

        typed = await manager.act("owner", session_id, "type", selector="#value", text="Changed title")
        assert typed["target_contenteditable"] is False
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

        slow_task = asyncio.create_task(
            manager.open(
                "owner",
                f"http://public.test:{port}/slow",
                wait_until="domcontentloaded",
                timeout_ms=15_000,
            )
        )
        await slow_started.wait()
        slow_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await slow_task
        assert manager.live_sessions == 0
        assert list((tmp_path / "sessions").iterdir()) == []
        release_slow.set()

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
        release_slow.set()
        await manager.shutdown()
        await artifacts.close()
        server.close()
        await server.wait_closed()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_fetch_in_open_session_keeps_it_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fixture(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        first = (await reader.read(65536)).split(b"\r\n", 1)[0].decode("latin-1")
        name = b"B" if " /b " in first else b"A"
        body = b"<!doctype html><title>" + name + b"</title><body><p>Page " + name + b"</p></body>"
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: "
            + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(fixture, "127.0.0.1", 0)
    port = int((server.sockets or [])[0].getsockname()[1])
    monkeypatch.setattr(security, "ALLOWED_DESTINATION_PORTS", frozenset({80, 443, port}))
    artifacts = ArtifactStore(tmp_path / "artifacts")
    await artifacts.start()
    manager = BrowserManager(
        artifacts, resolver_factory=lambda: BrowserFixtureResolver(port), sessions_dir=tmp_path / "sessions"
    )
    await manager.start()
    options = {"max_chars": 5000, "wait_until": "domcontentloaded", "timeout_ms": 15_000,
               "include_links": False, "include_screenshot": False}
    try:
        opened = await manager.open("owner", None, wait_until="domcontentloaded", timeout_ms=15_000)
        session_id = str(opened["session_id"])
        for name in ("a", "b"):
            fetched = await manager.fetch("owner", f"http://public.test:{port}/{name}", session_id=session_id, **options)
            assert fetched["session_id"] == session_id
            assert fetched["final_url"] == f"http://public.test:{port}/{name}"
            assert f"Page {name.upper()}" in str(fetched["text"])
        state = await manager.act("owner", session_id, "state")
        assert [tab["url"] for tab in state["tabs"]] == [f"http://public.test:{port}/b"]
        assert manager.live_sessions == 1

        with pytest.raises(WorkerError) as foreign:
            await manager.fetch("other-owner", f"http://public.test:{port}/a", session_id=session_id, **options)
        assert foreign.value.code == "invalid_session"
        with pytest.raises(security.NetworkPolicyError):
            await manager.fetch("owner", f"http://127.0.0.1:{port}/a", session_id=session_id, **options)
        assert manager.live_sessions == 1

        session = manager._sessions[session_id]
        await session.lock.acquire()
        try:
            with pytest.raises(WorkerError) as busy:
                await manager.fetch("owner", f"http://public.test:{port}/a", session_id=session_id, **options)
            assert busy.value.code == "session_busy"
        finally:
            session.lock.release()

        one_shot = await manager.fetch("owner", f"http://public.test:{port}/a", **options)
        assert "session_id" not in one_shot and manager.live_sessions == 1

        await manager.close("owner", session_id)
        with pytest.raises(WorkerError) as closed:
            await manager.fetch("owner", f"http://public.test:{port}/a", session_id=session_id, **options)
        assert closed.value.code == "invalid_session"
        assert manager.live_sessions == 0
    finally:
        await manager.shutdown()
        await artifacts.close()
        server.close()
        await server.wait_closed()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_read_waits_for_a_self_clearing_bot_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Cloudflare's "Just a moment..." page (its noscript text is no element), which reloads into the site once
    # its orchestrate script passes the browser, and one whose script never does.
    check = (
        b"<!doctype html><title>Just a moment...</title><body><p>Performing security verification</p>"
        b"<noscript><div id=challenge-error-text>Enable JavaScript</div></noscript>"
        b"<script src=/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1?KIND></script></body>"
    )
    pages = {
        "/check": check.replace(b"KIND", b"pass"),
        "/stuck": check.replace(b"KIND", b"stay"),
        "/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1?pass": b"setTimeout(() => location.replace('/site'), 1200)",
        # DataDome's interstitial: an iframe that loads the site once it passes.
        "/interstitial": b"<!doctype html><title>shop.test</title><body><script>var dd={'rt':'i'}</script>"
        b"<iframe src=/x.captcha-delivery.com/interstitial/?initialCid=1></iframe>"
        b"<script>setTimeout(() => location.replace('/site'), 1200)</script></body>",
        "/site": b"<!doctype html><title>Site</title><body><p>Site page</p>"
        b"<script src=/cdn-cgi/challenge-platform/scripts/jsd/main.js></script></body>",
    }

    async def fixture(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        path = (await reader.read(65536)).split(b"\r\n", 1)[0].decode("latin-1").split(" ")[1]
        body = pages.get(path, b"")
        kind = b"text/javascript" if path.startswith("/cdn-cgi/") else b"text/html"
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: " + kind + b"\r\nContent-Length: "
            + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(fixture, "127.0.0.1", 0)
    port = int((server.sockets or [])[0].getsockname()[1])
    monkeypatch.setattr(security, "ALLOWED_DESTINATION_PORTS", frozenset({80, 443, port}))
    artifacts = ArtifactStore(tmp_path / "artifacts")
    await artifacts.start()
    manager = BrowserManager(
        artifacts, resolver_factory=lambda: BrowserFixtureResolver(port), sessions_dir=tmp_path / "sessions"
    )
    await manager.start()
    options = {"max_chars": 5000, "wait_until": "domcontentloaded", "include_links": False, "include_screenshot": False}
    try:
        cleared = await manager.fetch("owner", f"http://public.test:{port}/check", timeout_ms=15_000, **options)
        assert cleared["final_url"] == f"http://public.test:{port}/site"
        assert "Site page" in str(cleared["text"])
        interstitial = await manager.fetch("owner", f"http://public.test:{port}/interstitial", timeout_ms=15_000, **options)
        assert interstitial["final_url"] == f"http://public.test:{port}/site"
        # A check that wants a person is returned as it is, once the wait (bounded by the call's timeout) ends.
        stuck = await manager.fetch("owner", f"http://public.test:{port}/stuck", timeout_ms=2_000, **options)
        assert stuck["title"] == "Just a moment..."
        assert "Performing security verification" in str(stuck["text"])

        # A person navigating in the live view watches the check themselves: nothing holds their next input.
        opened = await manager.open("owner", None, wait_until="domcontentloaded", timeout_ms=15_000)
        started = asyncio.get_running_loop().time()
        await manager.human_input(
            "owner", str(opened["session_id"]), {"type": "navigate", "url": f"http://public.test:{port}/stuck"}
        )
        assert asyncio.get_running_loop().time() - started < 5
    finally:
        await manager.shutdown()
        await artifacts.close()
        server.close()
        await server.wait_closed()


@pytest.mark.browser
@pytest.mark.asyncio
@pytest.mark.skipif(browser_runtime.HEADLESS, reason="a headless browser has no window")
async def test_windowed_browser_moves_its_window_into_the_corner(tmp_path: Path) -> None:
    artifacts = ArtifactStore(tmp_path / "artifacts")
    await artifacts.start()
    manager = BrowserManager(artifacts, sessions_dir=tmp_path / "sessions")
    await manager.start()
    try:
        opened = await manager.open("owner", None, wait_until="domcontentloaded", timeout_ms=15_000)
        session = manager._sessions[str(opened["session_id"])]
        await asyncio.gather(*session.tasks)
        page = session.context.pages[0]
        cdp = await session.context.new_cdp_session(page)
        window = await cdp.send("Browser.getWindowForTarget")
        bounds = (await cdp.send("Browser.getWindowBounds", {"windowId": window["windowId"]}))["bounds"]
        # Chrome keeps a sliver on screen, so the window ends far past the screen's right and bottom edges.
        assert bounds["left"] > 300 and bounds["top"] > 300
        assert len(await page.screenshot()) > 1000
    finally:
        await manager.shutdown()
        await artifacts.close()


def test_regular_user_agent_names_chrome_not_headless_chrome(tmp_path: Path) -> None:
    contents = tmp_path / "Chromium.app" / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    executable = contents / "MacOS" / "Chromium"
    (contents / "Info.plist").write_bytes(plistlib.dumps({"CFBundleShortVersionString": "149.0.7827.55"}))
    assert regular_user_agent(executable) == (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/149.0.0.0 Safari/537.36"
    )
    for broken in (b"not a plist", b"<?xml version='1.0'?><plist><dict><key>x", plistlib.dumps(["149"])):
        (contents / "Info.plist").write_bytes(broken)
        assert regular_user_agent(executable) is None
    assert regular_user_agent(tmp_path / "missing" / "MacOS" / "Chromium") is None
