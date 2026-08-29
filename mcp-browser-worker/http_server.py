#!/usr/bin/env python3
from __future__ import annotations

import logging
import os
import urllib.parse
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse

from auth import BearerAuthMiddleware
from config import MCP_HOST, MCP_PORT
from server import mcp, registry

LOG_LEVEL = os.environ.get("BROWSER_WORKER_LOG_LEVEL", "INFO").upper()
_ALLOWED_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def _loopback_authority(value: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(f"//{value}")
        _ = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.hostname
        and parsed.hostname.rstrip(".").lower() in _ALLOWED_LOOPBACK_HOSTS
        and parsed.username is None
        and parsed.password is None
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
    )


def _loopback_origin(value: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(value)
        _ = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme.lower() in {"http", "https"}
        and parsed.hostname
        and parsed.hostname.rstrip(".").lower() in _ALLOWED_LOOPBACK_HOSTS
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )


class LoopbackRequestGuard:
    def __init__(self, app: Callable[..., Awaitable[Any]]) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers") or []
        hosts = [value.decode("latin-1") for name, value in headers if name.lower() == b"host"]
        origins = [value.decode("latin-1") for name, value in headers if name.lower() == b"origin"]
        if len(hosts) != 1 or not _loopback_authority(hosts[0]):
            await PlainTextResponse("loopback Host required", status_code=421)(scope, receive, send)
            return
        if len(origins) > 1 or (origins and not _loopback_origin(origins[0])):
            await PlainTextResponse("loopback Origin required", status_code=403)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def build_app():
    return mcp.http_app(
        transport="streamable-http",
        json_response=True,
        stateless_http=True,
        middleware=[Middleware(LoopbackRequestGuard), Middleware(BearerAuthMiddleware, registry=registry)],
    )


def main() -> None:
    import uvicorn

    uvicorn.run(
        build_app(),
        host=MCP_HOST,
        port=MCP_PORT,
        log_level=LOG_LEVEL.lower(),
        access_log=False,
    )


if __name__ == "__main__":
    main()
