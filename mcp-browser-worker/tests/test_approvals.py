from __future__ import annotations

import asyncio
import json
import time

import pytest
from test_controls import (  # noqa: F401 - shared real Chromium/public-proxy fixture
    browser,
    call,
)

import approvals
import auth
from auth import Caller
from config import ABSOLUTE_TTL_SECONDS

pytestmark = pytest.mark.browser


@pytest.fixture
async def approved(browser):  # noqa: F811 - imported pytest fixture
    manager, sid, page, url, requests = browser
    token = auth._current_caller.set(Caller('research', frozenset({
        'inspect.read', 'inspect.controls', 'inspect.confirmed', 'inspect.artifact',
    })))
    await page.evaluate("""() => {
      document.body.innerHTML = `<form id="form" action="/submit" method="post">
        <label for="entry">Message</label><input id="entry" value="PRIVATE_INPUT_VALUE">
        <textarea id="area">PRIVATE_TEXTAREA_VALUE</textarea>
        <button id="submitter" type="submit">Send</button></form>
        <label id="label" for="submitter">Send via label</label>
        <button id="clicker" onclick="window.clicks++">Count</button>
        <a id="link" href="/destination">Destination</a>
        <a href="http://127.0.0.1/private">Private</a>
        <input id="password" type="password" value="PRIVATE_PASSWORD_VALUE">
        <input id="hidden" hidden value="PRIVATE_HIDDEN_VALUE">`;
      window.clicks = 0; window.submits = 0; window.inputs = 0;
      document.querySelector('#form').onsubmit = event => {event.preventDefault(); window.submits++};
      document.querySelector('#entry').oninput = () => window.inputs++;
    }""")
    try:
        yield manager, sid, page, url, requests
    finally:
        auth._current_caller.reset(token)


async def prepare(sid, operation='click', **kwargs):
    result = await call(sid, 'prepare_action', operation=operation, **kwargs)
    assert result['status'] == 'ok', result
    assert 0 < result['expires_in_seconds'] <= 120
    return result


async def execute(sid, proposal):
    return await call(sid, 'execute_prepared', proposal_id=proposal['proposal_id'])


async def test_discovery_and_prepare_are_readonly_exact_execution_once(approved):
    manager, sid, page, url, _ = approved
    elements = await call(sid, 'elements')
    assert elements['status'] == 'ok'
    assert 'PRIVATE_' not in json.dumps(elements)
    assert not any('127.0.0.1' in row['href'] for row in elements['elements'])
    assert 'hidden' not in [await page.locator(row['selector']).get_attribute('id')
                            for row in elements['elements']]
    for row in elements['elements']:
        assert await page.locator(row['selector']).count() == 1
    entry = next(row for row in elements['elements'] if row['label'] == 'Message' and row['tag'] == 'input')
    proposal = await prepare(sid, 'type', selector=entry['selector'], text='Exact \"message\"\n', clear=True, submit=False)
    assert proposal['preview'] == {
        'action': 'type', 'selector': entry['selector'], 'text': 'Exact \"message\"\n', 'clear': True,
        'submit': False, 'url': url, 'origin': url.rstrip('/'), 'target_label': 'Message',
        'target_tag': 'input', 'target_contenteditable': False, 'destination': url + 'submit',
        'interaction_mode': 'native', 'warning': approvals.NATIVE_WARNING,
    }
    assert await page.locator('#entry').input_value() == 'PRIVATE_INPUT_VALUE'
    assert await page.evaluate('[window.clicks,window.submits,window.inputs]') == [0, 0, 0]
    session = await manager._get('research', sid)
    assert not session.lock.locked()  # no lock held during human review
    assert (await execute(sid, proposal))['status'] == 'ok'
    assert await page.locator('#entry').input_value() == 'Exact "message"'  # input strips newline
    assert await page.evaluate('window.inputs') == 1
    assert (await execute(sid, proposal))['code'] == 'approval_unavailable'
    assert session.approvals.pending is None
    # Append uses frozen text, not the current caller's fields.
    append = await prepare(sid, 'type', selector='#entry', text='!', clear=False)
    assert (await execute(sid, append))['status'] == 'ok'
    assert (await page.locator('#entry').input_value()).endswith('!')
    click = await prepare(sid, selector='#clicker')
    assert await page.evaluate('window.clicks') == 0
    first, duplicate = await asyncio.gather(execute(sid, click), execute(sid, click))
    assert sorted([first.get('status'), duplicate.get('status')]) == ['error', 'ok']
    assert await page.evaluate('window.clicks') == 1


