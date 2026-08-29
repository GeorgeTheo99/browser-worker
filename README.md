# browser-worker

Standalone, loopback-only rendered browser MCP for Pi and My AI canaries.

It is intentionally independent of `local_web_search` and exposes exactly:

- `browser_fetch`
- `browser_inspect`

Production client registration is not part of the initial worker install. See [`docs/architecture.md`](docs/architecture.md) for the security contract and tool schemas, and [`docs/cutover.md`](docs/cutover.md) for the gated atomic cutover/rollback runbook.

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
