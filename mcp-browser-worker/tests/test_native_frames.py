# ruff: noqa: F811 - imported shared pytest fixtures
from __future__ import annotations

import asyncio
import time

import pytest
from patchright.async_api import Locator
from test_approvals import approved, execute, prepare  # noqa: F401
from test_controls import browser, call  # noqa: F401

import auth
from auth import Caller

pytestmark = pytest.mark.browser


@pytest.mark.parametrize('setup,selector,submits', [
    ("document.querySelector('#submitter').remove()", '#entry', 1),
    ("document.querySelector('#submitter').disabled = true", '#entry', 0),
    ("document.querySelector('#submitter').outerHTML = '<input type=image form=form>'", '#entry', 1),
    ('', '#area', 0),
])
async def test_enter_uses_browser_semantics_not_a_required_button(approved, setup, selector, submits):
    _, sid, page, _, _ = approved
    await page.evaluate('() => { ' + setup + '; }')
    await page.evaluate('''selector => {
      window.keys = [];
      document.querySelector(selector).onkeydown = e => window.keys.push([e.key, e.isTrusted]);
    }''', selector)
    proposal = await prepare(sid, 'type', selector=selector, text='Approved', submit=True)
    assert (await execute(sid, proposal))['status'] == 'ok'
    assert await page.evaluate('window.keys') == [['Enter', True]]
    assert await page.evaluate('window.submits') == submits
    assert await page.locator(selector).input_value() == ('Approved\n' if selector == '#area' else 'Approved')


@pytest.mark.parametrize('markup,selector,text', [
    ('<div id=editor contenteditable=true><b>Private old value</b></div>', '#editor', 'New value'),
    ('<textarea id=editor>Private old value</textarea>', '#editor', 'New\nvalue'),
    ('<input id=editor type=number>', '#editor', '42'),
    ('<input id=editor type=date>', '#editor', '2026-01-02'),
    ('<div id=host></div>', '#host input', 'Shadow value'),
])
async def test_native_editors_shadow_inputs_and_custom_enter(approved, markup, selector, text):
    _, sid, page, _, _ = approved
    await page.evaluate('''markup => {
      document.body.innerHTML = markup;
      if (document.querySelector('#host')) document.querySelector('#host').attachShadow({mode:'open'}).innerHTML = '<input>';
      window.keys = [];
      document.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); window.keys.push(e.isTrusted); } });
    }''', markup)
    proposal = await prepare(sid, 'type', selector=selector, text=text, submit=True)
    assert 'Private' not in proposal['preview'].get('target_label', '')
    assert proposal['preview']['target_contenteditable'] is ('contenteditable=true' in markup)
    assert (await execute(sid, proposal))['status'] == 'ok'
    assert await page.evaluate('window.keys') == [True]
    assert await page.locator(selector).evaluate('el => el.isContentEditable ? el.innerText : el.value') == text


async def test_sequential_typing_respects_selection_and_emits_trusted_keys(approved):
    _, sid, page, _, _ = approved
    await page.locator('#entry').fill('abcd')
    await page.locator('#entry').evaluate('el => { el.setSelectionRange(1,3); window.keys=[]; el.onkeydown=e=>window.keys.push([e.key,e.isTrusted]); }')
    proposal = await prepare(sid, 'type', selector='#entry', text='XY', clear=False)
    assert (await execute(sid, proposal))['status'] == 'ok'
    assert await page.locator('#entry').input_value() == 'aXYd'
    assert await page.evaluate('window.keys') == [['X', True], ['Y', True]]


async def add_frame(parent, url, name='child'):
    await parent.evaluate('''({url,name}) => new Promise(resolve => {
      const f = document.createElement('iframe'); f.name = name; f.src = url;
      f.style = 'width:700px;height:400px'; f.onload = () => resolve(); document.body.append(f);
    })''', {'url': url, 'name': name})
    frame = next(f for f in parent.child_frames if f.name == name)
    await frame.evaluate('''() => {
      document.body.innerHTML = '<p>Frame evidence</p><button id=go>Go</button><input id=entry><a href="/evidence">Evidence</a>';
      window.clicks=0; window.keys=[];
      document.querySelector('#go').onclick=e=>{window.clicks++;window.trusted=e.isTrusted;};
      document.querySelector('#entry').onkeydown=e=>window.keys.push(e.key);
    }''')
    return frame


