from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

import browser_runtime
from artifacts import ArtifactStore
from browser_runtime import BrowserManager, WorkerError


class FakePage:
    url = "about:blank"

    def __init__(self) -> None:
        self.closed = False

    def on(self, *_args: Any) -> None:
        return None

    def is_closed(self) -> bool:
        return self.closed

    async def title(self) -> str:
        return "Blank"

    async def bring_to_front(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self) -> None:
        self.pages = [FakePage()]
        self.closed = False
        self.callbacks: dict[str, list[Any]] = {}

    def on(self, event: str, callback: Any) -> None:
        self.callbacks.setdefault(event, []).append(callback)

    async def new_page(self) -> FakePage:
        page = FakePage()
        self.pages.append(page)
        return page

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for page in self.pages:
            page.closed = True
        for callback in self.callbacks.get("close", []):
            callback()


async def fake_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[BrowserManager, ArtifactStore]:
    artifacts = ArtifactStore(tmp_path / "artifacts")
    await artifacts.start()
    sessions = tmp_path / "sessions"
    sessions.mkdir(mode=0o700)
    manager = BrowserManager(artifacts, sessions_dir=sessions)
    manager._playwright = object()  # type: ignore[assignment]

    async def new_context(_profile: Path, proxy: Any) -> FakeContext:
        await proxy.start()
        return FakeContext()

    monkeypatch.setattr(manager, "_new_context", new_context)
    return manager, artifacts


@pytest.mark.asyncio
async def test_per_caller_capacity_and_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, artifacts = await fake_manager(tmp_path, monkeypatch)
    opened = [
        await manager.open("owner", None, wait_until="domcontentloaded", timeout_ms=1000)
        for _ in range(browser_runtime.MAX_CALLER_SESSIONS)
    ]
    with pytest.raises(WorkerError, match="caller session capacity"):
        await manager.open("owner", None, wait_until="domcontentloaded", timeout_ms=1000)
    for state in opened:
        await manager.close("owner", str(state["session_id"]))
    assert manager.live_sessions == 0
    assert list((tmp_path / "sessions").iterdir()) == []
    await artifacts.close()


@pytest.mark.asyncio
async def test_idle_sweep_removes_session_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, artifacts = await fake_manager(tmp_path, monkeypatch)
    state = await manager.open("owner", None, wait_until="domcontentloaded", timeout_ms=1000)
    session_id = str(state["session_id"])
    manager._sessions[session_id].touched_mono = time.monotonic() - browser_runtime.IDLE_TTL_SECONDS - 1
    await manager.sweep()
    assert manager.live_sessions == 0
    assert not (tmp_path / "sessions" / session_id).exists()
    await artifacts.close()


@pytest.mark.asyncio
async def test_absolute_ttl_and_global_capacity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_runtime, "MAX_GLOBAL_SESSIONS", 1)
    manager, artifacts = await fake_manager(tmp_path, monkeypatch)
    state = await manager.open("owner-a", None, wait_until="domcontentloaded", timeout_ms=1000)
    with pytest.raises(WorkerError, match="worker session capacity"):
        await manager.open("owner-b", None, wait_until="domcontentloaded", timeout_ms=1000)
    session_id = str(state["session_id"])
    manager._sessions[session_id].created_mono = (
        time.monotonic() - browser_runtime.ABSOLUTE_TTL_SECONDS - 1
    )
    await manager.sweep()
    assert manager.live_sessions == 0
    assert not (tmp_path / "sessions" / session_id).exists()
    await artifacts.close()


@pytest.mark.asyncio
async def test_cancellation_and_context_crash_evict_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, artifacts = await fake_manager(tmp_path, monkeypatch)
    first = await manager.open("owner", None, wait_until="domcontentloaded", timeout_ms=1000)
    first_id = str(first["session_id"])
    entered = asyncio.Event()

    async def blocked(*_args: Any, **_kwargs: Any) -> dict[str, object]:
        entered.set()
        await asyncio.Event().wait()
        return {}

    original = manager._act_impl
    monkeypatch.setattr(manager, "_act_impl", blocked)
    task = asyncio.create_task(manager.act("owner", first_id, "state"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert manager.live_sessions == 0
    assert not (tmp_path / "sessions" / first_id).exists()

    monkeypatch.setattr(manager, "_act_impl", original)
    second = await manager.open("owner", None, wait_until="domcontentloaded", timeout_ms=1000)
    second_id = str(second["session_id"])
    context = manager._sessions[second_id].context
    await context.close()
    for _ in range(20):
        if manager.live_sessions == 0:
            break
        await asyncio.sleep(0.01)
    assert manager.live_sessions == 0
    assert not (tmp_path / "sessions" / second_id).exists()
    await artifacts.close()


@pytest.mark.asyncio
async def test_operation_admission_fails_without_unbounded_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(browser_runtime, "MAX_ADMITTED_OPERATIONS", 2)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    await artifacts.start()
    manager = BrowserManager(artifacts, sessions_dir=tmp_path / "sessions")
    entered = asyncio.Event()
    release = asyncio.Event()
    count = 0

    async def holder() -> None:
        nonlocal count
        async with manager.operation():
            count += 1
            if count == 2:
                entered.set()
            await release.wait()

    tasks = [asyncio.create_task(holder()) for _ in range(2)]
    await entered.wait()
    with pytest.raises(WorkerError, match="admission limit"):
        async with manager.operation():
            pass
    release.set()
    await asyncio.gather(*tasks)
    await artifacts.close()
