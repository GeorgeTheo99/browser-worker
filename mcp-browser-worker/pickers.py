"""Pickers Chromium draws outside the page, chosen through the worker instead.

Chromium draws some controls' pickers as popups outside the page: a `<select>`'s list (an OS menu on macOS, a
separate window on Linux), the date and time pickers of `date`, `time`, `datetime-local`, `month` and `week`
inputs, a `color` input's colour panel, and a `datalist`'s suggestions. Nothing that sees or drives the page
reaches them: not the live view's screencast, not CDP input, not screenshots. A remote click that opens one
leaves a picker nobody can see or use. So:

- **The agent** never opens one. `elements` lists a select's options and a datalist field's suggestions;
  `select_option` picks an option by its ref; a date, time or colour field takes its value with `type`.
  Clicking a select, date, time or colour field is refused (`native_select`, `native_picker`).
- **A person in the live view** gets the picker from the worker instead. A press on a select, date, time or
  colour field (found by Blink's own hit test, so also inside closed shadow roots, embedded frames of other
  origins, and transformed frames), or a key that would open a focused select's list, reaches the page only as
  focus, and the response carries a `picker` the viewer shows. The person answers with `choose` (an option's
  index) or `set` (a date, time or colour value). A field with suggestions keeps its keys and clicks; its
  response carries the suggestions that match what it holds, which `choose` can fill in.
- **Anything that still opens one** (a page script's `showPicker()` behind a button, a keyboard shortcut) is
  caught after the input: an open picker matches `:open`, is closed, and is handed to the viewer (or, after the
  agent's action, reported as `closed_picker`).

Values are set as the browser sets them when someone picks: the field focused, its value set, then `input`
and `change` events. A field a press found runs these scripts in an isolated world of its frame, which the
page's scripts can't patch; one found by focus or by the open-picker check runs them in the page's world, as
the worker's other page scripts do, so a page could misreport or resist its own fields there, and only those.

A select whose list is part of the page (a listbox with `size` > 1, or a customizable `appearance:
base-select` one) is ordinary page content and is left alone.
"""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from typing import Any, Protocol

from patchright.async_api import BrowserContext, CDPSession, ElementHandle, Frame, Page
from patchright.async_api import Error as BrowserError

from errors import WorkerError, fail

# Enough for a whole country list; labels are at most MAX_LABEL characters.
MAX_PICKER_OPTIONS = 1000
MAX_LABEL = 200
MAX_VALUE = 64
# How deep a point is followed into frames of other processes, and how many frames are searched.
MAX_FRAME_DEPTH = 6
MAX_SCANNED_FRAMES = 30
DATE_TYPES = frozenset({"date", "time", "datetime-local", "month", "week"})
# Kinds whose picker opens outside the page on a click; suggestions show while the field is used.
POPUP_KINDS = frozenset({"select", "date", "color"})
_VALUES = {
    "date": re.compile(r"\d{4,6}-\d{2}-\d{2}"),
    "time": re.compile(r"\d{2}:\d{2}(?::\d{2}(?:\.\d{1,3})?)?"),
    "datetime-local": re.compile(r"\d{4,6}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,3})?)?"),
    "month": re.compile(r"\d{4,6}-\d{2}"),
    "week": re.compile(r"\d{4,6}-W\d{2}"),
    "color": re.compile(r"#[0-9a-f]{6}"),
}

_WORDS = r"""// A label's own words: not those of a control inside it (a select's options, a field's text).
  const words = n => {
    if (!n) return '';
    const walker = document.createTreeWalker(n, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {acceptNode: c =>
      c.nodeType === 1 && c.matches('select,datalist,textarea,input,script,style,[contenteditable]:not([contenteditable="false"])')
        ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT});
    let out = '', c, count = 0;
    while ((c = walker.nextNode()) && count++ < 400 && out.length < 400) if (c.nodeType === 3) out += c.textContent + ' ';
    return out.replace(/\s+/g, ' ').trim();
  };
  const nameOf = el => (el.getAttribute('aria-label') ||
    (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean).slice(0, 6)
      .map(id => words(el.getRootNode().getElementById?.(id))).join(' ') ||
    [...(el.labels || [])].slice(0, 6).map(words).join(' ') || el.getAttribute('title') ||
    el.getAttribute('placeholder') || '').replace(/\s+/g, ' ').trim();"""