@pytest.fixture
async def framed(approved):
    manager, sid, page, url, requests = approved
    child = await add_frame(page.main_frame, url + 'frame')
    nested = await add_frame(child, url.replace('public.test', 'other-public.test') + 'nested', 'nested')
    yield manager, sid, page, child, nested, url, requests


async def frame_rows(sid):
    result = await call(sid, 'frames')
    assert result['status'] == 'ok', result
    return result['frames']


async def test_same_nested_cross_origin_discovery_reads_and_previews(framed):
    _, sid, page, child, nested, url, _ = framed
    rows = await frame_rows(sid)
    assert len(rows) == 3
    top, child_row, nested_row = rows
    assert top['parent_frame_id'] is None
    assert child_row['parent_frame_id'] == top['frame_id']
    assert nested_row['parent_frame_id'] == child_row['frame_id']
    assert nested_row['origin'] == url.replace('public.test', 'other-public.test').rstrip('/')
    for frame, row in [(child, child_row), (nested, nested_row)]:
        fid = row['frame_id']
        assert (await call(sid, 'elements', frame_id=fid))['elements']
        evidence = await call(sid, 'extract_text', frame_id=fid, selector='p')
        assert evidence['text'] == 'Frame evidence' and evidence['url'] == frame.url
        assert (await call(sid, 'extract_links', frame_id=fid))['links'][0]['url'].endswith('/evidence')
        assert (await call(sid, 'wait', frame_id=fid, selector='#go'))['status'] == 'ok'
        assert (await call(sid, 'wait', frame_id=fid, url_contains=frame.url))['status'] == 'ok'
        assert (await call(sid, 'wait', frame_id=fid))['status'] == 'ok'
        proposal = await prepare(sid, frame_id=fid, selector='#go')
        preview = proposal['preview']
        assert preview['url'] == url and preview['origin'] == url.rstrip('/')
        assert preview['frame_id'] == fid and preview['frame_url'] == frame.url and preview['frame_origin'] == row['origin']
        assert preview['frame_ancestry'] == [
            {'url': r['url'], 'origin': r['origin']} for r in rows[:rows.index(row)]
        ]
        assert (await execute(sid, proposal))['status'] == 'ok'
        assert await frame.evaluate('[window.clicks,window.trusted]') == [1, True]
        proposal = await prepare(sid, 'type', frame_id=fid, selector='#entry', text='Native frame', submit=True)
        assert (await execute(sid, proposal))['status'] == 'ok'
        assert await frame.evaluate('window.keys') == ['Enter']
        proposal = await prepare(sid, 'evaluate', frame_id=fid, script='arg => {window.evaluated=true; return {url:location.href,arg};}', arg={'n': 1})
        assert (await execute(sid, proposal))['result'] == {'url': frame.url, 'arg': {'n': 1}}
    assert not await page.evaluate('Boolean(window.evaluated)')


@pytest.mark.parametrize('target,change', [
    ('selected', 'reload'), ('selected', 'history'), ('selected', 'document'), ('selected', 'replacement'),
    ('parent', 'reload'), ('parent', 'history'), ('parent', 'document'), ('top', 'history'),
])
async def test_frame_and_ancestor_changes_revoke_refs_and_approvals(framed, target, change):
    _, sid, page, child, nested, _, _ = framed
    row = (await frame_rows(sid))[-1]
    proposal = await prepare(sid, 'evaluate', frame_id=row['frame_id'], script='window.forbidden=true')
    frame = {'selected': nested, 'parent': child, 'top': page.main_frame}[target]
    if change == 'reload':
        await frame.goto(frame.url)
    elif change == 'history':
        await frame.evaluate("() => { history.pushState({}, '', '#changed'); history.replaceState({}, '', location.pathname); }")
    elif change == 'document':
        await frame.evaluate("() => { document.open(); document.write('<p>Replacement</p>'); document.close(); }")
    else:
        await child.evaluate("() => { const f=document.querySelector('iframe'); f.replaceWith(f.cloneNode(true)); }")
    assert (await call(sid, 'extract_text', frame_id=row['frame_id']))['code'] == 'approval_stale'
    assert (await execute(sid, proposal))['code'] == 'approval_stale'
    assert (await execute(sid, proposal))['code'] == 'approval_unavailable'
    if not nested.is_detached():
        assert not await nested.evaluate('Boolean(window.forbidden)')