async def test_typing_can_update_unrelated_validation_text_before_approved_submit(approved):
    _, sid, page, _, _ = approved
    await page.evaluate("""() => {
      const hint = document.createElement('p'); hint.id='validation-hint'; hint.textContent='Waiting';
      document.querySelector('#form').append(hint);
      document.querySelector('#entry').oninput = () => { hint.textContent='Ready to send'; };
    }""")
    proposal = await prepare(sid, 'type', selector='#entry', text='Approved text', submit=True)
    assert (await execute(sid, proposal))['status'] == 'ok'
    assert await page.evaluate('window.submits') == 1


async def test_nested_label_destination_is_disclosed_and_session_deadline_is_binding(approved):
    manager, sid, page, url, _ = approved
    await page.locator('#label').evaluate("el => el.innerHTML='<span id=label-child>Send via label</span>'")
    session = await manager._get('research', sid)
    session.created_mono = time.monotonic() - ABSOLUTE_TTL_SECONDS + 30
    proposal = await prepare(sid, selector='#label-child')
    assert proposal['preview']['destination'] == url + 'submit'
    assert proposal['expires_in_seconds'] <= 30
    assert await page.evaluate('window.submits') == 0
    session.created_mono -= 31
    assert (await execute(sid, proposal))['code'] == 'invalid_session'
    if not page.is_closed():
        assert await page.evaluate('window.submits') == 0


async def test_label_submission_requires_explicit_execute_and_discard_revokes(approved):
    _, sid, page, url, _ = approved
    proposal = await prepare(sid, selector='#label')
    assert proposal['preview']['destination'] == url + 'submit'
    assert await page.evaluate('window.submits') == 0
    assert (await call(sid, 'discard_prepared', proposal_id=proposal['proposal_id']))['discarded']
    assert (await execute(sid, proposal))['code'] == 'approval_unavailable'
    assert await page.evaluate('window.submits') == 0
    proposal = await prepare(sid, selector='#label')
    assert (await execute(sid, proposal))['status'] == 'ok'
    assert await page.evaluate('window.submits') == 1
    typed = await prepare(sid, 'type', selector='#entry', text='Approved submission', submit=True)
    assert await page.evaluate('window.submits') == 1
    assert (await execute(sid, typed))['status'] == 'ok'
    assert await page.evaluate('window.submits') == 2


async def test_evaluate_freezes_script_and_json_with_no_prepare_effect(approved):
    manager, sid, page, _, _ = approved
    arg = {'exact': ['sensitive', 2, False, None]}
    script = 'arg => { window.executions = (window.executions || 0) + 1; return arg; }'
    proposal = await prepare(sid, 'evaluate', script=script, arg=arg)
    assert proposal['preview']['script'] == script and proposal['preview']['arg'] == arg
    arg['exact'].append('mutated')
    proposal['preview']['arg']['exact'].append('changed preview')
    assert not await page.evaluate('Boolean(window.executions)')
    result = await execute(sid, proposal)
    assert result['result'] == {'exact': ['sensitive', 2, False, None]}
    assert await page.evaluate('window.executions') == 1
    assert (await execute(sid, proposal))['code'] == 'approval_unavailable'
    session = await manager._get('research', sid)
    assert session.approvals.pending is None
    failing = await prepare(sid, 'evaluate', script='() => { window.executions++; throw new Error("PRIVATE_ERROR"); }')
    failure = await execute(sid, failing)
    assert failure['status'] == 'error' and 'PRIVATE_ERROR' not in json.dumps(failure)
    assert (await execute(sid, failing))['code'] == 'approval_unavailable'
    assert await page.evaluate('window.executions') == 2


@pytest.mark.parametrize('change', [
    "document.querySelector('#clicker').replaceWith(document.querySelector('#clicker').cloneNode(true))",
    "document.querySelector('#clicker').textContent = 'Different action'",
    "document.querySelector('#clicker').setAttribute('aria-label', 'Different label')",
    "document.querySelector('#clicker').setAttribute('type', 'submit')",
    "document.querySelector('#clicker').setAttribute('form', 'form')",
    "document.querySelector('#clicker').hidden = true",
    "history.replaceState({}, '', '#different')",
    "history.pushState({}, '', '#different'); history.replaceState({}, '', '/')",
    "document.open(); document.write('<button id=clicker>Count</button>'); document.close()",
])
async def test_changed_target_url_document_is_stale(approved, change):
    _, sid, page, _, _ = approved
    proposal = await prepare(sid, selector='#clicker')
    await page.evaluate(change)
    assert (await execute(sid, proposal))['code'] == 'approval_stale'
    assert (await execute(sid, proposal))['code'] == 'approval_unavailable'
    assert await page.evaluate('window.clicks || 0') == 0


