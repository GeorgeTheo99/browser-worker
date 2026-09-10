# Browser Worker Architecture

Status: worker implementation contract. Client registrations and capability grants are separately operator-managed; the historical canary runbook is not a statement of current deployment state.

## Boundary

`browser-worker` is a standalone loopback MCP service. It does not import, call, proxy, monitor, or fall back to `local_web_search`. Search/fetch health is not a browser-worker dependency. The worker exposes exactly two model-facing tools:

- `browser_fetch` — one-shot rendered retrieval with no retained browser session.
- `browser_inspect` — short-lived, caller-owned browser sessions and actions.

Pi's `app_*` local/private testing tools remain in-process and out of scope.

## Repository and runtime

- Repository: `~/local_code/browser-worker`
- Runtime: Python 3.12, `uv`, FastMCP, Patchright/Chromium
- MCP: stateless streamable HTTP JSON at `http://127.0.0.1:8890/mcp`
- Service: `com.local.mcp-browser-worker`
- Releases: exact committed snapshots under `~/srv/browser-worker/releases/<sha>` with an atomic `current` symlink
- Data: owner-only `~/srv/browser-worker/shared`; configurable with `BROWSER_WORKER_DATA_DIR`
- Logs: owner-only `~/Library/Logs/browser-worker/`

The operator installs a locked environment inside an exact Git-archive release, writes and verifies a complete source manifest plus revision receipt, removes write permissions from the finalized snapshot, atomically selects it, and refuses dirty/uncommitted source. Health reports the deployed revision. The service has independent `/live`, `/ready`, and `/health` endpoints.

## Tool contracts

All schemas reject unknown fields. Limits are enforced server-side even if a client bypasses schema validation. Results are JSON serialized into MCP text content and contain no worker filesystem paths, cookies, profile data, or raw internal errors. Page content is untrusted and may contain sensitive data. Approval previews deliberately echo the exact caller-supplied text/script/JSON for private human review; clients must not log or expose those previews to the model.

### `browser_fetch`

Input:

| Field | Type | Default | Contract |
|---|---|---:|---|
| `url` | string | required | Absolute public `http(s)` URL without embedded credentials |
| `max_chars` | integer | `20000` | `1000..50000` |
| `wait_until` | enum | `domcontentloaded` | `load`, `domcontentloaded`, `networkidle`, or `commit` |
| `timeout_ms` | integer | `30000` | `1000..60000` |
| `include_links` | boolean | `false` | Return at most 100 visible public links |
| `include_screenshot` | boolean | `false` | Return an owner-bound artifact handle, never a caller path |

Output includes `status`, `requested_url`, `final_url`, `title`, rendered visible `text`, `truncated`, optional links/artifact metadata, and bounded timing/network-policy metadata. The profile, proxy, pages, and browser process are always destroyed before return.

### `browser_inspect`

A single action schema avoids exposing a family of competing tool names. `action` selects one operation; the server performs action-specific validation.

Actions:

- Session/lifecycle: `open`, `state`, `close`, `cleanup_scope`
- Navigation/tabs: `navigate`, `open_tab`, `list_tabs`, `switch_tab`, `close_tab`
- Read-only inspection: `extract_text`, `extract_links`, `controls`, `elements`, `wait`, `console`
- Restricted research interaction: `expand_control`, `select_option` (explicit `inspect.controls` grant)
- Artifacts: `screenshot`, `export_pdf`
- Server-mediated confirmation capability: `prepare_action`, `execute_prepared`, `discard_prepared`
- Interactive Pi capability: `click`, `type`
- Privileged Pi-only capability: `evaluate`

Common fields are optional at the JSON-Schema layer for model compatibility and strictly checked per action: `session_id`, `scope_id`, `url`, `selector`, `control_id`, `option`, `text`, `tab_index`, `timeout_ms`, `wait_until`, `state`, `url_contains`, `max_chars`, `limit`, `clear`, `submit`, `full_page`, `format`, `landscape`, `print_background`, `script`, `arg`, `operation`, and `proposal_id`.

`open` creates an isolated ephemeral profile and may navigate to `url`. Every later action requires the opaque `session_id`. Sessions are bound to the authenticated caller, expire after 5 minutes idle or 15 minutes total, and cannot survive a worker restart.

### Research dropdown contract

