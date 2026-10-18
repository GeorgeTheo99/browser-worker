from __future__ import annotations

import asyncio
import json
import time
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

HTML = b'''<!doctype html><title>Research dropdown fixture</title><body>
<label for="native">Vendor</label><select id="native">
<option value="PRIVATE_INTERNAL_VALUE">All</option><option>OpenAI</option>
<option disabled>Disabled</option><optgroup disabled><option>Group disabled</option></optgroup>
<option>Duplicate</option><option>Duplicate</option></select>
<label id="model-label">Model vendor</label>
<div id="combo" role="combobox" aria-labelledby="model-label" aria-haspopup="listbox"
 aria-controls="model-vendor-listbox" aria-expanded="false" tabindex="0"
 onclick="document.querySelector('#model-vendor-listbox').hidden=false;this.setAttribute('aria-expanded','true')">
<input type="text" value="PRIVATE_EDITABLE_VALUE"></div>
<div id="model-vendor-listbox" role="listbox" hidden>
 <div role="option" aria-selected="true">All vendors</div>
 <div role="option" onclick="document.querySelector('#result').textContent='Anthropic selected';this.setAttribute('aria-selected','true')">Anthropic</div>
 <div role="option" aria-disabled="true">Unavailable</div>
 <div role="option"><button onclick="window.forbidden=true">Unsafe button</button></div>
</div>
<div id="result">Initial</div><p>One</p><p>Two</p>
<form><label>Forbidden form<select><option>Secret form</option></select></label></form>
<input role="combobox" aria-haspopup="listbox" aria-controls="editable-list" value="SECRET">
<div role="listbox" id="editable-list"><div role="option">Editable</div></div>
<div role="combobox" aria-haspopup="dialog" aria-label="Unsupported">Dialog</div>
<button role="combobox" aria-haspopup="listbox" aria-controls="button-list">Button</button>
<div role="listbox" id="button-list"><div role="option">Button option</div></div>
<div contenteditable="true"><select><option>Editable select</option></select></div>
<select hidden><option>Hidden</option></select>
<main><a href="https://example.com/">Public link</a><a href="http://127.0.0.1/">Private link</a></main>
</body>'''


@pytest.fixture
async def browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    requests = []

    async def fixture(reader, writer):
        head = await reader.read(65536)
        requests.append(head.split(b"\r\n", 1)[0])
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: " +
                     str(len(HTML)).encode() + b"\r\nConnection: close\r\n\r\n" + HTML)
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
    token = auth._current_caller.set(Caller("research", frozenset({"inspect.read", "inspect.controls"})))
    url = f"http://public.test:{port}/"
    opened = await manager.open("research", url, wait_until="domcontentloaded", timeout_ms=15000)
    sid = opened["session_id"]
    session = await manager._get("research", sid)
    try:
        yield manager, sid, manager._active_page(session), url, requests
    finally:
        auth._current_caller.reset(token)
        await manager.shutdown()
        await artifacts.close()
        http.close()
        await http.wait_closed()


async def call(sid, action, **kwargs):
    result = await mcp_server.browser_inspect(action=action, session_id=sid, **kwargs)
    return result.structured_content