# What a control's picker is, for the viewer (labels are untrusted page text); null when it has none that
# Chromium draws outside the page. `frame` marks an embedded frame, whose content another process may draw.
_INFO = r"""(node, [max, maxLabel]) => {
  const el = node?.nodeType === 3 ? node.parentElement : node;
  if (!el || el.nodeType !== 1) return null;
  if (el.localName === 'iframe' || el.localName === 'frame') return {frame: true};
  """ + _WORDS + r"""
  const base = {label: nameOf(el).slice(0, maxLabel), disabled: el.matches(':disabled'), readonly: !!el.readOnly,
    focused: el.matches(':focus')};
  const clip = s => String(s || '').replace(/\s+/g, ' ').trim().slice(0, maxLabel);
  if (el.localName === 'select') {
    if (el.multiple || el.size > 1 || getComputedStyle(el).appearance === 'base-select') return null;
    const all = [...el.options];
    const options = all.slice(0, max).map(o => {
      const group = o.parentElement?.localName === 'optgroup' ? o.parentElement : null;
      return {label: clip(o.label || o.text), disabled: !!(o.disabled || group?.disabled), group: clip(group?.label)};
    });
    return {...base, kind: 'select', readonly: false, options,
      selected: el.selectedIndex < max ? el.selectedIndex : -1, truncated: all.length > max};
  }
  if (el.localName !== 'input') return null;
  const type = el.type;
  if (['date', 'time', 'datetime-local', 'month', 'week'].includes(type))
    return {...base, kind: 'date', input_type: type, value: el.value, min: el.min.slice(0, 64),
      max: el.max.slice(0, 64), step: el.step.slice(0, 64)};
  if (type === 'color') return {...base, kind: 'color', value: el.value};
  const list = el.list;
  if (list && !['hidden', 'checkbox', 'radio', 'file', 'range', 'color', 'button', 'submit', 'reset', 'image',
                'password'].includes(type)) {
    // As Chromium offers them: those whose value or label holds what the field holds.
    const typed = el.value.toLowerCase();
    const all = [...list.querySelectorAll('option')].filter(o => !o.disabled && o.value &&
      (!typed || o.value.toLowerCase().includes(typed) || (o.label || '').toLowerCase().includes(typed)) &&
      o.value.toLowerCase() !== typed);
    const options = all.slice(0, max).map(o => ({
      label: clip(o.label && o.label !== o.value ? o.value + ' · ' + o.label : o.value), disabled: false, group: ''}));
    return {...base, kind: 'suggestions', options, values: all.slice(0, max).map(o => o.value),
      selected: -1, truncated: all.length > max};
  }
  return null;
}"""

# Whether an element is a control whose picker Chromium draws outside the page: its kind, or null.
_KIND = r"""el => {
  if (!el || el.nodeType !== 1) return null;
  if (el.localName === 'select')
    return el.multiple || el.size > 1 || getComputedStyle(el).appearance === 'base-select' ? null : 'select';
  if (el.localName !== 'input') return null;
  if (['date', 'time', 'datetime-local', 'month', 'week'].includes(el.type)) return 'date';
  return el.type === 'color' ? 'color' : null;
}"""

# The focused element, through open shadow roots; an embedded frame when focus is inside one.
_FOCUSED = r"""() => {
  let el = document.activeElement;
  while (el?.shadowRoot?.activeElement) el = el.shadowRoot.activeElement;
  return el && el !== document.body ? el : null;
}"""

