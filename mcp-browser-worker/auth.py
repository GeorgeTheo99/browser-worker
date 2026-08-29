from __future__ import annotations

import contextvars
import hmac
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from starlette.responses import JSONResponse

VALID_CAPABILITIES = frozenset(
    {"fetch", "inspect.read", "inspect.artifact", "inspect.interact", "inspect.script"}
)


class AuthConfigError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Caller:
    id: str
    capabilities: frozenset[str]

    def require(self, capability: str) -> None:
        if capability not in self.capabilities:
            raise PermissionError("caller capability does not allow this operation")


@dataclass(frozen=True, slots=True)
class _ClientSecret:
    caller: Caller
    token: str


_current_caller: contextvars.ContextVar[Caller | None] = contextvars.ContextVar(
    "browser_worker_caller", default=None
)


def current_caller() -> Caller:
    caller = _current_caller.get()
    if caller is None:
        raise PermissionError("authenticated caller required")
    return caller


def _private_regular_file(path: Path, *, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise AuthConfigError(f"{label} is unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise AuthConfigError(f"{label} must be a regular file")
    if info.st_uid != os.getuid():
        raise AuthConfigError(f"{label} must be owned by the service user")
    if info.st_mode & 0o077:
        raise AuthConfigError(f"{label} must not be group/world accessible")
    return info


class ClientRegistry:
    def __init__(self, config_path: Path, data_dir: Path) -> None:
        self.config_path = config_path
        self.data_dir = data_dir
        self._clients: tuple[_ClientSecret, ...] = ()

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def load(self) -> None:
        try:
            data_info = self.data_dir.lstat()
        except OSError as exc:
            raise AuthConfigError("private data directory is unavailable") from exc
        if (
            not stat.S_ISDIR(data_info.st_mode)
            or stat.S_ISLNK(data_info.st_mode)
            or data_info.st_uid != os.getuid()
            or data_info.st_mode & 0o077
        ):
            raise AuthConfigError("private data directory must be owner-only and owned by the service user")
        _private_regular_file(self.config_path, label="client configuration")
        try:
            raw = self.config_path.read_text(encoding="utf-8")
            document = json.loads(raw)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AuthConfigError("client configuration is not valid JSON") from exc
        if not isinstance(document, dict) or document.get("version") != 1:
            raise AuthConfigError("client configuration version must be 1")
        rows = document.get("clients")
        if not isinstance(rows, list) or not rows:
            raise AuthConfigError("client configuration must contain at least one client")

        clients: list[_ClientSecret] = []
        ids: set[str] = set()
        tokens: set[str] = set()
        data_root = self.data_dir.resolve()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"id", "token_file", "capabilities"}:
                raise AuthConfigError("each client must contain exactly id, token_file, and capabilities")
            client_id = row["id"]
            token_file = row["token_file"]
            capabilities = row["capabilities"]
            if (
                not isinstance(client_id, str)
                or not client_id
                or len(client_id) > 64
                or not all(char.isalnum() or char in "-_" for char in client_id)
                or client_id in ids
            ):
                raise AuthConfigError("client IDs must be unique safe identifiers")
            token_parts = Path(token_file).parts if isinstance(token_file, str) else ()
            if (
                not isinstance(token_file, str)
                or not token_file
                or Path(token_file).is_absolute()
                or not token_parts
                or any(part in {"", ".", ".."} for part in token_parts)
            ):
                raise AuthConfigError("token_file must be a safe relative path")
            token_path = self.data_dir.joinpath(*token_parts)
            if token_path == data_root or data_root not in token_path.parents:
                raise AuthConfigError("token_file escapes the private data directory")
            current = self.data_dir
            for part in token_parts[:-1]:
                current = current / part
                try:
                    parent_info = current.lstat()
                except OSError as exc:
                    raise AuthConfigError(f"token directory for {client_id} is unavailable") from exc
                if (
                    not stat.S_ISDIR(parent_info.st_mode)
                    or stat.S_ISLNK(parent_info.st_mode)
                    or parent_info.st_uid != os.getuid()
                    or parent_info.st_mode & 0o077
                ):
                    raise AuthConfigError(f"token directory for {client_id} must be owner-only")
            _private_regular_file(token_path, label=f"token for {client_id}")
            try:
                token = token_path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError) as exc:
                raise AuthConfigError(f"token for {client_id} is unreadable") from exc
            if not 32 <= len(token) <= 256 or any(char.isspace() for char in token):
                raise AuthConfigError(f"token for {client_id} must be one 32..256 character value")
            if token in tokens:
                raise AuthConfigError("client tokens must be unique")
            if (
                not isinstance(capabilities, list)
                or not capabilities
                or not all(isinstance(value, str) for value in capabilities)
            ):
                raise AuthConfigError(f"capabilities for {client_id} must be a nonempty string list")
            capability_set = frozenset(capabilities)
            if len(capability_set) != len(capabilities) or not capability_set <= VALID_CAPABILITIES:
                raise AuthConfigError(f"capabilities for {client_id} are invalid or duplicated")
            ids.add(client_id)
            tokens.add(token)
            clients.append(_ClientSecret(Caller(client_id, capability_set), token))
        self._clients = tuple(clients)

    def authenticate_header(self, header: str | None) -> Caller | None:
        if not header or not header.startswith("Bearer "):
            return None
        supplied = header[7:].strip()
        if not supplied or any(char.isspace() for char in supplied):
            return None
        match: Caller | None = None
        for client in self._clients:
            if hmac.compare_digest(client.token, supplied):
                match = client.caller
        return match


class BearerAuthMiddleware:
    """Authenticate MCP requests and bind the caller to this async context."""

    def __init__(self, app: Any, registry: ClientRegistry) -> None:
        self.app = app
        self.registry = registry

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("path") != "/mcp":
            await self.app(scope, receive, send)
            return
        values = [
            value.decode("latin-1")
            for name, value in (scope.get("headers") or [])
            if name.lower() == b"authorization"
        ]
        caller = self.registry.authenticate_header(values[0] if len(values) == 1 else None)
        if caller is None:
            await JSONResponse(
                {"error": "authentication required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
            )(scope, receive, send)
            return
        scope["browser_worker.caller"] = caller
        token = _current_caller.set(caller)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_caller.reset(token)
