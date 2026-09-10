"""Bounded dropdown discovery and server-owned element references, not selector actions.

Only fixed worker scripts inspect DOM state. No caller script or input value is used.
Page event handlers can still cause public requests; this is not a no-side-effects API.
"""
from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from typing import Any

from patchright.async_api import ElementHandle, Error, Page
from patchright.async_api import TimeoutError as BrowserTimeoutError

from errors import WorkerError, fail

MAX_CONTROLS = 20
CONTROL_TIMEOUT_MS = 3_000

# Main-document controls only. An absent listbox may be mounted by expansion.
# Text inputs INSIDE a noneditable combobox wrapper are allowed, but never read/typed.
_INSPECT = r"""el => {
  const visible = e => !!e && e.isConnected && !e.closest('[aria-hidden="true"]') && e.checkVisibility() &&
    !!(e.getBoundingClientRect().width && e.getBoundingClientRect().height);
  const text = e => (e?.innerText || e?.textContent || '').trim();
  const forbidden = 'form,button,a[href],textarea,[contenteditable]:not([contenteditable="false"]),[role="button"],[role="link"]';
  const credential = document.querySelector('input[type="password"],input[type="email"],input[autocomplete="username"],input[autocomplete="current-password"],input[autocomplete="new-password"],input[autocomplete="one-time-code"]') ||
    /(?:^|[/_-])(login|log-in|signin|sign-in|signup|sign-up|oauth|authorize|checkout|payment)(?:[/_.?#-]|$)/i.test(location.pathname);
  if (!visible(el) || credential || el.closest(forbidden) || el.isContentEditable ||
      el.matches(':disabled,[aria-disabled="true"]') || el.closest('[inert],[aria-disabled="true"]')) return null;
  const native = el.tagName === 'SELECT';
  if (native && (el.form || el.multiple || el.size > 1)) return null;
  // Labels can activate an external submit button via native browser behavior,
  // even when neither the dropdown nor option is inside a form. Native selects
  // retain ordinary accessible labels; custom click targets must exclude them.
  const customForbidden = forbidden + ',label';
  if (!native && (!['DIV','SPAN'].includes(el.tagName) || el.getAttribute('role') !== 'combobox' ||
      el.getAttribute('aria-haspopup') !== 'listbox' || el.closest('label') || el.querySelector(customForbidden) ||
      el.querySelector('input[type]:not([type="text"]):not([type="search"])'))) return null;
  let box = null;
  if (!native) {
    const id = (el.getAttribute('aria-controls') || '').trim();
    if (!id || /\s/.test(id) || id.length > 200) return null;
    const boxes = document.querySelectorAll('#' + CSS.escape(id));
    const owners = [...document.querySelectorAll('[role="combobox"][aria-controls]')]
      .filter(e => e.getAttribute('aria-controls') === id);
    if (boxes.length > 1 || owners.length !== 1) return {error: 'ambiguous_control'};
    box = boxes[0];
    if (box && (box.getAttribute('role') !== 'listbox' || box.closest(customForbidden) ||
        box.isContentEditable || box.closest('[inert],[aria-disabled="true"]'))) return null;
    if (!box && el.getAttribute('aria-expanded') === 'true') return null;
  }
  let label = el.getAttribute('aria-label') || '';
  if (!label) label = (el.getAttribute('aria-labelledby') || '').split(/\s+/).slice(0, 5)
    .map(id => text(document.getElementById(id))).join(' ').trim();
  if (!label && native) label = [...el.labels].slice(0, 5).map(text).join(' ');
  const all = native ? [...el.options] : box && visible(box) ?
    [...box.querySelectorAll('[role="option"]')].filter(visible) : [];
  let truncated = all.length > 50 || label.length > 200;
  const options = [];
  const nodes = [];
  for (const o of all.slice(0, 50)) {
    const label = native ? o.label.trim() : text(o);
    if (!label || label.length > 200) { truncated = true; continue; }
    const unsafe = !native && (o.closest(customForbidden + ',input,select') || o.querySelector(customForbidden + ',input,select') ||
      o.isContentEditable || o.closest('[inert]') || o.closest('[role="listbox"]') !== box);
    const disabled = !!(unsafe || o.disabled || o.closest('optgroup:disabled,[aria-disabled="true"]'));
    options.push({label, disabled});
    nodes.push(o);
  }
  const selection = nodes.filter(o => native ? o.selected : o.getAttribute('aria-selected') === 'true')
    .map(o => native ? o.label.trim() : text(o));
  return {info: {role: native ? 'select' : 'combobox', label: label.slice(0,200),
    selection, options, expanded: native ? false : !!(box && visible(box)), truncated}, nodes};
}"""

_BLOCKED = r"""() => {
  const title = document.title.slice(0, 500);
  const body = (document.body?.innerText || '').slice(0, 8000);
  return !!document.querySelector('#cf-challenge-running,#challenge-form,.g-recaptcha,iframe[src*="captcha"]') ||
    /access denied|just a moment|attention required|security verification/i.test(title) ||
    /verify (?:that )?you are human|checking your browser|unusual traffic from your computer/i.test(body);
}"""


async def check_access(page: Page) -> None:
    if await page.evaluate(_BLOCKED):
        raise fail("blocked_access")


