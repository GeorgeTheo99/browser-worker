# Development-only data placeholder

Installed runtime state lives under the owner-only `~/srv/browser-worker/shared` directory by default. Active `clients.json`, bearer tokens, ephemeral profiles, and artifacts are never tracked in Git.

A custom `BROWSER_WORKER_DATA_DIR` may point elsewhere, but it must be a service-owned mode-0700 directory. Never commit tokens, cookies, browser profiles, artifacts, or active client configuration.
