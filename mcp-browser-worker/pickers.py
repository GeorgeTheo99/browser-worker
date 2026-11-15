"""Native dropdown lists (`<select>`): chosen through the worker, never through the browser's own popup.

Chromium draws a `<select>`'s list as a popup outside the page: an OS menu on macOS, a separate window on
Linux. Nothing that sees or drives the page reaches it: not the live view's screencast, not CDP input
(keys go to the page, not the menu), not screenshots. A remote click that opens one leaves an invisible menu
that nobody can use. So a remote click never opens one. The agent picks an option with `select_option` on
the select's ref (`elements` lists its options); a person in the live view gets the select's options back
from their click or key (a `picker`) and answers with a `choose` event. Either way the option is set as the
browser sets it when someone picks it from the list: the select focused, then `input` and `change` events.

A select whose list is part of the page (a listbox with `size` > 1, or a customizable `appearance:
base-select` one) is ordinary page content: clicks and keys reach it as usual.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

from patchright.async_api import ElementHandle, Frame, Page
from patchright.async_api import Error as BrowserError

from errors import fail

# Enough for a whole country list; labels are at most MAX_LABEL characters.
MAX_PICKER_OPTIONS = 1000
MAX_LABEL = 200
# How deep a point is followed into embedded frames.
MAX_FRAME_DEPTH = 6

# Whether an element is a select whose list Chromium draws outside the page, and if so what it offers.
_POPUP_SELECT = r"""el => {
  if (!el || el.localName !== 'select' || el.multiple || el.size > 1) return null;
  if (getComputedStyle(el).appearance === 'base-select') return null;
  return true;
}"""

# A select's name and options, for a person choosing one (labels are untrusted page text).
_SELECT_INFO = r"""(el, [max, maxLabel]) => {
  const words = n => (n?.innerText || n?.textContent || '').replace(/\s+/g, ' ').trim();
  const name = (el.getAttribute('aria-label') ||
    (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean).slice(0, 6)
      .map(id => words(el.getRootNode().getElementById?.(id))).join(' ') ||
    [...(el.labels || [])].slice(0, 6).map(words).join(' ') || el.getAttribute('title') || '').trim();
  const all = [...el.options];
  const options = all.slice(0, max).map(o => {
    const group = o.parentElement?.localName === 'optgroup' ? o.parentElement : null;
    return {label: (o.label || o.text || '').replace(/\s+/g, ' ').trim().slice(0, maxLabel),
      disabled: !!(o.disabled || group?.disabled), group: (group?.label || '').trim().slice(0, maxLabel)};
  });
  return {label: name.slice(0, maxLabel), options, selected: el.selectedIndex < max ? el.selectedIndex : -1,
    truncated: all.length > max,
    disabled: el.matches(':disabled')};
}"""

# Where an embedded frame's content starts in its parent's visible page: inside its border and padding.
_CONTENT_ORIGIN = r"""el => {
  const r = el.getBoundingClientRect(), s = getComputedStyle(el);
  return [r.left + el.clientLeft + parseFloat(s.paddingLeft || 0), r.top + el.clientTop + parseFloat(s.paddingTop || 0)];
}"""

# The element a point of the visible page lands on, through open shadow roots, and where an embedded frame's
# content starts when it is one.
_HIT = r"""([x, y]) => {
  if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) return null;
  let el = document.elementFromPoint(x, y);
  for (let inner; el?.shadowRoot && (inner = el.shadowRoot.elementFromPoint(x, y)) && inner !== el;) el = inner;
  if (!el) return null;
  return {el, frame: el.localName === 'iframe' || el.localName === 'frame' ? (""" + _CONTENT_ORIGIN + r""")(el) : null};
}"""

# The focused element, through open shadow roots; an embedded frame when focus is inside one.
_FOCUSED = r"""() => {
  let el = document.activeElement;
  while (el?.shadowRoot?.activeElement) el = el.shadowRoot.activeElement;
  return el && el !== document.body ? el : null;
}"""

# Every option's label and whether it can be picked, to match the agent's option by its exact label.
_OPTION_INDEX = r"""(el, wanted) => {
  const all = [...el.options], found = [];
  all.forEach((o, i) => { if ((o.label || o.text || '').replace(/\s+/g, ' ').trim() === wanted) found.push(i); });
  if (found.length !== 1) return {count: found.length};
  const o = all[found[0]];
  return {count: 1, index: found[0], disabled: !!(o.disabled || (o.parentElement?.localName === 'optgroup' && o.parentElement.disabled))};
}"""


async def popup_select(element: ElementHandle) -> bool:
    """Whether a click or key on this element would open a list Chromium draws outside the page."""
    try:
        return bool(await element.evaluate(_POPUP_SELECT))
    except BrowserError:
        return False


async def _child_frame(element: ElementHandle) -> Frame | None:
    try:
        return await element.content_frame()
    except BrowserError:
        return None


async def select_at(page: Page, x: float, y: float) -> tuple[Frame, ElementHandle] | None:
    """The select a point of the visible page is on, if its list would open outside the page; also inside
    embedded frames (other origins too), where a payment form's select often is."""
    frame: Frame = page.main_frame
    for _ in range(MAX_FRAME_DEPTH):
        try:
            hit = await frame.evaluate_handle(_HIT, [x, y])
        except BrowserError:
            return None
        try:
            element_handle = await hit.get_property("el")
            frame_handle = await hit.get_property("frame")
            try:
                offset = await frame_handle.json_value()
            finally:
                await frame_handle.dispose()
        except BrowserError:
            await hit.dispose()
            return None
        await hit.dispose()
        element = element_handle.as_element()
        if element is None:
            await element_handle.dispose()
            return None
        if offset:
            child = await _child_frame(element)
            await element.dispose()
            if child is None:
                return None
            frame, x, y = child, x - float(offset[0]), y - float(offset[1])
            continue
        if await popup_select(element):
            return frame, element
        await element.dispose()
        return None
    return None