- `controls` needs only `inspect.read`. Returns `status: "ok"`, `session_id`, `controls`, and top-level `truncated`.
- Each control has an opaque `control_id`, `role` (`select` or `combobox`), `label`, `selection` (observed selected labels), `options` (objects containing `label` and `disabled`), `expanded`, and per-control `truncated`.
- At most 20 visible main-document controls are returned from 100 candidates; at most 50 options per control; labels are at most 200 characters. Oversized option labels are omitted, not converted into actionable truncated labels. Truncated controls cannot be selected. Closed custom dropdowns may have no observed options or selection; no editable input value is read or returned.
- `expand_control` requires `session_id` and `control_id` (1..128 characters). Only a previously discovered noneditable DIV/SPAN ARIA combobox with `aria-haspopup="listbox"` and a unique `aria-controls` association is supported. Its listbox may mount on expansion. A contained text/search input is allowed as dropdown presentation, but is never read or typed into.
- `select_option` requires the same fields plus `option` (1..200 characters), exactly matching an observed, currently enabled, unambiguous option label. Supports single native selects and associated custom listbox `role="option"` elements. It does not accept native option values. Expand a closed custom control first to observe its options.
- Both mutations additionally require `inspect.controls`; success returns `status: "ok"`, `session_id`, and the refreshed `control` object with the same ID. Native expansion, multiple selects, list-style selects, arbitrary selectors, navigation fields, text, submission, scripts, and arguments are rejected on these actions.
- References are server-owned element handles in a caller-owned session, bound to the page and navigation generation. Discovery replaces previous IDs. Navigation (including reload/history events), tab changes/creation/closure, session expiry/closure/restart, detached elements, and changed control identity invalidate authority. Handles, role, association, enabled state, and option labels are revalidated before actions; stale references must be rediscovered.
- Controls in forms, editable regions, credential/payment paths, or pages with detectable password/email/credential inputs are refused. Buttons, links, and editable controls are not action targets; options containing such interactive descendants are disabled. Unsupported/ambiguous controls are omitted from discovery. Detected access-denied/challenge pages return `blocked_access`; never attempt bypasses.

**This is an explicit policy expansion, not read-only browsing or a guarantee of no side effects.** Page click/change handlers can issue public requests, mutate remote state, navigate, or run page code. A handler may already have run before a stale/timeout error is returned; do not blindly retry mutations. Form/credential/access detection is conservative and heuristic, not an authorization boundary. The pinned public-network proxy remains the egress boundary. No caller-provided selector clicking, typing, evaluation, upload, download, or credentials are added by `inspect.controls`.

### Confirmed action contract

`inspect.confirmed` is a trusted client-server capability, **not proof of human consent supplied by the worker**. My AI must expose `elements` to its model, but keep all three approval protocol actions out of its model schema. Its backend prepares the model's requested operation, privately persists the preview, displays it to the user, and calls execution only after an explicit user confirmation. Denial calls discard. Clear private text/script/argument details after every terminal outcome; never automatically retry an execution after an error or lost response. Pi's direct `click`/`type`/`evaluate` grants are unchanged.

