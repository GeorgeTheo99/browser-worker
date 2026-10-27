from __future__ import annotations

import asyncio
import importlib.util
import itertools
import json
import shutil
import sys
import tempfile
from pathlib import Path
from types import ModuleType

import pytest

HOST = Path(__file__).resolve().parents[2] / "vm" / "host.py"


def load_host(monkeypatch: pytest.MonkeyPatch, data_dir: Path, **env: str) -> ModuleType:
    monkeypatch.setenv("BROWSER_WORKER_VM_IMAGE", "browser-worker-vm:abc")
    monkeypatch.setenv("BROWSER_WORKER_DATA_DIR", str(data_dir))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    spec = importlib.util.spec_from_file_location("vm_host", HOST)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_vm_command_shares_only_the_socket_tokens_and_volume(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("BROWSER_WORKER_HEADLESS", raising=False)
    host = load_host(monkeypatch, tmp_path, BROWSER_WORKER_LOG_LEVEL="DEBUG", BROWSER_WORKER_IDLE_TTL_SECONDS="60")
    monkeypatch.setattr(host.os.path, "realpath", lambda _path: "/var/db/timezone/zoneinfo/America/New_York")
    command = host.vm_command()
    assert command[:5] == [host.CONTAINER, "run", "--rm", "--name", "browser-worker"]
    assert command[-1] == "browser-worker-vm:abc"
    pairs = list(itertools.pairwise(command))
    assert ("--publish-socket", f"{tmp_path}/run/worker.sock:/var/lib/browser-worker/worker.sock") in pairs
    assert ("--mount", f"type=bind,source={tmp_path},target=/auth,readonly") in pairs
    assert ("--volume", "browser-worker-data:/data") in pairs
    assert ("--cpus", "7") in pairs
    assert ("--env", "TZ=America/New_York") in pairs
    assert ("--env", "BROWSER_WORKER_LOG_LEVEL=DEBUG") in pairs
    # Only the listed settings cross into the VM.
    assert not any("IDLE_TTL" in part for part in command)
    assert "--publish" not in command and "-p" not in command


def test_a_shorter_socket_path_can_be_given(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    host = load_host(monkeypatch, tmp_path / "long data path", BROWSER_WORKER_VM_SOCKET="/tmp/app/w.sock")
    assert ("--publish-socket", "/tmp/app/w.sock:/var/lib/browser-worker/worker.sock") in list(
        itertools.pairwise(host.vm_command())
    )


def test_vm_command_without_a_known_time_zone(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    host = load_host(monkeypatch, tmp_path)
    monkeypatch.setattr(host.os.path, "realpath", lambda _path: "/etc/localtime")
    assert host.mac_time_zone() is None
    assert not any(part.startswith("TZ=") for part in host.vm_command())


# The VM: a process serving the worker's socket that echoes one line, as a stand-in for the worker.
FAKE_VM = """
import asyncio, pathlib, sys
# Like container, the socket accepts before the worker is ready: /live answers only once it is.
READY = pathlib.Path(sys.argv[1] + ".ready")
async def echo(reader, writer):
    line = await reader.readline()
    if line.startswith(b"GET /live "):  # host.py's readiness check
        writer.write(b"HTTP/1.1 200 OK\\r\\n\\r\\n" if READY.exists() else b"")
    elif line:
        writer.write(b"worker:" + line)
    await writer.drain()
    writer.close()
async def main():
    server = await asyncio.start_unix_server(echo, sys.argv[1])
    await asyncio.Event().wait()
asyncio.run(main())
"""


@pytest.fixture
def short_data_dir():
    # A short directory: macOS caps a Unix socket's path at 104 bytes.
    data_dir = Path(tempfile.mkdtemp(prefix="bw-", dir="/tmp"))
    yield data_dir
    shutil.rmtree(data_dir)


def fake_vm(monkeypatch: pytest.MonkeyPatch, host: ModuleType) -> list[tuple[str, ...]]:
    """Run FAKE_VM for the VM, and record the container commands host.py runs besides it."""
    commands: list[tuple[str, ...]] = []
    host.SOCKET.parent.mkdir()
    monkeypatch.setattr(host, "vm_command", lambda: [sys.executable, "-c", FAKE_VM, str(host.SOCKET)])
    Path(f"{host.SOCKET}.ready").touch()

    async def run(*args: str) -> int:
        commands.append(args)
        return 0

    monkeypatch.setattr(host, "run", run)
    return commands


async def ask(port: int) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"hello\n")
    await writer.drain()
    answer = await asyncio.wait_for(reader.read(), 10)
    writer.close()
    return answer


@pytest.mark.asyncio
async def test_the_vm_starts_on_first_use_and_stops_when_idle(
    monkeypatch: pytest.MonkeyPatch, short_data_dir: Path
) -> None:
    host_module = load_host(monkeypatch, short_data_dir, BROWSER_WORKER_VM_IDLE_MINUTES="30")
    commands = fake_vm(monkeypatch, host_module)
    host = host_module.Host()
    server = await asyncio.start_server(host.forward, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        assert not host.running()
        assert await ask(port) == b"worker:hello\n"
        assert host.running()
        assert commands == [("rm", "--force", "browser-worker")]
        # A second connection uses the running VM.
        assert await ask(port) == b"worker:hello\n"
        assert len(commands) == 1
        while host.open:  # the forward finishes just after the client has its answer
            await asyncio.sleep(0.01)

        assert not host.idle()
        host.last_used -= 30 * 60
        assert host.idle()
        # Not while a connection is open (a live view, a request in flight).
        host.open += 1
        assert not host.idle()
        host.open -= 1

        vm = host.vm
        monkeypatch.setattr(host_module, "STOP_SECONDS", 0)

        async def stop_vm(*args: str) -> int:
            commands.append(args)
            if args[0] == "stop":
                vm.terminate()
            return 0

        monkeypatch.setattr(host_module, "run", stop_vm)
        await host.stop()
        assert commands[-1] == ("stop", "--time", "0", "browser-worker")
        assert not host.running() and not host.idle()
        # The next connection starts it again.
        assert await ask(port) == b"worker:hello\n"
        assert host.running()
    finally:
        if host.running():
            host.vm.kill()
            await host.vm.wait()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_requests_wait_until_the_worker_in_the_vm_answers(
    monkeypatch: pytest.MonkeyPatch, short_data_dir: Path
) -> None:
    host_module = load_host(monkeypatch, short_data_dir)
    fake_vm(monkeypatch, host_module)
    ready = Path(f"{host_module.SOCKET}.ready")
    ready.unlink()
    host = host_module.Host()
    server = await asyncio.start_server(host.forward, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        answer = asyncio.create_task(ask(port))
        await asyncio.sleep(1)
        assert host.running() and not answer.done()  # the socket accepts, but the worker isn't up yet
        ready.touch()
        assert await answer == b"worker:hello\n"
    finally:
        host.vm.kill()
        await host.vm.wait()
        server.close()
        await server.wait_closed()


async def request(port: int, text: str) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(text.encode())
    await writer.drain()
    response = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    return response


@pytest.mark.asyncio
async def test_a_health_check_doesnt_start_the_vm(monkeypatch: pytest.MonkeyPatch, short_data_dir: Path) -> None:
    host_module = load_host(monkeypatch, short_data_dir, BROWSER_WORKER_DEPLOY_REVISION="abc123")
    commands = fake_vm(monkeypatch, host_module)
    host = host_module.Host()
    server = await asyncio.start_server(host.forward, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        response = await request(port, "GET /health HTTP/1.1\r\nHost: 127.0.0.1:8893\r\n\r\n")
        head, _, body = response.partition(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 200 OK")
        assert json.loads(body) == {"status": "ok", "service": "browser-worker", "revision": "abc123",
                                    "ready": True, "vm": "stopped", "live_sessions": 0}
        # As the worker's own guard: a page that rebinds a name to 127.0.0.1 can't read it.
        rebound = await request(port, "GET /health HTTP/1.1\r\nHost: evil.example:8893\r\n\r\n")
        assert rebound.startswith(b"HTTP/1.1 421")
        foreign = await request(port, "GET /health HTTP/1.1\r\nHost: localhost:8893\r\nOrigin: https://evil.example\r\n\r\n")
        assert foreign.startswith(b"HTTP/1.1 403")
        # A connection that sends nothing starts nothing.
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write_eof()
        assert await asyncio.wait_for(reader.read(), 5) == b""
        writer.close()
        assert not host.running() and commands == []
        # Anything else starts it, /live too: the installer's liveness wait gives a starting VM its time.
        assert await ask(port) == b"worker:hello\n"
        assert host.running()
        # Not while it's shutting down.
        host.stopping = True
        assert await ask(port) == b""
    finally:
        if host.running():
            host.vm.kill()
            await host.vm.wait()
        server.close()
        await server.wait_closed()


def test_a_bad_idle_setting_keeps_the_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert load_host(monkeypatch, tmp_path, BROWSER_WORKER_VM_IDLE_MINUTES="soon").IDLE_SECONDS == 30 * 60
    assert load_host(monkeypatch, tmp_path, BROWSER_WORKER_VM_IDLE_MINUTES="5").IDLE_SECONDS == 5 * 60


def test_idle_minutes_zero_keeps_the_vm_running(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    host_module = load_host(monkeypatch, tmp_path, BROWSER_WORKER_VM_IDLE_MINUTES="0")
    host = host_module.Host()
    monkeypatch.setattr(host, "running", lambda: True)
    host.last_used -= 24 * 3600
    assert not host.idle()


@pytest.mark.asyncio
async def test_a_vm_that_does_not_start_closes_the_connection(
    monkeypatch: pytest.MonkeyPatch, short_data_dir: Path
) -> None:
    host_module = load_host(monkeypatch, short_data_dir)
    fake_vm(monkeypatch, host_module)
    monkeypatch.setattr(host_module, "vm_command", lambda: [sys.executable, "-c", "raise SystemExit(3)"])
    host = host_module.Host()
    server = await asyncio.start_server(host.forward, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        assert await ask(port) == b""
        assert not host.running() and host.open == 0
    finally:
        server.close()
        await server.wait_closed()
