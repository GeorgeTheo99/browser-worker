"""Ephemeral, single-use approvals. Confirmation is a client-server trust boundary.

Preparation only reads DOM state. Execution can run arbitrary site handlers and is
not transactional: an error does not imply that no side effect occurred.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from patchright.async_api import ElementHandle, Error, Frame, JSHandle, Page
from patchright.async_api import TimeoutError as BrowserTimeoutError

from controls import check_access
from errors import fail
from frames import DocumentRef, ancestry, check_frame_access
from security import NetworkPolicyError, parse_public_url

APPROVAL_TTL_SECONDS = 120
MAX_ARG_BYTES = 20_000

NATIVE_WARNING = ('Native effects may change after preflight; errors may follow partial effects. '
                  'Never retry automatically.')

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
  if (el.isContentEditable || el.matches('input,textarea,select,[contenteditable]:not([contenteditable="false"])')) return '';
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
  if (!(el instanceof Element) || el.ownerDocument !== document ||
      !visible(el) || el.matches(':disabled,[aria-disabled="true"]')) return null;
  if (command.action === 'type' && (!(el.matches('input,textarea') || el.isContentEditable) || el.readOnly)) return null;
  // Disclose a known implicit submitter when present, without substituting its
  // activation for real Enter or restricting custom keyboard handlers.
  let defaultSubmitter = null, submission = null;
  if (command.action === 'type' && command.submit && el.tagName === 'INPUT' && el.form) {
    const candidates = el.getRootNode().querySelectorAll('button,input');
    if (candidates.length > 500) return null;
    defaultSubmitter = [...candidates].find(n =>
      n.form === el.form && ['submit','image'].includes(n.type));
    if (defaultSubmitter) {
      const f = el.form, s = defaultSubmitter;
      submission = {label: label(s), tag: s.tagName.toLowerCase(),
        action: s.hasAttribute('formaction') ? s.formAction : f.action,
        method: s.hasAttribute('formmethod') ? s.formMethod : f.method,
        target: s.hasAttribute('formtarget') ? s.formTarget : f.target,
        enctype: s.hasAttribute('formenctype') ? s.formEnctype : f.enctype,
        novalidate: s.formNoValidate || f.noValidate};
    }
  }
  const nodes = [el], associations = defaultSubmitter ? [defaultSubmitter] : [];
  const parent = n => n.parentElement || n.getRootNode().host;
  for (let p = parent(el); p; p = parent(p)) {
    nodes.push(p); if (nodes.length > 32) return null;
  }
  const descendants = command.action === 'type' && el.isContentEditable ? [] : el.querySelectorAll('*');
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
      n.matches('input[type="file"]'))) return null;
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
      ...(command.action === 'type' ? {target_contenteditable: !!el.isContentEditable} : {}),
      ...(submission ? {default_submitter: submission} : {})}};
}"""