# A picker that is open (`:open`), through open shadow roots.
# Only a frame the person or the agent just acted in can open one (showPicker needs that activation), so only
# such a frame's shadow roots are searched, and a picker opened a frame later (from a timer or a promise) is
# waited for once.
_OPEN = r"""async () => {
  const query = 'select:open, input:open';
  try { document.querySelector(query); } catch { return null; }
  const search = () => {
    const found = document.querySelector(query);
    if (found || !navigator.userActivation?.isActive) return found;
    const roots = [document];
    let seen = 0;
    for (let i = 0; i < roots.length && i < 200 && seen < 20000; i++) {
      const inner = i && roots[i].querySelector(query);
      if (inner) return inner;
      for (const el of roots[i].querySelectorAll('*')) { seen++; if (el.shadowRoot) roots.push(el.shadowRoot); }
    }
    return null;
  };
  const now = search();
  if (now || !navigator.userActivation?.isActive) return now;
  await new Promise(done => requestAnimationFrame(() => done()));
  return search();
}"""

# Close an open picker: losing focus closes a list or a date picker. A colour panel stays open after its field
# loses focus (and after it is briefly disabled, most times); it closes when its field stops being a colour field
# for a moment, which keeps its value.
_CLOSE = r"""el => {
  el.blur();
  if (el.type === 'color' && el.matches(':open')) {
    const value = el.value;
    el.type = 'text';
    el.type = 'color';
    if (el.value !== value) Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, value);
  }
}"""

# Pick a select's option, or set a field's value, as a person does: focus, the value, then input and change.
_PICK = r"""(el, [index, value]) => {
  el.focus();
  if (el.localName === 'select') {
    el.selectedIndex = index;
    if (el.selectedIndex !== index) return false;
  } else {
    // A value the field can't hold (2026-02-30) would empty it: it keeps what it held instead.
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set, was = el.value;
    setter.call(el, value);
    if (el.value !== value) { setter.call(el, was); return false; }
  }
  el.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return true;
}"""

_RECT = "el => { const r = el.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; }"

# Where an embedded frame's content starts in its parent's visible page: inside its border and padding.
_CONTENT_ORIGIN = r"""el => {
  const r = el.getBoundingClientRect(), s = getComputedStyle(el);
  return [r.left + el.clientLeft + parseFloat(s.paddingLeft || 0), r.top + el.clientTop + parseFloat(s.paddingTop || 0)];
}"""

# Every option's label and whether it can be picked, to match the agent's option by its exact label.
_OPTION_INDEX = r"""(el, wanted) => {
  const all = [...el.options], found = [];
  all.forEach((o, i) => { if ((o.label || o.text || '').replace(/\s+/g, ' ').trim() === wanted) found.push(i); });
  if (found.length !== 1) return {count: found.length};
  const o = all[found[0]];
  return {count: 1, index: found[0], disabled: !!(o.disabled || (o.parentElement?.localName === 'optgroup' && o.parentElement.disabled))};
}"""


class Target(Protocol):
    """A control in some frame that the worker's scripts can run on."""

    async def call(self, script: str, arg: Any = None) -> Any: ...
    async def box(self) -> list[float] | None: ...
    async def dispose(self) -> None: ...


@dataclass
class HandleTarget:
    """A control the page's own document can reach (through open shadow roots and frames)."""
    element: ElementHandle

    async def call(self, script: str, arg: Any = None) -> Any:
        return await self.element.evaluate(script, arg)

    async def box(self) -> list[float] | None:
        box = await self.element.evaluate(_RECT)
        frame = await self.element.owner_frame()
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

    async def dispose(self) -> None:
        try:
            await self.element.dispose()
        except BrowserError:
            pass


# A frame drawn by another process, placed in its parent's visible page: its content box's corners (top left,
# top right, bottom left; any transform keeps them a parallelogram) and its size in CSS pixels.
Placement = tuple[tuple[float, float], tuple[float, float], tuple[float, float], float, float]


def _to_parent(point: tuple[float, float], placement: Placement) -> tuple[float, float]:
    (x0, y0), (x1, y1), (x2, y2), width, height = placement
    u, v = point[0] / width, point[1] / height
    return x0 + u * (x1 - x0) + v * (x2 - x0), y0 + u * (y1 - y0) + v * (y2 - y0)


