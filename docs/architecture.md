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

All schemas reject unknown fields. Limits are enforced server-side even if a client bypasses schema validation. Results are JSON serialized into MCP text content and contain no filesystem paths, credentials, cookies, profile data, or raw internal errors.

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
- Read-only inspection: `extract_text`, `extract_links`, `controls`, `wait`, `console`
- Restricted research interaction: `expand_control`, `select_option` (explicit `inspect.controls` grant)
- Artifacts: `screenshot`, `export_pdf`
- Interactive Pi capability: `click`, `type`
- Privileged Pi-only capability: `evaluate`

Common fields are optional at the JSON-Schema layer for model compatibility and strictly checked per action: `session_id`, `scope_id`, `url`, `selector`, `control_id`, `option`, `text`, `tab_index`, `timeout_ms`, `wait_until`, `state`, `url_contains`, `max_chars`, `limit`, `clear`, `submit`, `full_page`, `format`, `landscape`, `print_background`, `script`, and `arg`.

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

### Extraction and errors

`extract_text` requires exactly one matching element (default `body`), checks the match count immediately, and bounds extraction to the smaller of `timeout_ms` and 3 seconds. Use `wait` explicitly for late content. `extract_links.selector` targets actual links, e.g. `main a[href]`, not the `main` container; multiple matches are expected. Returned links are visible and syntax-filtered (including literal private IP denial); hostname DNS policy is enforced on navigation, not by resolving every returned link.

Errors use `{"status":"error","code":"...","error":"fixed safe message"}` with MCP `isError=true`. Stable codes: `selector_not_found`, `selector_ambiguous`, `invalid_selector`, `extraction_timeout`, `invalid_session` (also expired/closed/cross-owner), `capability_denied`, `stale_control`, `unsupported_control`, `ambiguous_control`, `blocked_access`, `invalid_request`, `operation_timeout`, `operation_failed`, and `artifact_error`. No exception strings, selectors, option values, paths, or internal driver traces are copied into errors. Successful `close`/`cleanup_scope` retain `status: "closed"`.

## Caller authentication and capabilities

Loopback is necessary but insufficient. `/mcp` and artifact reads require `Authorization: Bearer ...`. Tokens are generated into mode-0600 files under the shared data root's `tokens/`; the active mode-0600 `clients.json` maps token files to caller IDs and capabilities. Secrets never appear in launchd plists, command lines, logs, health responses, or MCP results.

Capability tiers:

| Capability | Allows |
|---|---|
| `fetch` | `browser_fetch` |
| `inspect.read` | lifecycle, navigation, tabs, extraction, controls discovery, wait, state, console |
| `inspect.controls` | restricted dropdown expansion/exact observed-option selection; also requires `inspect.read` |
| `inspect.artifact` | screenshot/PDF creation and owner-bound retrieval |
| `inspect.interact` | click/type; form submission still requires `submit=true` |
| `inspect.script` | page evaluation |

Existing installer/example client policy is unchanged:

- Pi canary: `fetch`, `inspect.read`, `inspect.artifact`, `inspect.interact`, `inspect.script` (not automatically granted `inspect.controls`).
- My AI staging: `fetch`, `inspect.read`; no mutation grant.

An explicitly approved research client could use the capability list below in its private client configuration. This is an example, **not** an instruction to broaden existing Pi/My AI clients or tokens:

```json
["fetch", "inspect.read", "inspect.controls"]
```

The new tier still denies `click`, `type`, `evaluate`, screenshots/PDFs (unless separately granted), upload, download, and credential APIs.

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
