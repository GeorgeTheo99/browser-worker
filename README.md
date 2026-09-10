# browser-worker

Standalone, loopback-only rendered browser MCP for Pi and My AI.

It is intentionally independent of `local_web_search` and exposes exactly:

- `browser_fetch`
- `browser_inspect`

Client registrations and capability grants are operator-managed. `browser_inspect` supports bounded dropdown discovery plus explicitly granted `inspect.controls` expansion/selection; existing client grants are not broadened automatically. A separate `inspect.confirmed` grant supports server-mediated, single-use human confirmation for frozen click/type/evaluate commands, with read-only `elements` discovery. The client backend must keep approval protocol actions out of its model schema and enforce explicit user consent; the worker does not itself authenticate a human approval. Pi's raw interaction/script grants and the separate artifact grant are unchanged. Page handlers may issue public requests: this is not a no-side-effects guarantee. See [`docs/architecture.md`](docs/architecture.md) for schemas, safe error codes, and the security contract, and [`docs/cutover.md`](docs/cutover.md) for the historical gated cutover/rollback runbook.

## Development

```bash
cd mcp-browser-worker
uv sync
uv run pytest -q
uv run python -m py_compile server.py http_server.py browser_runtime.py security.py auth.py artifacts.py
```

## Operator commands

```bash
scripts/browser-worker check
scripts/browser-worker install --no-start  # requires a clean committed tree
scripts/browser-worker start
scripts/browser-worker verify
scripts/browser-worker status
scripts/browser-worker logs
scripts/browser-worker stop
```

The MCP endpoint is `http://127.0.0.1:8890/mcp`. `/mcp` requires a configured bearer token; `/live`, `/ready`, and `/health` expose only bounded diagnostics. Installs create exact revision releases under `~/srv/browser-worker/releases/`, select `current` atomically, and keep private state under `~/srv/browser-worker/shared`.
