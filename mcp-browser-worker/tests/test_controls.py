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