# Trial actionability may wait while the page changes. This read-only guard is
# deliberately separate from native dispatch: no atomic/exact-effect guarantee.
_PREFLIGHT = r"""(saved, args) => {
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
    const hit = el.getRootNode().elementFromPoint((left + right) / 2, (top + bottom) / 2);
    if (right <= left || bottom <= top || !hit || (hit !== el && !el.contains(hit))) return 'approval_stale';
    return performance.now() >= args.deadline ? 'approval_expired' : null;
  };
  return {error: guard()};
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


async def discover_elements(page: Page | Frame, limit: int) -> dict[str, Any]:
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
    documents: list[DocumentRef] = field(default_factory=list)

    @property
    def frame(self) -> Frame:
        return self.documents[-1].frame

    async def dispose(self) -> None:
        self.command.clear()
        for document in self.documents:
            await document.dispose()
        for handle in (self.snapshot, self.element):
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

    async def prepare(self, page: Page, operation: str, params: dict[str, Any], *, frame: Frame | None = None, session_deadline: float | None = None) -> dict[str, Any]:
        await self.clear()  # one pending proposal per session; replacements revoke old IDs
        proposal = None
        documents: list[DocumentRef] = []
        frame = frame or page.main_frame
        try:
            async with asyncio.timeout(min(3, APPROVAL_TTL_SECONDS, max(0, (session_deadline or float('inf')) - time.monotonic()))):
                await check_frame_access(page, frame)
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
                if params.get('frame_id') is not None:
                    command['frame_id'] = params['frame_id']
                expires = min(time.monotonic() + APPROVAL_TTL_SECONDS, session_deadline or float('inf'))
                # Patchright defaults to an isolated realm. Retain the main-world
                # reader, but keep all proposal authority in the isolated realm.
                for current in ancestry(frame):
                    documents.append(await DocumentRef.capture(current, require_navigation=True))
                proposal = Proposal(secrets.token_urlsafe(24), page, url, epoch,
                                    expires, command, documents[-1].handle, documents=documents)
                clock = await proposal.document.evaluate('d => d.clock')
                # Subtract the entire clock-sampling round trip: transport delay
                # can shorten authority, never extend it in the browser realm.
                proposal.browser_clock_offset = clock - time.monotonic() * 1000
                parts = urlsplit(url)
                preview = {k: v for k, v in command.items() if k != 'timeout_ms'}
                preview.update(url=url, origin=f'{parts.scheme}://{parts.netloc}')
                if params.get('frame_id') is not None:
                    preview.update(frame_url=documents[-1].url, frame_origin=documents[-1].origin,
                                   frame_ancestry=[{'url': d.url, 'origin': d.origin} for d in documents[:-1]])
                if operation in {'click', 'type'}:
                    preview.update(interaction_mode='native', warning=NATIVE_WARNING)
                if operation in {'click', 'type'}:
                    locator = frame.locator(command['selector'])
                    count = await locator.count()
                    if count != 1:
                        raise fail('selector_not_found' if count == 0 else 'selector_ambiguous')
                    proposal.element = await locator.element_handle(timeout=1000)
                    if proposal.element is None:
                        raise fail('selector_not_found')
                    if await proposal.element.owner_frame() != frame:
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
                await check_frame_access(page, frame)
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
            if proposal is not None and self.pending is not proposal:
                await proposal.dispose()
            elif proposal is None:
                for document in documents:
                    await document.dispose()

    async def _valid(self, proposal: Proposal, page: Page) -> bool:
        if page is not proposal.page or page.is_closed() or self.epoch != proposal.epoch or page.url != proposal.url:
            return False
        if ancestry(proposal.frame) != [d.frame for d in proposal.documents]:
            return False
        for document in proposal.documents:
            if not await document.valid():
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
                await check_frame_access(page, proposal.frame)
                # Native operations remain separate from this preflight.
                if not await self._valid(proposal, page):
                    raise fail('approval_stale')
                command, element = proposal.command, proposal.element
                deadline = stop_at * 1000 + proposal.browser_clock_offset
                if command['action'] in {'click', 'type'}:
                    assert element is not None and proposal.snapshot is not None
                    await element.click(trial=True, timeout=max(1, (stop_at - time.monotonic()) * 1000))
                    async def preflight() -> None:
                        await check_frame_access(page, proposal.frame)
                        if not await self._valid(proposal, page):
                            raise fail('approval_stale')
                        outcome = await proposal.snapshot.evaluate(_PREFLIGHT, {'deadline': deadline})
                        if outcome.get('error'):
                            raise fail(outcome['error'])
                        if time.monotonic() >= stop_at:
                            raise fail('approval_expired')

                    await preflight()  # after the potentially waiting trial
                    def remaining() -> float:
                        return max(1, (stop_at - time.monotonic()) * 1000)
                    if command['action'] == 'click':
                        await element.click(timeout=remaining())
                    else:
                        if command['clear']:
                            await element.fill(command['text'], timeout=remaining())
                        else:
                            await element.type(command['text'], timeout=remaining())
                        if command['submit']:
                            # Input/focus/keyboard handlers may already have had
                            # effects. Check again before a distinct Enter call.
                            await preflight()
                            await element.press('Enter', timeout=remaining())
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
                    await check_frame_access(page, proposal.frame)
                    return {'result': result}
                await check_frame_access(page, proposal.frame)
                return {'url': page.url}
        except (TimeoutError, BrowserTimeoutError) as exc:
            raise fail('operation_timeout') from exc
        except Error as exc:
            raise fail('approval_stale') from exc
        finally:
            await proposal.dispose()
