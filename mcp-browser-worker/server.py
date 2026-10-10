from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.tools.tool import ToolResult
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from approvals import MAX_ARG_BYTES
from artifacts import ArtifactError, ArtifactStore
from auth import Caller, ClientRegistry, current_caller
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
    VIEWPORT_HEIGHT,
    VIEWPORT_WIDTH,
)
from errors import ERROR_MESSAGES
from frames import FRAME_ACTIONS
from security import NetworkPolicyError, parse_public_url

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
    "controls",
    "elements",
    "frames",
    "prepare_action",
    "execute_prepared",
    "discard_prepared",
    "expand_control",
    "select_option",
    "wait",
    "console",
    "downloads",
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
        "Rendered public-web retrieval and caller-owned browser sessions. "
        "Use browser_fetch for a one-shot rendered page and browser_inspect for stateful actions. "
        "Callers granted durable profiles can open profile='default' to reuse one long-lived browser "
        "whose sign-ins persist; a person can watch and take over that browser through the live view."
    ),
    lifespan=lifespan,
    strict_input_validation=True,
    mask_error_details=True,
)


def _json_result(value: dict[str, Any]) -> ToolResult:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return ToolResult(content=text, structured_content=value)


def _error_result(code: str) -> ToolResult:
    code = code if code in ERROR_MESSAGES else "operation_failed"
    value = {"status": "error", "code": code, "error": ERROR_MESSAGES[code]}
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
        return _error_result("capability_denied")
    except WorkerError as exc:
        return _error_result(exc.code)
    except NetworkPolicyError:
        return _error_result("blocked_access")
    except ArtifactError:
        return _error_result("artifact_error")
    except TimeoutError:
        return _error_result("operation_timeout")
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - MCP boundary returns a privacy-safe generic failure
        logger.error("browser operation failed: %s", type(exc).__name__)
        return _error_result("operation_failed")


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
        return _error_result("capability_denied")
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
    frame_id: Annotated[str | None, Field(min_length=1, max_length=128)] = None,
    control_id: Annotated[str | None, Field(min_length=1, max_length=128)] = None,
    option: Annotated[str | None, Field(min_length=1, max_length=200)] = None,
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
    operation: Literal["click", "type", "evaluate"] | None = None,
    proposal_id: Annotated[str | None, Field(min_length=1, max_length=128)] = None,
    profile: Annotated[
        str | None, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    ] = None,
) -> ToolResult:
    """Create or operate a caller-owned browser session using one explicit action.

    `open` with `profile` reattaches to (or launches) a durable named browser profile whose
    cookies and sign-ins persist across sessions; without it, the session is ephemeral.
    `downloads` lists files the page downloaded, each retrievable as an artifact.
    """
    caller = current_caller()
    try:
        caller.require("inspect.read")
        timeout = _bounded_timeout(timeout_ms)
        if profile is not None:
            if action != "open":
                raise WorkerError("profile is only accepted for open")
            caller.require("inspect.persistent")
        if frame_id is not None and action not in FRAME_ACTIONS:
            raise WorkerError("frame_id is unsupported for this action")
        if frame_id is not None:
            _bounded_text(frame_id, name="frame_id", maximum=128, required=True)
        if action in {"prepare_action", "execute_prepared", "discard_prepared"}:
            caller.require("inspect.confirmed")
        elif operation is not None or proposal_id is not None:
            raise WorkerError("approval fields are only accepted for approval actions")
        if action in {"execute_prepared", "discard_prepared"}:
            if any(value is not None for value in (
                operation, scope_id, url, selector, control_id, option, text, tab_index,
                url_contains, script, arg,
            )) or (timeout_ms, wait_until, state, max_chars, limit, clear, submit, full_page,
                   format, landscape, print_background) != (
                       DEFAULT_TIMEOUT_MS, "domcontentloaded", "visible", 20_000, 50, True,
                       False, True, "A4", False, True,
                   ):
                raise WorkerError("terminal approval actions accept only session_id and proposal_id")
            sid = _bounded_text(session_id, name="session_id", maximum=128, required=True)
            pid = _bounded_text(proposal_id, name="proposal_id", maximum=128, required=True)
            return await _safe_call(manager.act(caller.id, sid, action, proposal_id=pid))
        if action == "prepare_action":
            if operation not in {"click", "type", "evaluate"} or proposal_id is not None:
                raise WorkerError("prepare_action requires an operation and no proposal_id")
            if any(value is not None for value in (scope_id, url, control_id, option, tab_index, url_contains)):
                raise WorkerError("prepare_action accepts only command fields")
            if operation != "evaluate" and (script is not None or arg is not None):
                raise WorkerError("script and arg require evaluate")
            if operation == "evaluate" and selector is not None:
                raise WorkerError("evaluate accepts no selector")
            if operation != "type" and (text is not None or submit or not clear):
                raise WorkerError("input fields require type")
        if action in {"controls", "expand_control", "select_option"}:
            if action != "controls":
                caller.require("inspect.controls")
            if any(value is not None for value in (selector, text, script, arg, url, tab_index, scope_id, url_contains)) or submit:
                raise WorkerError("dropdown actions accept no selectors, input, scripts, or navigation")
            if action == "controls" and (control_id is not None or option is not None):
                raise WorkerError("controls accepts no control_id or option")
            if action == "expand_control" and option is not None:
                raise WorkerError("expand_control accepts no option")
        elif control_id is not None or option is not None:
            raise WorkerError("dropdown fields are only accepted for dropdown actions")
        if action == "open":
            if session_id is not None:
                raise WorkerError("session_id is not accepted for open")
            if url is not None:
                _bounded_text(url, name="url", maximum=8192, required=True)
            scope = _bounded_text(scope_id, name="scope_id", maximum=128)
            if profile is not None and scope is not None:
                raise WorkerError("durable profiles are not scope-bound")
            return await _safe_call(
                manager.open(
                    caller.id,
                    url,
                    wait_until=wait_until,
                    timeout_ms=timeout,
                    scope_id=scope,
                    profile=profile,
                    downloads="inspect.download" in caller.capabilities,
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
        if frame_id is not None:
            params["frame_id"] = frame_id
        if action == "prepare_action":
            params["operation"] = operation
            if operation in {"click", "type"}:
                params["selector"] = _bounded_text(selector, name="selector", maximum=2000, required=True)
                if operation == "type":
                    params["text"] = _bounded_text(text, name="text", maximum=20_000, required=True)
            else:
                params["script"] = _bounded_text(script, name="script", maximum=20_000, required=True)
                try:
                    encoded = json.dumps(arg, allow_nan=False)
                    if len(encoded.encode("utf-8")) > MAX_ARG_BYTES:
                        raise ValueError("oversized argument")
                    params["arg"] = json.loads(encoded)
                except (TypeError, ValueError, RecursionError) as exc:
                    raise WorkerError("arg must be bounded JSON") from exc
        elif action in {"expand_control", "select_option"}:
            params["control_id"] = _bounded_text(control_id, name="control_id", maximum=128, required=True)
            if action == "select_option":
                params["option"] = _bounded_text(option, name="option", maximum=200, required=True)
        elif action in {"navigate"}:
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
        elif action == "downloads":
            caller.require("inspect.download")
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
        return _error_result("capability_denied")
    except WorkerError:
        return _error_result("invalid_request")


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
    if record.name is not None:
        filename = record.name
    else:
        filename = f"browser-artifact-{record.id}.{'png' if record.mime_type == 'image/png' else 'pdf'}"
    return Response(
        data,
        media_type=record.mime_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )


_LIVE_KEY = re.compile(r"(?:(?:Control|Shift|Alt|Meta)\+){0,3}(?:[A-Za-z0-9]{1,16}|[-=\[\];',./`\\])")
_NO_STORE = {"Cache-Control": "no-store"}


def _route_caller(request: Request, capability: str) -> Caller | JSONResponse:
    values = request.headers.getlist("authorization")
    caller = registry.authenticate_header(values[0] if len(values) == 1 else None)
    if caller is None:
        return JSONResponse(
            {"error": "authentication required"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer", **_NO_STORE},
        )
    try:
        caller.require(capability)
    except PermissionError:
        return JSONResponse({"error": ERROR_MESSAGES["capability_denied"]}, status_code=403, headers=_NO_STORE)
    return caller


def _route_error(exc: WorkerError) -> JSONResponse:
    status = {"invalid_session": 404, "session_busy": 409, "blocked_access": 403, "invalid_request": 400}.get(
        exc.code, 502
    )
    return JSONResponse(
        {"status": "error", "code": exc.code, "error": ERROR_MESSAGES[exc.code]}, status_code=status, headers=_NO_STORE
    )


def _coordinate(value: Any, maximum: int) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise WorkerError("coordinate is invalid", code="invalid_request")
    if not 0 <= value <= maximum:
        raise WorkerError("coordinate is outside the viewport", code="invalid_request")
    return float(value)


def _delta(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > 10_000:
        raise WorkerError("scroll delta is invalid", code="invalid_request")
    return float(value)


def parse_live_input(body: Any) -> dict[str, Any]:
    """Validate one human input event; coordinates are CSS pixels in the viewport."""
    if not isinstance(body, dict) or not isinstance(body.get("type"), str):
        raise WorkerError("input event is invalid", code="invalid_request")
    kind = body["type"]
    fields: dict[str, set[str]] = {
        "click": {"type", "x", "y", "button", "click_count"},
        "wheel": {"type", "x", "y", "dx", "dy"},
        "key": {"type", "key"},
        "text": {"type", "text"},
        "navigate": {"type", "url"},
        "back": {"type"},
        "forward": {"type"},
        "reload": {"type"},
    }
    if kind not in fields or not set(body) <= fields[kind]:
        raise WorkerError("input event is invalid", code="invalid_request")
    event: dict[str, Any] = {"type": kind}
    if kind in {"click", "wheel"}:
        event["x"] = _coordinate(body.get("x"), VIEWPORT_WIDTH)
        event["y"] = _coordinate(body.get("y"), VIEWPORT_HEIGHT)
    if kind == "click":
        button = body.get("button", "left")
        clicks = body.get("click_count", 1)
        if button not in {"left", "middle", "right"} or clicks not in {1, 2, 3} or isinstance(clicks, bool):
            raise WorkerError("click is invalid", code="invalid_request")
        event.update(button=button, click_count=clicks)
    elif kind == "wheel":
        event.update(dx=_delta(body.get("dx", 0)), dy=_delta(body.get("dy", 0)))
    elif kind == "key":
        key = body.get("key")
        if not isinstance(key, str) or not _LIVE_KEY.fullmatch(key):
            raise WorkerError("key is invalid", code="invalid_request")
        event["key"] = key
    elif kind == "text":
        text = body.get("text")
        if not isinstance(text, str) or not 1 <= len(text) <= 2000:
            raise WorkerError("text is invalid", code="invalid_request")
        event["text"] = text
    elif kind == "navigate":
        url = body.get("url")
        try:
            parse_public_url(url)
        except NetworkPolicyError as exc:
            raise WorkerError("URL is not public", code="invalid_request") from exc
        event["url"] = url
    return event


@mcp.custom_route("/sessions", methods=["GET"])
async def list_sessions(request: Request) -> JSONResponse:
    caller = _route_caller(request, "inspect.live")
    if isinstance(caller, JSONResponse):
        return caller
    return JSONResponse({"status": "ok", "sessions": manager.list_sessions(caller.id)}, headers=_NO_STORE)


@mcp.custom_route("/sessions/{session_id}/live", methods=["GET"])
async def live_view(request: Request) -> Response:
    caller = _route_caller(request, "inspect.live")
    if isinstance(caller, JSONResponse):
        return caller
    events = manager.live_events(caller.id, str(request.path_params["session_id"])[:128])
    try:
        first = await anext(events)
    except WorkerError as exc:
        return _route_error(exc)

    async def stream() -> Any:
        try:
            event: dict[str, Any] | None = first
            while event is not None:
                name = str(event.pop("event"))
                if name == "ping":
                    yield ": keepalive\n\n"
                else:
                    yield f"event: {name}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"
                if await request.is_disconnected():
                    break
                event = await anext(events, None)
        finally:
            await events.aclose()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no", "X-Content-Type-Options": "nosniff"},
    )


@mcp.custom_route("/sessions/{session_id}/input", methods=["POST"])
async def live_input(request: Request) -> JSONResponse:
    caller = _route_caller(request, "inspect.live")
    if isinstance(caller, JSONResponse):
        return caller
    raw = await request.body()
    try:
        if len(raw) > 16_384:
            raise WorkerError("input event is too large", code="invalid_request")
        try:
            body = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise WorkerError("input event is not JSON", code="invalid_request") from exc
        event = parse_live_input(body)
        result = await manager.human_input(caller.id, str(request.path_params["session_id"])[:128], event)
    except WorkerError as exc:
        return _route_error(exc)
    except NetworkPolicyError:
        return _route_error(WorkerError("blocked", code="blocked_access"))
    return JSONResponse(result, headers=_NO_STORE)


if __name__ == "__main__":
    raise SystemExit("Use http_server.py; stdio is intentionally disabled for caller-bound authentication")
