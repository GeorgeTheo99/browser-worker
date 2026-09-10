"""Ephemeral, single-use approvals. Confirmation is a client-server trust boundary.

Preparation only reads DOM state. Execution can run arbitrary site handlers and is
not transactional: an error does not imply that no side effect occurred.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from patchright.async_api import ElementHandle, Error, JSHandle, Page
from patchright.async_api import TimeoutError as BrowserTimeoutError

from controls import check_access
from errors import fail
from security import NetworkPolicyError, parse_public_url

APPROVAL_TTL_SECONDS = 120
MAX_ARG_BYTES = 20_000

# Installed in the main world before site scripts. Native currententrychange is
# synchronous for History API mutations (also from other realms) and traversals.
# Unlike history wrappers, it cannot be bypassed with a borrowed native method.
# Only a frozen reader is exposed; the counter and listener stay in a closure.
# Capture registration precedes site listeners, so stopImmediatePropagation
# cannot hide changes. Unsupported documents fail closed during preparation.
NAVIGATION_INIT_SCRIPT = r"""(function () {
  const nav = this.navigation;
  if (!nav || !nav.currentEntry) return;
  let generation = 0n;
  const revoke = () => { generation++; };
  nav.addEventListener('currententrychange', revoke, true);
  // Leaving a document must also revoke a retained/BFCache-restored realm.
  this.addEventListener('pagehide', revoke, true);
  Object.defineProperty(this, '__browserWorkerNavigationGeneration', {
    value: Object.freeze(() => generation), writable: false, configurable: false
  });
})();"""

# Never read input values, textarea content, or editable content as labels. Refuse
# oversized signatures instead of silently comparing truncated identity fields.
_DOM_HELPERS = r"""
const visible = el => el.isConnected && !el.closest('[aria-hidden="true"],[inert]') &&
  el.checkVisibility() && !!(el.getBoundingClientRect().width && el.getBoundingClientRect().height);
const text = el => {
  if (!el) return '';
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
    acceptNode: n => n.nodeType === 1 && n.matches('input,textarea,select,script,style,[contenteditable]:not([contenteditable="false"])') ?
      NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT
  });
  if (el.matches('input,textarea,select,[contenteditable]:not([contenteditable="false"])')) return '';
  let result = '', n, count = 0;
  while ((n = walker.nextNode()) && count++ < 200 && result.length < 501)
    if (n.nodeType === 3) result += n.textContent;
  return result.trim().slice(0, 501);
};
const label = el => el.getAttribute('aria-label') ||
  (el.getAttribute('aria-labelledby') || '').split(/\s+/).slice(0, 6)
    .map(id => text(document.getElementById(id))).join(' ').trim() ||
  Array.from(el.labels || []).slice(0, 6).map(text).join(' ') || text(el) ||
  el.getAttribute('title') || el.getAttribute('alt') || '';
