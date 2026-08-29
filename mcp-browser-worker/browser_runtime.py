from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import stat
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from patchright.async_api import (
    BrowserContext,
    ConsoleMessage,
    Page,
    Playwright,
    async_playwright,
)

from artifacts import ArtifactStore
from config import (
    ABSOLUTE_TTL_SECONDS,
    BROWSER_EXECUTABLE,
    DEFAULT_TIMEOUT_MS,
    HEADLESS,
    IDLE_TTL_SECONDS,
    MAX_ADMITTED_OPERATIONS,
    MAX_CALLER_SESSIONS,
    MAX_CAPTURE_HEIGHT,
    MAX_CAPTURE_PIXELS,
    MAX_CONCURRENT_OPERATIONS,
    MAX_GLOBAL_SESSIONS,
    MAX_LINKS,
    MAX_PDF_BYTES,
    MAX_SCREENSHOT_BYTES,
    MAX_TABS_PER_SESSION,
    MAX_TEXT_CHARS,
    SESSIONS_DIR,
    VIEWPORT_HEIGHT,
    VIEWPORT_WIDTH,
)
from security import (
    NetworkPolicyError,
    PublicEgressProxy,
    PublicResolver,
    parse_public_url,
    resolve_public_url,
)

WaitUntil = Literal["load", "domcontentloaded", "networkidle", "commit"]
logger = logging.getLogger("browser-worker.runtime")


class WorkerError(RuntimeError):
    pass


def chromium_hardening_args() -> list[str]:
    return [
        "--disable-blink-features=AutomationControlled",
        "--disable-quic",
        "--proxy-bypass-list=<-loopback>",
        "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--webrtc-ip-handling-policy=disable_non_proxied_udp",
    ]


@dataclass(slots=True)
class BrowserSession:
    id: str
    owner: str
    created_mono: float
    touched_mono: float
    profile_dir: Path
    proxy: PublicEgressProxy
    context: BrowserContext
    active_index: int = 0
    console: list[dict[str, object]] = field(default_factory=list)
    tracked_pages: set[Page] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    dispose_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    disposed: bool = False