@pytest.mark.parametrize('change', [
    "document.querySelector('#form').action = '/different'",
    "document.querySelector('#form').method = 'get'",
    "document.querySelector('#submitter').formAction = '/different'",
    "document.querySelector('#submitter').replaceWith(document.querySelector('#submitter').cloneNode(true))",
    "document.querySelector('#label').htmlFor = 'clicker'",
])
async def test_label_form_associations_and_destinations_revalidated(approved, change):
    _, sid, page, _, _ = approved
    proposal = await prepare(sid, selector='#label')
    await page.evaluate(change)
    assert (await execute(sid, proposal))['code'] == 'approval_stale'
    assert await page.evaluate('window.submits') == 0


async def test_href_reload_tab_and_script_document_binding(approved):
    _, sid, page, url, _ = approved
    link = await prepare(sid, selector='#link')
    await page.locator('#link').evaluate("el => el.href = '/different'")
    assert (await execute(sid, link))['code'] == 'approval_stale'
    proposal = await prepare(sid, 'evaluate', script='window.forbidden = true')
    await page.reload()
    assert (await execute(sid, proposal))['code'] == 'approval_stale'
    assert not await page.evaluate('Boolean(window.forbidden)')
    proposal = await prepare(sid, 'evaluate', script='window.forbidden = true')
    await call(sid, 'open_tab', url=url)
    await call(sid, 'switch_tab', tab_index=0)
    assert (await execute(sid, proposal))['code'] == 'approval_stale'


@pytest.mark.parametrize('change', ['reload', 'document_write', 'url'])
async def test_script_document_guard_handles_navigation_after_prevalidation(approved, monkeypatch, change):
    manager, sid, page, _, _ = approved
    proposal = await prepare(sid, 'evaluate', script='window.forbidden = true')
    session = await manager._get('research', sid)
    valid = session.approvals._valid
    checks = 0

    async def race(*args):
        nonlocal checks
        result = await valid(*args)
        checks += 1
        if checks == 2:
            if change == 'reload':
                await page.reload()
            elif change == 'url':
                await page.evaluate("history.replaceState({}, '', '#changed')")
            else:
                await page.evaluate("document.open(); document.write('<p>New document</p>'); document.close()")
        return result

    monkeypatch.setattr(session.approvals, '_valid', race)
    assert (await execute(sid, proposal))['code'] == 'approval_stale'
    assert not await page.evaluate('Boolean(window.forbidden)')
    assert (await execute(sid, proposal))['code'] == 'approval_unavailable'


async def test_ownership_expiry_replacement_close_and_validation(approved):
    manager, sid, page, url, _ = approved
    first = await prepare(sid, selector='#clicker')
    second = await prepare(sid, selector='#clicker')
    assert (await execute(sid, first))['code'] == 'approval_unavailable'
    other = await manager.open('research', url, wait_until='domcontentloaded', timeout_ms=15000)
    assert (await execute(other['session_id'], second))['code'] == 'approval_unavailable'
    token = auth._current_caller.set(Caller('other', frozenset({'inspect.read', 'inspect.confirmed'})))
    try:
        assert (await execute(sid, second))['code'] == 'invalid_session'
        assert (await call(sid, 'discard_prepared', proposal_id=second['proposal_id']))['code'] == 'invalid_session'
    finally:
        auth._current_caller.reset(token)
    session = await manager._get('research', sid)
    session.approvals.pending.expires -= 121
    assert (await execute(sid, second))['code'] == 'approval_expired'
    assert (await execute(sid, second))['code'] == 'approval_unavailable'
    assert await page.evaluate('window.clicks') == 0
    for selector in ['button', '#missing', '#hidden']:
        assert (await call(sid, 'prepare_action', operation='click', selector=selector))['status'] == 'error'
    for kwargs in [{'selector': '#clicker'}, {'text': 'substitute'}, {'script': '1'}, {'arg': {}},
                   {'operation': 'click'}, {'submit': True}, {'timeout_ms': 1000}]:
        assert (await call(sid, 'execute_prepared', proposal_id='x', **kwargs))['code'] == 'invalid_request'
    for kwargs in [{'arg': 'x' * 20001}, {'arg': float('nan')}, {'arg': {'bad': {1}}}]:
        assert (await call(sid, 'prepare_action', operation='evaluate', script='1', **kwargs))['code'] == 'invalid_request'
    pending = await prepare(sid, selector='#clicker')
    stored = session.approvals.pending
    await call(sid, 'close')
    assert session.approvals.pending is None and stored.command == {}
    assert (await execute(sid, pending))['code'] == 'invalid_session'