def _to_child(point: tuple[float, float], placement: Placement) -> tuple[float, float] | None:
    (x0, y0), (x1, y1), (x2, y2), width, height = placement
    ax, ay, bx, by = x1 - x0, y1 - y0, x2 - x0, y2 - y0
    det = ax * by - ay * bx
    if abs(det) < 1e-6:
        return None
    px, py = point[0] - x0, point[1] - y0
    u, v = (px * by - py * bx) / det, (ax * py - ay * px) / det
    if not (0 <= u < 1 and 0 <= v < 1):
        return None
    return u * width, v * height


@dataclass
class NodeTarget:
    """A control found by Blink's hit test, wherever it is: inside a closed shadow root too. Scripts run on it in
    an isolated world of its frame."""
    session: CDPSession
    object_id: str
    backend_node_id: int
    # From its frame's process up to the page's: where each frame drawn by another process is placed.
    placements: list[Placement] = field(default_factory=list)

    async def call(self, script: str, arg: Any = None) -> Any:
        result = await self.session.send("Runtime.callFunctionOn", {
            "objectId": self.object_id,
            "functionDeclaration": f"function(arg) {{ return ({script})(this, arg); }}",
            "arguments": [{"value": arg}],
            "returnByValue": True,
            "awaitPromise": True,
        })
        if "exceptionDetails" in result:
            raise WorkerError("picker script failed", code="stale_picker")
        return result.get("result", {}).get("value")

    async def box(self) -> list[float] | None:
        model = (await self.session.send("DOM.getBoxModel", {"backendNodeId": self.backend_node_id}))["model"]
        quad = model["border"]
        points = [(quad[i], quad[i + 1]) for i in range(0, 8, 2)]
        for placement in self.placements:
            points = [_to_parent(point, placement) for point in points]
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        return [round(min(xs), 1), round(min(ys), 1), round(max(xs) - min(xs), 1), round(max(ys) - min(ys), 1)]

    async def dispose(self) -> None:
        try:
            await self.session.send("Runtime.releaseObject", {"objectId": self.object_id})
        except BrowserError:
            pass