@dataclass
class ControlRef:
    element: ElementHandle
    page: Page
    url: str
    epoch: int
    info: dict[str, Any]
    association: str | None


class ControlStore:
    def __init__(self) -> None:
        self.epoch = 0
        self.refs: dict[str, ControlRef] = {}

    def invalidate(self, *_args: Any) -> None:
        # Event callbacks cannot await disposal; references are bounded and disposed on
        # the next discovery. Navigation/context destruction also releases browser DOMs.
        self.epoch += 1

    async def clear(self) -> None:
        refs, self.refs = self.refs, {}
        for ref in refs.values():
            try:
                await ref.element.dispose()
            except Error:
                pass

    async def _snapshot(self, element: ElementHandle) -> tuple[dict[str, Any], list[ElementHandle]]:
        snapshot = await element.evaluate_handle(_INSPECT)
        nodes_handle = None
        info_handle = None
        try:
            if await snapshot.evaluate("value => value === null"):
                raise fail("unsupported_control")
            info_handle = await snapshot.get_property("info")
            info = await info_handle.json_value()
            if not info:
                error_handle = await snapshot.get_property("error")
                try:
                    error = await error_handle.json_value()
                finally:
                    await error_handle.dispose()
                raise fail("ambiguous_control" if error == "ambiguous_control" else "unsupported_control")
            nodes_handle = await snapshot.get_property("nodes")
            nodes = [h.as_element() for h in (await nodes_handle.get_properties()).values()]
            return info, [node for node in nodes if node is not None]
        finally:
            if info_handle:
                await info_handle.dispose()
            if nodes_handle:
                await nodes_handle.dispose()
            await snapshot.dispose()

    async def discover(self, page: Page) -> dict[str, Any]:
        await self.clear()
        epoch, url = self.epoch, page.url
        # Bounded DOM candidates, even on hostile pages with huge control counts.
        candidates = await page.evaluate_handle(
            "() => Array.from(document.querySelectorAll('select,[role=combobox]')).slice(0,101)"
        )
        handles = list((await candidates.get_properties()).values())
        results = []
        truncated = len(handles) > 100
        try:
            for handle in handles[:100]:
                if len(results) == MAX_CONTROLS:
                    truncated = True
                    break
                element = handle.as_element()
                if element is None:
                    continue
                try:
                    info, nodes = await self._snapshot(element)
                except WorkerError as exc:
                    if exc.code in {"unsupported_control", "ambiguous_control"}:
                        continue
                    raise
                for node in nodes:
                    await node.dispose()
                control_id = secrets.token_urlsafe(24)
                self.refs[control_id] = ControlRef(
                    element, page, url, epoch, info, await element.get_attribute("aria-controls")
                )
                results.append({"control_id": control_id, **info})
            if epoch != self.epoch or url != page.url:
                raise fail("stale_control")
            return {"controls": results, "truncated": truncated}
        finally:
            kept = {id(ref.element) for ref in self.refs.values()}
            for handle in handles:
                if id(handle) not in kept:
                    await handle.dispose()
            await candidates.dispose()

    async def act(self, page: Page, action: str, control_id: str, option: str | None) -> dict[str, Any]:
        ref = self.refs.get(control_id)
        if not ref or ref.page is not page or ref.epoch != self.epoch or ref.url != page.url:
            raise fail("stale_control")
        if not await ref.element.evaluate("el => el.isConnected"):
            raise fail("stale_control")
        if await ref.element.get_attribute("aria-controls") != ref.association:
            raise fail("stale_control")
        info, nodes = await self._snapshot(ref.element)
        try:
            if info["role"] != ref.info["role"] or info["label"] != ref.info["label"]:
                raise fail("stale_control")
            if action == "expand_control":
                if info["role"] != "combobox":
                    raise fail("unsupported_control")
                if not info["expanded"]:
                    await ref.element.click(timeout=CONTROL_TIMEOUT_MS)
            else:
                if info["truncated"] or not any(row["label"] == option for row in ref.info["options"]):
                    raise fail("unsupported_control")
                matches = [i for i, row in enumerate(info["options"]) if row["label"] == option]
                if len(matches) > 1:
                    raise fail("ambiguous_control")
                if not matches or info["options"][matches[0]]["disabled"]:
                    raise fail("unsupported_control")
                node = nodes[matches[0]]
                if info["role"] == "select":
                    await ref.element.select_option(element=node, timeout=CONTROL_TIMEOUT_MS)
                else:
                    await node.click(timeout=CONTROL_TIMEOUT_MS)
        finally:
            for node in nodes:
                await node.dispose()
        if ref.epoch != self.epoch or ref.url != page.url:
            raise fail("stale_control")
        await check_access(page)
        info, nodes = await self._snapshot(ref.element)
        for node in nodes:
            await node.dispose()
        ref.info = info
        return {"control": {"control_id": control_id, **info}}

    async def run(self, page: Page, action: str, control_id: str = "", option: str | None = None) -> dict[str, Any]:
        try:
            async with asyncio.timeout(CONTROL_TIMEOUT_MS / 1000):
                await check_access(page)
                if action == "controls":
                    return await self.discover(page)
                return await self.act(page, action, control_id, option)
        except (TimeoutError, BrowserTimeoutError) as exc:
            raise fail("operation_timeout") from exc
        except Error as exc:
            raise fail("stale_control") from exc
