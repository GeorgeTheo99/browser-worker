"""Session/page-owned frame observations and retained document ancestry.

Checks are preflight, not atomic with subsequent browser operations.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

from patchright.async_api import Error, Frame, JSHandle, Page

from controls import check_access
from errors import fail
from security import parse_public_url

MAX_FRAMES = 100
MAX_FRAME_DEPTH = 32
FRAME_ACTIONS = frozenset({
    'elements', 'extract_text', 'extract_links', 'prepare_action',
    'click', 'type', 'evaluate', 'wait',
})

# Installed before site listeners. Closure-owned authority cannot be reset by
# page scripts or bypassed using borrowed History methods.
NAVIGATION_INIT_SCRIPT = r"""(function () {
  const nav = this.navigation;
  const supported = !!(nav && nav.currentEntry);
  let generation = 0n;
  const revoke = () => { generation++; };
  if (supported) {
    nav.addEventListener('currententrychange', revoke, true);
    this.addEventListener('pagehide', revoke, true);
  }
  // Lock unsupported realms too: otherwise a blank/opaque page could install
  // a counterfeit reader and turn read-only access into approval authority.
  Object.defineProperty(this, '__browserWorkerNavigationGeneration', {
    value: supported ? Object.freeze(() => generation) : null,
    writable: false, configurable: false
  });
})();"""


def ancestry(frame: Frame) -> list[Frame]:
    result = []
    while frame is not None:
        result.append(frame)
        if len(result) > MAX_FRAME_DEPTH:
            raise fail('approval_unavailable')
        frame = frame.parent_frame
    return list(reversed(result))


async def check_frame_access(page: Page, frame: Frame) -> None:
    chain = ancestry(frame)
    if not chain or chain[0] != page.main_frame or frame.is_detached():
        raise fail('approval_stale')
    for current in chain:
        # Local child documents are allowed only under a public ancestor. Never
        # reinterpret private URLs, data:, blob:, or a top-level blank as public.
        # Patchright can report an empty cached frame.url for opaque srcdoc
        # frames. Read the actual URL from its isolated realm, never substitute
        # a parent's public URL for a child's evidence identity.
        url = await current.evaluate('location.href')
        if current == page.main_frame or url not in {'about:blank', 'about:srcdoc'}:
            parse_public_url(url)
        await check_access(current)


@dataclass
class DocumentRef:
    frame: Frame
    handle: JSHandle
    url: str
    origin: str

    @classmethod
    async def capture(cls, frame: Frame, *, require_navigation: bool = False) -> DocumentRef:
        navigation = await frame.evaluate_handle(
            'function () { return this.__browserWorkerNavigationGeneration; }', isolated_context=False)
        handle = None
        try:
            handle = await frame.evaluate_handle('''navigation => ({navigation,
              generation: typeof navigation === 'function' ? navigation() : null,
              document, root: document.documentElement, url: location.href,
              origin: self.origin, clock: performance.now()})''', navigation)
            info = await handle.evaluate('d => ({url:d.url, origin:d.origin, guarded:d.generation !== null})')
            if frame.parent_frame is None or info['url'] not in {'about:blank', 'about:srcdoc'}:
                parse_public_url(info['url'])
            if require_navigation and not info['guarded']:
                raise fail('approval_unavailable')
            return cls(frame, handle, info['url'], info['origin'])
        except BaseException:
            if handle is not None:
                await handle.dispose()
            raise
        finally:
            await navigation.dispose()

    async def valid(self) -> bool:
        return not self.frame.is_detached() and await self.handle.evaluate('''d =>
          d.document === document && d.root === document.documentElement && d.url === location.href &&
          (d.navigation ? d.navigation() === d.generation : true)''')

    async def dispose(self) -> None:
        try:
            await self.handle.dispose()
        except Error:
            pass


@dataclass
class FrameRef:
    id: str
    page: Page
    documents: list[DocumentRef]
    generations: list[int]


class FrameStore:
    def __init__(self) -> None:
        self.refs: dict[str, FrameRef] = {}
        self.generations: dict[Frame, int] = {}

    def invalidate(self, frame: Frame) -> None:
        if frame in self.generations:
            self.generations[frame] += 1

    async def clear(self) -> None:
        refs, self.refs = self.refs, {}
        for ref in refs.values():
            for document in ref.documents:
                await document.dispose()
        self.generations.clear()

    async def discover(self, page: Page, limit: int) -> dict[str, Any]:
        await self.clear()
        await check_frame_access(page, page.main_frame)
        rows = []
        ids: dict[Frame, str] = {}
        candidates = page.frames[:MAX_FRAMES]
        try:
            for frame in candidates:
                if len(rows) >= min(MAX_FRAMES, limit):
                    break
                # Parents precede children; do not expose an incomplete chain.
                chain = ancestry(frame)
                if frame.parent_frame is not None and frame.parent_frame not in ids:
                    continue
                await check_frame_access(page, frame)
                documents = []
                try:
                    generations = [self.generations.setdefault(f, 0) for f in chain]
                    for current in chain:
                        documents.append(await DocumentRef.capture(current))
                    if not all([await d.valid() for d in documents]) or generations != [self.generations.get(f, 0) for f in chain]:
                        raise fail('approval_stale')
                    fid = secrets.token_urlsafe(24)
                    self.refs[fid] = FrameRef(fid, page, documents, generations)
                except BaseException:
                    for document in documents:
                        await document.dispose()
                    raise
                ids[frame] = fid
                rows.append({'frame_id': fid, 'parent_frame_id': ids.get(frame.parent_frame),
                             'url': documents[-1].url, 'origin': documents[-1].origin})
            return {'frames': rows, 'truncated': len(rows) < len(page.frames)}
        except BaseException:
            await self.clear()
            raise

    async def resolve(self, page: Page, frame_id: str) -> Frame:
        ref = self.refs.get(frame_id)
        try:
            if ref is None or ref.page is not page:
                raise fail('approval_stale')
            for document, generation in zip(ref.documents, ref.generations, strict=True):
                if self.generations.get(document.frame, 0) != generation or not await document.valid():
                    raise fail('approval_stale')
            frame = ref.documents[-1].frame
            if ancestry(frame) != [d.frame for d in ref.documents]:
                raise fail('approval_stale')
            await check_frame_access(page, frame)
            return frame
        except Error as exc:
            raise fail('approval_stale') from exc