- `elements` requires `inspect.read`; optional `limit` defaults to 50 (schema `1..200`, output capped at 100). Returns `{status:"ok",session_id,elements:[{selector,tag,role,label,type,href}],truncated}`. Scans at most 500 main-document interactive candidates. Structural CSS selectors are unique at discovery; labels are at most 500 characters, roles/types 100, selectors 2000, hrefs 8192. Hidden elements and literal private/unsafe hrefs are omitted or blanked. No input values, textarea content, or editable content are read as labels. These are untrusted observations, not durable target IDs.
- `prepare_action` requires `inspect.read` plus `inspect.confirmed`, `session_id`, and `operation: "click" | "type" | "evaluate"`. Click requires `selector` (1..2000 characters). Type additionally requires `text` (1..20000), with `clear=true` and `submit=false` defaults. Evaluate requires `script` (1..20000) and optional JSON `arg` (default null, serialized UTF-8 capped at 20000 bytes; nonfinite numbers rejected). Optional `timeout_ms` keeps existing `1000..60000` bounds and is frozen. Unrelated command/navigation fields are rejected.
- Preparation returns `{status:"ok",session_id,proposal_id,expires_in_seconds,preview:{action,url,origin,selector?,target_label?,target_tag?,destination?,default_submitter?,text?,clear?,submit?,script?,arg?}}`. Text/script/arg are the **exact frozen command**, never truncated or redacted in the approval preview. Target labels and destination are observed DOM metadata, not a prediction of site-handler behavior. Destination includes detected links/forms and effective submit actions, including external label-associated controls. For `type(submit=true)`, `default_submitter` discloses the actual first form-associated submit button in document order (including external buttons): `{label,tag,action,method,target,enctype,novalidate}` with effective overrides. Clients must display this submission metadata with the preview.
- `execute_prepared` and `discard_prepared` accept only `session_id` and `proposal_id` (1..128 characters), besides the action discriminator; no command overrides. Both require `inspect.read` plus `inspect.confirmed`. Successful discard returns `{status:"ok",session_id,discarded:true}`. Successful click/type execution returns `{status:"ok",session_id,url}`; evaluation returns `{status:"ok",session_id,result}` with the existing 50000-character JSON result bound.
- One in-memory pending proposal per session; preparation replaces/revokes an older proposal. Lifetime is at most 120 seconds, below the 300-second session idle timeout (absolute session TTL still applies). Preparation releases the session lock immediately after returning; no browser operation or lock is held while waiting for UI confirmation. Ownership is inherited from the session; another owner/session cannot use or discard its proposal. Close, cancellation, session expiry, and worker restart destroy proposals.
- Click/type preparation resolves exactly one visible **main-document** ElementHandle; frame selectors and shadow-tree targets are refused. Execution retains that handle and document, never re-resolves the selector. After a click-free Playwright trial actionability wait, Python rechecks the session epoch and proposal identity, then a single synchronous DOM task rechecks navigation generation, document/root/URL, the bounded DOM signature, node/association identities, a viewport-center hit test, and a browser-monotonic deadline immediately before dispatch. The signature includes default submitter identity and effective overrides; changing or inserting a default button revokes authority. All execution waits are capped by the earlier of command timeout, proposal expiry, and absolute session expiry. Browser clock calibration conservatively subtracts transport delay; even a queued dispatch cannot extend authority. Evaluation also checks retained document/root/URL and the browser deadline in-call.
- Navigation revocation also lives in the browser: an init script registers main-world native Navigation API `currententrychange` and `pagehide` capture listeners before site scripts. A closure-owned monotonic counter is exposed only through a frozen, nonreplaceable reader; proposal state stays in Patchright's isolated realm. Click/type/evaluate guards compare the retained generation synchronously at dispatch, so `pushState`, `replaceState`, hash changes, and back/forward traversal permanently revoke approval even when the URL is restored after Python's last preflight. State-only history changes also revoke. Native observation covers mutations from either realm and borrowed history methods; later site listeners cannot suppress the earlier capture observer. Documents without Navigation API/current-entry support fail closed (`approval_unavailable`); this relies on the supported Chromium event semantics, not a sandbox against a compromised browser. Existing DOM/document/deadline guards remain in force.
- **Confirmed actions deliberately use DOM activation, not trusted pointer/keyboard input.** Click calls native `HTMLElement.click()` after the guard (no pointerdown/up or automatic pointer-focus sequence, and no navigation-completion wait). Typing supports only editable native text-like inputs and textareas: native focus, guarded value replacement/append-at-end, then a guarded synthetic `input` event; no per-character key events, selection-aware insertion, or `change` event is synthesized. `submit=true` supports only single-line inputs with a supported, enabled default submit button: after input handlers, it revalidates and natively clicks that button, rather than pressing Enter. The original input is hit-tested; the implicit submitter need not be visible, matching implicit button activation. Missing/disabled/image submitters, dialog submissions, custom editors, file/image controls, and unsupported/oversized targets fail closed. Pi's direct Playwright click/type/Enter semantics remain unchanged.
- Execution consumes the proposal **before validation or any effect**. Expiration, staleness, errors, cancellation, duplicate calls, or a lost response never make it replayable. Unknown/replaced/used IDs return `approval_unavailable`; a matching expired ID returns `approval_expired`; invalidated document/target returns `approval_stale`. Cross-owner/closed sessions return `invalid_session`. Detectable access-denied/CAPTCHA pages are refused by the existing `check_access` check at discovery, preparation, and execution.