async def test_frame_refs_not_transferable_to_tabs_sessions_or_rediscovery(framed):
    manager, sid, _, _, _, url, _ = framed
    fid = (await frame_rows(sid))[-1]['frame_id']
    other = await manager.open('research', url, wait_until='domcontentloaded', timeout_ms=15000)
    assert (await call(other['session_id'], 'elements', frame_id=fid))['code'] == 'approval_stale'
    await call(sid, 'open_tab', url=url)
    assert (await call(sid, 'elements', frame_id=fid))['code'] == 'approval_stale'
    await call(sid, 'switch_tab', tab_index=0)
    await frame_rows(sid)
    assert (await call(sid, 'elements', frame_id=fid))['code'] == 'approval_stale'


@pytest.mark.parametrize('target', ['selected', 'parent', 'top'])
async def test_frame_access_checks_all_ancestors(framed, target):
    _, sid, page, child, nested, _, _ = framed
    fid = (await frame_rows(sid))[-1]['frame_id']
    proposal = await prepare(sid, frame_id=fid, selector='#go')
    frame = {'selected': nested, 'parent': child, 'top': page.main_frame}[target]
    await frame.evaluate("document.title='Access denied'")
    assert (await call(sid, 'elements', frame_id=fid))['code'] == 'blocked_access'
    assert (await execute(sid, proposal))['code'] == 'blocked_access'
    assert await nested.evaluate('window.clicks') == 0


@pytest.mark.parametrize('action,selector', [
    ('extract_text', 'p'), ('extract_links', 'a'),
])
async def test_extraction_cannot_enter_unchecked_child_with_selector(framed, action, selector):
    _, sid, _page, child, nested, _, _ = framed
    rows = await frame_rows(sid)
    # Both omitted main-frame scope and an explicit parent scope must reject
    # internal Playwright frame traversal, even when discovery itself is blocked.
    await nested.evaluate("document.title='Access denied'")
    through_two = 'iframe >> internal:control=enter-frame >> iframe >> internal:control=enter-frame >> ' + selector
    through_one = 'iframe >> internal:control=enter-frame >> ' + selector
    result = await call(sid, action, selector=through_two)
    assert result['code'] == 'invalid_selector'
    result = await call(sid, action, frame_id=rows[1]['frame_id'], selector=through_one)
    assert result['code'] == 'invalid_selector'
    assert (await call(sid, action, frame_id=rows[-1]['frame_id'], selector=selector))['code'] == 'blocked_access'
    assert (await call(sid, 'frames'))['code'] == 'blocked_access'
    # Same-origin DOM access is not an exception to the explicit frame boundary.
    await child.evaluate("document.title='Access denied'")
    assert (await call(sid, action, selector=through_one))['code'] == 'invalid_selector'


async def test_link_batch_cannot_retarget_after_document_capture(approved, monkeypatch):
    _, sid, page, url, _ = approved
    original = Locator.evaluate_all
    async def navigate_before_batch(locator, expression, arg=None):
        if isinstance(arg, dict) and 'document' in arg:
            await page.goto(url + 'replacement')
        return await original(locator, expression, arg)
    monkeypatch.setattr(Locator, 'evaluate_all', navigate_before_batch)
    result = await call(sid, 'extract_links')
    assert result['code'] == 'invalid_selector'
    assert 'links' not in result


async def test_link_extraction_batches_large_match_sets_within_budget(approved):
    _, sid, page, _, _ = approved
    await page.evaluate('''() => {
      document.body.innerHTML = Array.from({length:4000}, (_,i) =>
        '<a href="https://example.com/'+i+'">Link '+i+'</a>').join('');
    }''')
    started = time.monotonic()
    result = await call(sid, 'extract_links', limit=1)
    assert result['status'] == 'ok', result
    assert result['links'] == [{'text': 'Link 0', 'url': 'https://example.com/0'}]
    assert time.monotonic() - started < 3


