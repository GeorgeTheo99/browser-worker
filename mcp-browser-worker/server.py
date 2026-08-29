from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.tools.tool import ToolResult
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from artifacts import ArtifactError, ArtifactStore
from auth import ClientRegistry, current_caller
from browser_runtime import BrowserManager, WorkerError
from config import (
    ARTIFACTS_DIR,
    CLIENTS_CONFIG,
    DATA_DIR,
    DEFAULT_TIMEOUT_MS,
    DEPLOY_REVISION,
    MAX_OPERATION_TIMEOUT_MS,
    MAX_TEXT_CHARS,
    ROOT_DIR,
)
from security import NetworkPolicyError

logger = logging.getLogger("browser-worker.mcp")

InspectAction = Literal[
    "open",
    "state",
    "close",
    "cleanup_scope",
    "navigate",
    "open_tab",
    "list_tabs",
    "switch_tab",
    "close_tab",
    "extract_text",
    "extract_links",
    "wait",
    "console",
    "screenshot",
    "export_pdf",
    "click",
    "type",
    "evaluate",
]
WaitUntil = Literal["load", "domcontentloaded", "networkidle", "commit"]
WaitState = Literal["attached", "detached", "visible", "hidden"]

registry = ClientRegistry(CLIENTS_CONFIG, DATA_DIR)
artifacts = ArtifactStore(ARTIFACTS_DIR)
manager = BrowserManager(artifacts)


def verify_release_receipt(revision: str, root: Path) -> None:
    if not revision:
        return
    receipt = root / ".browser-worker-revision"
    try:
        receipt_revision = receipt.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("browser-worker release receipt is unavailable") from exc
    if receipt_revision != revision:
        raise RuntimeError("browser-worker release receipt does not match deployment")


@asynccontextmanager
async def lifespan(_server: FastMCP):
    verify_release_receipt(DEPLOY_REVISION, ROOT_DIR)
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    registry.load()
    await artifacts.start()
    await manager.start()
    try:
        yield {}
    finally:
        await manager.shutdown()
        await artifacts.close()


mcp = FastMCP(
    "browser-worker",
    version="0.1.0",
    instructions=(
        "Rendered public-web retrieval and short-lived browser inspection. "
        "Use browser_fetch for a one-shot rendered page and browser_inspect for stateful actions."
    ),
    lifespan=lifespan,
    strict_input_validation=True,
    mask_error_details=True,
)


def _json_result(value: dict[str, Any]) -> ToolResult:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return ToolResult(content=text, structured_content=value)


def _error_result(message: str) -> ToolResult:
    value = {"status": "error", "error": message}
    return ToolResult(
        content=json.dumps(value, separators=(",", ":")),
        structured_content=value,
        is_error=True,
    )


def _bounded_timeout(value: int) -> int:
    if not 1_000 <= int(value) <= MAX_OPERATION_TIMEOUT_MS:
        raise WorkerError("timeout_ms must be between 1000 and 60000")
    return int(value)