class Probe:
    """CDP sessions for hit tests: one for a page, and one for each frame another process draws."""

    def __init__(self, context: BrowserContext) -> None:
        self.context = context
        self._sessions: dict[Any, CDPSession | None] = {}
        self._frame_ids: dict[Frame, str] = {}
        self._worlds: dict[tuple[int, str], int] = {}

    async def _session(self, owner: Page | Frame) -> CDPSession | None:
        if owner not in self._sessions:
            try:
                self._sessions[owner] = await self.context.new_cdp_session(owner)
            except BrowserError:  # a frame its parent's process draws
                self._sessions[owner] = None
        return self._sessions[owner]

    async def forget(self, owner: Page | Frame) -> None:
        session = self._sessions.pop(owner, None)
        if isinstance(owner, Frame):
            self._frame_ids.pop(owner, None)
        if session is not None:
            self._worlds = {key: world for key, world in self._worlds.items() if key[0] != id(session)}
            try:
                await session.detach()
            except BrowserError:
                pass

    async def _frame_session(self, page: Page, frame_id: str) -> tuple[Frame, CDPSession] | None:
        """The frame another process draws with this CDP frame id, and its session."""
        # Frames that left the page (ads come and go) take their sessions with them.
        live = set(page.frames)
        for gone in [owner for owner in self._sessions if isinstance(owner, Frame) and owner.page == page and owner not in live]:
            await self.forget(gone)
        for frame in page.frames[1:MAX_SCANNED_FRAMES]:
            if frame.is_detached():
                await self.forget(frame)
                continue
            session = await self._session(frame)
            if session is None:
                continue
            if frame not in self._frame_ids:
                try:
                    self._frame_ids[frame] = (await session.send("Page.getFrameTree"))["frameTree"]["frame"]["id"]
                except BrowserError:
                    await self.forget(frame)
                    continue
            if self._frame_ids[frame] == frame_id:
                return frame, session
        return None

    async def _resolve(self, session: CDPSession, backend_node_id: int, frame_id: str) -> str | None:
        """The node as an object in an isolated world of its frame (made once per frame and document)."""
        for attempt in range(2):
            key = (id(session), frame_id)
            if key not in self._worlds:
                world = await session.send(
                    "Page.createIsolatedWorld", {"frameId": frame_id, "worldName": "browser-worker-pickers"}
                )
                self._worlds[key] = world["executionContextId"]
            try:
                resolved = await session.send(
                    "DOM.resolveNode", {"backendNodeId": backend_node_id, "executionContextId": self._worlds[key]}
                )
                return resolved["object"]["objectId"]
            except BrowserError:
                self._worlds.pop(key, None)  # its document was replaced: a new world for the new one
                if attempt:
                    raise
        return None

    async def node_at(self, page: Page, x: float, y: float) -> NodeTarget | None:
        """The element a point of the visible page lands on, by Blink's own hit test: through shadow roots of
        any kind and transformed frames; into a frame another process draws, by where that frame is placed."""
        owner: Page | Frame = page
        session = await self._session(page)
        placements: list[Placement] = []
        point = (x, y)
        try:
            for _ in range(MAX_FRAME_DEPTH):
                if session is None:
                    return None
                # The hit test takes the document's coordinates: the point plus how far the page is scrolled.
                visual = (await session.send("Page.getLayoutMetrics"))["cssVisualViewport"]
                try:
                    hit = await session.send("DOM.getNodeForLocation", {
                        "x": int(point[0] + visual["pageX"]), "y": int(point[1] + visual["pageY"]),
                        "includeUserAgentShadowDOM": False, "ignorePointerEventsNone": True,
                    })
                except BrowserError as exc:
                    if "No node found" in str(exc):
                        return None  # nothing there: the press goes to the page as usual
                    raise
                object_id = await self._resolve(session, hit["backendNodeId"], hit["frameId"])
                if object_id is None:
                    return None
                target = NodeTarget(session, object_id, hit["backendNodeId"], list(placements))
                info = await target.call(_INFO, [1, 1])
                if not (info and info.get("frame")):
                    return target
                await target.dispose()
                described = (await session.send("DOM.describeNode", {"backendNodeId": hit["backendNodeId"]}))["node"]
                child_frame = await self._frame_session(page, described.get("frameId") or "")
                if child_frame is None:
                    return None
                owner, child = child_frame
                quad = (await session.send("DOM.getBoxModel", {"backendNodeId": hit["backendNodeId"]}))["model"]["content"]
                size = (await child.send("Page.getLayoutMetrics"))["cssLayoutViewport"]
                placement: Placement = (
                    (quad[0], quad[1]), (quad[2], quad[3]), (quad[6], quad[7]),
                    float(size["clientWidth"]), float(size["clientHeight"]),
                )
                local = _to_child(point, placement)
                if local is None:
                    return None
                placements.insert(0, placement)
                session, point = child, local
        except BrowserError:
            # Its session went away (a frame that swapped processes, a closed tab): a new one next time.
            await self.forget(owner)
            return None
        except (KeyError, TypeError):
            return None
        return None

    async def close(self) -> None:
        sessions, self._sessions = self._sessions, {}
        self._frame_ids.clear()
        self._worlds.clear()
        for session in sessions.values():
            if session is not None:
                try:
                    await session.detach()
                except BrowserError:
                    pass


async def control_kind(element: ElementHandle) -> str | None:
    """Whether a click on this element would open a picker Chromium draws outside the page: its kind."""
    try:
        return await element.evaluate(_KIND)
    except BrowserError:
        return None


async def focused(page: Page) -> HandleTarget | None:
    """The focused element, through open shadow roots and into frames."""
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
            child = await element.content_frame() if holder else None
        except BrowserError:  # its document went away meanwhile
            await element.dispose()
            return None
        if not holder:
            return HandleTarget(element)
        await element.dispose()
        if child is None:
            return None
        frame = child
    return None


async def open_popup(page: Page) -> HandleTarget | None:
    """A picker that opened outside the page anyway (a page script's showPicker, a shortcut), closed now."""
    for frame in page.frames[:MAX_SCANNED_FRAMES]:
        try:
            handle = await frame.evaluate_handle(_OPEN)
        except BrowserError:
            continue
        element = handle.as_element()
        if element is None:
            await handle.dispose()
            continue
        try:
            await element.evaluate(_CLOSE)
        except BrowserError:
            await element.dispose()
            continue
        return HandleTarget(element)
    return None