async def test_access_blocks_discovery_prepare_and_execution(approved):
    _, sid, page, _, _ = approved
    proposal = await prepare(sid, 'evaluate', script='window.forbidden = true')
    await page.evaluate("document.title = 'Access denied'")
    for action in ['elements', 'prepare_action']:
        kwargs = {'operation': 'click', 'selector': '#clicker'} if action == 'prepare_action' else {}
        assert (await call(sid, action, **kwargs))['code'] == 'blocked_access'
    # Failed replacement has already discarded the previous proposal.
    assert (await execute(sid, proposal))['code'] == 'approval_unavailable'
    await page.evaluate("document.title = 'Fixture'")
    proposal = await prepare(sid, 'evaluate', script='window.forbidden = true')
    await page.evaluate("document.title = 'Access denied'")
    assert (await execute(sid, proposal))['code'] == 'blocked_access'
    assert (await execute(sid, proposal))['code'] == 'approval_unavailable'
    assert not await page.evaluate('Boolean(window.forbidden)')


async def test_cancellation_consumes_and_cleans_session(approved, monkeypatch):
    manager, sid, page, _, _ = approved
    proposal = await prepare(sid, 'evaluate', script='window.forbidden = true')
    session = await manager._get('research', sid)
    stored = session.approvals.pending
    started = asyncio.Event()

    async def interrupted(*_args):
        started.set()
        await asyncio.Future()

    monkeypatch.setattr(session.approvals, '_valid', interrupted)
    task = asyncio.create_task(execute(sid, proposal))
    await started.wait()
    assert session.approvals.pending is None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stored.command == {} and sid not in manager._sessions and page.is_closed()


async def test_execution_timeout_is_consumed_even_after_an_effect(approved):
    manager, sid, page, _, _ = approved
    proposal = await prepare(sid, 'evaluate', timeout_ms=1000,
                             script='() => { window.started = true; return new Promise(() => {}); }')
    result = await execute(sid, proposal)
    assert result['code'] == 'operation_timeout'
    assert await page.evaluate('window.started')
    assert (await execute(sid, proposal))['code'] == 'approval_unavailable'
    assert (await manager._get('research', sid)).approvals.pending is None


async def test_discovery_and_target_data_are_bounded(approved):
    _, sid, page, _, _ = approved
    await page.evaluate("""() => {
      for (let i = 0; i < 600; i++) {
        const button = document.createElement('button');
        button.textContent = 'x'.repeat(600); button.setAttribute('role', 'r'.repeat(200));
        document.body.append(button);
      }
    }""")
    result = await call(sid, 'elements', limit=200)
    assert result['truncated'] and len(result['elements']) == 100
    assert all(len(row['label']) <= 500 and len(row['role']) <= 100 and
               len(row['type']) <= 100 and len(row['selector']) <= 2000 and
               len(row['href']) <= 8192 for row in result['elements'])
    assert (await call(sid, 'prepare_action', operation='click',
                       selector='body > button:last-of-type'))['code'] == 'approval_unavailable'


async def test_overlay_wait_cannot_authorize_changed_link(approved):
    _, sid, page, _, requests = approved
    proposal = await prepare(sid, selector='#link', timeout_ms=3000)
    await page.evaluate("""() => {
      window.linkClicks = 0;
      document.querySelector('#link').addEventListener('click', () => window.linkClicks++);
      const overlay = document.createElement('div');
      overlay.style = 'position:fixed;inset:0;z-index:9999;background:white';
      document.body.append(overlay);
      setTimeout(() => {
        const link = document.querySelector('#link');
        link.href = '/NOT-APPROVED'; link.textContent = 'Not approved'; overlay.remove();
      }, 700);
    }""")
    assert (await execute(sid, proposal))['code'] == 'approval_stale'
    assert await page.evaluate('window.linkClicks') == 0
    assert not any(b' /NOT-APPROVED ' in request for request in requests)
    assert (await execute(sid, proposal))['code'] == 'approval_unavailable'