**Approved actions are not transactional or side-effect-free.** Approved clicks allow forms and label activation; `submit=true` requests the bound default submitter activation after typing. Trial actionability can scroll and trigger scroll handlers. Guards run again after focus and input handlers, but cannot roll back those handlers or the value already written. Handlers triggered by an approved dispatch can change destinations, issue public requests, navigate, or mutate targets within that same activation, even past the deadline. DOM guards bind dispatch, not arbitrary handler side effects or the eventual network transaction. Any error/lost response may mean partial execution with an unknown outcome: never automatically replay it. Confirmation grants neither uploads/downloads nor credential/profile APIs; the pinned public-network proxy and download cancellation remain unchanged.

### Extraction and errors

`extract_text` requires exactly one matching element (default `body`), checks the match count immediately, and bounds extraction to the smaller of `timeout_ms` and 3 seconds. Use `wait` explicitly for late content. `extract_links.selector` targets actual links, e.g. `main a[href]`, not the `main` container; multiple matches are expected. Returned links are visible and syntax-filtered (including literal private IP denial); hostname DNS policy is enforced on navigation, not by resolving every returned link.

Errors use `{"status":"error","code":"...","error":"fixed safe message"}` with MCP `isError=true`. Stable codes: `selector_not_found`, `selector_ambiguous`, `invalid_selector`, `extraction_timeout`, `invalid_session` (also expired/closed/cross-owner), `capability_denied`, `stale_control`, `unsupported_control`, `ambiguous_control`, `approval_expired`, `approval_stale`, `approval_unavailable`, `blocked_access`, `invalid_request`, `operation_timeout`, `operation_failed`, and `artifact_error`. No exception strings, selectors, option values, paths, or internal driver traces are copied into errors. Successful `close`/`cleanup_scope` retain `status: "closed"`.

## Caller authentication and capabilities

Loopback is necessary but insufficient. `/mcp` and artifact reads require `Authorization: Bearer ...`. Tokens are generated into mode-0600 files under the shared data root's `tokens/`; the active mode-0600 `clients.json` maps token files to caller IDs and capabilities. Secrets never appear in launchd plists, command lines, logs, health responses, or MCP results.

Capability tiers:

| Capability | Allows |
|---|---|
| `fetch` | `browser_fetch` |
| `inspect.read` | lifecycle, navigation, tabs, extraction, controls/elements discovery, wait, state, console |
| `inspect.controls` | restricted dropdown expansion/exact observed-option selection; also requires `inspect.read` |
| `inspect.confirmed` | prepare/execute/discard a frozen click/type/evaluate command; also requires `inspect.read` and trusted client confirmation mediation |
| `inspect.artifact` | screenshot/PDF creation and owner-bound retrieval |
| `inspect.interact` | direct click/type; type's extra Enter press requires `submit=true`, but clicks can activate forms |
| `inspect.script` | page evaluation |

Existing installer/example client policy is unchanged:

- Pi canary: `fetch`, `inspect.read`, `inspect.artifact`, `inspect.interact`, `inspect.script` (not automatically granted `inspect.controls`).
- My AI staging: `fetch`, `inspect.read`; no mutation grant.

An explicitly approved research client could use the capability list below in its private client configuration. This is an example, **not** an instruction to broaden existing Pi/My AI clients or tokens:

```json
["fetch", "inspect.read", "inspect.controls"]
```

The controls tier still denies raw `click`, `type`, `evaluate`, screenshots/PDFs (unless separately granted), upload, download, and credential APIs. A separately authorized My AI confirmation backend can be granted `["fetch", "inspect.read", "inspect.controls", "inspect.confirmed", "inspect.artifact"]`: this permits private confirmation mediation and artifacts but still denies raw `click`/`type`/`evaluate`. This implementation does not modify any runtime client policy.

Session IDs and artifact IDs are checked against the authenticated caller on every use. Authentication failures are generic and constant-time.

## Public-network enforcement

Input hostname checks are not the security boundary. Each browser session uses a worker-owned forward proxy and Chromium is launched with:

- the proxy as the only HTTP/HTTPS/WebSocket path,
- Chromium DNS disabled for destinations,
- proxy bypass disabled, including loopback,
- QUIC disabled,
- service workers blocked,
- non-proxied WebRTC disabled,
- downloads disabled.