@pytest.mark.browser
async def test_native_and_custom_dropdowns_and_forbidden_actions(browser):
    _, sid, page, url, requests = browser
    token = auth._current_caller.set(Caller("research", frozenset({"inspect.read"})))
    try:
        controls = await call(sid, "controls")
    finally:
        auth._current_caller.reset(token)
    assert controls["status"] == "ok"
    assert len(controls["controls"]) == 2
    native, custom = controls["controls"]
    assert native["role"] == "select" and native["selection"] == ["All"]
    assert custom["role"] == "combobox" and custom["label"] == "Model vendor"
    assert not custom["expanded"] and custom["options"] == []
    assert "PRIVATE" not in json.dumps(controls) and "SECRET" not in json.dumps(controls)
    native_id, custom_id = native["control_id"], custom["control_id"]
    assert len(native_id) <= 128 and native_id != custom_id

    await page.evaluate("""urls => document.querySelector('#native').addEventListener('change', () => {
      Promise.allSettled(urls.map(url => fetch(url))).then(() => window.requestsDone = true);
    }, {once: true})""", [url + "selection", url.replace("public.test", "127.0.0.1") + "private"])
    selected = await call(sid, "select_option", control_id=native_id, option="OpenAI")
    assert selected["control"]["selection"] == ["OpenAI"]
    await page.wait_for_function("window.requestsDone", timeout=3000)
    assert any(b" /selection " in request for request in requests)
    assert not any(b" /private " in request for request in requests)
    for label in ["Missing", "Disabled", "Group disabled", "openai", "PRIVATE_INTERNAL_VALUE"]:
        denied = await call(sid, "select_option", control_id=native_id, option=label)
        assert denied["code"] == "unsupported_control"
    assert (await call(sid, "select_option", control_id=native_id, option="Duplicate"))["code"] == "ambiguous_control"
    assert (await call(sid, "expand_control", control_id=native_id))["code"] == "unsupported_control"
    # Options must be observed, not guessed while the custom dropdown is closed.
    assert (await call(sid, "select_option", control_id=custom_id, option="Anthropic"))["code"] == "unsupported_control"
    expanded = await call(sid, "expand_control", control_id=custom_id)
    assert expanded["control"]["expanded"]
    assert [row["label"] for row in expanded["control"]["options"]] == [
        "All vendors", "Anthropic", "Unavailable", "Unsafe button"]
    for label in ["Unavailable", "Unsafe button"]:
        assert (await call(sid, "select_option", control_id=custom_id, option=label))["code"] == "unsupported_control"
    assert (await call(sid, "select_option", control_id=custom_id, option="Anthropic"))["status"] == "ok"
    assert await page.locator("#result").inner_text() == "Anthropic selected"
    assert await page.locator("#combo input").input_value() == "PRIVATE_EDITABLE_VALUE"
    assert not await page.evaluate("Boolean(window.forbidden)")

    for action, kwargs in [("click", {"selector": "#combo"}),
                           ("type", {"selector": "input", "text": "x"}),
                           ("evaluate", {"script": "1"})]:
        assert (await call(sid, action, **kwargs))["code"] == "capability_denied"
    for kwargs in [{"selector": "button"}, {"text": "x"}, {"script": "1"},
                   {"arg": {}}, {"submit": True}, {"url": "https://example.com"}]:
        assert (await call(sid, "expand_control", control_id=custom_id, **kwargs))["code"] == "invalid_request"
    for kwargs in [{}, {"control_id": "x" * 129}, {"control_id": native_id, "option": "x" * 201}]:
        assert (await call(sid, "select_option", **kwargs))["code"] == "invalid_request"
    assert (await call(sid, "controls", option="not allowed"))["code"] == "invalid_request"
    # A now-disabled or form-reparented handle cannot retain authority.
    await page.locator("#native").evaluate("el => el.disabled = true")
    assert (await call(sid, "select_option", control_id=native_id, option="All"))["code"] == "unsupported_control"
    await page.locator("#combo").evaluate("el => el.setAttribute('aria-controls','different-listbox')")
    assert (await call(sid, "expand_control", control_id=custom_id))["code"] == "stale_control"
    await page.locator("#combo").evaluate("el => el.setAttribute('aria-controls','model-vendor-listbox')")
    await page.locator("#combo").evaluate("el => document.querySelector('form').append(el)")
    assert (await call(sid, "expand_control", control_id=custom_id))["code"] == "unsupported_control"


@pytest.mark.browser
@pytest.mark.parametrize("shape", ["option", "ancestor", "descendant", "combo-ancestor", "combo-descendant"])
async def test_labels_cannot_indirectly_activate_external_submit_controls(browser, shape):
    _, sid, page, _, _ = browser
    await page.evaluate("""shape => {
      document.body.insertAdjacentHTML('beforeend',
        '<form id="external-form"><button id="submitter" type="submit">Submit</button></form>');
      document.querySelector('#external-form').onsubmit = event => {
        event.preventDefault(); window.submitted = true;
      };
      const box = document.querySelector('#model-vendor-listbox');
      box.hidden = false;
      const combo = document.querySelector('#combo');
      combo.setAttribute('aria-expanded', 'true');
      if (shape === 'option') box.innerHTML = '<label role="option" for="submitter">Anthropic</label>';
      if (shape === 'ancestor') box.innerHTML = '<label for="submitter"><span role="option">Anthropic</span></label>';
      if (shape === 'descendant') box.innerHTML = '<div role="option"><label for="submitter">Anthropic</label></div>';
      if (shape === 'combo-ancestor') {
        const label = document.createElement('label'); label.htmlFor = 'submitter';
        combo.before(label); label.append(combo);
      }
      if (shape === 'combo-descendant') combo.innerHTML = '<label for="submitter">Select vendor</label>';
      // Native selects must keep ordinary accessible label wrappers.
      const label = document.createElement('label'); label.textContent = 'Native vendor';
      const native = document.querySelector('#native'); native.before(label); label.append(native);
    }""", shape)
    controls = (await call(sid, "controls"))["controls"]
    native = next(c for c in controls if c["role"] == "select")
    assert (await call(sid, "select_option", control_id=native["control_id"], option="OpenAI"))["status"] == "ok"
    custom = next((c for c in controls if c["role"] == "combobox"), None)
    if shape.startswith("combo-"):
        assert custom is None
    else:
        assert custom is not None
        assert custom["options"] == [{"label": "Anthropic", "disabled": True}]
        denied = await call(sid, "select_option", control_id=custom["control_id"], option="Anthropic")
        assert denied["code"] == "unsupported_control"
    assert not await page.evaluate("Boolean(window.submitted)")