@pytest.mark.parametrize('operation,change', [
    ('click', "document.querySelector('#clicker').textContent = 'Changed'"),
    ('type', "document.querySelector('#entry').readOnly = true"),
    ('click', "const overlay = document.createElement('div'); overlay.style='position:fixed;inset:0;z-index:9999'; document.body.append(overlay)"),
])
async def test_guard_rechecks_target_and_hit_test_after_trial(approved, monkeypatch, operation, change):
    manager, sid, page, _, _ = approved
    params = {'selector': '#clicker'} if operation == 'click' else {'selector': '#entry', 'text': 'Approved'}
    proposal = await prepare(sid, operation, **params)
    session = await manager._get('research', sid)
    element = session.approvals.pending.element
    click = element.click

    async def changed_after_trial(**kwargs):
        assert kwargs['trial'] is True
        await click(**kwargs)
        await page.evaluate('() => { ' + change + '; }')

    monkeypatch.setattr(element, 'click', changed_after_trial)
    assert (await execute(sid, proposal))['code'] == 'approval_stale'
    assert await page.evaluate('window.clicks') == 0
    assert await page.locator('#entry').input_value() == 'PRIVATE_INPUT_VALUE'


async def history_round_trip(page, kind, *, isolated_context=False):
    await page.evaluate("""async kind => {
      const original = location.href;
      if (kind === 'traverse') {
        const back = new Promise(resolve => addEventListener('popstate', resolve, {once: true}));
        history.back(); await back;
        const forward = new Promise(resolve => addEventListener('popstate', resolve, {once: true}));
        history.forward(); await forward;
      } else if (kind === 'hash') {
        const changed = new Promise(resolve => addEventListener('hashchange', resolve, {once: true}));
        location.hash = 'different'; await changed;
        const restored = new Promise(resolve => addEventListener('hashchange', resolve, {once: true}));
        location.href = original; await restored;
      } else {
        history[kind]({}, '', '#different');
        history[kind]({}, '', original);
      }
      if (location.href !== original) throw new Error('fixture did not restore URL');
    }""", kind, isolated_context=isolated_context)


@pytest.mark.parametrize('kind', ['pushState', 'replaceState', 'traverse', 'hash'])
async def test_overlay_history_change_and_restore_revokes_approval(approved, monkeypatch, kind):
    manager, sid, page, url, _ = approved
    if kind == 'traverse':
        await page.evaluate("url => { history.pushState({}, '', '#previous'); history.pushState({}, '', url); }",
                            url, isolated_context=False)
    if kind == 'hash':
        await page.evaluate("() => { location.hash = 'original'; }", isolated_context=False)
    proposal = await prepare(sid, selector='#clicker', timeout_ms=5000)
    session = await manager._get('research', sid)
    stored = session.approvals.pending
    await page.evaluate("""() => {
      const overlay = document.createElement('div'); overlay.id = 'overlay';
      overlay.style = 'position:fixed;inset:0;z-index:9999;background:white'; document.body.append(overlay);
    }""")
    entered_trial = asyncio.Event()
    original = stored.element.click

    async def trial(**kwargs):
        assert kwargs['trial'] is True
        entered_trial.set()
        await original(**kwargs)

    monkeypatch.setattr(stored.element, 'click', trial)
    task = asyncio.create_task(execute(sid, proposal))
    try:
        await asyncio.wait_for(entered_trial.wait(), 3)
        await asyncio.sleep(0.15)  # real overlay-delayed actionability, not a mocked trial
        await history_round_trip(page, kind)
        assert page.url == stored.url and session.approvals.epoch > stored.epoch
        await page.locator('#overlay').evaluate('el => el.remove()')
        assert (await task)['code'] == 'approval_stale'
        assert await page.evaluate('window.clicks') == 0
        assert (await execute(sid, proposal))['code'] == 'approval_unavailable'
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize('operation', ['click', 'type', 'evaluate'])
@pytest.mark.parametrize('kind', ['pushState', 'replaceState', 'traverse', 'hash'])
@pytest.mark.parametrize('isolated_context', [False, True])
async def test_final_browser_preflight_revokes_restored_history(
        approved, monkeypatch, operation, kind, isolated_context):
    manager, sid, page, url, _ = approved
    if kind == 'traverse':
        await page.evaluate("url => { history.pushState({}, '', '#previous'); history.pushState({}, '', url); }",
                            url, isolated_context=False)
    if kind == 'hash':
        await page.evaluate("() => { location.hash = 'original'; }", isolated_context=False)
    params = {'selector': '#clicker'} if operation == 'click' else (
        {'selector': '#entry', 'text': 'Not approved', 'submit': True} if operation == 'type' else
        {'script': 'window.forbidden = true'})
    proposal = await prepare(sid, operation, **params)
    session = await manager._get('research', sid)
    stored = session.approvals.pending
    handle = stored.document if operation == 'evaluate' else stored.snapshot
    original = handle.evaluate

    async def changed(expression, arg=None):
        if arg and 'deadline' in arg:
            # This runs after *all* Python validation, including post-trial.
            await history_round_trip(page, kind, isolated_context=isolated_context)
            assert page.url == stored.url
        return await original(expression, arg)

    monkeypatch.setattr(handle, 'evaluate', changed)
    assert (await execute(sid, proposal))['code'] == 'approval_stale'
    assert await page.evaluate('window.clicks') == 0
    assert await page.evaluate('window.submits') == 0
    assert await page.locator('#entry').input_value() == 'PRIVATE_INPUT_VALUE'
    assert not await page.evaluate('Boolean(window.forbidden)')


