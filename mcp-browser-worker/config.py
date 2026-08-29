from __future__ import annotations

import os
from pathlib import Path


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


ROOT_DIR = Path(__file__).resolve().parent.parent
DEPLOY_REVISION = os.environ.get("BROWSER_WORKER_DEPLOY_REVISION", "").strip()
DATA_DIR = Path(
    os.environ.get("BROWSER_WORKER_DATA_DIR", str(Path.home() / "srv/browser-worker/shared"))
).expanduser().resolve()
CLIENTS_CONFIG = Path(
    os.environ.get("BROWSER_WORKER_CLIENTS_CONFIG", str(DATA_DIR / "clients.json"))
).expanduser().resolve()
SESSIONS_DIR = DATA_DIR / "sessions"
ARTIFACTS_DIR = DATA_DIR / "artifacts"

MCP_HOST = "127.0.0.1"
MCP_PORT = _bounded_int("BROWSER_WORKER_PORT", 8890, 1024, 65535)
HEADLESS = _flag("BROWSER_WORKER_HEADLESS", True)
BROWSER_EXECUTABLE = os.environ.get("BROWSER_WORKER_BROWSER_EXECUTABLE") or None
DEFAULT_TIMEOUT_MS = _bounded_int("BROWSER_WORKER_DEFAULT_TIMEOUT_MS", 30_000, 1_000, 60_000)
MAX_OPERATION_TIMEOUT_MS = 60_000
VIEWPORT_WIDTH = _bounded_int("BROWSER_WORKER_VIEWPORT_WIDTH", 1440, 320, 3840)
VIEWPORT_HEIGHT = _bounded_int("BROWSER_WORKER_VIEWPORT_HEIGHT", 900, 240, 2160)

MAX_GLOBAL_SESSIONS = _bounded_int("BROWSER_WORKER_MAX_GLOBAL_SESSIONS", 4, 1, 8)
MAX_CALLER_SESSIONS = _bounded_int("BROWSER_WORKER_MAX_CALLER_SESSIONS", 2, 1, 4)
MAX_ADMITTED_OPERATIONS = _bounded_int("BROWSER_WORKER_MAX_ADMITTED_OPERATIONS", 8, 1, 32)
MAX_CONCURRENT_OPERATIONS = _bounded_int("BROWSER_WORKER_MAX_CONCURRENT_OPERATIONS", 4, 1, 8)
IDLE_TTL_SECONDS = _bounded_int("BROWSER_WORKER_IDLE_TTL_SECONDS", 300, 30, 900)
ABSOLUTE_TTL_SECONDS = _bounded_int("BROWSER_WORKER_ABSOLUTE_TTL_SECONDS", 900, 60, 3600)

MAX_TEXT_CHARS = 50_000
MAX_LINKS = 100
MAX_TABS_PER_SESSION = 8
MAX_CAPTURE_PIXELS = 40_000_000
MAX_CAPTURE_HEIGHT = 50_000
MAX_PROXY_HEADER_BYTES = 64 * 1024
MAX_PROXY_STREAM_BYTES = 64 * 1024 * 1024
DNS_TIMEOUT_SECONDS = 8
ALLOWED_DESTINATION_PORTS = frozenset({80, 443})

ARTIFACT_TTL_SECONDS = _bounded_int("BROWSER_WORKER_ARTIFACT_TTL_SECONDS", 1800, 60, 3600)
MAX_SCREENSHOT_BYTES = 20 * 1024 * 1024
MAX_PDF_BYTES = 50 * 1024 * 1024
MAX_CALLER_ARTIFACT_BYTES = 100 * 1024 * 1024
