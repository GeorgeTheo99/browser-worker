"""Owner-bound live view of a session's active tab via a Chromium CDP screencast.

Frames are pushed by the browser only while at least one viewer is attached;
the screencast follows whichever tab is active and stops with the last viewer.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from patchright.async_api import CDPSession, Page

from config import LIVE_FRAME_QUALITY, VIEWPORT_HEIGHT, VIEWPORT_WIDTH

logger = logging.getLogger("browser-worker.live")


class LiveView:
    def __init__(self) -> None:
        self.viewers = 0
        self.frame: dict[str, Any] | None = None
        self._seq = 0
        self._page: Page | None = None
        self._cdp: CDPSession | None = None
        self._lock = asyncio.Lock()
        self._tick = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def tick(self) -> asyncio.Event:
        """Event set on the next frame or wake-up; capture it before waiting."""
        return self._tick

    def wake(self) -> None:
        tick, self._tick = self._tick, asyncio.Event()
        tick.set()

    async def follow(self, page: Page) -> None:
        async with self._lock:
            if self._page is page and self._cdp is not None:
                return
            await self._stop_locked()
            cdp = await page.context.new_cdp_session(page)
            cdp.on("Page.screencastFrame", lambda params: self._on_frame(cdp, params))
            await cdp.send(
                "Page.startScreencast",
                {
                    "format": "jpeg",
                    "quality": LIVE_FRAME_QUALITY,
                    "maxWidth": VIEWPORT_WIDTH,
                    "maxHeight": VIEWPORT_HEIGHT,
                    "everyNthFrame": 1,
                },
            )
            self._page, self._cdp = page, cdp

    def _on_frame(self, cdp: CDPSession, params: dict[str, Any]) -> None:
        if cdp is not self._cdp:
            return
        metadata = params.get("metadata") or {}
        self._seq += 1
        self.frame = {
            "seq": self._seq,
            "data": str(params.get("data") or ""),
            "width": int(metadata.get("deviceWidth") or VIEWPORT_WIDTH),
            "height": int(metadata.get("deviceHeight") or VIEWPORT_HEIGHT),
        }
        task = asyncio.create_task(self._ack(cdp, params.get("sessionId")))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        self.wake()

    @staticmethod
    async def _ack(cdp: CDPSession, screencast_session: Any) -> None:
        try:
            await cdp.send("Page.screencastFrameAck", {"sessionId": screencast_session})
        except Exception as exc:  # noqa: BLE001 - a closing tab ends its screencast
            logger.debug("screencast ack ended: %s", type(exc).__name__)

    async def _stop_locked(self) -> None:
        cdp, self._cdp, self._page = self._cdp, None, None
        if cdp is None:
            return
        try:
            async with asyncio.timeout(2):
                await cdp.send("Page.stopScreencast")
                await cdp.detach()
        except Exception as exc:  # noqa: BLE001 - the tab or browser may already be gone
            logger.debug("screencast stop ended: %s", type(exc).__name__)

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_locked()
        self.frame = None
        self.wake()