@pytest.mark.browser
async def test_lazy_custom_listbox_and_option_revalidation(browser):
    _, sid, page, _, _ = browser
    await page.evaluate("""() => {
      document.querySelector('#model-vendor-listbox').remove();
      document.querySelector('#combo').onclick = function () {
        const box = document.createElement('div');
        box.id = 'model-vendor-listbox'; box.setAttribute('role', 'listbox');
        box.innerHTML = '<div role="option">Lazy option</div>';
        this.after(box); this.setAttribute('aria-expanded', 'true');
      };
    }""")
    controls = await call(sid, "controls")
    control_id = controls["controls"][1]["control_id"]
    expanded = await call(sid, "expand_control", control_id=control_id)
    assert expanded["control"]["options"] == [{"label": "Lazy option", "disabled": False}]
    await page.locator('#model-vendor-listbox [role=option]').evaluate("el => el.setAttribute('aria-disabled','true')")
    assert (await call(sid, "select_option", control_id=control_id, option="Lazy option"))["code"] == "unsupported_control"
    await page.locator('#model-vendor-listbox [role=option]').evaluate("el => el.outerHTML='<button role=option>Lazy option</button>'")
    assert (await call(sid, "select_option", control_id=control_id, option="Lazy option"))["code"] == "unsupported_control"


@pytest.mark.browser
async def test_stale_cross_session_and_navigation_references(browser):
    manager, sid, page, url, _ = browser
    async def control_id():
        return (await call(sid, "controls"))["controls"][0]["control_id"]

    first = await control_id()
    second = await control_id()
    assert (await call(sid, "select_option", control_id=first, option="All"))["code"] == "stale_control"
    other = await manager.open("research", url, wait_until="domcontentloaded", timeout_ms=15000)
    assert (await call(other["session_id"], "select_option", control_id=second, option="All"))["code"] == "stale_control"
    token = auth._current_caller.set(Caller("other-owner", frozenset({"inspect.read", "inspect.controls"})))
    try:
        assert (await call(sid, "select_option", control_id=second, option="All"))["code"] == "invalid_session"
    finally:
        auth._current_caller.reset(token)
    await manager.close("research", other["session_id"])
    await page.locator("#native").evaluate("el => el.replaceWith(el.cloneNode(true))")
    assert (await call(sid, "select_option", control_id=second, option="All"))["code"] == "stale_control"
    third = await control_id()
    await call(sid, "navigate", url=url)
    assert (await call(sid, "select_option", control_id=third, option="All"))["code"] == "stale_control"
    fourth = await control_id()
    await call(sid, "open_tab", url=url)
    await call(sid, "switch_tab", tab_index=0)
    assert (await call(sid, "select_option", control_id=fourth, option="All"))["code"] == "stale_control"
    await call(sid, "close_tab", tab_index=1)
    fifth = await control_id()
    await page.reload()
    assert (await call(sid, "select_option", control_id=fifth, option="All"))["code"] == "stale_control"
    sixth = await control_id()
    await page.evaluate("history.replaceState({}, '', '#changed')")
    assert (await call(sid, "select_option", control_id=sixth, option="All"))["code"] == "stale_control"
    assert (await call(sid, "close"))["status"] == "closed"
    assert (await call(sid, "controls"))["code"] == "invalid_session"
    expired = await manager.open("research", url, wait_until="domcontentloaded", timeout_ms=15000)
    expired_session = await manager._get("research", expired["session_id"])
    expired_session.created_mono -= 10000
    assert (await call(expired["session_id"], "controls"))["code"] == "invalid_session"


