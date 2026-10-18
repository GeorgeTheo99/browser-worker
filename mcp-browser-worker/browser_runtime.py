from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import plistlib
import re
import secrets
import shutil
import stat
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from patchright.async_api import (
    BrowserContext,
    ConsoleMessage,
    Download,
    ElementHandle,
    Frame,
    Page,
    Playwright,
    async_playwright,
)
from patchright.async_api import (
    Error as BrowserError,
)
from patchright.async_api import TimeoutError as BrowserTimeoutError

from approvals import ApprovalStore, discover_elements
from artifacts import ArtifactStore
from config import (
    ABSOLUTE_TTL_SECONDS,
    BROWSER_EXECUTABLE,
    DEFAULT_TIMEOUT_MS,
    DOWNLOAD_DEADLINE_SECONDS,
    HEADLESS,
    IDLE_TTL_SECONDS,
    MAX_ADMITTED_OPERATIONS,
    MAX_CALLER_SESSIONS,
    MAX_CAPTURE_HEIGHT,
    MAX_CAPTURE_PIXELS,
    MAX_CONCURRENT_OPERATIONS,
    MAX_DOWNLOAD_BYTES,
    MAX_GLOBAL_SESSIONS,
    MAX_LINKS,
    MAX_LIVE_VIEWERS_PER_SESSION,
    MAX_PDF_BYTES,
    MAX_SCREENSHOT_BYTES,
    MAX_SESSION_DOWNLOADS,
    MAX_TABS_PER_SESSION,
    MAX_TEXT_CHARS,
    PERSISTENT_ABSOLUTE_TTL_SECONDS,
    PERSISTENT_IDLE_TTL_SECONDS,
    PROFILES_DIR,
    SESSIONS_DIR,
    VIEWPORT_HEIGHT,
    VIEWPORT_WIDTH,
)
from controls import ControlStore
from errors import WorkerError, fail
from frames import FRAME_ACTIONS, NAVIGATION_INIT_SCRIPT, FrameStore, check_frame_access
from live import LiveView
from security import (
    NetworkPolicyError,
    PublicEgressProxy,
    PublicResolver,
    parse_public_url,
    resolve_public_url,
)

WaitUntil = Literal["load", "domcontentloaded", "networkidle", "commit"]
logger = logging.getLogger("browser-worker.runtime")

PROFILE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._ ()-]+")


def _private_dir(path: Path, *, label: str) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
        raise WorkerError(f"{label} must be a service-owned directory")
    os.chmod(path, 0o700)


def safe_download_name(suggested: str) -> str:
    name = _UNSAFE_FILENAME.sub("_", suggested.replace("\\", "/").rsplit("/", 1)[-1]).strip(" ._")
    return name[-200:] or "download"


def _tree_bytes(root: Path) -> int:
    total = 0
    for directory, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += os.lstat(os.path.join(directory, name)).st_size
            except OSError:
                continue
    return total


def chromium_hardening_args() -> list[str]:
    return [
        "--disable-blink-features=AutomationControlled",
        "--disable-quic",
        "--proxy-bypass-list=<-loopback>",
        "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--webrtc-ip-handling-policy=disable_non_proxied_udp",
    ]


def regular_user_agent(executable: Path) -> str | None:
    """Chrome's own user agent for this build, without the "HeadlessChrome" that sites treat as a bot.

    Built from the macOS app bundle's version, in Chrome's reduced form; None when it can't be read.
    """
    try:
        info = plistlib.loads((executable.parent.parent / "Info.plist").read_bytes())
    except Exception:  # noqa: BLE001 - a missing or malformed bundle keeps Chromium's own user agent
        return None
    if not isinstance(info, dict):
        return None
    major = str(info.get("CFBundleShortVersionString") or "").split(".")[0]
    if not major.isdigit():
        return None
    return (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36"
    )


@dataclass(slots=True)
class BrowserSession:
    id: str
    owner: str
    scope_id: str | None
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
    controls: ControlStore = field(default_factory=ControlStore)
    approvals: ApprovalStore = field(default_factory=ApprovalStore)
    frames: FrameStore = field(default_factory=FrameStore)
    profile: str | None = None
    idle_ttl: int = IDLE_TTL_SECONDS
    absolute_ttl: int = ABSOLUTE_TTL_SECONDS
    downloads_dir: Path | None = None
    downloads: list[dict[str, object]] = field(default_factory=list)
    tasks: set[asyncio.Task[None]] = field(default_factory=set)
    live: LiveView = field(default_factory=LiveView)

    @property
    def persistent(self) -> bool:
        return self.profile is not None

    def expired(self, now: float) -> bool:
        return now - self.touched_mono > self.idle_ttl or now - self.created_mono > self.absolute_ttl