For every HTTP request, HTTPS `CONNECT`, WebSocket upgrade, redirect target, and subresource connection, the proxy:

1. parses a credential-free `http(s)` destination,
2. resolves A/AAAA records with a bounded timeout,
3. rejects the destination if any answer is loopback, private, link-local, multicast, unspecified, reserved, or otherwise non-global,
4. connects directly to one validated IP rather than resolving the hostname again,
5. preserves the original hostname for HTTP `Host` and end-to-end TLS verification.

This closes DNS-rebinding and redirect/subresource gaps: validation and connection use the same selected address. Mixed public/private DNS answers fail closed. Browser-side `file:`, `data:` top-level navigation, `ftp:`, custom schemes, and credential-bearing URLs are rejected. `data:`/`blob:` subresources generated by an already-authorized page remain local to the renderer and cannot create network egress.

## Session and resource limits

Defaults, all bounded by hard caps:

- 4 live sessions globally
- 2 live sessions per caller
- 8 admitted operations globally
- 5-minute idle TTL
- 15-minute absolute TTL
- 60-second operation deadline
- 64 KiB proxy request-header cap
- 50,000 returned text characters
- 100 returned links
- 20 MiB screenshot and 50 MiB PDF artifact caps
- 30-minute artifact TTL and 100 MiB artifact quota per caller

A startup sweep removes orphaned session/profile directories. Close, expiration, cancellation, browser crashes, and shutdown all close Chromium/proxy resources and remove profiles. Admission fails quickly instead of allowing an unbounded queue.

## Artifact boundary

Callers cannot provide output paths. Screenshots and PDFs are written atomically beneath one owner-only worker artifact root with random IDs. Metadata stores owner, MIME type, size, hash, creation, and expiry. Retrieval is authenticated, owner-bound, `private, no-store`, `nosniff`, and attachment-safe. Symlinks, non-regular files, traversal, size/hash changes, and expired handles fail closed.

## Test matrix

1. Contract: exact two-tool inventory; JSON schemas; action-specific validation; bounded outputs.
2. Authentication: missing/bad tokens; mode checks; caller capability denials; generic errors.
3. Ownership: cross-caller session and artifact denial; restart invalidation.
4. Egress: literal private IPv4/IPv6, localhost aliases, mixed DNS, DNS re-resolution, HTTP redirects, HTTPS CONNECT, subresources, WebSockets, Chromium proxy bypass, and QUIC/WebRTC policy.
5. Lifecycle: idle/absolute TTL, per-caller/global limits, admission, cancellation, crash cleanup, startup orphan cleanup.
6. Artifacts: traversal, symlink, hardlink/replacement, quota, expiry, MIME, hash, and atomic write checks.
7. Browser behavior: rendered text, navigation, tabs, waits, screenshots/PDFs, console, interaction, downloads blocked, and no persistent cookies across sessions.
8. Operator: plist validation, private modes, exact listener/tool inventory, restart and health.
9. Confirmation: no-effect discovery/preparation, exact frozen text/script/JSON, explicit label submission, discard/expiry/replay, owner/session isolation, target/form/URL/document/tab changes, navigation race guards, cancellation cleanup, unchanged raw/artifact capabilities and public egress.

Network-dependent browser smokes are marked separately; the security suite uses injected resolvers/connectors and local fixtures so private-network denial cannot be disabled in production.

## Historical canary and cutover sequence

These gates describe the initial rollout, not current registration state. New control grants require their own explicit client-policy review.

1. Build and test with no client registration.
2. Raw authenticated MCP canary against the exact two-tool inventory.
3. Pi canary in an isolated `PI_CODING_AGENT_DIR` that does not load the current public `browser_*` extension. It registers only `browser_fetch` and `browser_inspect` wrappers.
4. My AI staging-only feature flag and read-only token. Production remains unchanged.
5. Run parity, security, concurrency, crash-recovery, and artifact gates.
6. Atomic Pi cutover: one reviewed pi-shared revision removes only the old public `src/index.ts` registration and adds the two MCP wrappers; `app-testing.ts` stays loaded. Never expose old and new public-browser tools in the same profile.
7. Atomic My AI promotion from the tested staging revision.
8. Roll back by selecting the prior configuration/revision and restarting. Do not add compatibility aliases or expose both tool families.