@pytest.mark.browser
async def test_bounds_ambiguity_credentials_and_blocked_access(browser):
    _, sid, page, _, _ = browser
    await page.evaluate("""() => {
      for (let i=0; i<30; i++) {
        const s=document.createElement('select'); s.setAttribute('aria-label', 'x'.repeat(300));
        for (let j=0; j<70; j++) s.add(new Option('Option '+j, 'SECRET_VALUE'));
        document.body.append(s);
      }
    }""")
    result = await call(sid, "controls")
    assert result["truncated"] and len(result["controls"]) == 20
    for control in result["controls"]:
        assert len(control["label"]) <= 200
        assert len(control["options"]) <= 50
        assert all(len(row["label"]) <= 200 for row in control["options"])
    assert "SECRET_VALUE" not in json.dumps(result)
    bounded = result["controls"][-1]
    assert bounded["truncated"]
    assert (await call(sid, "select_option", control_id=bounded["control_id"], option="Option 0"))["code"] == "unsupported_control"
    custom_id = result["controls"][1]["control_id"]
    await page.evaluate("document.querySelector('#model-vendor-listbox').after(document.querySelector('#model-vendor-listbox').cloneNode(true))")
    assert (await call(sid, "expand_control", control_id=custom_id))["code"] == "ambiguous_control"
    await page.evaluate("document.body.insertAdjacentHTML('beforeend','<input type=password value=VERY_SECRET>')")
    assert (await call(sid, "controls"))["controls"] == []
    await page.evaluate("document.title='Access denied'")
    assert (await call(sid, "controls"))["code"] == "blocked_access"
    assert (await call(sid, "extract_text"))["code"] == "blocked_access"


@pytest.mark.browser
async def test_extraction_is_bounded_and_errors_do_not_leak(browser):
    _, sid, page, _, _ = browser
    start = time.monotonic()
    for selector, code in [("#absent_SECRET", "selector_not_found"), ("p", "selector_ambiguous"),
                           ("[SECRET_INVALID", "invalid_selector")]:
        result = await call(sid, "extract_text", selector=selector)
        assert result["code"] == code
        assert "SECRET" not in json.dumps(result)
    assert time.monotonic() - start < 3
    assert (await call(sid, "extract_text", selector="#result"))["text"] == "Initial"
    links = await call(sid, "extract_links", selector="main a[href]")
    assert links["links"] == [{"text": "Public link", "url": "https://example.com/"}]
    assert (await call(sid, "extract_links", selector="main"))["links"] == []
    assert (await call(sid, "extract_links", selector="#absent"))["code"] == "selector_not_found"
    await page.evaluate("document.querySelector('#result').textContent='x'.repeat(10000)")
    text = await call(sid, "extract_text", selector="#result", max_chars=1000)
    assert text["truncated"] and len(text["text"]) == 1000


@pytest.mark.asyncio
async def test_safe_exception_boundary_and_extraction_timeout(monkeypatch):
    from patchright.async_api import TimeoutError as BrowserTimeoutError

    from security import NetworkPolicyError

    async def fail_with(exc):
        raise exc

    for exc, code in [(RuntimeError("SECRET /private/path bearer abc"), "operation_failed"),
                      (WorkerError("SECRET"), "operation_failed"),
                      (NetworkPolicyError("SECRET"), "blocked_access"),
                      (TimeoutError("SECRET"), "operation_timeout")]:
        result = await mcp_server._safe_call(fail_with(exc))
        assert result.structured_content["code"] == code
        assert "SECRET" not in json.dumps(result.structured_content)

    # Exercise the runtime extraction handler without a 30-second driver wait.
    class Locator:
        async def count(self):
            raise BrowserTimeoutError("SECRET browser trace")

    class Page:
        parent_frame = None

        @property
        def main_frame(self):
            return self

        def is_detached(self):
            return False

        async def evaluate(self, script):
            return 'https://public.test/' if script == 'location.href' else False

        def locator(self, _selector):
            return Locator()

    class Session:
        lock = asyncio.Lock()
        touched_mono = 0

    manager = BrowserManager(None)
    async def get(*_args):
        return Session()
    monkeypatch.setattr(manager, "_get", get)
    monkeypatch.setattr(manager, "_active_page", lambda _session: Page())
    for action in ["extract_text", "extract_links"]:
        result = await mcp_server._safe_call(manager.act("owner", "session", action))
        assert result.structured_content["code"] == "extraction_timeout"
        assert "SECRET" not in json.dumps(result.structured_content)


@pytest.mark.browser
async def test_click_and_type_use_the_only_visible_match_and_say_why_otherwise(browser):
    _, sid, page, _, _ = browser
    await page.evaluate('''() => {
      document.body.insertAdjacentHTML('beforeend',
        '<div hidden><button id=add>Add</button><input id=qty></div><button id=add>Add</button><input id=qty>' +
        '<button class=twin>A</button><button class=twin>B</button>');
      window.added = 0;
      for (const b of document.querySelectorAll('#add')) b.onclick = () => window.added++;
    }''')
    token = auth._current_caller.set(Caller("research", frozenset({"inspect.read", "inspect.interact"})))
    try:
        assert (await call(sid, "click", selector="#add"))["status"] == "ok"
        assert await page.evaluate("window.added") == 1
        assert (await call(sid, "type", selector="#qty", text="2"))["status"] == "ok"
        assert await page.locator("#qty").nth(1).input_value() == "2"
        assert (await call(sid, "click", selector=".twin"))["code"] == "selector_ambiguous"
        assert (await call(sid, "click", selector="#nothing", timeout_ms=1000))["code"] == "selector_not_found"
    finally:
        auth._current_caller.reset(token)