def _bounded_text(value: str | None, *, name: str, maximum: int, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise WorkerError(f"{name} is required for this action")
        return None
    if not isinstance(value, str) or (required and not value) or len(value) > maximum:
        raise WorkerError(f"{name} is invalid or too long")
    return value


async def _safe_call(coro: Any, *, total_seconds: float = 90.0) -> ToolResult:
    try:
        async with asyncio.timeout(total_seconds):
            result = await coro
        return _json_result(result)
    except PermissionError:
        return _error_result("caller capability does not allow this operation")
    except (WorkerError, NetworkPolicyError, ArtifactError) as exc:
        return _error_result(str(exc))
    except TimeoutError:
        return _error_result("browser operation exceeded its total deadline")
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - MCP boundary returns a privacy-safe generic failure
        logger.error("browser operation failed: %s", type(exc).__name__)
        return _error_result("browser operation failed")


@mcp.tool()
async def browser_fetch(
    url: Annotated[str, Field(min_length=1, max_length=8192)],
    max_chars: Annotated[int, Field(ge=1_000, le=MAX_TEXT_CHARS)] = 20_000,
    wait_until: WaitUntil = "domcontentloaded",
    timeout_ms: Annotated[int, Field(ge=1_000, le=MAX_OPERATION_TIMEOUT_MS)] = DEFAULT_TIMEOUT_MS,
    include_links: bool = False,
    include_screenshot: bool = False,
) -> ToolResult:
    """Render one public page, return visible text, and destroy the browser profile before returning."""
    caller = current_caller()
    try:
        caller.require("fetch")
        if include_screenshot:
            caller.require("inspect.artifact")
    except PermissionError:
        return _error_result("caller capability does not allow this operation")
    return await _safe_call(
        manager.fetch(
            caller.id,
            url,
            max_chars=int(max_chars),
            wait_until=wait_until,
            timeout_ms=_bounded_timeout(timeout_ms),
            include_links=bool(include_links),
            include_screenshot=bool(include_screenshot),
        )
    )


@mcp.tool()
async def browser_inspect(
    action: InspectAction,
    session_id: Annotated[str | None, Field(max_length=128)] = None,
    scope_id: Annotated[str | None, Field(max_length=128)] = None,
    url: Annotated[str | None, Field(max_length=8192)] = None,
    selector: Annotated[str | None, Field(max_length=2000)] = None,
    text: Annotated[str | None, Field(max_length=20_000)] = None,
    tab_index: Annotated[int | None, Field(ge=0, le=100)] = None,
    timeout_ms: Annotated[int, Field(ge=1_000, le=MAX_OPERATION_TIMEOUT_MS)] = DEFAULT_TIMEOUT_MS,
    wait_until: WaitUntil = "domcontentloaded",
    state: WaitState = "visible",
    url_contains: Annotated[str | None, Field(max_length=2000)] = None,
    max_chars: Annotated[int, Field(ge=1_000, le=MAX_TEXT_CHARS)] = 20_000,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
    clear: bool = True,
    submit: bool = False,
    full_page: bool = True,
    format: Literal["A4", "Letter"] = "A4",
    landscape: bool = False,
    print_background: bool = True,
    script: Annotated[str | None, Field(max_length=20_000)] = None,
    arg: Any | None = None,
) -> ToolResult:
    """Create or operate a short-lived caller-owned browser session using one explicit action."""
    caller = current_caller()
    try:
        caller.require("inspect.read")
        timeout = _bounded_timeout(timeout_ms)
        if action == "open":
            if session_id is not None:
                raise WorkerError("session_id is not accepted for open")
            if url is not None:
                _bounded_text(url, name="url", maximum=8192, required=True)
            scope = _bounded_text(scope_id, name="scope_id", maximum=128)
            return await _safe_call(
                manager.open(
                    caller.id,
                    url,
                    wait_until=wait_until,
                    timeout_ms=timeout,
                    scope_id=scope,
                )
            )
        if action == "cleanup_scope":
            if session_id is not None:
                raise WorkerError("session_id is not accepted for cleanup_scope")
            scope = _bounded_text(
                scope_id, name="scope_id", maximum=128, required=True
            )
            assert scope is not None
            return await _safe_call(manager.cleanup_scope(caller.id, scope))
        sid = _bounded_text(session_id, name="session_id", maximum=128, required=True)
        assert sid is not None

        params: dict[str, Any] = {
            "timeout_ms": timeout,
            "wait_until": wait_until,
            "state": state,
            "max_chars": int(max_chars),
            "limit": int(limit),
            "clear": bool(clear),
            "submit": bool(submit),
            "full_page": bool(full_page),
            "format": format,
            "landscape": bool(landscape),
            "print_background": bool(print_background),
        }
        if action in {"navigate"}:
            params["url"] = _bounded_text(url, name="url", maximum=8192, required=True)
        elif action == "open_tab":
            params["url"] = _bounded_text(url, name="url", maximum=8192)
        elif action in {"switch_tab"}:
            if tab_index is None:
                raise WorkerError("tab_index is required for this action")
            params["tab_index"] = tab_index
        elif action == "close_tab":
            params["tab_index"] = tab_index
        elif action == "extract_text" or action == "extract_links":
            params["selector"] = _bounded_text(selector, name="selector", maximum=2000)
        elif action == "click":
            caller.require("inspect.interact")
            params["selector"] = _bounded_text(selector, name="selector", maximum=2000, required=True)
        elif action == "type":
            caller.require("inspect.interact")
            params["selector"] = _bounded_text(selector, name="selector", maximum=2000, required=True)
            params["text"] = _bounded_text(text, name="text", maximum=20_000, required=True)
        elif action == "wait":
            params["selector"] = _bounded_text(selector, name="selector", maximum=2000)
            params["url_contains"] = _bounded_text(url_contains, name="url_contains", maximum=2000)
        elif action in {"screenshot", "export_pdf"}:
            caller.require("inspect.artifact")
        elif action == "evaluate":
            caller.require("inspect.script")
            params["script"] = _bounded_text(script, name="script", maximum=20_000, required=True)
            try:
                json.dumps(arg, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise WorkerError("arg must be JSON serializable") from exc
            params["arg"] = arg
        return await _safe_call(manager.act(caller.id, sid, action, **params))
    except PermissionError:
        return _error_result("caller capability does not allow this operation")
    except WorkerError as exc:
        return _error_result(str(exc))


@mcp.custom_route("/live", methods=["GET"])
async def live(_request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "service": "browser-worker",
            "revision": DEPLOY_REVISION or "development",
        },
        headers={"Cache-Control": "no-store"},
    )


@mcp.custom_route("/ready", methods=["GET"])
async def ready(_request: Request) -> JSONResponse:
    available = registry.client_count > 0
    return JSONResponse(
        {"status": "ok" if available else "not_ready", "ready": available},
        status_code=200 if available else 503,
        headers={"Cache-Control": "no-store"},
    )


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "service": "browser-worker",
            "revision": DEPLOY_REVISION or "development",
            "ready": registry.client_count > 0,
            "tool_count": 2,
            "live_sessions": manager.live_sessions,
            "network_policy": "public-proxy-pinned",
        },
        headers={"Cache-Control": "no-store"},
    )


@mcp.custom_route("/artifacts/{artifact_id}", methods=["GET"])
async def artifact(request: Request) -> Response:
    values = request.headers.getlist("authorization")
    caller = registry.authenticate_header(values[0] if len(values) == 1 else None)
    if caller is None:
        return JSONResponse(
            {"error": "authentication required"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
        )
    try:
        caller.require("inspect.artifact")
        record, data = await artifacts.read(caller.id, request.path_params["artifact_id"])
    except (PermissionError, ArtifactError):
        return JSONResponse({"error": "artifact not found"}, status_code=404, headers={"Cache-Control": "no-store"})
    suffix = "png" if record.mime_type == "image/png" else "pdf"
    return Response(
        data,
        media_type=record.mime_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'attachment; filename="browser-artifact-{record.id}.{suffix}"',
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )


if __name__ == "__main__":
    raise SystemExit("Use http_server.py; stdio is intentionally disabled for caller-bound authentication")