@pytest.mark.parametrize('operation', ['click', 'type', 'evaluate'])
async def test_poisoned_global_alias_before_prepare_cannot_forge_navigation_authority(approved, monkeypatch, operation):
    manager, sid, page, _, _ = approved
    assert await page.evaluate("function () { const poison = {__browserWorkerNavigationGeneration: Object.freeze(() => 0n)}; this.globalThis = poison; return this.globalThis === poison; }", isolated_context=False)
    params = {'selector': '#clicker'} if operation == 'click' else (
        {'selector': '#entry', 'text': 'Not approved', 'submit': True} if operation == 'type' else
        {'script': 'window.forbidden = true'})
    proposal = await prepare(sid, operation, **params)
    session = await manager._get('research', sid)
    stored = session.approvals.pending
    handle = stored.document if operation == 'evaluate' else stored.snapshot
    original = handle.evaluate

    async def changed(expression, arg=None):
        if arg and 'deadline' in arg:
            await history_round_trip(page, 'pushState')
        return await original(expression, arg)

    monkeypatch.setattr(handle, 'evaluate', changed)
    assert (await execute(sid, proposal))['code'] == 'approval_stale'
    assert await page.evaluate('window.clicks') == 0
    assert await page.evaluate('window.submits') == 0
    assert await page.locator('#entry').input_value() == 'PRIVATE_INPUT_VALUE'
    assert not await page.evaluate('Boolean(window.forbidden)')


@pytest.mark.parametrize('method', ['pushState', 'replaceState'])
async def test_navigation_authority_cannot_be_reset_or_observer_suppressed(approved, monkeypatch, method):
    manager, sid, page, _, _ = approved
    proposal = await prepare(sid, selector='#clicker')
    session = await manager._get('research', sid)
    handle = session.approvals.pending.snapshot
    original = handle.evaluate

    async def changed(expression, arg=None):
        if arg and 'deadline' in arg:
            assert await page.evaluate("""method => {
              const key = '__browserWorkerNavigationGeneration', read = globalThis[key], before = read();
              navigation.addEventListener('currententrychange', e => e.stopImmediatePropagation(), true);
              navigation.oncurrententrychange = e => e.stopImmediatePropagation();
              const replaced = Reflect.set(globalThis, key, () => before);
              const removed = Reflect.deleteProperty(globalThis, key);
              // No URL difference even transiently; state-only history changes revoke too.
              History.prototype[method].call(history, {changed: true}, '', location.href);
              return !replaced && !removed && Object.isFrozen(read) && read() > before;
            }""", method, isolated_context=False)
        return await original(expression, arg)

    monkeypatch.setattr(handle, 'evaluate', changed)
    assert (await execute(sid, proposal))['code'] == 'approval_stale'
    assert await page.evaluate('window.clicks') == 0


@pytest.mark.parametrize('deadline_kind', ['proposal', 'session'])
async def test_overlay_wait_cannot_outlive_approval_or_session(approved, monkeypatch, deadline_kind):
    manager, sid, page, _, _ = approved
    if deadline_kind == 'proposal':
        monkeypatch.setattr(approvals, 'APPROVAL_TTL_SECONDS', 0.5)
    else:
        session = await manager._get('research', sid)
        session.created_mono = time.monotonic() - ABSOLUTE_TTL_SECONDS + 0.5
    proposal = await call(sid, 'prepare_action', operation='click', selector='#clicker', timeout_ms=3000)
    assert proposal['status'] == 'ok'
    await page.evaluate("""() => {
      const overlay = document.createElement('div');
      overlay.style = 'position:fixed;inset:0;z-index:9999;background:white';
      document.body.append(overlay);
      setTimeout(() => { overlay.remove(); window.overlayRemoved = true; }, 900);
    }""")
    result = await execute(sid, proposal)
    assert result['code'] in {'operation_timeout', 'approval_expired'}
    await page.wait_for_function('window.overlayRemoved')
    assert await page.evaluate('window.clicks') == 0