CART = '''() => {
  document.body.innerHTML = `<header><nav>${Array.from({length: 120}, (_, i) => `<a href="/n${i}">Menu ${i}</a>`).join('')}</nav></header>
    <main><div class=row><a href="/dp/1">511 Impregnator Sealer, one quart bottle</a><span><button class=del>Delete</button></span></div>
    <div class=row><a href="/dp/2">MB-11 Marble Etch Remover Kit with polishing pads</a><span><button class=del>Delete</button></span></div>
    <input id=qty aria-label=Quantity></main>`;
  window.deleted = [];
  document.querySelectorAll('.del').forEach((b, i) => b.onclick = () => window.deleted.push(i));
}'''


@pytest.mark.browser
async def test_elements_lists_page_content_first_with_refs_text_and_nearby_context(browser):
    _, sid, page, _, _ = browser
    await page.evaluate(CART)
    listed = await call(sid, "elements")
    assert listed["status"] == "ok" and listed["truncated"]
    assert [row["label"] for row in listed["elements"][:5]] == [
        "511 Impregnator Sealer, one quart bottle", "Delete", "MB-11 Marble Etch Remover Kit with polishing pads",
        "Delete", "Quantity"]
    assert listed["elements"][5]["label"] == "Menu 0"
    refs = [row["ref"] for row in listed["elements"]]
    assert refs[:3] == ["e1", "e2", "e3"] and len(set(refs)) == len(refs)
    assert listed["elements"][1]["near"].startswith("511 Impregnator Sealer, one quart bottle Delete")
    assert "near" not in listed["elements"][0]  # a label of its own needs no context
    # Listing again keeps an element's ref, so an uncertain click can't be replayed under a new name,
    # even after a listing with another filter in between.
    again = await call(sid, "elements")
    assert [row["ref"] for row in again["elements"]] == refs
    await call(sid, "elements", text="quantity")
    assert [row["ref"] for row in (await call(sid, "elements", text="mb-11"))["elements"]] == [refs[2], refs[3]]

    scoped = await call(sid, "elements", selector="main .row:nth-of-type(1)")
    assert [row["label"] for row in scoped["elements"]] == ["511 Impregnator Sealer, one quart bottle", "Delete"]
    found = await call(sid, "elements", text="mb-11")
    assert [row["label"] for row in found["elements"]] == ["MB-11 Marble Etch Remover Kit with polishing pads", "Delete"]
    assert not found["truncated"]
    # A failed listing keeps the refs of the last one.
    assert (await call(sid, "elements", selector=".absent"))["code"] == "selector_not_found"
    assert (await call(sid, "elements", selector="##bad"))["code"] == "invalid_selector"

    token = auth._current_caller.set(Caller("research", frozenset({"inspect.read", "inspect.interact"})))
    try:
        delete = next(row["ref"] for row in found["elements"] if row["label"] == "Delete")
        clicked = await call(sid, "click", ref=delete)
        assert clicked["status"] == "ok", clicked
        assert await page.evaluate("window.deleted") == [1]
        # An earlier listing's refs stop working; refs never repeat, so none means another element.
        assert (await call(sid, "click", ref=listed["elements"][1]["ref"]))["code"] == "stale_ref"
        assert (await call(sid, "click", ref="e999999"))["code"] == "stale_ref"
        qty = next(row["ref"] for row in (await call(sid, "elements", text="quantity"))["elements"])
        assert (await call(sid, "type", ref=qty, text="3"))["status"] == "ok"
        assert await page.locator("#qty").input_value() == "3"
        await page.evaluate("document.querySelector('#qty').replaceWith(document.querySelector('#qty').cloneNode())")
        assert (await call(sid, "type", ref=qty, text="4"))["code"] == "stale_ref"
        assert (await call(sid, "click", ref=qty, selector="#qty"))["code"] == "invalid_request"
        assert (await call(sid, "extract_text", ref=qty))["code"] == "invalid_request"
    finally:
        auth._current_caller.reset(token)


