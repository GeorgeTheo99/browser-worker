"""Dropdown lists (`<select>`): the agent picks with select_option on a ref, a person through a picker."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from test_browser_runtime import BrowserFixtureResolver

import auth
import security
import server as mcp_server
from artifacts import ArtifactStore
from auth import Caller
from browser_runtime import BrowserManager
from errors import WorkerError

PAGE = '''<!doctype html><title>Pay</title><body style="margin:0">
<form action="/pay"><label for=method>Payment Method</label>
<select id=method name=method style="position:absolute;left:20px;top:20px;width:300px;height:30px">
<option value="" disabled selected>Payment Method</option><option>Bank Account (Checking)</option>
<option>Bank Account (Savings)</option><option>Card or Digital Wallet</option><option>Card or Digital Wallet</option>
<optgroup label="Other" disabled><option>Cash</option></optgroup></select>
<select id=list size=3 aria-label=Plan style="position:absolute;left:20px;top:80px;width:300px;height:60px">
<option>Basic</option><option>Plus</option></select>
<select id=off disabled aria-label=Off style="position:absolute;left:400px;top:20px;width:100px;height:30px"><option>Only</option></select>
<button id=other type=button style="position:absolute;left:400px;top:80px;width:100px;height:30px">Other</button>
</form>
<iframe src="http://other-public.test:PORT/frame" style="position:absolute;left:0;top:200px;width:600px;height:200px;border:7px solid;padding:3px"></iframe>
<script>
window.events = [];
method.addEventListener('input', () => events.push('input:' + method.value));
method.addEventListener('change', () => events.push('change:' + method.value));
other.onclick = () => events.push('other');
</script></body>'''

FRAME = '''<!doctype html><body style="margin:0"><form><select id=state aria-label=State
style="position:absolute;left:30px;top:30px;width:200px;height:30px"><option>NY</option><option>NJ</option></select></form>
<script>window.changes = []; state.onchange = () => changes.push(state.value);</script></body>'''


@pytest.fixture
async def browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    async def fixture(reader, writer):
        path = (await reader.read(65536)).split(b"\r\n", 1)[0].split(b" ")[1]
        body = (FRAME if path == b"/frame" else PAGE.replace("PORT", str(port))).encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: " +
                     str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    http = await asyncio.start_server(fixture, "127.0.0.1", 0)
    port = http.sockets[0].getsockname()[1]
    monkeypatch.setattr(security, "ALLOWED_DESTINATION_PORTS", frozenset({80, 443, port}))
    artifacts = ArtifactStore(tmp_path / "artifacts")
    await artifacts.start()
    manager = BrowserManager(artifacts, resolver_factory=lambda: BrowserFixtureResolver(port),
                             sessions_dir=tmp_path / "sessions")
    await manager.start()
    monkeypatch.setattr(mcp_server, "manager", manager)
    token = auth._current_caller.set(Caller("owner", frozenset({"inspect.read", "inspect.interact"})))
    opened = await manager.open("owner", f"http://public.test:{port}/", wait_until="load", timeout_ms=15000)
    sid = opened["session_id"]
    page = manager._active_page(await manager._get("owner", sid))
    await page.wait_for_function("document.querySelector('iframe').contentWindow && frames.length === 1")
    try:
        yield manager, sid, page
    finally:
        auth._current_caller.reset(token)
        await manager.shutdown()
        await artifacts.close()
        http.close()
        await http.wait_closed()


async def call(sid, action, **kwargs):
    return (await mcp_server.browser_inspect(action=action, session_id=sid, **kwargs)).structured_content


def frame_of(page):
    return next(frame for frame in page.frames if frame.url.endswith("/frame"))


@pytest.mark.browser
async def test_agent_lists_a_selects_options_and_picks_one_by_its_ref(browser):
    _, sid, page = browser
    found = await call(sid, "elements", text="card or digital")
    assert [row["label"] for row in found["elements"]] == ["Payment Method"]
    row = found["elements"][0]
    assert row["tag"] == "select" and row["selected"] == "Payment Method"
    # The options the words are in, and that there are others.
    assert row["options"] == ["Card or Digital Wallet", "Card or Digital Wallet"] and row["options_truncated"]
    listed = (await call(sid, "elements", selector="form"))["elements"]
    row = next(r for r in listed if r["tag"] == "select" and r["label"] == "Payment Method")
    # Only options that can be picked: not the disabled placeholder or a disabled group's.
    assert row["options"] == [
        "Bank Account (Checking)", "Bank Account (Savings)", "Card or Digital Wallet", "Card or Digital Wallet"]
    assert "options_truncated" not in row

    # A click or keys would open a list Chromium draws outside the page: refused, with what to do instead.
    for action, extra in (("click", {}), ("type", {"text": "C"})):
        refused = await call(sid, action, ref=row["ref"], **extra)
        assert refused["code"] == "native_select", refused
    assert (await call(sid, "click", selector="#method"))["code"] == "native_select"
    assert await page.evaluate("events") == []

    picked = await call(sid, "select_option", ref=row["ref"], option="Bank Account (Savings)")
    assert picked["status"] == "ok" and picked["selected"] == "Bank Account (Savings)", picked
    assert await page.evaluate("events") == ["input:Bank Account (Savings)", "change:Bank Account (Savings)"]
    assert await page.evaluate("document.activeElement.id") == "method"
    for option, code in (("Missing", "unknown_option"), ("bank account (savings)", "unknown_option"),
                         ("Payment Method", "unsupported_control"), ("Cash", "unsupported_control"),
                         ("Card or Digital Wallet", "ambiguous_control")):
        assert (await call(sid, "select_option", ref=row["ref"], option=option))["code"] == code
    other = (await call(sid, "elements", text="Other"))["elements"][0]["ref"]
    assert (await call(sid, "select_option", ref=other, option="Other"))["code"] == "not_a_select"
    # A listbox draws its options in the page: clicking it is fine.
    plan = (await call(sid, "elements", text="Plan"))["elements"][0]
    assert plan["options"] == ["Basic", "Plus"]
    assert (await call(sid, "click", ref=plan["ref"]))["status"] == "ok"

    # A long list shows the options the filter's words are in, so the one wanted is always listed exactly.
    await page.evaluate("""() => {
      const s = document.createElement('select'); s.id = 'country'; s.setAttribute('aria-label', 'Country');
      s.style.cssText = 'position:absolute;left:600px;top:20px;width:200px;height:30px';
      for (let i = 0; i < 250; i++) s.add(new Option(i === 230 ? 'United States' : 'Country ' + i));
      document.body.append(s);
    }""")
    first = (await call(sid, "elements", text="Country"))["elements"]
    country = next(row for row in first if row["label"] == "Country")
    assert len(country["options"]) == 50 and country["options_truncated"]
    states = (await call(sid, "elements", text="united st"))["elements"]
    assert [(row["label"], row["options"], row.get("options_truncated")) for row in states] == [
        ("Country", ["United States"], True)]
    assert (await call(sid, "select_option", ref=states[0]["ref"], option="United States"))["status"] == "ok"

    # A frame's select the same way, with a ref from that frame's elements.
    frames = await call(sid, "frames")
    frame_id = next(f["frame_id"] for f in frames["frames"] if str(f.get("url", "")).endswith("/frame"))
    state = (await call(sid, "elements", frame_id=frame_id))["elements"][0]
    assert state["options"] == ["NY", "NJ"]
    assert (await call(sid, "select_option", ref=state["ref"], option="NJ"))["status"] == "ok"
    assert await frame_of(page).evaluate("changes") == ["NJ"]

    # It is a page action: only for a caller that may act on pages.
    token = auth._current_caller.set(Caller("owner", frozenset({"inspect.read", "inspect.controls"})))
    try:
        denied = await call(sid, "select_option", ref=row["ref"], option="Bank Account (Checking)")
    finally:
        auth._current_caller.reset(token)
    assert denied["code"] == "capability_denied"


@pytest.mark.browser
async def test_a_person_picks_through_their_viewer_never_the_browsers_own_list(browser):
    manager, sid, page = browser

    # A click on a select focuses it and gives the viewer its options; nothing else reaches the page.
    clicked = await manager.human_input("owner", sid, {"type": "click", "x": 100, "y": 35, "button": "left", "click_count": 1})
    picker = clicked["picker"]
    assert picker["label"] == "Payment Method" and picker["selected"] == 0 and not picker["truncated"]
    assert [o["label"] for o in picker["options"]][:2] == ["Payment Method", "Bank Account (Checking)"]
    assert picker["options"][0]["disabled"] and picker["options"][-1] == {"label": "Cash", "disabled": True, "group": "Other"}
    assert picker["box"] == [20, 20, 300, 30]
    assert await page.evaluate("document.activeElement.id") == "method" and await page.evaluate("events") == []

    with pytest.raises(WorkerError) as disabled:
        await manager.human_input("owner", sid, {"type": "choose", "picker": picker["id"], "index": 0})
    assert disabled.value.code == "unsupported_control"
    # A choice is good once: a picker is shown again to choose again.
    # Another picker's id changes nothing: this one still stands.
    with pytest.raises(WorkerError) as other:
        await manager.human_input("owner", sid, {"type": "choose", "picker": "not-this-one", "index": 3})
    assert other.value.code == "stale_picker"
    assert "picker" not in await manager.human_input("owner", sid, {"type": "key", "key": "Meta+c"})  # a shortcut
    await manager.human_input("owner", sid, {"type": "choose", "picker": picker["id"], "index": 1})
    assert await page.evaluate("events") == ["input:Bank Account (Checking)", "change:Bank Account (Checking)"]
    await page.evaluate("events.length = 0")
    with pytest.raises(WorkerError) as used:
        await manager.human_input("owner", sid, {"type": "choose", "picker": picker["id"], "index": 3})
    assert used.value.code == "stale_picker"

    # Keys on the focused select show it again (they would open its list); Tab moves on as usual.
    keyed = await manager.human_input("owner", sid, {"type": "key", "key": "ArrowDown"})
    chosen = await manager.human_input("owner", sid, {"type": "choose", "picker": keyed["picker"]["id"], "index": 3})
    assert "picker" not in chosen
    assert await page.evaluate("events") == ["input:Card or Digital Wallet", "change:Card or Digital Wallet"]
    assert "picker" not in await manager.human_input("owner", sid, {"type": "key", "key": "Tab"})

    # A picker whose list changed meanwhile is stale: the person sees it again.
    again = await manager.human_input("owner", sid, {"type": "click", "x": 100, "y": 35, "button": "left", "click_count": 1})
    await page.evaluate("method.add(new Option('Late'))")
    with pytest.raises(WorkerError) as stale:
        await manager.human_input("owner", sid, {"type": "choose", "picker": again["picker"]["id"], "index": 1})
    assert stale.value.code == "stale_picker"

    # A list longer than a picker shows: its first options, and no selection when the selected one is past them.
    await page.evaluate("""() => {
      for (let i = 0; i < 1100; i++) method.add(new Option('Late ' + i));
      method.selectedIndex = 1050;
    }""")
    long = (await manager.human_input("owner", sid, {"type": "click", "x": 100, "y": 35, "button": "left", "click_count": 1}))["picker"]
    assert len(long["options"]) == 1000 and long["truncated"] and long["selected"] == -1

    # A press-and-hold on a select in a frame of another origin (inside its border and padding): its release
    # goes nowhere, and the choice lands in that frame.
    held = await manager.human_input("owner", sid, {"type": "down", "x": 60, "y": 255, "button": "left"})
    assert held["picker"]["label"] == "State" and held["picker"]["box"] == [40, 240, 200, 30]
    await manager.human_input("owner", sid, {"type": "up", "x": 60, "y": 255, "button": "left"})
    await manager.human_input("owner", sid, {"type": "choose", "picker": held["picker"]["id"], "index": 1})
    assert await frame_of(page).evaluate("changes") == ["NJ"]

    # Everything else goes to the page as before: a button, a listbox, a disabled select.
    for x, y in ((450, 95), (100, 90), (450, 35)):
        assert "picker" not in await manager.human_input("owner", sid, {"type": "click", "x": x, "y": y, "button": "left", "click_count": 1})
    assert "other" in await page.evaluate("events")
    assert await page.evaluate("document.querySelector('#list').value") == "Basic"
