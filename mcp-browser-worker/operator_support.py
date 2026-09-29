"""Private local operator helpers. Never print token values or put them in argv."""

from __future__ import annotations

import argparse
import asyncio
import http.client
import json
import os
import secrets
import stat
import sys
import tempfile
from pathlib import Path

PI_CAPABILITIES = [
    "fetch",
    "inspect.read",
    "inspect.artifact",
    "inspect.interact",
    "inspect.script",
]


def private_dir(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        path.mkdir(parents=True, mode=0o700)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise ValueError(
            f"directory must be service-owned, private and not a symlink: {path}"
        )


def private_file(path: Path) -> None:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise ValueError(
            f"file must be service-owned, private and not a symlink: {path}"
        )


def production_client(data: Path):
    from auth import ClientRegistry

    registry = ClientRegistry(data / "clients.json", data)
    registry.load()
    config = json.loads((data / "clients.json").read_text())
    row = next((row for row in config["clients"] if row["id"] == "pi-production"), None)
    if row is None:
        raise ValueError(
            "pi-production is not configured; rerun browser-worker install"
        )
    if not {"fetch", "inspect.read"} <= set(row["capabilities"]):
        raise ValueError(
            "existing pi-production caller lacks fetch/inspect.read; review its policy explicitly"
        )
    return config, row, registry


def ensure_clients(data: Path) -> None:
    from auth import ClientRegistry

    private_dir(data)
    private_dir(data / "tokens")
    config_path = data / "clients.json"
    config = {"version": 1, "clients": []}
    registry = None
    if config_path.exists() or config_path.is_symlink():
        registry = ClientRegistry(config_path, data)
        registry.load()  # Validate before modifying anything; never repair unsafe input silently.
        config = json.loads(config_path.read_text())
        if any(row["id"] == "pi-production" for row in config["clients"]):
            production_client(data)
            print("Existing pi-production caller and token preserved")
            return
    relative = "tokens/pi-production"
    if any(row["token_file"] == relative for row in config["clients"]):
        raise ValueError(
            "production token path is assigned to another caller; review clients.json"
        )
    token_path = data / relative
    created = False
    if token_path.exists() or token_path.is_symlink():
        private_file(token_path)
        value = token_path.read_text().strip()
        if registry and registry.authenticate_header("Bearer " + value):
            raise ValueError(
                "production token would reuse another caller's credential; review it explicitly"
            )
    else:
        value = secrets.token_urlsafe(48)
        fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(value + "\n")
        created = True
    config["clients"].append(
        {"id": "pi-production", "token_file": relative, "capabilities": PI_CAPABILITIES}
    )
    fd, temporary = tempfile.mkstemp(prefix=".clients-", dir=data)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(config, stream, indent=2)
            stream.write("\n")
        # Uses the service's actual validation, including token uniqueness and modes.
        ClientRegistry(Path(temporary), data).load()
        os.replace(temporary, config_path)
    except BaseException:
        if created:
            token_path.unlink(missing_ok=True)
        raise
    finally:
        Path(temporary).unlink(missing_ok=True)
    print("Local pi-production caller provisioned; no external API key required")


async def browser_check(render: bool) -> None:
    from patchright.async_api import async_playwright

    from config import BROWSER_EXECUTABLE

    async with async_playwright() as driver:
        binary = Path(BROWSER_EXECUTABLE or driver.chromium.executable_path)
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise ValueError(
                "browser executable is missing/unusable; rerun browser-worker install"
            )
    if render:
        # Exercise the real worker launch/proxy/profile/cleanup code without any
        # remote page or credentials. No live worker state is used or deleted.
        from artifacts import ArtifactStore
        from browser_runtime import BrowserManager

        with tempfile.TemporaryDirectory(prefix="browser-worker-install-check-") as td:
            root = Path(td)
            artifacts = ArtifactStore(root / "artifacts")
            manager = BrowserManager(artifacts, sessions_dir=root / "sessions")
            await artifacts.start()
            await manager.start()
            try:
                opened = await manager.open(
                    "installer", None, wait_until="domcontentloaded", timeout_ms=10_000
                )
                result = await manager.act(
                    "installer",
                    str(opened["session_id"]),
                    "evaluate",
                    script="() => { document.body.textContent = 'BROWSER_WORKER_READY'; return document.body.innerText; }",
                )
                if result.get("result") != "BROWSER_WORKER_READY":
                    raise ValueError(
                        "browser did not render the local installation check"
                    )
                await manager.close("installer", str(opened["session_id"]))
                if manager.live_sessions:
                    raise ValueError(
                        "browser installation check did not clean up its session"
                    )
            finally:
                await manager.shutdown()
                await artifacts.close()
    print(
        "Browser launch/render verified" if render else "Browser executable available"
    )


def request(port: int, path: str, body=None, token=None):
    connection = http.client.HTTPConnection(
        "127.0.0.1",
        port,
        timeout=70 if body and body.get("method") == "tools/call" else 10,
    )
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        connection.request(
            "POST" if body else "GET",
            path,
            body=json.dumps(body) if body else None,
            headers=headers,
        )
        response = connection.getresponse()
        content = response.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise ValueError("oversized worker response")
        return response.status, json.loads(content)
    finally:
        connection.close()


def verify(data: Path, port: int, revision: str, smoke: bool) -> None:
    _, row, _ = production_client(data)
    token = (data / row["token_file"]).read_text().strip()
    status, health = request(port, "/health")
    if (
        status != 200
        or not isinstance(health, dict)
        or health.get("status") != "ok"
        or health.get("service") != "browser-worker"
        or health.get("ready") is not True
        or health.get("revision") != revision
    ):
        raise ValueError("live browser-worker revision mismatch or service not ready")
    body = {
        "jsonrpc": "2.0",
        "id": "verify-worker",
        "method": "tools/list",
        "params": {},
    }
    status, _ = request(port, "/mcp", body)
    if status != 401:
        raise ValueError("worker did not reject an unauthenticated MCP request")
    status, payload = request(port, "/mcp", body, token)
    result = payload.get("result") if isinstance(payload, dict) else None
    tools = result.get("tools") if isinstance(result, dict) else None
    names = (
        [tool.get("name") for tool in tools if isinstance(tool, dict)]
        if isinstance(tools, list)
        else []
    )
    if (
        status != 200
        or not isinstance(payload, dict)
        or payload.get("jsonrpc") != "2.0"
        or payload.get("id") != body["id"]
        or "error" in payload
        or not isinstance(tools, list)
        or len(tools) != 2
        or names.count("browser_fetch") != 1
        or names.count("browser_inspect") != 1
        or result.get("nextCursor") is not None
    ):
        raise ValueError("authenticated MCP inventory does not match browser-worker")
    print(
        "Authenticated pi-production inventory verified: browser_fetch,browser_inspect"
    )
    if smoke:
        body.update(
            method="tools/call",
            params={
                "name": "browser_fetch",
                "arguments": {
                    "url": "https://example.com",
                    "max_chars": 2000,
                    "timeout_ms": 30000,
                },
            },
        )
        status, payload = request(port, "/mcp", body, token)
        result = payload.get("result", {})
        if (
            status != 200
            or payload.get("jsonrpc") != "2.0"
            or payload.get("id") != body["id"]
            or "error" in payload
            or result.get("isError")
        ):
            raise ValueError(
                "public browser smoke failed; no security policy bypass or fallback attempted"
            )
        text = "\n".join(
            item["text"]
            for item in result.get("content", [])
            if item.get("type") == "text"
        )
        rendered = json.loads(text)
        if rendered.get("status") != "ok" or "Example Domain" not in rendered.get(
            "text", ""
        ):
            raise ValueError(
                "public browser smoke returned no expected rendered content"
            )
        print("Public browser smoke passed: Example Domain rendered")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("dirs")
    p.add_argument("paths", nargs="+", type=Path)
    p = commands.add_parser("files")
    p.add_argument("paths", nargs="+", type=Path)
    p = commands.add_parser("clients")
    p.add_argument("data", type=Path)
    p = commands.add_parser("browser")
    p.add_argument("--render", action="store_true")
    p = commands.add_parser("verify")
    p.add_argument("data", type=Path)
    p.add_argument("port", type=int)
    p.add_argument("revision")
    p.add_argument("--smoke", action="store_true")
    p = commands.add_parser("token-path")
    p.add_argument("data", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "dirs":
            for path in args.paths:
                private_dir(path)
        elif args.command == "files":
            for path in args.paths:
                private_file(path)
        elif args.command == "clients":
            ensure_clients(args.data)
        elif args.command == "browser":
            asyncio.run(asyncio.wait_for(browser_check(args.render), timeout=45))
        elif args.command == "verify":
            if not 1024 <= args.port <= 65535:
                raise ValueError("invalid browser-worker port")
            verify(args.data, args.port, args.revision, args.smoke)
        elif args.command == "token-path":
            _, row, _ = production_client(args.data)
            print(args.data / row["token_file"])
    except Exception as error:  # noqa: BLE001 - CLI boundary must fail rather than report readiness
        # Avoid HTTP bodies, request headers or token values in diagnostics.
        print(
            f"Browser-worker {args.command} failed: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