async def focused_select(page: Page) -> tuple[Frame, ElementHandle] | None:
    """The focused select, also inside embedded frames, if its list would open outside the page."""
    frame: Frame = page.main_frame
    for _ in range(MAX_FRAME_DEPTH):
        try:
            handle = await frame.evaluate_handle(_FOCUSED)
        except BrowserError:
            return None
        element = handle.as_element()
        if element is None:
            await handle.dispose()
            return None
        try:
            holder = await element.evaluate("el => el.localName === 'iframe' || el.localName === 'frame'")
        except BrowserError:  # its document went away meanwhile: the key goes to the page as usual
            await element.dispose()
            return None
        if holder:
            child = await _child_frame(element)
            await element.dispose()
            if child is None:
                return None
            frame = child
            continue
        if await popup_select(element):
            return frame, element
        await element.dispose()
        return None
    return None


async def select_by_label(element: ElementHandle, option: str, timeout_ms: int) -> None:
    """Pick the select's one enabled option with exactly this label, as a person picking it from the list."""
    if not await element.evaluate("el => el.localName === 'select' && !el.multiple"):
        raise fail("not_a_select")
    if await element.evaluate("el => el.matches(':disabled')"):
        raise fail("unsupported_control")
    found = await element.evaluate(_OPTION_INDEX, option)
    if found["count"] > 1:
        raise fail("ambiguous_control")
    if found["count"] == 0:
        raise fail("unknown_option")
    if found["disabled"]:
        raise fail("unsupported_control")
    await element.focus()
    await element.select_option(index=int(found["index"]), timeout=timeout_ms)


@dataclass
class Picker:
    """The select a person's click or key asked to open, and the options they were shown."""
    id: str
    page: Page
    element: ElementHandle
    labels: list[str]

    async def dispose(self) -> None:
        try:
            await self.element.dispose()
        except BrowserError:
            pass


async def _page_box(element: ElementHandle) -> list[float] | None:
    """Where an element is on the visible page (CSS pixels), also inside embedded frames."""
    box = await element.evaluate("el => { const r = el.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; }")
    frame = await element.owner_frame()
    while frame is not None and frame.parent_frame is not None:
        holder = await frame.frame_element()
        try:
            left, top = await holder.evaluate(_CONTENT_ORIGIN)
        finally:
            await holder.dispose()
        box[0] += left
        box[1] += top
        frame = frame.parent_frame
    return [round(value, 1) for value in box]


async def open_picker(page: Page, element: ElementHandle) -> tuple[Picker, dict[str, Any]] | None:
    """Focus the select (as a click would, but without its list) and describe it for the person's viewer.
    None for a disabled one, which a click wouldn't open either."""
    try:
        info = await element.evaluate(_SELECT_INFO, [MAX_PICKER_OPTIONS, MAX_LABEL])
        if info["disabled"]:
            return None
        await element.focus()
        box = await _page_box(element)
    except BrowserError:
        return None
    picker = Picker(secrets.token_urlsafe(12), page, element, [row["label"] for row in info["options"]])
    view = {
        "id": picker.id,
        "label": info["label"],
        "options": info["options"],
        "selected": info["selected"],
        "truncated": info["truncated"],
        # Where the select is on the visible page (CSS pixels), so the viewer can show its list beside it.
        "box": box,
    }
    return picker, view


async def choose(picker: Picker, page: Page, index: int, timeout_ms: int) -> None:
    """Pick the option at `index` of what the person was shown, if the select still offers exactly that."""
    if picker.page is not page:
        raise fail("stale_picker")
    try:
        connected = await picker.element.evaluate("el => el.isConnected")
        info = await picker.element.evaluate(_SELECT_INFO, [MAX_PICKER_OPTIONS, MAX_LABEL]) if connected else None
    except BrowserError as exc:
        raise fail("stale_picker") from exc
    if info is None or [row["label"] for row in info["options"]] != picker.labels:
        raise fail("stale_picker")
    if not 0 <= index < len(info["options"]) or info["options"][index]["disabled"] or info["disabled"]:
        raise fail("unsupported_control")
    await picker.element.focus()
    await picker.element.select_option(index=index, timeout=timeout_ms)