async def describe(target: Target) -> dict[str, Any] | None:
    try:
        return await target.call(_INFO, [MAX_PICKER_OPTIONS, MAX_LABEL])
    except (BrowserError, WorkerError):
        return None


async def select_by_label(element: ElementHandle, option: str) -> None:
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
    if not await element.evaluate(_PICK, [int(found["index"]), None]):
        raise fail("unsupported_control")


@dataclass
class Picker:
    """A picker the worker showed a person instead of Chromium: its control, and what they were shown."""
    id: str
    page: Page
    target: Target
    kind: str
    # What must still hold for a choice to apply: the option labels shown, or a date field's type.
    shown: list[str]
    values: list[str]
    # Chosen from already: a picker is good for one choice. Its control stays known, so a key on it while it
    # keeps the focus (also inside a closed shadow root) shows its picker again.
    used: bool = False

    async def dispose(self) -> None:
        await self.target.dispose()


async def open_picker(page: Page, target: Target, *, kinds: frozenset[str] = POPUP_KINDS) -> tuple[Picker, dict[str, Any]] | None:
    """Describe a control's picker for the person's viewer, focusing it as a click would (without its picker).
    None for a control of another kind, or a disabled or read-only one, which opens no picker either."""
    info = await describe(target)
    if not info or info.get("kind") not in kinds or info["disabled"] or info["readonly"]:
        return None
    if info["kind"] == "suggestions" and not info["options"]:
        return None
    try:
        if info["kind"] != "suggestions":
            await target.call("el => el.focus()")
        box = await target.box()
    except (BrowserError, WorkerError, KeyError, TypeError):
        return None
    kind = info["kind"]
    picker = Picker(
        secrets.token_urlsafe(12), page, target, kind,
        [row["label"] for row in info.get("options", [])] if kind in {"select", "suggestions"} else [info.get("input_type", kind)],
        info.get("values", []),
    )
    view: dict[str, Any] = {"id": picker.id, "kind": kind, "label": info["label"], "box": box}
    if kind in {"select", "suggestions"}:
        view.update(options=info["options"], selected=info["selected"], truncated=info["truncated"])
    elif kind == "date":
        view.update({key: info[key] for key in ("input_type", "value", "min", "max", "step")})
    else:
        view["value"] = info["value"]
    return picker, view


async def apply(picker: Picker, page: Page, *, index: int | None = None, value: str | None = None) -> None:
    """Pick the option at `index` of what the person was shown, or set the value they chose, if the control
    still offers exactly that."""
    if picker.page is not page or picker.used:
        raise fail("stale_picker")
    info = await describe(picker.target)
    if not info or info.get("kind") != picker.kind:
        raise fail("stale_picker")
    if picker.kind in {"select", "suggestions"}:
        if index is None or value is not None:
            raise fail("invalid_request")
        if [row["label"] for row in info["options"]] != picker.shown:
            raise fail("stale_picker")
        if not 0 <= index < len(info["options"]) or info["options"][index]["disabled"] or info["disabled"]:
            raise fail("unsupported_control")
        arg = [index, None] if picker.kind == "select" else [None, picker.values[index]]
    else:
        if value is None or index is not None:
            raise fail("invalid_request")
        kind = info.get("input_type", "color")
        if kind != picker.shown[0]:
            raise fail("stale_picker")
        value = value.lower() if kind == "color" else value
        if info["disabled"] or info["readonly"] or not (
            _VALUES[kind].fullmatch(value) or (value == "" and kind in DATE_TYPES)
        ):
            raise fail("unsupported_control")
        arg = [None, value]
    try:
        done = await picker.target.call(_PICK, arg)
    except BrowserError as exc:
        raise fail("stale_picker") from exc
    if not done:  # the field refused the value (one it can't hold)
        raise fail("unsupported_control")
    picker.used = True