"""

_SNAPSHOT = r"""(el, command = {}) => {
""" + _DOM_HELPERS + r"""
  if (!(el instanceof HTMLElement) || window !== window.top || el.ownerDocument !== document || el.getRootNode() !== document ||
      !visible(el) || el.matches(':disabled,[aria-disabled="true"]')) return null;
  if (command.action === 'type' && (!el.matches('input,textarea') || el.readOnly ||
      (el.tagName === 'INPUT' && !['text','search','tel','url','email','password'].includes(el.type)))) return null;
  if (command.action === 'click' && !el.closest('a[href],button,input,label,[role="button"],[role="link"]')) return null;
  // Enter's default submitter is the first submit button in document order,
  // including external form-associated buttons. No-button/image/custom flows
  // are deliberately unsupported rather than guessed.
  let defaultSubmitter = null, submission = null;
  if (command.action === 'type' && command.submit) {
    if (el.tagName !== 'INPUT' || !el.form) return null;
    const candidates = document.querySelectorAll('button,input');
    if (candidates.length > 500) return null;
    defaultSubmitter = [...candidates].find(n =>
      n.form === el.form && ['submit','image'].includes(n.type));
    if (!defaultSubmitter || defaultSubmitter.type === 'image' ||
        defaultSubmitter.matches(':disabled,[aria-disabled="true"]')) return null;
    const f = el.form, s = defaultSubmitter;
    submission = {label: label(s), tag: s.tagName.toLowerCase(),
      action: s.hasAttribute('formaction') ? s.formAction : f.action,
      method: s.hasAttribute('formmethod') ? s.formMethod : f.method,
      target: s.hasAttribute('formtarget') ? s.formTarget : f.target,
      enctype: s.hasAttribute('formenctype') ? s.formEnctype : f.enctype,
      novalidate: s.formNoValidate || f.noValidate};
    if (!['get','post'].includes(submission.method)) return null;
  }
  const nodes = [el], associations = defaultSubmitter ? [defaultSubmitter] : [];
  for (let p = el.parentElement; p; p = p.parentElement) {
    nodes.push(p); if (nodes.length > 32) return null;
  }
  const descendants = el.querySelectorAll('*');
  if (descendants.length > 100) return null;
  nodes.push(...descendants);
  // Include activation through labels (including external submitters), links,
  // form ownership, and accessible label nodes. Retain association identities.
  for (const n of [...nodes]) {
    associations.push(...Array.from(n.labels || []));
    if (n.control) associations.push(n.control);
    if (n.form) associations.push(n.form);
    for (const id of (n.getAttribute('aria-labelledby') || '').split(/\s+/)) {
      const target = document.getElementById(id); if (target) associations.push(target);
    }
  }
  for (const n of [...associations]) if (n.form) associations.push(n.form);
  if (associations.length > 100 || [...nodes, ...associations].some(n =>
      n.matches('input[type="file"],input[type="image"],select,option'))) return null;
  const attrs = ['id','role','type','href','target','download','form','action','method','for',
    'formaction','formmethod','formtarget','formenctype','formnovalidate','enctype','novalidate',
    'aria-label','aria-labelledby','aria-disabled','disabled','readonly','contenteditable','name'];
  const describe = (n, includeLabel) => ({tag: n.tagName, attrs: attrs.map(a => n.getAttribute(a)),
    href: n.href || null, action: n.action || null, method: n.method || null,
    type: n.type || null, label: includeLabel ? label(n) : null});
  const targetLabel = label(el);
  // Global body/form text (timers, validation hints) is not target identity.
  // Keep target/label semantics and every activation/ownership attribute bound.
  const signature = JSON.stringify({
    nodes: nodes.map(n => describe(n, n === el || el.contains(n) || n.matches('label,button,a[href],[role="button"],[role="link"],[role="combobox"]'))),
    associations: associations.map(n => describe(n, n.tagName !== 'FORM')), submission
  });
  if (targetLabel.length > 500 || signature.length > 20000) return null;
  // Native activation can travel through ancestor/descendant labels or an
  // external form owner. Disclose all known destinations, not just el.form.
  const destinations = new Set();
  for (const n of [...nodes, ...associations]) {
    if (n.matches('a[href]')) destinations.add(n.href);
    const submitter = n.control || n;
    const form = submitter.form || (n.tagName === 'FORM' ? n : null);
    if (form) destinations.add(submitter.hasAttribute('formaction') ? submitter.formAction : form.action);
  }
  const destination = [...destinations].join('\n');
  if (destination.length > 8192) return null;
  return {document, root: document.documentElement, url: location.href, command,
    element: el, associations, nodes, signature, defaultSubmitter,
    info: {target_label: targetLabel, target_tag: el.tagName.toLowerCase(), destination: destination || null,
      ...(submission ? {default_submitter: submission} : {})}};
}"""

# Trial actionability may wait arbitrarily while the page changes. It grants no
# authority: inspect, hit-test, deadline and DOM dispatch share one synchronous
# task below, with another guard after every handler-producing operation.
_GUARDED_ACTION = r"""(saved, args) => {
  const inspect = """ + _SNAPSHOT + r""";
  const el = saved.element;
  const guard = () => {
    if (saved.authority.navigation() !== saved.authority.generation ||
        saved.document !== document || saved.root !== document.documentElement || saved.url !== location.href)
      return 'approval_stale';
    const now = inspect(el, saved.command);
    if (!now || now.signature !== saved.signature || now.nodes.length !== saved.nodes.length ||
        !now.nodes.every((n,i) => n === saved.nodes[i]) ||
        now.associations.length !== saved.associations.length ||
        !now.associations.every((n,i) => n === saved.associations[i])) return 'approval_stale';
    const r = el.getBoundingClientRect();
    const left = Math.max(0, r.left), right = Math.min(innerWidth, r.right);
    const top = Math.max(0, r.top), bottom = Math.min(innerHeight, r.bottom);
    const hit = document.elementFromPoint((left + right) / 2, (top + bottom) / 2);
    if (right <= left || bottom <= top || !hit || (hit !== el && !el.contains(hit))) return 'approval_stale';
    return performance.now() >= args.deadline ? 'approval_expired' : null;
  };
  let error = guard();
  if (error) return {error};
  if (saved.command.action === 'click') {
    HTMLElement.prototype.click.call(el);
  } else {
    HTMLElement.prototype.focus.call(el);
    error = guard(); // focus handlers may change the approved target or form
    if (error) return {error};
    const prototype = el.tagName === 'INPUT' ? HTMLInputElement.prototype : HTMLTextAreaElement.prototype;
    const value = args.command.clear ? args.command.text : el.value + args.command.text;
    Object.getOwnPropertyDescriptor(prototype, 'value').set.call(el, value);
    error = guard();
    if (error) return {error};
    el.dispatchEvent(new InputEvent('input', {bubbles: true, composed: true,
      inputType: 'insertText', data: args.command.text}));
    if (saved.command.submit) {
      error = guard(); // input handlers can change default submitter/overrides
      if (error) return {error};
      HTMLElement.prototype.click.call(saved.defaultSubmitter);
    }
  }
  return {};
}"""

_ELEMENTS = r"""limit => {
""" + _DOM_HELPERS + r"""
  const candidates = document.querySelectorAll('a[href],button,input:not([type="hidden"]),textarea,select,label,[role],[tabindex],[contenteditable="true"]');
  const elements = [];
  let scanned = 0;
  for (const el of candidates) {
    if (scanned++ >= 500 || elements.length >= limit) break;
    if (!visible(el)) continue;
    // Structural CSS only: no value attributes or text-selector interpolation.
    const parts = []; let n = el;
    while (n && n !== document.documentElement && parts.length < 32) {
      let index = 1;
      for (let p = n.previousElementSibling; p; p = p.previousElementSibling)
        if (p.tagName === n.tagName) index++;
      parts.unshift(CSS.escape(n.localName) + ':nth-of-type(' + index + ')'); n = n.parentElement;
    }
    if (n !== document.documentElement) continue;
    const selector = 'html > ' + parts.join(' > ');
    if (selector.length > 2000 || document.querySelectorAll(selector).length !== 1) continue;
    elements.push({selector, tag: el.tagName.toLowerCase(), role: (el.getAttribute('role') || '').slice(0,100),
      label: label(el).slice(0,500), type: (el.getAttribute('type') || '').slice(0,100),
      href: (el.matches('a[href]') ? el.href : '').slice(0,8192)});
  }
  return {elements, truncated: scanned < candidates.length};
}"""


async def discover_elements(page: Page, limit: int) -> dict[str, Any]:
    try:
        async with asyncio.timeout(3):
            await check_access(page)
            result = await page.evaluate(_ELEMENTS, min(100, max(1, limit)))
            for row in result['elements']:
                if row['href']:
                    try:
                        parse_public_url(row['href'])
                    except NetworkPolicyError:
                        row['href'] = ''
            return result
    except (TimeoutError, BrowserTimeoutError) as exc:
        raise fail('extraction_timeout') from exc
    except Error as exc:
        raise fail('operation_failed') from exc


@dataclass
class Proposal:
    id: str
    page: Page
    url: str
    epoch: int
    expires: float
    command: dict[str, Any]
    document: JSHandle
    element: ElementHandle | None = None
    snapshot: JSHandle | None = None
    browser_clock_offset: float = 0

    async def dispose(self) -> None:
        self.command.clear()
        for handle in (self.snapshot, self.element, self.document):
            if handle is not None:
                try:
                    await handle.dispose()
                except Error:
                    pass


class ApprovalStore:
    def __init__(self) -> None:
        self.epoch = 0
        self.pending: Proposal | None = None

    def invalidate(self, *_args: Any) -> None:
        self.epoch += 1

    async def clear(self) -> None:
        pending, self.pending = self.pending, None
        if pending:
            await pending.dispose()

    async def prepare(self, page: Page, operation: str, params: dict[str, Any], *, session_deadline: float | None = None) -> dict[str, Any]:
        await self.clear()  # one pending proposal per session; replacements revoke old IDs
        proposal = None
        navigation = None
        try:
            async with asyncio.timeout(min(3, APPROVAL_TTL_SECONDS, max(0, (session_deadline or float('inf')) - time.monotonic()))):
                await check_access(page)
                url, epoch = page.url, self.epoch
                parse_public_url(url)
                command = {'action': operation, 'timeout_ms': params['timeout_ms']}
                if operation in {'click', 'type'}:
                    command['selector'] = params['selector']
                    if operation == 'type':
                        command.update(text=params['text'], clear=params['clear'], submit=params['submit'])
                elif operation == 'evaluate':
                    encoded = json.dumps(params.get('arg'), allow_nan=False)
                    if len(encoded.encode('utf-8')) > MAX_ARG_BYTES:
                        raise fail('invalid_request')
                    command.update(script=params['script'], arg=json.loads(encoded))
                else:
                    raise fail('invalid_request')
                expires = min(time.monotonic() + APPROVAL_TTL_SECONDS, session_deadline or float('inf'))
                # Patchright defaults to an isolated realm. Retain the main-world
                # reader, but keep all proposal authority in the isolated realm.
                navigation = await page.evaluate_handle(
                    'function () { return this.__browserWorkerNavigationGeneration; }', isolated_context=False)
                proposal = Proposal(secrets.token_urlsafe(24), page, url, epoch,
                                    expires, command,
                                    await page.evaluate_handle('navigation => ({navigation, generation: navigation(), document, root: document.documentElement, url: location.href, clock: performance.now()})', navigation))
                clock = await proposal.document.evaluate('d => d.clock')
                # Subtract the entire clock-sampling round trip: transport delay
                # can shorten authority, never extend it in the browser realm.
                proposal.browser_clock_offset = clock - time.monotonic() * 1000
                parts = urlsplit(url)
                preview = {k: v for k, v in command.items() if k != 'timeout_ms'}
                preview.update(url=url, origin=f'{parts.scheme}://{parts.netloc}')
                if operation in {'click', 'type'}:
                    locator = page.locator(command['selector'])
                    count = await locator.count()
                    if count != 1:
                        raise fail('selector_not_found' if count == 0 else 'selector_ambiguous')
                    proposal.element = await locator.element_handle(timeout=1000)
                    if proposal.element is None:
                        raise fail('selector_not_found')
                    if await proposal.element.owner_frame() != page.main_frame:
                        raise fail('approval_unavailable')
                    proposal.snapshot = await proposal.element.evaluate_handle(r"""(el, args) => {
                      const inspect = """ + _SNAPSHOT + r""";
                      const saved = inspect(el, args.command);
                      if (saved) saved.authority = args.authority;
                      return saved;
                    }""", {'command': command, 'authority': proposal.document})
                    info = await proposal.snapshot.evaluate('s => s?.info || null')
                    if info is None:
                        raise fail('approval_unavailable')
                    preview.update({k: v for k, v in info.items() if v is not None})
                if not await self._valid(proposal, page):
                    raise fail('approval_stale')
                if time.monotonic() >= proposal.expires:
                    raise fail('approval_expired')
                # Copy preview JSON so no caller reference can mutate the frozen command.
                result = {'proposal_id': proposal.id,
                          'expires_in_seconds': max(0, int(proposal.expires - time.monotonic())),
                          'preview': json.loads(json.dumps(preview, allow_nan=False))}
                self.pending = proposal
                return result
        except (TimeoutError, BrowserTimeoutError) as exc:
            raise fail('operation_timeout') from exc
        except Error as exc:
            raise fail('approval_unavailable') from exc
        finally:
            if navigation is not None:
                try:
                    await navigation.dispose()
                except Error:
                    pass
            if proposal is not None and self.pending is not proposal:
                await proposal.dispose()

    async def _valid(self, proposal: Proposal, page: Page) -> bool:
        if page is not proposal.page or page.is_closed() or self.epoch != proposal.epoch or page.url != proposal.url:
            return False
        if not await proposal.document.evaluate('d => d.navigation() === d.generation && d.document === document && d.root === document.documentElement'):
            return False
        if proposal.snapshot is not None:
            return await proposal.snapshot.evaluate(r"""saved => {
              const inspect = """ + _SNAPSHOT + r""";
              const now = inspect(saved.element, saved.command);
              return !!now && now.document === saved.document && now.root === saved.root &&
                now.url === saved.url && now.signature === saved.signature &&
                now.nodes.length === saved.nodes.length && now.nodes.every((n,i) => n === saved.nodes[i]) &&
                now.associations.length === saved.associations.length &&
                now.associations.every((n,i) => n === saved.associations[i]);
            }""")
        return True

    async def finish(self, page: Page, proposal_id: str, *, execute: bool) -> dict[str, Any]:
        proposal = self.pending
        if proposal is None or proposal.id != proposal_id:
            raise fail('approval_unavailable')
        self.pending = None  # consume before any await or effect, including failed validation
        try:
            if time.monotonic() >= proposal.expires:
                raise fail('approval_expired')
            if not execute:
                return {'discarded': True}
            stop_at = min(proposal.expires, time.monotonic() + proposal.command['timeout_ms'] / 1000)
            async with asyncio.timeout(max(0, stop_at - time.monotonic())):
                if not await self._valid(proposal, page):
                    raise fail('approval_stale')
                await check_access(page)
                # Preflight only; the authoritative guard runs at DOM dispatch.
                if not await self._valid(proposal, page):
                    raise fail('approval_stale')
                command, element = proposal.command, proposal.element
                deadline = stop_at * 1000 + proposal.browser_clock_offset
                if command['action'] in {'click', 'type'}:
                    assert element is not None and proposal.snapshot is not None
                    await element.click(trial=True, timeout=max(1, (stop_at - time.monotonic()) * 1000))
                    if not await self._valid(proposal, page):
                        raise fail('approval_stale')
                    outcome = await proposal.snapshot.evaluate(_GUARDED_ACTION, {'command': command, 'deadline': deadline})
                    if outcome.get('error'):
                        raise fail(outcome['error'])
                else:
                    # Execute in the retained document's realm, with an in-call
                    # identity guard. Page.evaluate could race navigation and run
                    # the approved script in a replacement document.
                    result = await proposal.document.evaluate(r"""(saved, command) => {
                      if (saved.navigation() !== saved.generation ||
                          saved.document !== document || saved.root !== document.documentElement || saved.url !== location.href)
                        throw new Error('stale document');
                      if (performance.now() >= command.deadline) throw new Error('expired approval');
                      let expression = command.script;
                      try {
                        new Function('return (' + expression + '\n)');
                        expression = '(' + expression + '\n)';
                      } catch (error) {
                        if (!(error instanceof SyntaxError)) throw error;
                      }
                      if (performance.now() >= command.deadline) throw new Error('expired approval');
                      const value = (0, eval)(expression);
                      return typeof value === 'function' ? value(command.arg) : value;
                    }""", {'script': command['script'], 'arg': command['arg'], 'deadline': deadline})
                    if len(json.dumps(result, allow_nan=False)) > 50_000:
                        raise fail('operation_failed')
                    await check_access(page)
                    return {'result': result}
                await check_access(page)
                return {'url': page.url}
        except (TimeoutError, BrowserTimeoutError) as exc:
            raise fail('operation_timeout') from exc
        except Error as exc:
            raise fail('approval_stale') from exc
        finally:
            await proposal.dispose()