@pytest.mark.parametrize('kind', ['blank', 'srcdoc', 'sandbox'])
async def test_local_child_documents_disclose_effective_origin_without_public_evidence_identity(approved, kind):
    _, sid, page, url, _ = approved
    await page.evaluate('''kind => new Promise(resolve => {
      const f=document.createElement('iframe');
      if (kind === 'sandbox') f.sandbox='allow-scripts';
      if (kind !== 'blank') f.srcdoc='<p>Local frame</p><button>Go</button>';
      f.onload=()=>resolve(); document.body.append(f);
    })''', kind)
    frame = page.frames[-1]
    if kind == 'blank':
        await frame.evaluate("document.body.innerHTML='<p>Local frame</p><button>Go</button>'")
    row = (await frame_rows(sid))[-1]
    assert row['url'] == ('about:blank' if kind == 'blank' else 'about:srcdoc')
    assert row['origin'] == ('null' if kind == 'sandbox' else url.rstrip('/'))
    result = await call(sid, 'extract_text', frame_id=row['frame_id'])
    assert result['url'] == row['url'] and 'Local frame' in result['text']
    assert (await call(sid, 'wait', frame_id=row['frame_id'], url_contains=row['url']))['status'] == 'ok'
    # Chromium documents without Navigation API/currentEntry have no secure
    # retained same-document generation. Reads work; approval fails closed.
    proposal = await call(sid, 'prepare_action', frame_id=row['frame_id'], operation='click', selector='button')
    if kind == 'blank' or kind == 'sandbox':
        assert proposal['code'] == 'approval_unavailable'
        assert await frame.evaluate('''function () {
          const key = '__browserWorkerNavigationGeneration';
          return this[key] === null && !Reflect.set(this, key, () => 0n) && !Reflect.deleteProperty(this, key);
        }''', isolated_context=False)
        assert (await call(sid, 'prepare_action', frame_id=row['frame_id'], operation='evaluate', script='1'))['code'] == 'approval_unavailable'
    else:
        assert proposal['preview']['frame_origin'] == url.rstrip('/')
        assert (await execute(sid, proposal))['status'] == 'ok'


async def test_raw_frame_actions_keep_existing_capabilities(framed):
    _, sid, _, _, nested, _, _ = framed
    fid = (await frame_rows(sid))[-1]['frame_id']
    token = auth._current_caller.set(Caller('research', frozenset({'inspect.read','inspect.interact','inspect.script'})))
    try:
        assert (await call(sid, 'click', frame_id=fid, selector='#go'))['status'] == 'ok'
        assert (await call(sid, 'type', frame_id=fid, selector='#entry', text='raw', submit=True))['status'] == 'ok'
        assert (await call(sid, 'evaluate', frame_id=fid, script='location.href'))['result'] == nested.url
        assert await nested.locator('#entry').input_value() == 'raw'
        assert await nested.evaluate('window.keys') == ['Enter']
    finally:
        auth._current_caller.reset(token)


async def test_frame_approval_expiry_cancel_single_use(framed):
    manager, sid, _, _, nested, _, _ = framed
    fid = (await frame_rows(sid))[-1]['frame_id']
    proposal = await prepare(sid, frame_id=fid, selector='#go')
    assert (await call(sid, 'discard_prepared', proposal_id=proposal['proposal_id']))['discarded']
    assert (await execute(sid, proposal))['code'] == 'approval_unavailable'
    proposal = await prepare(sid, frame_id=fid, selector='#go')
    session = await manager._get('research', sid)
    session.approvals.pending.expires -= 121
    assert (await execute(sid, proposal))['code'] == 'approval_expired'
    assert await nested.evaluate('window.clicks') == 0
    proposal = await prepare(sid, frame_id=fid, selector='#go')
    results = await asyncio.gather(execute(sid, proposal), execute(sid, proposal))
    assert sorted(r['status'] for r in results) == ['error', 'ok']
    assert await nested.evaluate('window.clicks') == 1