class BrowserManager:
    def __init__(
        self,
        artifact_store: ArtifactStore,
        *,
        resolver_factory: Any = PublicResolver,
        sessions_dir: Path = SESSIONS_DIR,
    ) -> None:
        self.artifacts = artifact_store
        self.resolver_factory = resolver_factory
        self.sessions_dir = sessions_dir
        self._playwright: Playwright | None = None
        self._sessions: dict[str, BrowserSession] = {}
        self._starting_global = 0
        self._starting_by_owner: dict[str, int] = {}
        self._state_lock = asyncio.Lock()
        self._admission_lock = asyncio.Lock()
        self._admitted = 0
        self._operation_slots = asyncio.Semaphore(MAX_CONCURRENT_OPERATIONS)
        self._sweeper: asyncio.Task[None] | None = None

    @property
    def live_sessions(self) -> int:
        return len(self._sessions)

    async def start(self) -> None:
        if self._playwright is not None:
            return
        self.sessions_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.sessions_dir.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()
        ):
            raise WorkerError("session root must be a service-owned directory")
        os.chmod(self.sessions_dir, 0o700)
        for child in self.sessions_dir.iterdir():
            if child.is_symlink() or child.is_file():
                child.unlink(missing_ok=True)
            elif child.is_dir():
                shutil.rmtree(child)
        self._playwright = await async_playwright().start()
        self._sweeper = asyncio.create_task(self._sweep_loop(), name="browser-worker-sweeper")

    async def shutdown(self) -> None:
        sweeper = self._sweeper
        self._sweeper = None
        if sweeper is not None:
            sweeper.cancel()
            await asyncio.gather(sweeper, return_exceptions=True)
        async with self._state_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        await asyncio.gather(
            *(self._dispose_after_action(session) for session in sessions), return_exceptions=True
        )
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(15)
            await self.sweep()
            await self.artifacts.sweep()

    async def sweep(self) -> None:
        now = time.monotonic()
        async with self._state_lock:
            expired = [
                session
                for session in self._sessions.values()
                if now - session.touched_mono > IDLE_TTL_SECONDS
                or now - session.created_mono > ABSOLUTE_TTL_SECONDS
            ]
            for session in expired:
                self._sessions.pop(session.id, None)
        await asyncio.gather(
            *(self._dispose_after_action(session) for session in expired), return_exceptions=True
        )

    @asynccontextmanager
    async def operation(self):
        async with self._admission_lock:
            if self._admitted >= MAX_ADMITTED_OPERATIONS:
                raise WorkerError("browser worker is at its operation admission limit")
            self._admitted += 1
        try:
            async with self._operation_slots:
                yield
        finally:
            async with self._admission_lock:
                self._admitted -= 1

    async def _new_context(self, profile_dir: Path, proxy: PublicEgressProxy) -> BrowserContext:
        if self._playwright is None:
            raise WorkerError("browser runtime is not started")
        port = await proxy.start()
        args = chromium_hardening_args()
        options: dict[str, Any] = {
            "headless": HEADLESS,
            "accept_downloads": False,
            "service_workers": "block",
            "ignore_https_errors": False,
            "viewport": {"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            "channel": "chromium",
            "proxy": {"server": f"http://127.0.0.1:{port}", "bypass": ""},
            "args": args,
        }
        if BROWSER_EXECUTABLE:
            options.pop("channel", None)
            options["executable_path"] = BROWSER_EXECUTABLE
        context = await self._playwright.chromium.launch_persistent_context(str(profile_dir), **options)
        context.set_default_timeout(DEFAULT_TIMEOUT_MS)
        context.set_default_navigation_timeout(DEFAULT_TIMEOUT_MS)
        await context.add_init_script(
            """
            (() => {
              for (const name of ['RTCPeerConnection', 'webkitRTCPeerConnection']) {
                try { Object.defineProperty(globalThis, name, { value: undefined, configurable: false }); } catch {}
              }
            })();
            """
        )
        return context

    def _attach_page(self, session: BrowserSession, page: Page) -> None:
        if page in session.tracked_pages:
            return
        session.tracked_pages.add(page)

        def on_console(message: ConsoleMessage) -> None:
            try:
                index = session.context.pages.index(page)
            except ValueError:
                index = -1
            session.console.append(
                {
                    "timestamp": time.time(),
                    "tab_index": index,
                    "type": message.type,
                    "text": message.text[:2000],
                }
            )
            del session.console[:-200]

        page.on("console", on_console)
        page.on("download", lambda download: asyncio.create_task(download.cancel()))

    async def open(self, owner: str, url: str | None, *, wait_until: WaitUntil, timeout_ms: int) -> dict[str, object]:
        async with self.operation():
            await self.sweep()
            async with self._state_lock:
                owner_count = sum(session.owner == owner for session in self._sessions.values())
                owner_count += self._starting_by_owner.get(owner, 0)
                if len(self._sessions) + self._starting_global >= MAX_GLOBAL_SESSIONS:
                    raise WorkerError("browser worker session capacity reached")
                if owner_count >= MAX_CALLER_SESSIONS:
                    raise WorkerError("caller session capacity reached")
                self._starting_global += 1
                self._starting_by_owner[owner] = self._starting_by_owner.get(owner, 0) + 1
                session_id = secrets.token_urlsafe(24)
                profile_dir = self.sessions_dir / session_id
                profile_dir.mkdir(mode=0o700)
            proxy = PublicEgressProxy(self.resolver_factory())
            context: BrowserContext | None = None
            session: BrowserSession | None = None
            inserted = False
            try:
                context = await self._new_context(profile_dir, proxy)
                now = time.monotonic()
                session = BrowserSession(session_id, owner, now, now, profile_dir, proxy, context)
                for page in context.pages:
                    self._attach_page(session, page)
                context.on("page", lambda page: self._attach_page(session, page))
                context.on(
                    "close",
                    lambda *_args: asyncio.create_task(self._handle_context_closed(session)),
                )
                if not context.pages:
                    page = await context.new_page()
                    self._attach_page(session, page)
                async with self._state_lock:
                    self._sessions[session_id] = session
                    inserted = True
                if url:
                    await self._navigate(session, url, wait_until=wait_until, timeout_ms=timeout_ms)
                return await self._state(session)
            except BaseException:
                if inserted:
                    async with self._state_lock:
                        self._sessions.pop(session_id, None)
                if session is not None:
                    await asyncio.shield(self._dispose(session))
                else:
                    if context is not None:
                        try:
                            await asyncio.shield(context.close())
                        except BaseException as exc:  # noqa: BLE001 - cancellation cleanup continues
                            logger.debug("partial browser cleanup ended: %s", type(exc).__name__)
                    try:
                        await asyncio.shield(proxy.close())
                    except BaseException as exc:  # noqa: BLE001 - profile cleanup must still run
                        logger.debug("partial proxy cleanup ended: %s", type(exc).__name__)
                    shutil.rmtree(profile_dir, ignore_errors=True)
                raise
            finally:
                async with self._state_lock:
                    self._starting_global -= 1
                    remaining = self._starting_by_owner.get(owner, 1) - 1
                    if remaining > 0:
                        self._starting_by_owner[owner] = remaining
                    else:
                        self._starting_by_owner.pop(owner, None)

    async def _get(self, owner: str, session_id: str) -> BrowserSession:
        await self.sweep()
        async with self._state_lock:
            session = self._sessions.get(session_id)
        if session is None or session.owner != owner:
            raise WorkerError("browser session not found")
        return session

    async def close(self, owner: str, session_id: str) -> dict[str, object]:
        async with self._state_lock:
            session = self._sessions.get(session_id)
            if session is None or session.owner != owner:
                raise WorkerError("browser session not found")
            self._sessions.pop(session_id, None)
        await self._dispose_after_action(session)
        return {"status": "closed", "session_id": session_id}

    async def _handle_context_closed(self, session: BrowserSession) -> None:
        async with self._state_lock:
            if self._sessions.get(session.id) is session:
                self._sessions.pop(session.id, None)
        if session.disposed:
            return
        await self._dispose_after_action(session)

    async def _dispose_after_action(self, session: BrowserSession) -> None:
        try:
            async with asyncio.timeout(12):
                async with session.lock:
                    await self._dispose(session)
        except TimeoutError:
            await self._dispose(session)

    async def _dispose(self, session: BrowserSession) -> None:
        async def cleanup() -> None:
            async with session.dispose_lock:
                if session.disposed:
                    return
                session.disposed = True
                try:
                    async with asyncio.timeout(5):
                        await session.context.close()
                except Exception as exc:  # noqa: BLE001 - cleanup must continue after browser-driver failures
                    logger.debug("browser context cleanup failed: %s", type(exc).__name__)
                try:
                    async with asyncio.timeout(5):
                        await session.proxy.close()
                except Exception as exc:  # noqa: BLE001 - filesystem cleanup must still run
                    logger.warning("browser proxy cleanup failed: %s", type(exc).__name__)
                finally:
                    shutil.rmtree(session.profile_dir, ignore_errors=True)

        task = asyncio.create_task(cleanup(), name=f"browser-dispose-{session.id}")
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                async with asyncio.timeout(12):
                    await asyncio.shield(task)
            finally:
                raise

    @staticmethod
    def _pages(session: BrowserSession) -> list[Page]:
        return [page for page in session.context.pages if not page.is_closed()]

    def _active_page(self, session: BrowserSession) -> Page:
        pages = self._pages(session)
        if not pages:
            raise WorkerError("browser session has no open tabs")
        session.active_index = min(max(session.active_index, 0), len(pages) - 1)
        return pages[session.active_index]

    async def _navigate(self, session: BrowserSession, url: str, *, wait_until: WaitUntil, timeout_ms: int) -> None:
        await resolve_public_url(url, session.proxy.resolver)
        page = self._active_page(session)
        navigation = asyncio.create_task(
            page.goto(url, wait_until=wait_until, timeout=timeout_ms),
            name=f"browser-navigate-{session.id}",
        )
        try:
            await navigation
            await resolve_public_url(page.url, session.proxy.resolver)
        except asyncio.CancelledError:
            navigation.cancel()
            await asyncio.gather(navigation, return_exceptions=True)
            raise
        except NetworkPolicyError as exc:
            raise WorkerError("browser navigation was blocked by public-network policy") from exc
        except Exception as exc:
            raise WorkerError("browser navigation failed") from exc
        session.touched_mono = time.monotonic()

    async def navigate(self, owner: str, session_id: str, url: str, *, wait_until: WaitUntil, timeout_ms: int) -> dict[str, object]:
        async with self.operation():
            session = await self._get(owner, session_id)
            async with session.lock:
                await self._navigate(session, url, wait_until=wait_until, timeout_ms=timeout_ms)
                return await self._state(session)

    async def _state(self, session: BrowserSession) -> dict[str, object]:
        pages = self._pages(session)
        now = time.monotonic()
        return {
            "status": "ok",
            "session_id": session.id,
            "active_tab": session.active_index,
            "tabs": [
                {"index": index, "url": page.url, "title": (await page.title())[:500]}
                for index, page in enumerate(pages)
            ],
            "idle_expires_in_seconds": max(0, int(IDLE_TTL_SECONDS - (now - session.touched_mono))),
            "absolute_expires_in_seconds": max(0, int(ABSOLUTE_TTL_SECONDS - (now - session.created_mono))),
        }

    async def _ensure_capture_bounds(self, page: Page) -> None:
        try:
            dimensions = await page.evaluate(
                """() => {
                  const root = document.documentElement;
                  const body = document.body;
                  return {
                    width: Math.max(root?.scrollWidth || 0, body?.scrollWidth || 0, innerWidth || 0),
                    height: Math.max(root?.scrollHeight || 0, body?.scrollHeight || 0, innerHeight || 0)
                  };
                }"""
            )
            width = int(dimensions.get("width", 0))
            height = int(dimensions.get("height", 0))
        except Exception as exc:
            raise WorkerError("page dimensions could not be validated") from exc
        if width < 1 or height < 1 or height > MAX_CAPTURE_HEIGHT or width * height > MAX_CAPTURE_PIXELS:
            raise WorkerError("page is too large for a bounded artifact capture")

    async def act(self, owner: str, session_id: str, action: str, **params: Any) -> dict[str, object]:
        try:
            return await self._act_impl(owner, session_id, action, **params)
        except asyncio.CancelledError:
            async with self._state_lock:
                session = self._sessions.get(session_id)
                if session is not None and session.owner == owner:
                    self._sessions.pop(session_id, None)
                else:
                    session = None
            if session is not None:
                await self._dispose_after_action(session)
            raise

    async def _act_impl(self, owner: str, session_id: str, action: str, **params: Any) -> dict[str, object]:
        if action == "close":
            return await self.close(owner, session_id)
        async with self.operation():
            session = await self._get(owner, session_id)
            async with session.lock:
                session.touched_mono = time.monotonic()
                page = self._active_page(session)
                timeout_ms = int(params.get("timeout_ms") or DEFAULT_TIMEOUT_MS)
                if action == "state":
                    return await self._state(session)
                if action == "navigate":
                    await self._navigate(
                        session,
                        str(params["url"]),
                        wait_until=params.get("wait_until", "domcontentloaded"),
                        timeout_ms=timeout_ms,
                    )
                    return await self._state(session)
                if action == "open_tab":
                    if len(self._pages(session)) >= MAX_TABS_PER_SESSION:
                        raise WorkerError("browser session tab limit reached")
                    url = params.get("url")
                    if url:
                        await resolve_public_url(str(url), session.proxy.resolver)
                    new_page = await session.context.new_page()
                    self._attach_page(session, new_page)
                    session.active_index = len(self._pages(session)) - 1
                    if url:
                        await self._navigate(
                            session,
                            str(url),
                            wait_until=params.get("wait_until", "domcontentloaded"),
                            timeout_ms=timeout_ms,
                        )
                    return await self._state(session)
                if action == "list_tabs":
                    return await self._state(session)
                if action == "switch_tab":
                    index = int(params["tab_index"])
                    pages = self._pages(session)
                    if not 0 <= index < len(pages):
                        raise WorkerError("tab index is out of range")
                    session.active_index = index
                    await pages[index].bring_to_front()
                    return await self._state(session)
                if action == "close_tab":
                    index = int(params.get("tab_index", session.active_index))
                    pages = self._pages(session)
                    if not 0 <= index < len(pages):
                        raise WorkerError("tab index is out of range")
                    await pages[index].close()
                    session.active_index = max(0, min(session.active_index, len(self._pages(session)) - 1))
                    return await self._state(session)
                if action == "extract_text":
                    selector = params.get("selector")
                    max_chars = min(MAX_TEXT_CHARS, int(params.get("max_chars") or 20_000))
                    try:
                        if selector:
                            text = await page.locator(str(selector)).evaluate(
                                "(el, limit) => (el.innerText || el.textContent || '').slice(0, limit + 1)",
                                max_chars,
                            )
                        else:
                            text = await page.locator("body").evaluate(
                                "(el, limit) => (el.innerText || el.textContent || '').slice(0, limit + 1)",
                                max_chars,
                            )
                    except Exception as exc:
                        raise WorkerError("text extraction failed") from exc
                    return {
                        "status": "ok",
                        "session_id": session.id,
                        "url": page.url,
                        "title": (await page.title())[:500],
                        "text": str(text)[:max_chars],
                        "truncated": len(str(text)) > max_chars,
                    }
                if action == "extract_links":
                    selector = str(params.get("selector") or "a[href]")
                    limit = min(MAX_LINKS, int(params.get("limit") or 50))
                    try:
                        raw = await page.locator(selector).evaluate_all(
                            "(els, limit) => els.slice(0, limit).map(el => ({text:(el.innerText||el.textContent||'').trim(), url:el.href||''}))",
                            min(400, limit * 4),
                        )
                    except Exception as exc:
                        raise WorkerError("link extraction failed") from exc
                    links: list[dict[str, str]] = []
                    for row in raw if isinstance(raw, list) else []:
                        if not isinstance(row, dict):
                            continue
                        try:
                            parse_public_url(str(row.get("url") or ""))
                        except NetworkPolicyError:
                            continue
                        links.append({"text": str(row.get("text") or "")[:500], "url": str(row.get("url"))[:8192]})
                        if len(links) >= limit:
                            break
                    return {"status": "ok", "session_id": session.id, "links": links}
                if action == "click":
                    try:
                        await page.locator(str(params["selector"])).click(timeout=timeout_ms)
                    except Exception as exc:
                        raise WorkerError("click failed") from exc
                    return await self._state(session)
                if action == "type":
                    locator = page.locator(str(params["selector"]))
                    try:
                        if params.get("clear", True):
                            await locator.fill(str(params["text"]), timeout=timeout_ms)
                        else:
                            await locator.press_sequentially(str(params["text"]), timeout=timeout_ms)
                        if params.get("submit", False):
                            await locator.press("Enter", timeout=timeout_ms)
                    except Exception as exc:
                        raise WorkerError("typing failed") from exc
                    return {"status": "ok", "session_id": session.id, "url": page.url}
                if action == "wait":
                    selector = params.get("selector")
                    url_contains = params.get("url_contains")
                    if selector:
                        try:
                            await page.locator(str(selector)).wait_for(
                                state=params.get("state", "visible"), timeout=timeout_ms
                            )
                        except Exception as exc:
                            raise WorkerError("wait condition was not met") from exc
                    elif url_contains:
                        deadline = time.monotonic() + timeout_ms / 1000
                        while str(url_contains) not in page.url:
                            if time.monotonic() >= deadline:
                                raise WorkerError("wait condition was not met")
                            await asyncio.sleep(0.05)
                    else:
                        try:
                            await page.wait_for_load_state(params.get("wait_until", "domcontentloaded"), timeout=timeout_ms)
                        except Exception as exc:
                            raise WorkerError("wait condition was not met") from exc
                    return await self._state(session)
                if action == "screenshot":
                    await self._ensure_capture_bounds(page)
                    try:
                        data = await page.screenshot(full_page=bool(params.get("full_page", True)), type="png")
                    except Exception as exc:
                        raise WorkerError("screenshot failed") from exc
                    record = await self.artifacts.create(owner, data, suffix="png", mime_type="image/png", max_bytes=MAX_SCREENSHOT_BYTES)
                    return {"status": "ok", "session_id": session.id, "artifact": record.public()}
                if action == "export_pdf":
                    await self._ensure_capture_bounds(page)
                    try:
                        data = await page.pdf(
                            format=str(params.get("format") or "A4"),
                            landscape=bool(params.get("landscape", False)),
                            print_background=bool(params.get("print_background", True)),
                        )
                    except Exception as exc:
                        raise WorkerError("PDF export failed") from exc
                    record = await self.artifacts.create(owner, data, suffix="pdf", mime_type="application/pdf", max_bytes=MAX_PDF_BYTES)
                    return {"status": "ok", "session_id": session.id, "artifact": record.public()}
                if action == "console":
                    limit = min(50, int(params.get("limit") or 50))
                    return {"status": "ok", "session_id": session.id, "events": session.console[-limit:]}
                if action == "evaluate":
                    try:
                        result = await page.evaluate(str(params["script"]), params.get("arg"))
                        encoded = json.dumps(result, allow_nan=False)
                        if len(encoded) > MAX_TEXT_CHARS:
                            raise ValueError("evaluation result is too large")
                    except Exception as exc:
                        raise WorkerError("page evaluation failed or returned a non-JSON value") from exc
                    return {"status": "ok", "session_id": session.id, "result": result}
                raise WorkerError("unsupported browser action")

    async def fetch(
        self,
        owner: str,
        url: str,
        *,
        max_chars: int,
        wait_until: WaitUntil,
        timeout_ms: int,
        include_links: bool,
        include_screenshot: bool,
    ) -> dict[str, object]:
        started = time.monotonic()
        state = await self.open(owner, url, wait_until=wait_until, timeout_ms=timeout_ms)
        session_id = str(state["session_id"])
        try:
            session = await self._get(owner, session_id)
            async with session.lock:
                page = self._active_page(session)
                try:
                    await page.wait_for_load_state("networkidle", timeout=min(3000, timeout_ms))
                except Exception as exc:  # noqa: BLE001 - network idle is explicitly best-effort
                    logger.debug("best-effort network-idle wait ended: %s", type(exc).__name__)
                try:
                    text = await page.locator("body").evaluate(
                        "(el, limit) => (el.innerText || el.textContent || '').slice(0, limit + 1)",
                        max_chars,
                    )
                except Exception as exc:
                    raise WorkerError("rendered text extraction failed") from exc
                result: dict[str, object] = {
                    "status": "ok",
                    "requested_url": url,
                    "final_url": page.url,
                    "title": (await page.title())[:500],
                    "text": text[:max_chars],
                    "truncated": len(text) > max_chars,
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                }
                if include_links:
                    raw_links = await page.locator("a[href]").evaluate_all(
                        "(els, limit) => els.slice(0, limit).map(el => ({text:(el.innerText||el.textContent||'').trim(), url:el.href||''}))",
                        400,
                    )
                    links: list[dict[str, str]] = []
                    for row in raw_links if isinstance(raw_links, list) else []:
                        if not isinstance(row, dict):
                            continue
                        try:
                            parse_public_url(str(row.get("url") or ""))
                        except NetworkPolicyError:
                            continue
                        links.append(
                            {
                                "text": str(row.get("text") or "")[:500],
                                "url": str(row.get("url") or "")[:8192],
                            }
                        )
                        if len(links) >= MAX_LINKS:
                            break
                    result["links"] = links
                if include_screenshot:
                    await self._ensure_capture_bounds(page)
                    data = await page.screenshot(full_page=True, type="png")
                    record = await self.artifacts.create(owner, data, suffix="png", mime_type="image/png", max_bytes=MAX_SCREENSHOT_BYTES)
                    result["artifact"] = record.public()
                return result
        finally:
            try:
                await asyncio.shield(self.close(owner, session_id))
            except BaseException as exc:  # noqa: BLE001 - one-shot cleanup must not replace the result
                logger.warning("one-shot browser cleanup ended: %s", type(exc).__name__)