@pytest.mark.parametrize('operation', ['click', 'type', 'evaluate'])
async def test_final_browser_preflight_rejects_queued_delay(approved, monkeypatch, operation):
    manager, sid, page, _, _ = approved
    monkeypatch.setattr(approvals, 'APPROVAL_TTL_SECONDS', 0.6)
    params = {'selector': '#clicker'} if operation == 'click' else (
        {'selector': '#entry', 'text': 'Not approved'} if operation == 'type' else
        {'script': 'window.forbidden = true'})
    proposal = await call(sid, 'prepare_action', operation=operation, **params)
    assert proposal['status'] == 'ok'
    session = await manager._get('research', sid)
    stored = session.approvals.pending
    # Simulate transport/renderer queuing *after* preflight/trial. The browser
    # callback still runs, so Python cancellation alone cannot make this pass.
    handle = stored.document if operation == 'evaluate' else stored.snapshot
    original = handle.evaluate

    async def delayed(expression, arg=None):
        if arg and ('deadline' in arg):
            expression = expression.replace('=> {', '=> { const end = performance.now() + 800; while (performance.now() < end) {}', 1)
        return await original(expression, arg)

    monkeypatch.setattr(handle, 'evaluate', delayed)
    result = await execute(sid, proposal)
    assert result['status'] == 'error'
    assert await page.evaluate('window.clicks') == 0
    assert await page.locator('#entry').input_value() == 'PRIVATE_INPUT_VALUE'
    assert not await page.evaluate('Boolean(window.forbidden)')


@pytest.mark.parametrize('external', [False, True])
@pytest.mark.parametrize('change', [
    "s.formAction = '/NOT-APPROVED'",
    "s.formMethod = 'get'",
    "s.formTarget = '_blank'",
    "s.formEnctype = 'text/plain'",
    "s.formNoValidate = true",
    "s.replaceWith(s.cloneNode(true))",
    "const other = document.createElement('button'); other.type='submit'; other.setAttribute('form','form'); document.body.prepend(other)",
])
async def test_implicit_default_submitter_is_disclosed_and_bound(approved, external, change):
    _, sid, page, url, _ = approved
    await page.evaluate("""external => {
      const s = document.querySelector('#submitter');
      s.formAction = '/approved-override'; s.formMethod = 'post';
      if (external) { s.setAttribute('form', 'form'); document.body.prepend(s); }
    }""", external)
    proposal = await prepare(sid, 'type', selector='#entry', text='Approved', submit=True)
    assert url + 'approved-override' in proposal['preview']['destination'].splitlines()
    assert proposal['preview']['default_submitter'] == {
        'label': 'Send via label', 'tag': 'button', 'action': url + 'approved-override',
        'method': 'post', 'target': '', 'enctype': 'application/x-www-form-urlencoded', 'novalidate': False,
    }
    await page.evaluate("() => { const s = document.querySelector('#submitter'); " + change + '; }')
    assert (await execute(sid, proposal))['code'] == 'approval_stale'
    assert await page.evaluate('window.submits') == 0
    assert await page.locator('#entry').input_value() == 'PRIVATE_INPUT_VALUE'


@pytest.mark.parametrize('external', [False, True])
async def test_bound_default_submitter_is_the_one_activated(approved, external):
    _, sid, page, url, _ = approved
    await page.evaluate("""external => {
      const s = document.querySelector('#submitter');
      s.formAction = '/approved-override';
      if (external) { s.setAttribute('form', 'form'); s.hidden = true; document.body.prepend(s); }
      document.querySelector('#form').addEventListener('submit', e => {
        window.actualSubmitter = {id: e.submitter.id, action: e.submitter.formAction};
      });
    }""", external)
    proposal = await prepare(sid, 'type', selector='#entry', text='Approved', submit=True)
    assert proposal['preview']['default_submitter']['action'] == url + 'approved-override'
    assert (await execute(sid, proposal))['status'] == 'ok'
    assert await page.evaluate('window.actualSubmitter') == {'id': 'submitter', 'action': url + 'approved-override'}
    assert await page.evaluate('window.submits') == 1


@pytest.mark.parametrize('handler', ['focus', 'input'])
async def test_type_rechecks_after_handlers_before_further_dispatch(approved, handler):
    _, sid, page, _, _ = approved
    await page.evaluate("""handler => {
      document.querySelector('#entry').addEventListener(handler, () => {
        document.querySelector('#submitter').formAction = '/NOT-APPROVED';
      });
    }""", handler)
    proposal = await prepare(sid, 'type', selector='#entry', text='Approved', submit=True)
    assert (await execute(sid, proposal))['code'] == 'approval_stale'
    assert await page.evaluate('window.submits') == 0
    # Native fill includes focus and input; guards cannot interleave that call.
    assert await page.locator('#entry').input_value() == 'Approved'
    assert (await execute(sid, proposal))['code'] == 'approval_unavailable'