async def test_native_dispatch_gap_is_not_an_atomic_target_guarantee(approved, monkeypatch):
    manager, sid, page, _, _ = approved
    proposal = await prepare(sid, selector='#clicker')
    session = await manager._get('research', sid)
    element = session.approvals.pending.element
    original = element.click

    async def changed_after_all_preflight(**kwargs):
        if not kwargs.get('trial'):
            await page.evaluate('''() => {
              const button = document.querySelector('#clicker');
              button.textContent = 'Changed after preflight';
              button.onclick = () => window.changedEffect = true;
            }''')
        return await original(**kwargs)

    monkeypatch.setattr(element, 'click', changed_after_all_preflight)
    assert (await execute(sid, proposal))['status'] == 'ok'
    assert await page.evaluate('window.changedEffect')
    assert await page.evaluate('window.clicks') == 0
    assert (await execute(sid, proposal))['code'] == 'approval_unavailable'


@pytest.mark.parametrize('operation', ['click', 'type'])
async def test_frame_ancestor_restored_history_after_trial_blocks_dispatch(framed, monkeypatch, operation):
    manager, sid, _, child, nested, _, _ = framed
    fid = (await frame_rows(sid))[-1]['frame_id']
    fields = {'selector': '#go'} if operation == 'click' else {'selector': '#entry', 'text': 'Forbidden', 'submit': True}
    proposal = await prepare(sid, operation, frame_id=fid, **fields)
    session = await manager._get('research', sid)
    element = session.approvals.pending.element
    original = element.click

    async def race(**kwargs):
        assert kwargs['trial']
        await original(**kwargs)
        await child.evaluate("() => { history.pushState({}, '', '#changed'); history.replaceState({}, '', location.pathname); }")

    monkeypatch.setattr(element, 'click', race)
    assert (await execute(sid, proposal))['code'] == 'approval_stale'
    assert await nested.evaluate('window.clicks') == 0
    assert await nested.locator('#entry').input_value() == ''


async def test_frame_evaluate_retains_guarded_realm_after_final_preflight(framed, monkeypatch):
    manager, sid, _, _, nested, _, _ = framed
    fid = (await frame_rows(sid))[-1]['frame_id']
    proposal = await prepare(sid, 'evaluate', frame_id=fid, script='window.forbidden = true')
    session = await manager._get('research', sid)
    document = session.approvals.pending.document
    original = document.evaluate

    async def race(expression, arg=None):
        if arg and 'deadline' in arg:
            await nested.evaluate("() => { history.pushState({}, '', '#changed'); history.replaceState({}, '', location.pathname); }")
        return await original(expression, arg)

    monkeypatch.setattr(document, 'evaluate', race)
    assert (await execute(sid, proposal))['code'] == 'approval_stale'
    assert not await nested.evaluate('Boolean(window.forbidden)')


async def test_frames_limit_and_private_child_are_not_public(framed):
    _, sid, _, child, _, url, requests = framed
    result = await call(sid, 'frames', limit=2)
    assert len(result['frames']) == 2 and result['truncated']
    await child.evaluate('''url => new Promise(resolve => {
      const f=document.createElement('iframe'); f.src=url;
      f.onload=()=>resolve(); f.onerror=()=>resolve(); document.body.append(f);
    })''', url.replace('public.test', '127.0.0.1') + 'private-frame')
    # The proxy does not fetch private content; the resulting browser error
    # document is also not treated as public frame content.
    assert not any(b' /private-frame ' in request for request in requests)
    assert (await call(sid, 'frames'))['status'] == 'error'


async def test_frame_ancestry_is_checked_between_native_input_and_enter(framed):
    _, sid, _, child, nested, _, _ = framed
    # A same-origin child input can mutate its parent synchronously on input.
    local = await add_frame(child, child.url + '-local', 'local')
    fid = next(row['frame_id'] for row in await frame_rows(sid) if row['url'] == local.url)
    await local.evaluate('''() => {
      document.querySelector('#entry').oninput = () => {
        parent.history.pushState({}, '', '#changed');
        parent.history.replaceState({}, '', parent.location.pathname);
      };
    }''', isolated_context=False)
    proposal = await prepare(sid, 'type', frame_id=fid, selector='#entry', text='Partial', submit=True)
    assert (await execute(sid, proposal))['code'] == 'approval_stale'
    assert await local.locator('#entry').input_value() == 'Partial'
    assert await local.evaluate('window.keys') == []
    assert await nested.evaluate('window.clicks') == 0