@pytest.mark.browser
async def test_nearby_text_never_reads_fields_or_editable_content(browser):
    _, sid, page, _, _ = browser
    await page.evaluate("""() => {
      document.body.innerHTML = [1, 2].map(i => `<div><p>Item ${i}</p><div contenteditable=true>PRIVATE_DRAFT_${i}</div>` +
        `<textarea>PRIVATE_AREA</textarea><button>Delete</button></div>`).join('');
      document.querySelectorAll('textarea').forEach(t => t.value = 'PRIVATE_TYPED');
    }""")
    listed = await call(sid, "elements", text="delete")
    assert [row["near"] for row in listed["elements"] if row["label"] == "Delete"] == ["Item 1 Delete", "Item 2 Delete"]
    assert "PRIVATE" not in json.dumps(listed)
    assert all(row["label"] != "Delete" for row in (await call(sid, "elements", text="private"))["elements"])
    # Nor when the block around the buttons is itself editable, or inside editable content.
    await page.evaluate("""() => {
      document.body.innerHTML = '<div contenteditable=true><div>PRIVATE_ROOT_1 <button>Delete</button></div></div>' +
        '<div contenteditable=true><div>PRIVATE_ROOT_2 <button>Delete</button></div></div>';
    }""")
    assert "PRIVATE" not in json.dumps(await call(sid, "elements"))
    assert (await call(sid, "elements", text="private"))["elements"] == []


@pytest.mark.browser
async def test_click_at_clicks_a_visible_point_and_says_what_it_hit(browser):
    _, sid, page, _, _ = browser
    await page.evaluate('''() => {
      document.body.style.margin = 0;
      document.body.innerHTML = '<button id=go style="position:absolute;left:100px;top:200px;width:80px;height:40px">Go</button>' +
        '<iframe style="position:absolute;left:300px;top:200px;width:100px;height:100px"></iframe>' +
        '<label style="position:absolute;left:500px;top:200px">Upload<input type=file></label>' +
        '<div id=far style="position:absolute;top:3000px">Far</div>';
      window.clicks = 0; document.querySelector('#go').onclick = () => window.clicks++;
    }''')
    token = auth._current_caller.set(Caller("research", frozenset({"inspect.read", "inspect.interact", "inspect.artifact"})))
    try:
        shot = await call(sid, "screenshot", full_page=False)
        assert shot["viewport"] == {"width": 1440, "height": 900}
        clicked = await call(sid, "click_at", x=140, y=220)
        assert clicked["status"] == "ok" and clicked["clicked"] == {"tag": "button", "label": "Go"}
        assert await page.evaluate("window.clicks") == 1
        assert (await call(sid, "click_at", x=350, y=250))["code"] == "point_in_frame"
        assert (await call(sid, "click_at", x=510, y=205))["code"] == "approval_unavailable"
        assert (await call(sid, "click_at", x=1439, y=1000))["code"] == "point_outside"
        assert (await call(sid, "click_at", x=140))["code"] == "invalid_request"
        assert (await call(sid, "click", selector="#go", x=1, y=1))["code"] == "invalid_request"
        # A screenshot of an element scrolls it into view and captures the visible page.
        far = await call(sid, "screenshot", selector="#far")
        assert far["status"] == "ok" and far["viewport"]["height"] == 900
        assert await page.evaluate("scrollY") > 2000
        assert "viewport" not in await call(sid, "screenshot")
    finally:
        auth._current_caller.reset(token)