@pytest.mark.parametrize('handler', ['focus', 'input'])
async def test_type_deadline_is_rechecked_after_handlers(approved, monkeypatch, handler):
    _, sid, page, _, _ = approved
    monkeypatch.setattr(approvals, 'APPROVAL_TTL_SECONDS', 0.6)
    await page.evaluate("""handler => {
      document.querySelector('#entry').addEventListener(handler, () => {
        const until = performance.now() + 800;
        while (performance.now() < until) {}
      });
    }""", handler)
    proposal = await call(sid, 'prepare_action', operation='type', selector='#entry', text='Approved', submit=True)
    assert proposal['status'] == 'ok'
    assert (await execute(sid, proposal))['status'] == 'error'
    assert await page.evaluate('window.submits') == 0
    # Timeout can interrupt fill before or after input, but Enter must not run.
    assert await page.locator('#entry').input_value() in {'Approved', 'PRIVATE_INPUT_VALUE'}


async def test_confirmed_actions_use_native_trusted_pointer_keyboard_and_activation(approved):
    _, sid, page, _, _ = approved
    await page.evaluate("""() => {
      window.events = [];
      for (const event of ['pointerdown','keydown','click','input'])
        document.addEventListener(event, e => window.events.push([e.type, e.isTrusted, navigator.userActivation.isActive]));
    }""")
    proposal = await prepare(sid, selector='#clicker')
    assert (await execute(sid, proposal))['status'] == 'ok'
    proposal = await prepare(sid, 'type', selector='#entry', text='Approved', submit=True)
    assert (await execute(sid, proposal))['status'] == 'ok'
    assert await page.evaluate('window.events') == [
        ['pointerdown', True, True], ['click', True, True], ['input', True, True],
        ['keydown', True, True], ['click', True, True],
    ]
    assert await page.evaluate('window.submits') == 1


async def test_confirmed_actions_reject_frame_selectors(approved):
    _, sid, page, _, _ = approved
    await page.evaluate("""() => {
      const frame = document.createElement('iframe');
      frame.srcdoc = '<button onclick="window.forbidden=true">Frame action</button>';
      document.body.append(frame);
    }""")
    selector = 'iframe >> internal:control=enter-frame >> button'
    await page.locator(selector).wait_for()
    assert (await call(sid, 'prepare_action', operation='click', selector=selector))['code'] == 'approval_unavailable'
    assert not await page.frames[1].evaluate('Boolean(window.forbidden)')


@pytest.mark.parametrize('setup,selector,operation,params', [

    ("document.querySelector('#entry').readOnly = true", '#entry', 'type', {}),
    ("document.querySelector('#entry').type = 'file'", '#entry', 'click', {}),
])
async def test_unsupported_confirmed_controls_fail_closed(approved, setup, selector, operation, params):
    _, sid, page, _, _ = approved
    if setup:
        await page.evaluate('() => { ' + setup + '; }')
    if operation == 'type':
        params = {**params, 'text': 'Approved'}
    assert (await call(sid, 'prepare_action', operation=operation, selector=selector, **params))['code'] == 'approval_unavailable'
    assert await page.evaluate('window.submits') == 0


async def test_approved_execution_keeps_egress_and_artifact_permissions(approved):
    _, sid, page, url, requests = approved
    script = """async urls => {
      await Promise.allSettled(urls.map(url => fetch(url))); return 'done';
    }"""
    proposal = await prepare(sid, 'evaluate', script=script,
                             arg=[url + 'approved', url.replace('public.test', '127.0.0.1') + 'private'])
    assert not any(b' /approved ' in request for request in requests)
    assert (await execute(sid, proposal))['result'] == 'done'
    assert any(b' /approved ' in request for request in requests)
    assert not any(b' /private ' in request for request in requests)
    for action in ['screenshot', 'export_pdf']:
        assert (await call(sid, action))['status'] == 'ok'
    for action, kwargs in [('click', {'selector': '#clicker'}),
                           ('type', {'selector': '#entry', 'text': 'unapproved'}),
                           ('evaluate', {'script': 'window.forbidden = true'})]:
        assert (await call(sid, action, **kwargs))['code'] == 'capability_denied'
    assert not await page.evaluate('Boolean(window.forbidden)')
