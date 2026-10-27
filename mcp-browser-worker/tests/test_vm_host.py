from __future__ import annotations

import asyncio
import importlib.util
import itertools
import shutil
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


def test_vm_command_without_a_known_time_zone(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    host = load_host(monkeypatch, tmp_path)
    monkeypatch.setattr(host.os.path, "realpath", lambda _path: "/etc/localtime")
    assert host.mac_time_zone() is None
    assert not any(part.startswith("TZ=") for part in host.vm_command())


@pytest.mark.asyncio
async def test_forward_connects_loopback_to_the_vm_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    # A short directory: macOS caps a Unix socket's path at 104 bytes.
    data_dir = Path(tempfile.mkdtemp(prefix="bw-", dir="/tmp"))
    try:
        host = load_host(monkeypatch, data_dir)
        host.SOCKET.parent.mkdir()

        async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(b"worker:" + await reader.readline())
            await writer.drain()
            writer.close()

        worker = await asyncio.start_unix_server(echo, str(host.SOCKET))
        server = await asyncio.start_server(host.forward, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"hello\n")
            await writer.drain()
            assert await asyncio.wait_for(reader.read(), 5) == b"worker:hello\n"
            writer.close()

            # While the VM is still starting (no socket yet), a connection is closed, not left hanging.
            worker.close()
            await worker.wait_closed()
            host.SOCKET.unlink(missing_ok=True)
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            assert await asyncio.wait_for(reader.read(), 5) == b""
            writer.close()
        finally:
            server.close()
            await server.wait_closed()
    finally:
        shutil.rmtree(data_dir)