@pytest.mark.browser
async def test_elements_at_a_point_of_a_tracked_screenshot_gives_the_control_it_showed_there(browser):
    _, sid, page, _, _ = browser
    await page.evaluate('''() => {
      document.body.style.margin = 0;
      document.body.innerHTML = '<div id=list style="position:absolute;left:0;top:0;width:400px;height:100px;overflow:auto">' +
        ['A', 'B'].map(n => `<div style="height:100px"><button id=${n} style="width:200px;height:40px"><span>Add to Cart</span></button></div>`).join('') +
        '</div><div id=host style="position:absolute;left:0;top:200px"></div>' +
        '<button id=under style="position:absolute;left:500px;top:0;width:100px;height:40px">Delete</button>' +
        '<button id=cover aria-label="Add to Cart" style="position:absolute;left:500px;top:0;width:100px;height:40px">Remove all</button>';
      document.querySelector('#host').attachShadow({mode: 'open'}).innerHTML = '<button style="width:200px;height:40px">Shadow buy</button>';
      window.added = [];
      for (const b of document.querySelectorAll('#list button')) b.onclick = () => window.added.push(b.id);
    }''')
    token = auth._current_caller.set(Caller("research", frozenset({"inspect.read", "inspect.interact", "inspect.artifact"})))
    try:
        async def track():
            return (await call(sid, "screenshot", full_page=False, track=True))["capture_id"]

        capture = await track()
        [row] = (await call(sid, "elements", x=50, y=20, capture_id=capture))["elements"]
        assert (row["tag"], row["shown"], row["aria"]) == ("button", "Add to Cart", "")  # the button, not its span
        assert row["box"] == [0, 0, 200, 40]  # where it is on the visible page, in CSS pixels
        assert (await call(sid, "click", ref=row["ref"]))["status"] == "ok"
        assert await page.evaluate("window.added") == ["A"]
        # What it shows and what it tells screen readers come apart.
        [cover] = (await call(sid, "elements", x=550, y=20, capture_id=capture))["elements"]
        assert (cover["shown"], cover["aria"]) == ("Remove all", "Add to Cart")
        # Inside an open shadow root: the shadow button itself.
        [shadow] = (await call(sid, "elements", x=50, y=220, capture_id=capture))["elements"]
        assert shadow["shown"] == "Shadow buy"

        # Since the screenshot: the list scrolled (B's identical button is there now), the shadow button was
        # replaced: neither is what it showed.
        await page.evaluate("document.querySelector('#list').scrollTop = 100")
        assert (await call(sid, "elements", x=50, y=20, capture_id=capture))["code"] == "stale_capture"
        capture = await track()
        await page.evaluate("const r = document.querySelector('#host').shadowRoot; r.innerHTML = r.innerHTML")
        assert (await call(sid, "elements", x=50, y=220, capture_id=capture))["code"] == "stale_capture"
        # A reused node or an uncovered one is what is there now, and says so: the caller checks its words.
        capture = await track()
        await page.evaluate("document.querySelector('#cover').remove(); document.querySelector('#B span').textContent = 'Delete'")
        [under] = (await call(sid, "elements", x=550, y=20, capture_id=capture))["elements"]
        assert under["shown"] == "Delete"
        [reused] = (await call(sid, "elements", x=50, y=20, capture_id=capture))["elements"]
        assert reused["shown"] == "Delete"
        assert (await call(sid, "elements", x=50, y=20, capture_id="x" * 16))["code"] == "stale_capture"
        assert (await call(sid, "elements", x=50, y=20))["code"] == "invalid_request"
        assert (await call(sid, "elements", x=50, y=20, capture_id=capture, text="add"))["code"] == "invalid_request"
        assert (await call(sid, "screenshot", track=True)).get("capture_id") is None  # a full page isn't tracked
        # Something moving elsewhere doesn't matter; the target moving does.
        await page.evaluate("setInterval(() => document.querySelector('#under').style.left = (500 + Math.random() * 50) + 'px', 0)")
        capture = await track()
        assert (await call(sid, "elements", x=50, y=20, capture_id=capture))["elements"][0]["shown"] == "Delete"
        assert (await call(sid, "elements", x=510, y=20, capture_id=capture))["code"] == "stale_capture"
    finally:
        auth._current_caller.reset(token)


@pytest.mark.browser
async def test_elements_at_a_point_reads_web_components_as_they_render(browser):
    _, sid, page, _, _ = browser
    await page.evaluate('''() => {
      document.body.style.margin = 0;
      document.body.innerHTML = '<my-row id=row style="position:absolute;left:0;top:0"><span id=slotted>Add to Cart</span></my-row>' +
        '<div id=labelled style="position:absolute;left:0;top:100px"></div>' +
        '<div id=host role=button style="position:absolute;left:0;top:200px">Add to Cart</div>';
      // A slotted span inside a shadow button that says otherwise.
      document.querySelector('#row').attachShadow({mode: 'open'}).innerHTML =
        '<button aria-label="Delete account" style="width:200px;height:40px"><slot></slot></button>';
      // A shadow button labelled by an element of its own shadow root.
      document.querySelector('#labelled').attachShadow({mode: 'open'}).innerHTML =
        '<span id=name style="display:block">Delete account</span><button aria-labelledby=name style="width:200px;height:40px">Go</button>';
      // A shadow host rendering other words than its (unslotted) light DOM.
      document.querySelector('#host').attachShadow({mode: 'open'}).innerHTML =
        '<b style="display:block;width:200px;height:40px">Remove everything</b>';
    }''')
    token = auth._current_caller.set(Caller("research", frozenset({"inspect.read", "inspect.artifact"})))
    try:
        capture = (await call(sid, "screenshot", full_page=False, track=True))["capture_id"]
        [slotted] = (await call(sid, "elements", x=50, y=20, capture_id=capture))["elements"]
        assert (slotted["tag"], slotted["aria"], slotted["shown"]) == ("button", "Delete account", "Add to Cart")
        [labelled] = (await call(sid, "elements", x=50, y=125, capture_id=capture))["elements"]
        assert (labelled["aria"], labelled["shown"], labelled["aria_unknown"]) == ("Delete account", "Go", False)
        [host] = (await call(sid, "elements", x=50, y=220, capture_id=capture))["elements"]
        assert (host["tag"], host["shown"]) == ("div", "Remove everything")  # what renders, not the hidden light DOM
        await page.evaluate("document.querySelector('#labelled').shadowRoot.querySelector('button').setAttribute('aria-labelledby', 'nowhere')")
        [unknown] = (await call(sid, "elements", x=50, y=125, capture_id=capture))["elements"]
        assert unknown["aria"] == "" and unknown["aria_unknown"]
    finally:
        auth._current_caller.reset(token)


