# Atomic Cutover and Rollback

This is a future release runbook. The current state is an unregistered worker plus isolated canaries. Do not execute the production steps until every gate below is green and the user explicitly authorizes production cutover.

## Non-negotiable invariant

A Pi profile must never expose the legacy public `browser_open` / `browser_navigate` / related `browser_*` family at the same time as `browser_fetch` and `browser_inspect`. There is no compatibility alias or dual-registration window. `app_*` remains loaded.

My AI's independent `web_search` and `web_fetch` tools remain available because they are owned by `local_web_search`, not the retired Pi public-browser family. Browser-worker does not become their fallback or health dependency.

## Current canary receipts

- Worker canary release: `749522b18fb3c98edb671e9c3747bc6de0bf05b7`, loopback `127.0.0.1:8890`, exact tools `browser_fetch,browser_inspect`.
- Pi: isolated temporary `PI_CODING_AGENT_DIR`, explicit canary extension only; `browser_fetch` returned `Example Domain`.
- My AI staging revision after scoped lost-response cleanup: `84fb8c92e1957549cf75690a59fce8f8c366ea46`.
- My AI staging `browser_fetch`: completed and persisted a browser-worker receipt.
- My AI staging `browser_inspect`: completed `open → extract_text → close`; worker returned to zero live sessions.
- My AI production revision and Pi shared package are unchanged.

## Release gates

1. Worker unit/integration tests, Ruff, compile, shell syntax, and diff checks pass from the committed tree.
2. Public success plus private IPv4/IPv6, mixed-DNS, redirect, subresource, WebSocket, and proxy-bypass tests pass.
3. Caller ownership, read-only My AI denials, concurrency/admission, TTL, cancellation, crash/startup cleanup, artifact integrity/quota/expiry, and restart invalidation pass.
4. Worker `tools/list` is exactly `browser_fetch,browser_inspect`.
5. Pi isolated profile contains new tools and no legacy public-browser tools; `app_*` remains separately available in the intended production profile.
6. My AI staging exposes read-only schemas only and has no click/type/submit/evaluate/upload/download/credential surface.
7. My AI staging and production have separate client tokens and therefore separate worker owners.
8. Final independent reviewer returns release GO.

## Prepare production caller

Create a distinct owner-only token and add a distinct `my-ai-production` client to the worker's private `data/clients.json` with only:

```json
["fetch", "inspect.read"]
```

Do not reuse `my-ai-staging`. Restart the worker, verify exact tools, verify staging still works, and verify a production-token evaluation call is denied. Token values must remain only in mode-0600 files; plists contain paths, never token values.

## Pi hard cutover

Prepare one pi-shared commit that does all of the following atomically:

1. Adds the reviewed MCP wrapper for `browser_fetch` and `browser_inspect`.
2. Changes `extensions/pi-browser-capture/package.json` so `./src/index.ts` is no longer loaded.
3. Keeps `./src/app-testing.ts` loaded without behavior changes.
4. Updates shared instructions from the granular public `browser_*` family to the two-tool contract.
5. Adds inventory tests that fail if any legacy public-browser name and either new name coexist.

Test that exact commit in an isolated profile, then update/reload the production Pi package once. The expected public-browser inventory after reload is only:

```text
browser_fetch
browser_inspect
```

The separate `app_*` inventory is unchanged.

### Pi rollback

Create/reselect the prior known-good pi-shared revision and reload once. That revision restores the legacy public-browser entrypoint and removes the two worker wrappers. Never implement rollback by re-enabling the old entrypoint while retaining the new wrapper.

## My AI production cutover

Prepare a new commit on top of the tested staging revision that changes only production activation:

- Add `MY_AI_BROWSER_WORKER_ENABLED=1` to `com.local.my-ai.plist`.
- Add `MY_AI_BROWSER_WORKER_MCP_URL=http://127.0.0.1:8890/mcp`.
- Add `MY_AI_BROWSER_WORKER_MCP_TOKEN_FILE=<owner-only my-ai-production token path>`.
- Keep the production schema read-only.

Push the candidate to `refs/heads/staging`, run both tools through the exact deployed revision, and then promote that exact attested staging tip through the existing fast-forward promotion pipeline. Verify revision-bearing health, one production `browser_fetch`, one production `browser_inspect open→extract→close`, no forbidden tool schemas, and zero residual sessions. Purge all smoke conversations.

### My AI rollback

Stage and promote a revert/disable commit that removes the three production plist keys (or sets only `MY_AI_BROWSER_WORKER_ENABLED=0`) and restarts My AI. The code may remain dormant, but no browser-worker schema is advertised. Do not add legacy public-browser aliases. Revoke/remove the unused production worker token only after the disabled runtime is verified.

## Worker rollback

The worker is separately supervised. Client rollback does not require replacing it: once Pi/My AI disable their registrations, the idle worker can remain available for diagnosis or be stopped with:

```bash
scripts/browser-worker stop
```

To roll back a worker release while clients remain enabled, select the prior committed worker revision, run its locked install, restart, verify exact tool inventory, and repeat both caller canaries. Never route failures to `local_web_search`.