def _remove_profile_dir(path: Path) -> None:
    """Delete a durable profile, or fail: a caller told "deleted" must not leave a sign-in on disk."""
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("durable profile delete failed")
    if path.exists():
        raise fail("operation_failed")


class BrowserManager:
    def __init__(
        self,
        artifact_store: ArtifactStore,
        *,
        resolver_factory: Any = PublicResolver,
        sessions_dir: Path = SESSIONS_DIR,
        profiles_dir: Path | None = None,
    ) -> None:
        self.artifacts = artifact_store
        self.resolver_factory = resolver_factory
        self.sessions_dir = sessions_dir
        # Durable profiles live beside the session root (PROFILES_DIR in production).
        self.profiles_dir = profiles_dir or sessions_dir.parent / PROFILES_DIR.name
        self._starting_profiles: set[tuple[str, str]] = set()
        self._profile_cleanups: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._playwright: Playwright | None = None
        self._user_agent: str | None = None
        self._sessions: dict[str, BrowserSession] = {}
        self._closed_scopes: dict[tuple[str, str], float] = {}
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
        _private_dir(self.sessions_dir, label="session root")
        # Durable profiles are never swept; only their owner-scoped root is validated.
        _private_dir(self.profiles_dir, label="profile root")
        for child in self.sessions_dir.iterdir():
            if child.is_symlink() or child.is_file():
                child.unlink(missing_ok=True)
            elif child.is_dir():
                shutil.rmtree(child)
        self._playwright = await async_playwright().start()
        if HEADLESS:
            self._user_agent = regular_user_agent(Path(BROWSER_EXECUTABLE or self._playwright.chromium.executable_path))
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
            self._closed_scopes.clear()
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
            expired = [session for session in self._sessions.values() if session.expired(now)]
            for session in expired:
                self._sessions.pop(session.id, None)
            self._closed_scopes = {
                key: closed_at
                for key, closed_at in self._closed_scopes.items()
                if now - closed_at <= ABSOLUTE_TTL_SECONDS
            }
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

    async def _new_context(
        self, profile_dir: Path, proxy: PublicEgressProxy, downloads_dir: Path | None = None
    ) -> BrowserContext:
        if self._playwright is None:
            raise WorkerError("browser runtime is not started")
        port = await proxy.start()
        args = chromium_hardening_args()
        # Bound the on-disk cache that durable profiles would otherwise accumulate.
        args.append("--disk-cache-size=104857600")
        options: dict[str, Any] = {
            "headless": HEADLESS,
            "accept_downloads": downloads_dir is not None,
            "service_workers": "block",
            "ignore_https_errors": False,
            "viewport": {"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            "channel": "chromium",
            "proxy": {"server": f"http://127.0.0.1:{port}", "bypass": ""},
            "args": args,
        }
        if downloads_dir is not None:
            options["downloads_path"] = str(downloads_dir)
        if self._user_agent:
            options["user_agent"] = self._user_agent
        if BROWSER_EXECUTABLE:
            options.pop("channel", None)
            options["executable_path"] = BROWSER_EXECUTABLE
        context = await self._playwright.chromium.launch_persistent_context(str(profile_dir), **options)
        context.set_default_timeout(DEFAULT_TIMEOUT_MS)
        context.set_default_navigation_timeout(DEFAULT_TIMEOUT_MS)
        await context.add_init_script(NAVIGATION_INIT_SCRIPT)
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
        self._spawn(session, self._no_passkey_prompt(page), f"browser-no-passkey-prompt-{session.id}")
        session.approvals.invalidate()
        page.on("framenavigated", lambda frame: session.approvals.invalidate() if frame == page.main_frame else None)
        page.on("close", session.approvals.invalidate)
        page.on("framenavigated", session.frames.invalidate)
        page.on("framedetached", session.frames.invalidate)
        session.controls.invalidate()
        page.on("framenavigated", lambda frame: session.controls.invalidate() if frame == page.main_frame else None)
        page.on("close", session.controls.invalidate)

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
        page.on("download", lambda download: self._on_download(session, download))

    async def _one_target(self, frame: Frame, selector: str, timeout_ms: int) -> ElementHandle:
        """The element a click or typing goes to: the selector's only match, or its only visible one.

        Sites repeat ids in hidden copies (Amazon's buy box has an Add to Cart per purchase option).
        """
        locator = frame.locator(selector)
        try:
            await locator.first.wait_for(state="attached", timeout=timeout_ms)
        except BrowserTimeoutError as exc:  # nothing matched in time
            raise fail("selector_not_found") from exc
        matches = [locator.nth(index) for index in range(await locator.count())]
        if len(matches) > 1:
            matches = [match for match in matches if await match.is_visible()]
            if len(matches) != 1:
                raise fail("selector_ambiguous")
        element = await matches[0].element_handle(timeout=timeout_ms) if matches else None
        if element is None:
            raise fail("selector_not_found")
        return element

    async def _no_passkey_prompt(self, page: Page) -> None:
        """Turn off Chrome's passkey prompt. A headless page can't show it, yet it takes the keyboard:
        Amazon's password page asks for a passkey on load, and then no key (nor Enter) reaches the page."""
        try:
            cdp = await page.context.new_cdp_session(page)
            await cdp.send("WebAuthn.enable", {"enableUI": False})
        except Exception:  # a page closed before this ran needs nothing
            logger.debug("passkey prompt left on for a closed page", exc_info=True)

    def _spawn(self, session: BrowserSession, coro: Any, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        session.tasks.add(task)
        task.add_done_callback(session.tasks.discard)

    def _on_download(self, session: BrowserSession, download: Download) -> None:
        if session.downloads_dir is None or session.disposed:
            self._spawn(session, download.cancel(), f"browser-download-cancel-{session.id}")
            return
        self._spawn(session, self._capture_download(session, download), f"browser-download-{session.id}")

    async def _capture_download(self, session: BrowserSession, download: Download) -> None:
        """Retain one completed download as an owner-bound artifact, bounded in size and time."""
        assert session.downloads_dir is not None
        name = safe_download_name(download.suggested_filename)
        try:
            source_url = parse_public_url(download.url).geturl()
        except NetworkPolicyError:
            source_url = ""
        entry: dict[str, object] = {
            "download_id": secrets.token_urlsafe(12),
            "filename": name,
            "url": source_url[:8192],
            "status": "in_progress",
        }
        session.downloads.append(entry)
        del session.downloads[:-MAX_SESSION_DOWNLOADS]
        completion = asyncio.create_task(download.path())
        deadline = time.monotonic() + DOWNLOAD_DEADLINE_SECONDS
        path: Path | None = None
        try:
            while not completion.done():
                await asyncio.wait({completion}, timeout=0.5)
                if completion.done():
                    break
                if _tree_bytes(session.downloads_dir) > MAX_DOWNLOAD_BYTES:
                    entry.update(status="failed", error="download exceeds the size limit")
                    await download.cancel()
                    return
                if time.monotonic() > deadline:
                    entry.update(status="failed", error="download exceeded its deadline")
                    await download.cancel()
                    return
            path = Path(completion.result())
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_DOWNLOAD_BYTES:
                entry.update(status="failed", error="download exceeds the size limit")
                return
            data = await asyncio.to_thread(path.read_bytes)
            mime_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
            record = await self.artifacts.create(
                session.owner,
                data,
                suffix="download",
                mime_type=mime_type,
                max_bytes=MAX_DOWNLOAD_BYTES,
                name=name,
            )
            entry.update(status="completed", artifact=record.public())
        except asyncio.CancelledError:
            entry.update(status="failed", error="download was cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - downloads report a safe status, never raw errors
            logger.debug("browser download ended: %s", type(exc).__name__)
            entry.update(status="failed", error="download failed")
        finally:
            if not completion.done():
                completion.cancel()
            await asyncio.gather(completion, return_exceptions=True)
            if path is not None:
                path.unlink(missing_ok=True)

    async def open(
        self,
        owner: str,
        url: str | None,
        *,
        wait_until: WaitUntil,
        timeout_ms: int,
        scope_id: str | None = None,
        profile: str | None = None,
        downloads: bool = False,
    ) -> dict[str, object]:
        """Open an ephemeral session, or reattach/launch the caller's durable named profile."""
        if profile is not None and (scope_id is not None or not PROFILE_NAME.fullmatch(profile)):
            raise fail("invalid_request")
        async with self.operation():
            await self.sweep()
            async with self._state_lock:
                if scope_id and (owner, scope_id) in self._closed_scopes:
                    raise WorkerError("browser session scope is already closed")
                existing = next(
                    (
                        live
                        for live in self._sessions.values()
                        if profile is not None and live.owner == owner and live.profile == profile
                    ),
                    None,
                )
                if existing is None and (owner, str(profile)) in self._starting_profiles:
                    raise fail("session_busy")
            if existing is not None:
                return await self._reattach(existing, url, wait_until=wait_until, timeout_ms=timeout_ms)
            closing = self._profile_cleanups.get((owner, str(profile)))
            if closing is not None:
                # Chromium holds a profile lock until the previous context is fully closed.
                async with asyncio.timeout(15):
                    await asyncio.shield(closing)
            async with self._state_lock:
                owner_count = sum(session.owner == owner for session in self._sessions.values())
                owner_count += self._starting_by_owner.get(owner, 0)
                if len(self._sessions) + self._starting_global >= MAX_GLOBAL_SESSIONS:
                    raise WorkerError("browser worker session capacity reached")
                if owner_count >= MAX_CALLER_SESSIONS:
                    raise WorkerError("caller session capacity reached")
                if profile is not None and (owner, profile) in self._starting_profiles:
                    raise fail("session_busy")
                self._starting_global += 1
                self._starting_by_owner[owner] = self._starting_by_owner.get(owner, 0) + 1
                if profile is not None:
                    self._starting_profiles.add((owner, profile))
                session_id = secrets.token_urlsafe(24)
            downloads_dir = self.sessions_dir / f"{session_id}.downloads" if downloads else None
            profile_dir = self.sessions_dir / session_id
            proxy = PublicEgressProxy(self.resolver_factory())
            context: BrowserContext | None = None
            session: BrowserSession | None = None
            inserted = False
            try:
                if profile is None:
                    profile_dir.mkdir(mode=0o700)
                else:
                    profile_dir = self._profile_dir(owner, profile)
                if downloads_dir is not None:
                    downloads_dir.mkdir(mode=0o700)
                context = await self._new_context(profile_dir, proxy, downloads_dir)
                now = time.monotonic()
                session = BrowserSession(
                    session_id, owner, scope_id, now, now, profile_dir, proxy, context
                )
                session.downloads_dir = downloads_dir
                if profile is not None:
                    session.profile = profile
                    session.idle_ttl = PERSISTENT_IDLE_TTL_SECONDS
                    session.absolute_ttl = PERSISTENT_ABSOLUTE_TTL_SECONDS
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
                    if scope_id and (owner, scope_id) in self._closed_scopes:
                        raise WorkerError("browser session scope was closed during startup")
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
                    if profile is None:
                        shutil.rmtree(profile_dir, ignore_errors=True)
                    if downloads_dir is not None:
                        shutil.rmtree(downloads_dir, ignore_errors=True)
                raise
            finally:
                async with self._state_lock:
                    if profile is not None:
                        self._starting_profiles.discard((owner, profile))
                    self._starting_global -= 1
                    remaining = self._starting_by_owner.get(owner, 1) - 1
                    if remaining > 0:
                        self._starting_by_owner[owner] = remaining
                    else:
                        self._starting_by_owner.pop(owner, None)

    async def delete_profile(self, owner: str, profile: str) -> dict[str, object]:
        """Close the caller's live session of a durable profile, if any, and delete the profile."""
        if not PROFILE_NAME.fullmatch(profile):
            raise fail("invalid_request")
        key = (owner, profile)
        async with self.operation():
            async with self._state_lock:
                if key in self._starting_profiles:
                    raise fail("session_busy")
                # Held as "starting" so no open can relaunch the profile while it is deleted.
                self._starting_profiles.add(key)
                live = next(
                    (session for session in self._sessions.values() if session.owner == owner and session.profile == profile),
                    None,
                )
                if live is not None:
                    self._sessions.pop(live.id, None)
            try:
                if live is not None:
                    await self._dispose_after_action(live)
                closing = self._profile_cleanups.get(key)
                if closing is not None:
                    async with asyncio.timeout(15):
                        await asyncio.shield(closing)
                owner_dir = self.profiles_dir / hashlib.sha256(owner.encode("utf-8")).hexdigest()[:32]
                await asyncio.to_thread(_remove_profile_dir, owner_dir / profile)
            finally:
                async with self._state_lock:
                    self._starting_profiles.discard(key)
        return {"status": "deleted", "profile": profile, "closed": live is not None}

    def _profile_dir(self, owner: str, profile: str) -> Path:
        # Caller IDs are validated identifiers, but hash them so the on-disk layout
        # never depends on caller-controlled text beyond the validated profile name.
        owner_dir = self.profiles_dir / hashlib.sha256(owner.encode("utf-8")).hexdigest()[:32]
        _private_dir(owner_dir, label="profile owner directory")
        profile_dir = owner_dir / profile
        _private_dir(profile_dir, label="browser profile")
        return profile_dir

    async def _reattach(
        self, session: BrowserSession, url: str | None, *, wait_until: WaitUntil, timeout_ms: int
    ) -> dict[str, object]:
        async with session.lock:
            if session.disposed:
                raise fail("invalid_session")
            session.touched_mono = time.monotonic()
            if url:
                await self._navigate(session, url, wait_until=wait_until, timeout_ms=timeout_ms)
            return {**await self._state(session), "reattached": True}

    def list_sessions(self, owner: str) -> list[dict[str, object]]:
        now = time.monotonic()
        rows: list[dict[str, object]] = []
        for session in self._sessions.values():
            # An expired session awaiting the sweeper can no longer be opened, so it is not listed.
            if session.owner != owner or session.disposed or session.expired(now):
                continue
            pages = self._pages(session)
            active = pages[min(session.active_index, len(pages) - 1)] if pages else None
            rows.append(
                {
                    "session_id": session.id,
                    "profile": session.profile,
                    "persistent": session.persistent,
                    "url": active.url if active is not None else "",
                    "tab_count": len(pages),
                    "idle_expires_in_seconds": max(0, int(session.idle_ttl - (now - session.touched_mono))),
                }
            )
        return rows

    async def live_events(self, owner: str, session_id: str) -> AsyncIterator[dict[str, object]]:
        """Yield screencast frames and tab state for a viewer until the session ends."""
        session = await self._get(owner, session_id)
        live = session.live
        if live.viewers >= MAX_LIVE_VIEWERS_PER_SESSION:
            raise fail("session_busy")
        live.viewers += 1
        last_seq = -1
        last_state: dict[str, object] | None = None
        last_yield = time.monotonic()
        try:
            while not session.disposed and self._sessions.get(session.id) is session:
                tick = live.tick
                pages = self._pages(session)
                if not pages:
                    break
                page = pages[min(session.active_index, len(pages) - 1)]
                # Watching counts as activity so a supervised session does not idle out.
                session.touched_mono = time.monotonic()
                try:
                    await live.follow(page)
                except Exception as exc:  # noqa: BLE001 - a closing tab is followed by the next one
                    logger.debug("live view follow ended: %s", type(exc).__name__)
                state: dict[str, object] = {
                    "url": page.url,
                    "active_tab": session.active_index,
                    "tab_count": len(pages),
                }
                if state != last_state:
                    last_state = state
                    last_yield = time.monotonic()
                    yield {"event": "state", **state}
                frame = live.frame
                if frame is not None and frame["seq"] != last_seq:
                    last_seq = int(frame["seq"])
                    last_yield = time.monotonic()
                    yield {"event": "frame", **frame}
                    # Cap at ~10 frames/s; intermediate frames are superseded, not queued.
                    await asyncio.sleep(0.1)
                    continue
                if time.monotonic() - last_yield >= 15:
                    last_yield = time.monotonic()
                    yield {"event": "ping"}
                try:
                    async with asyncio.timeout(0.5):
                        await tick.wait()
                except TimeoutError:
                    pass
            yield {"event": "closed"}
        finally:
            live.viewers -= 1
            if live.viewers == 0:
                await asyncio.shield(live.stop())

    async def human_input(self, owner: str, session_id: str, event: dict[str, Any]) -> dict[str, object]:
        """Apply one validated input event from a person supervising the live view."""
        async with self.operation():
            session = await self._get(owner, session_id)
            try:
                async with asyncio.timeout(10):
                    await session.lock.acquire()
            except TimeoutError as exc:
                raise fail("session_busy") from exc
            try:
                if session.disposed:
                    raise fail("invalid_session")
                session.touched_mono = time.monotonic()
                page = self._active_page(session)
                kind = event["type"]
                try:
                    if kind == "click":
                        await page.mouse.click(
                            event["x"], event["y"], button=event["button"], click_count=event["click_count"]
                        )
                    elif kind == "wheel":
                        await page.mouse.move(event["x"], event["y"])
                        await page.mouse.wheel(event["dx"], event["dy"])
                    elif kind in {"down", "move", "up"}:
                        # Separate press, move and release let a person hold or drag (press-and-hold checks, sliders).
                        await page.mouse.move(event["x"], event["y"])
                        if kind == "down":
                            await page.mouse.down(button=event["button"])
                        elif kind == "up":
                            await page.mouse.up(button=event["button"])
                    elif kind == "key":
                        await page.keyboard.press(event["key"])
                    elif kind == "text":
                        await page.keyboard.insert_text(event["text"])
                    elif kind == "navigate":
                        await self._navigate(
                            session, event["url"], wait_until="domcontentloaded", timeout_ms=DEFAULT_TIMEOUT_MS
                        )
                    elif kind in {"back", "forward", "reload"}:
                        move = {"back": page.go_back, "forward": page.go_forward, "reload": page.reload}[kind]
                        await move(wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
                    else:
                        raise fail("invalid_request")
                except WorkerError:
                    raise
                except Exception as exc:
                    raise WorkerError("browser input failed") from exc
                # A person changed the page: discovered references are no longer trustworthy.
                session.controls.invalidate()
                session.approvals.invalidate()
                session.live.wake()
                return {"status": "ok", "session_id": session.id, "url": page.url}
            finally:
                session.lock.release()

    async def _get(self, owner: str, session_id: str) -> BrowserSession:
        await self.sweep()
        async with self._state_lock:
            session = self._sessions.get(session_id)
        if session is None or session.owner != owner:
            raise WorkerError("browser session not found", code="invalid_session")
        return session

    async def cleanup_scope(self, owner: str, scope_id: str) -> dict[str, object]:
        async with self._state_lock:
            self._closed_scopes[(owner, scope_id)] = time.monotonic()
            sessions = [
                session
                for session in self._sessions.values()
                if session.owner == owner and session.scope_id == scope_id
            ]
            for session in sessions:
                self._sessions.pop(session.id, None)
        await asyncio.gather(
            *(self._dispose_after_action(session) for session in sessions),
            return_exceptions=True,
        )
        return {"status": "closed", "closed_count": len(sessions)}

    async def close(self, owner: str, session_id: str) -> dict[str, object]:
        async with self._state_lock:
            session = self._sessions.get(session_id)
            if session is None or session.owner != owner:
                raise WorkerError("browser session not found", code="invalid_session")

        async def evict_and_dispose() -> None:
            async with self._state_lock:
                if self._sessions.get(session_id) is session:
                    self._sessions.pop(session_id, None)
            await self._dispose_after_action(session)

        cleanup = asyncio.create_task(
            evict_and_dispose(), name=f"browser-close-{session_id}"
        )
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            try:
                async with asyncio.timeout(15):
                    await asyncio.shield(cleanup)
            finally:
                raise
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
                for task in list(session.tasks):
                    task.cancel()
                await asyncio.gather(*session.tasks, return_exceptions=True)
                try:
                    async with asyncio.timeout(2):
                        await session.live.stop()
                        await session.approvals.clear()
                        await session.frames.clear()
                except Exception as exc:  # noqa: BLE001 - context cleanup must still run
                    logger.debug("browser approval cleanup failed: %s", type(exc).__name__)
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
                    if not session.persistent:
                        shutil.rmtree(session.profile_dir, ignore_errors=True)
                    if session.downloads_dir is not None:
                        shutil.rmtree(session.downloads_dir, ignore_errors=True)

        task = asyncio.create_task(cleanup(), name=f"browser-dispose-{session.id}")
        if session.profile is not None:
            key = (session.owner, session.profile)
            self._profile_cleanups[key] = task
            task.add_done_callback(
                lambda done: self._profile_cleanups.pop(key, None)
                if self._profile_cleanups.get(key) is done
                else None
            )
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
        session.controls.invalidate()
        session.approvals.invalidate()
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
            raise WorkerError("browser navigation was blocked by public-network policy", code="blocked_access") from exc
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
        state: dict[str, object] = {
            "status": "ok",
            "session_id": session.id,
            "active_tab": session.active_index,
            "tabs": [
                {"index": index, "url": page.url, "title": (await page.title())[:500]}
                for index, page in enumerate(pages)
            ],
            "idle_expires_in_seconds": max(0, int(session.idle_ttl - (now - session.touched_mono))),
            "absolute_expires_in_seconds": max(0, int(session.absolute_ttl - (now - session.created_mono))),
        }
        if session.persistent:
            state["profile"] = session.profile
        return state

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

    @staticmethod
    async def _no_upload(element: ElementHandle) -> None:
        if await element.evaluate('''el => {
          for (let n = el; n; n = n.parentElement || n.getRootNode().host)
            if (n.matches('input[type="file"]') || n.control?.matches('input[type="file"]')) return true;
          return !!el.querySelector('input[type="file"]');
        }'''):
            raise fail('approval_unavailable')

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
        if params.get("frame_id") is not None and action not in FRAME_ACTIONS:
            raise fail("invalid_request")
        if action == "close":
            return await self.close(owner, session_id)
        async with self.operation():
            session = await self._get(owner, session_id)
            async with session.lock:
                session.touched_mono = time.monotonic()
                page = self._active_page(session)
                timeout_ms = int(params.get("timeout_ms") or DEFAULT_TIMEOUT_MS)
                frame = page.main_frame
                if params.get("frame_id") is not None:
                    async with asyncio.timeout(min(timeout_ms, 3000) / 1000):
                        frame = await session.frames.resolve(page, params["frame_id"])
                if action == "frames":
                    async with asyncio.timeout(min(timeout_ms, 3000) / 1000):
                        result = await session.frames.discover(page, int(params.get("limit") or 50))
                    return {"status": "ok", "session_id": session.id, **result}
                if action == "elements":
                    await check_frame_access(page, frame)
                    result = await discover_elements(frame, int(params.get("limit") or 50))
                    return {"status": "ok", "session_id": session.id, **result}
                if action in {"prepare_action", "execute_prepared", "discard_prepared"} and (
                    self._sessions.get(session_id) is not session or session.disposed
                    or time.monotonic() >= session.created_mono + session.absolute_ttl
                ):
                    raise fail("invalid_session")
                if action == "prepare_action":
                    result = await session.approvals.prepare(
                        page, params["operation"], params, frame=frame,
                        session_deadline=session.created_mono + session.absolute_ttl,
                    )
                    if params.get("frame_id") is not None:
                        try:
                            await session.frames.resolve(page, params["frame_id"])
                        except BaseException:
                            await session.approvals.clear()
                            raise
                    return {"status": "ok", "session_id": session.id, **result}
                if action in {"execute_prepared", "discard_prepared"}:
                    result = await session.approvals.finish(
                        page, params["proposal_id"], execute=action == "execute_prepared"
                    )
                    return {"status": "ok", "session_id": session.id, **result}
                if action in {"controls", "expand_control", "select_option"}:
                    result = await session.controls.run(
                        page, action, str(params.get("control_id") or ""), params.get("option")
                    )
                    return {"status": "ok", "session_id": session.id, **result}
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
                    session.controls.invalidate()
                    session.approvals.invalidate()
                    session.active_index = index
                    await pages[index].bring_to_front()
                    return await self._state(session)
                if action == "close_tab":
                    index = int(params["tab_index"] if params.get("tab_index") is not None else session.active_index)
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
                        async with asyncio.timeout(min(timeout_ms, 3000) / 1000):
                            await check_frame_access(page, frame)
                            locator = frame.locator(str(selector or "body"))
                            count = await locator.count()
                            if count == 0:
                                raise fail("selector_not_found")
                            if count > 1:
                                raise fail("selector_ambiguous")
                            element = await locator.element_handle(timeout=min(timeout_ms, 3000))
                            if element is None:
                                raise fail("selector_not_found")
                            try:
                                if await element.owner_frame() != frame:
                                    raise fail("invalid_selector")
                                # Read the retained checked-document target, never
                                # re-resolve a selector into an unchecked child.
                                observation = await element.evaluate(
                                    "(el, limit) => { if (!el.isConnected || el.ownerDocument !== document) throw new Error('stale target'); return {text:(el.innerText || el.textContent || '').slice(0, limit + 1), url:location.href, title:document.title.slice(0,500)}; }",
                                    max_chars,
                                )
                            finally:
                                await element.dispose()
                    except (TimeoutError, BrowserTimeoutError) as exc:
                        raise fail("extraction_timeout") from exc
                    except BrowserError as exc:
                        raise fail("invalid_selector") from exc
                    return {
                        "status": "ok",
                        "session_id": session.id,
                        "url": observation["url"],
                        "title": observation["title"],
                        "text": str(observation["text"])[:max_chars],
                        "truncated": len(str(observation["text"])) > max_chars,
                    }
                if action == "extract_links":
                    selector = str(params.get("selector") or "a[href]")
                    limit = min(MAX_LINKS, int(params.get("limit") or 50))
                    try:
                        async with asyncio.timeout(min(timeout_ms, 3000) / 1000):
                            await check_frame_access(page, frame)
                            locator = frame.locator(selector)
                            if await locator.count() == 0:
                                raise fail("selector_not_found")
                            document = await frame.evaluate_handle("document")
                            try:
                                # A single retained document authority keeps the
                                # batch in the checked realm, even if the selector
                                # enters a child. Never materialize N remote handles.
                                raw = await locator.evaluate_all(
                                    "(els,args) => { if (document !== args.document || !els.every(el => el.isConnected && el.ownerDocument === args.document)) throw new Error('wrong document'); return els.filter(el => el.matches('a[href]') && el.checkVisibility()).slice(0, args.limit).map(el => ({text:(el.innerText||el.textContent||'').trim().slice(0,500), url:el.href.slice(0,8192)})); }",
                                    {"document": document, "limit": min(400, limit * 4)},
                                )
                            finally:
                                await document.dispose()
                    except (TimeoutError, BrowserTimeoutError) as exc:
                        raise fail("extraction_timeout") from exc
                    except BrowserError as exc:
                        raise fail("invalid_selector") from exc
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
                        await check_frame_access(page, frame)
                        element = await self._one_target(frame, str(params["selector"]), timeout_ms)
                        try:
                            if await element.owner_frame() != frame:
                                raise fail("invalid_request")
                            await self._no_upload(element)
                            await element.click(timeout=timeout_ms)
                        finally:
                            await element.dispose()
                    except WorkerError:
                        raise
                    except Exception as exc:
                        raise WorkerError("click failed") from exc
                    return await self._state(session)
                if action == "type":
                    element = None
                    editable = True
                    try:
                        await check_frame_access(page, frame)
                        element = await self._one_target(frame, str(params["selector"]), timeout_ms)
                        if await element.owner_frame() != frame:
                            raise fail("invalid_request")
                        await self._no_upload(element)
                        # Disclose contenteditable targets: typed prose there is page content, not input.
                        editable = bool(await element.evaluate("el => el.isContentEditable"))
                        if params.get("clear", True):
                            await element.fill(str(params["text"]), timeout=timeout_ms)
                        else:
                            await element.type(str(params["text"]), timeout=timeout_ms)
                        if params.get("submit", False):
                            await check_frame_access(page, frame)
                            await element.press("Enter", timeout=timeout_ms)
                    except WorkerError:
                        raise
                    except Exception as exc:
                        raise WorkerError("typing failed") from exc
                    finally:
                        if element is not None:
                            await element.dispose()
                    return {"status": "ok", "session_id": session.id, "url": frame.url, "target_contenteditable": editable}
                if action == "wait":
                    await check_frame_access(page, frame)
                    selector = params.get("selector")
                    url_contains = params.get("url_contains")
                    if selector:
                        try:
                            await frame.locator(str(selector)).wait_for(
                                state=params.get("state", "visible"), timeout=timeout_ms
                            )
                        except Exception as exc:
                            raise WorkerError("wait condition was not met") from exc
                    elif url_contains:
                        try:
                            await frame.wait_for_function(
                                "part => location.href.includes(part)", arg=str(url_contains), timeout=timeout_ms,
                            )
                        except Exception as exc:
                            raise WorkerError("wait condition was not met") from exc
                    else:
                        try:
                            await frame.wait_for_load_state(params.get("wait_until", "domcontentloaded"), timeout=timeout_ms)
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
                if action == "downloads":
                    return {"status": "ok", "session_id": session.id, "downloads": [dict(row) for row in session.downloads]}
                if action == "console":
                    limit = min(50, int(params.get("limit") or 50))
                    return {"status": "ok", "session_id": session.id, "events": session.console[-limit:]}
                if action == "evaluate":
                    try:
                        await check_frame_access(page, frame)
                        result = await frame.evaluate(str(params["script"]), params.get("arg"))
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
        session_id: str | None = None,
    ) -> dict[str, object]:
        """Render one page in a throwaway session, or in the caller's open `session_id`, kept open."""
        started = time.monotonic()
        render = {"max_chars": max_chars, "timeout_ms": timeout_ms, "include_links": include_links,
                  "include_screenshot": include_screenshot}
        if session_id is not None:
            async with self.operation():
                session = await self._get(owner, session_id)
                # Like live input, a fetch does not queue behind someone using the browser.
                try:
                    async with asyncio.timeout(10):
                        await session.lock.acquire()
                except TimeoutError as exc:
                    raise fail("session_busy") from exc
                try:
                    if session.disposed:
                        raise fail("invalid_session")
                    await self._navigate(session, url, wait_until=wait_until, timeout_ms=timeout_ms)
                    return {**await self._render(owner, session, url, started, **render), "session_id": session.id}
                finally:
                    session.lock.release()
        state = await self.open(owner, url, wait_until=wait_until, timeout_ms=timeout_ms)
        session_id = str(state["session_id"])
        try:
            session = await self._get(owner, session_id)
            async with session.lock:
                return await self._render(owner, session, url, started, **render)
        finally:
            try:
                await asyncio.shield(self.close(owner, session_id))
            except BaseException as exc:  # noqa: BLE001 - one-shot cleanup must not replace the result
                logger.warning("one-shot browser cleanup ended: %s", type(exc).__name__)

    async def _render(
        self,
        owner: str,
        session: BrowserSession,
        url: str,
        started: float,
        *,
        max_chars: int,
        timeout_ms: int,
        include_links: bool,
        include_screenshot: bool,
    ) -> dict[str, object]:
        """Read the active tab's visible text; the caller holds the session lock."""
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