@pytest.mark.browser
async def test_elements_at_a_point_says_only_what_renders_and_what_its_own_scope_labels(browser):
    _, sid, page, _, _ = browser
    await page.evaluate('''() => {
      document.body.style.margin = 0;
      document.body.innerHTML = '<span id=outside>Approve purchase</span>' +
        '<div id=scoped style="position:absolute;left:0;top:0"></div>' +
        '<button id=hidden style="position:absolute;left:0;top:100px;width:200px;height:40px" aria-labelledby=name>' +
        'Delete<span style="visibility:hidden"> nothing</span><span style="opacity:0"> at all</span></button>' +
        '<span id=name hidden>Delete account</span>' +
        '<my-card id=card style="position:absolute;left:0;top:200px"><span slot=gone>Approve</span>Pay</my-card>' +
        '<canvas id=pad width=600 height=100 style="position:absolute;left:0;top:300px" tabindex=0></canvas>' +
        '<a href="/pay" style="position:absolute;left:700px;top:300px"><canvas width=200 height=100></canvas></a>' +
        '<p style="position:absolute;left:0;top:450px;width:300px">Plain words</p>';
      // A shadow button naming an id only the document has: not its label.
      document.querySelector('#scoped').attachShadow({mode: 'open'}).innerHTML =
        '<button aria-labelledby=outside style="width:200px;height:40px">Delete account</button>';
      // Text assigned to a slot that doesn't render.
      document.querySelector('#card').attachShadow({mode: 'open'}).innerHTML =
        '<button style="width:200px;height:40px"><slot name=gone style="display:none"></slot><slot></slot></button>';
    }''')
    token = auth._current_caller.set(Caller("research", frozenset({"inspect.read", "inspect.artifact"})))
    try:
        capture = (await call(sid, "screenshot", full_page=False, track=True))["capture_id"]

        async def at(x, y):
            [row] = (await call(sid, "elements", x=x, y=y, capture_id=capture))["elements"]
            return row

        scoped = await at(50, 20)
        assert (scoped["shown"], scoped["aria"], scoped["aria_unknown"]) == ("Delete account", "", True)
        hidden = await at(50, 120)
        # A hidden label still names it for screen readers; invisible text isn't shown.
        assert (hidden["shown"], hidden["aria"], hidden["control"]) == ("Delete", "Delete account", True)
        assert (await at(50, 220))["shown"] == "Pay"
        # A canvas, or plain content, isn't a control: its ref would click the middle of it.
        assert (await at(30, 350))["control"] is False
        assert (await at(750, 350))["control"] is False  # a link around a canvas: its middle isn't the point
        assert (await at(30, 455))["control"] is False
    finally:
        auth._current_caller.reset(token)


@pytest.mark.browser
async def test_elements_at_a_point_box_covers_everything_the_control_paints(browser):
    _, sid, page, _, _ = browser
    await page.evaluate('''() => {
      document.body.style.margin = 0;
      document.body.innerHTML = '<button id=pay style="position:absolute;left:0;top:0;width:100px;height:40px">' +
        '<span style="position:absolute;left:300px;top:200px;width:80px;height:20px">Pay $1</span></button>';
    }''')
    token = auth._current_caller.set(Caller("research", frozenset({"inspect.read", "inspect.artifact"})))
    try:
        capture = (await call(sid, "screenshot", full_page=False, track=True))["capture_id"]
        [row] = (await call(sid, "elements", x=310, y=210, capture_id=capture))["elements"]
        assert row["tag"] == "button"
        left, top, width, height = row["box"]  # the button and its label positioned outside it
        assert (left, top) == (0, 0) and width >= 380 and height >= 220
        # Too much in it to measure: all of the visible page.
        await page.evaluate("document.querySelector('#pay').insertAdjacentHTML('afterbegin', '<i hidden></i>'.repeat(2001))")
        capture = (await call(sid, "screenshot", full_page=False, track=True))["capture_id"]
        [row] = (await call(sid, "elements", x=310, y=210, capture_id=capture))["elements"]
        assert row["box"] == [0, 0, 1440, 900]
    finally:
        auth._current_caller.reset(token)
