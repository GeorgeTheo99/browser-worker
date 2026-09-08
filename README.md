# browser-worker

Standalone, loopback-only rendered browser MCP. It is independent of
`local_web_search` and of the Databricks web-search service, and exposes exactly:

- `browser_fetch` — one-shot rendered page retrieval
- `browser_inspect` — short-lived caller-owned browser sessions/actions

## Install

```bash
git clone https://github.com/GeorgeTheo99/browser-worker.git ~/local_code/browser-worker
cd ~/local_code/browser-worker
./install.sh
./scripts/browser-worker verify
./scripts/browser-worker verify --smoke   # opt-in public Example Domain fetch
```

Requires macOS, `uv`, Git, curl and launchd. The installer selects/provisions a
working Python **3.12** rather than relying on ambient `python3`, installs locked
Python dependencies, and provisions matching Patchright Chromium. Cached browser
binaries are reused; a missing cache is downloaded (potentially hundreds of MB).
Browser versions used by other applications are not garbage-collected.

Before selecting a release or stopping a running worker, installation validates a
real local browser launch/render/cleanup without a remote page. It then provisions
a distinct `pi-production` caller, starts the loopback service, and verifies revision,
authentication and exact MCP tool inventory. There is **no external API key to buy
or paste**: the local bearer token is generated into an owner-only file.

Existing caller registrations, capabilities and tokens are preserved. Unsafe
permissions, symlinks or malformed configuration fail without silent credential
replacement. Existing production capabilities are not automatically escalated.
An activation failure restores the previous release/plist and attempts to restart
the previous service; failures are never reported as readiness.

Source must be committed and clean. Releases are immutable, exact Git snapshots
under `~/srv/browser-worker/releases/<revision>`, selected through `current`.
Private data lives under `~/srv/browser-worker/shared`. The default endpoint is
`http://127.0.0.1:8890/mcp` and default Pi token is
`~/srv/browser-worker/shared/tokens/pi-production`. `/mcp` requires bearer auth;
`/live`, `/ready` and `/health` expose only bounded diagnostics. Operator verification
reads credentials inside Python, not through shell/curl arguments.

The Pi installer can wire the endpoint and token **path** automatically. Standalone
clients may use `BROWSER_WORKER_MCP_URL` / `BROWSER_WORKER_MCP_TOKEN_FILE` in their
client configuration. Do not copy credentials between machines or reuse canary
credentials as production identities.

## Configuration and recovery

- `BROWSER_WORKER_PORT` — default `8890`; saved custom ports are reused on later commands.
- `BROWSER_WORKER_DEPLOY_ROOT`, `BROWSER_WORKER_DATA_DIR` — dedicated absolute private directories.
- `BROWSER_WORKER_BROWSER_EXECUTABLE` — optional explicit browser executable; when set,
  the installer validates it rather than downloading a replacement.
- Run `scripts/browser-worker env` for resolved non-secret paths, including the actual
  production token path. Tokens are never printed.
- Retry installation to repair missing browser cache or bootstrap missing production
  credentials. Preserve/reconcile local source changes before retrying a dirty tree.
- An unsafe existing config must be reviewed explicitly; the installer will not
  change permissions on unrelated directories or replace existing credentials.
- Inspect `~/Library/Logs/browser-worker/browser-worker.log` for service failures.
- If a finalized release's Python environment is missing/corrupt, restore/rebuild
  that release before retrying; do not treat a metadata-only health response as proof
  that the browser works.

```bash
scripts/browser-worker install --no-start  # prepare only; no readiness claim
scripts/browser-worker start
scripts/browser-worker status
scripts/browser-worker logs
scripts/browser-worker restart
scripts/browser-worker stop
scripts/browser-worker uninstall          # retains source/private state
```

## Development

```bash
cd mcp-browser-worker
uv sync --locked
.venv/bin/ruff check .
.venv/bin/python -m pytest -q
.venv/bin/python operator_support.py browser --render  # real offline browser check
```

Operator tests use temporary homes and fake platform commands. Browser/security
suites retain their separate marked tests. See [architecture](docs/architecture.md)
for the egress and capability boundary, and [cutover](docs/cutover.md) for historical
canary receipts and separate My AI rollout/rollback gates.
