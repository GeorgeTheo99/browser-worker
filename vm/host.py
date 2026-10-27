#!/usr/bin/env python3
"""Run browser-worker in a Linux VM (Apple `container`) and serve it on the Mac's loopback.

Chromium runs windowed on the VM's virtual display, so nothing shows on the Mac's screen or plays from
its speakers. The VM publishes the worker's Unix socket to the Mac, and this forwards
127.0.0.1:BROWSER_WORKER_PORT to it: a published TCP port would need macOS's Local Network permission
for `container`'s helper, which a Mac without anyone at it can't grant (apple/container#2029).

The VM starts on the first connection and stops after BROWSER_WORKER_VM_IDLE_MINUTES (30; 0 keeps it
running) with no connection open, which gives its memory back to the Mac. launchd keeps this running; it
stops the VM when it's told to stop. Standard library only: it runs on the installer's Python, not the
release's environment.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
import time
import urllib.parse
from pathlib import Path

CONTAINER = os.environ.get("BROWSER_WORKER_CONTAINER_CLI", "/usr/local/bin/container")
NAME = os.environ.get("BROWSER_WORKER_VM_NAME", "browser-worker")
IMAGE = os.environ["BROWSER_WORKER_VM_IMAGE"]
VOLUME = os.environ.get("BROWSER_WORKER_VM_VOLUME", "browser-worker-data")
# The guest sees one CPU more than this: 8, a count real computers have (odd counts are rare).
CPUS = os.environ.get("BROWSER_WORKER_VM_CPUS", "7")
MEMORY = os.environ.get("BROWSER_WORKER_VM_MEMORY", "6g")
PORT = int(os.environ.get("BROWSER_WORKER_PORT", "8890"))
# The Mac keeps the client registry and tokens here; the VM gets them read-only.
DATA_DIR = Path(os.environ["BROWSER_WORKER_DATA_DIR"])
# macOS caps a Unix socket's path at 104 bytes: an app with a long data path gives a shorter one.
SOCKET = Path(os.environ.get("BROWSER_WORKER_VM_SOCKET") or DATA_DIR / "run" / "worker.sock")
GUEST_SOCKET = "/var/lib/browser-worker/worker.sock"
# Settings the worker reads, passed into the VM as they are.
PASSED_SETTINGS = (
    "BROWSER_WORKER_LOG_LEVEL",
    "BROWSER_WORKER_HEADLESS",
    "BROWSER_WORKER_DEFAULT_TIMEOUT_MS",
    "BROWSER_WORKER_VIEWPORT_WIDTH",
    "BROWSER_WORKER_VIEWPORT_HEIGHT",
    "BROWSER_WORKER_MAX_GLOBAL_SESSIONS",
    "BROWSER_WORKER_MAX_CALLER_SESSIONS",
    "BROWSER_WORKER_LIVE_FRAME_QUALITY",
)
STOP_SECONDS = 20
# How long a connection waits for a starting VM's worker to accept it.
START_SECONDS = 90
# A setting that isn't a whole number of minutes keeps the default rather than stopping the service.
_IDLE_MINUTES = os.environ.get("BROWSER_WORKER_VM_IDLE_MINUTES", "30").strip()
IDLE_SECONDS = (int(_IDLE_MINUTES) if _IDLE_MINUTES.isdigit() else 30) * 60
IDLE_CHECK_SECONDS = 30
REVISION = os.environ.get("BROWSER_WORKER_DEPLOY_REVISION", "")
# While the VM is stopped, this answers a health check itself: one shouldn't start a 6 GB VM. Not /live:
# the installer's liveness wait is what gives a starting VM its time.
HEALTH_REQUEST = b"GET /health "
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def log(message: str) -> None:
    print(f"browser-worker host: {message}", file=sys.stderr, flush=True)


async def run(*args: str) -> int:
    process = await asyncio.create_subprocess_exec(
        CONTAINER, *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        return await asyncio.wait_for(process.wait(), 60)
    except TimeoutError:  # a wedged container service
        process.kill()
        return await process.wait()


def mac_time_zone() -> str | None:
    """The Mac's time zone (America/New_York), which the VM's browser shows sites: one that doesn't match
    the IP address's location is a bot signal."""
    target = os.path.realpath("/etc/localtime")
    _, found, name = target.partition("/zoneinfo/")
    return name if found and name else None


def vm_command() -> list[str]:
    command = [
        CONTAINER, "run", "--rm", "--name", NAME,
        "--cpus", CPUS, "--memory", MEMORY, "--shm-size", "1g",
        "--publish-socket", f"{SOCKET}:{GUEST_SOCKET}",
        "--mount", f"type=bind,source={DATA_DIR},target=/auth,readonly",
        "--volume", f"{VOLUME}:/data",
    ]
    if zone := mac_time_zone():
        command += ["--env", f"TZ={zone}"]
    for key in PASSED_SETTINGS:
        if key in os.environ:
            command += ["--env", f"{key}={os.environ[key]}"]
    return [*command, IMAGE]


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except (ConnectionError, OSError):
        pass


async def worker_live() -> bool:
    """The worker answers /live. `container` accepts on the published socket before the worker in the VM
    listens, and then closes the connection, so connecting isn't enough."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(SOCKET)), 2)
    except (OSError, TimeoutError):
        return False
    try:
        writer.write(b"GET /live HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        await writer.drain()
        status = await asyncio.wait_for(reader.readline(), 2)
        return status.startswith(b"HTTP/1.1 200")
    except (OSError, TimeoutError):
        return False
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


class Host:
    """The VM, started on demand and stopped when idle, and the connections forwarded to it."""

    def __init__(self) -> None:
        self.vm: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self.open = 0
        self.last_used = time.monotonic()
        # Set on shutdown: no connection starts the VM again.
        self.stopping = False

    def running(self) -> bool:
        return self.vm is not None and self.vm.returncode is None

    async def start(self) -> None:
        """Start the VM unless it runs, and wait until its worker accepts connections."""
        async with self.lock:
            if self.running():
                return
            SOCKET.unlink(missing_ok=True)
            # A VM left by a crash would hold the name and the socket.
            await run("rm", "--force", NAME)
            log(f"starting VM {NAME} from {IMAGE}")
            self.vm = await asyncio.create_subprocess_exec(*vm_command())
            deadline = time.monotonic() + START_SECONDS
            while self.running() and time.monotonic() < deadline:
                if await worker_live():
                    return
                await asyncio.sleep(0.2)
            log(f"VM {NAME} did not start (exit {self.vm.returncode})")

    async def stop(self) -> None:
        async with self.lock:
            if not self.running():
                return
            vm = self.vm
            assert vm is not None
            log(f"stopping VM {NAME}")
            await run("stop", "--time", str(STOP_SECONDS), NAME)
            try:
                await asyncio.wait_for(vm.wait(), STOP_SECONDS + 10)
            except TimeoutError:
                await run("kill", NAME)
                await vm.wait()

    async def forward(self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
        try:
            first = await asyncio.wait_for(client_reader.readline(), 30)
        except (OSError, TimeoutError, ValueError):
            client_writer.close()
            return
        if not first or self.stopping:
            client_writer.close()
            return
        if not self.running() and first.startswith(HEALTH_REQUEST):
            await answer_stopped(client_reader, client_writer)
            return
        self.open += 1
        try:
            await self.start()
            try:
                worker_reader, worker_writer = await asyncio.open_unix_connection(str(SOCKET))
            except OSError:  # the VM didn't start
                client_writer.close()
                return
            try:
                worker_writer.write(first)
                await asyncio.gather(pipe(client_reader, worker_writer), pipe(worker_reader, client_writer))
            finally:
                for writer in (client_writer, worker_writer):
                    writer.close()
                    with contextlib.suppress(Exception):
                        await writer.wait_closed()
        finally:
            self.open -= 1
            self.last_used = time.monotonic()

    def idle(self) -> bool:
        return (IDLE_SECONDS > 0 and self.running() and self.open == 0
                and time.monotonic() - self.last_used >= IDLE_SECONDS)

    async def stop_when_idle(self) -> None:
        while True:
            await asyncio.sleep(IDLE_CHECK_SECONDS)
            if self.idle():
                log(f"VM {NAME} idle for {IDLE_SECONDS // 60} minutes")
                try:
                    await self.stop()
                except Exception as error:  # noqa: BLE001 - the next check tries again
                    log(f"stopping VM {NAME} failed: {type(error).__name__}: {error}")


def loopback(value: str, *, origin: bool) -> bool:
    """As the worker's own guard: a loopback Host (or Origin), so a page that rebinds a name to 127.0.0.1
    can't read it."""
    try:
        parsed = urllib.parse.urlsplit(value if origin else f"//{value}")
        _ = parsed.port
    except ValueError:
        return False
    return bool(parsed.hostname and parsed.hostname.rstrip(".").lower() in LOOPBACK_HOSTS
                and (parsed.scheme in {"http", "https"} if origin else not parsed.path))


async def answer_stopped(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    hosts, origins = [], []
    try:
        for _ in range(100):
            line = await asyncio.wait_for(reader.readline(), 10)
            if line in (b"\r\n", b"\n", b""):
                break
            name, _, value = line.decode("latin-1").partition(":")
            if name.strip().lower() == "host":
                hosts.append(value.strip())
            elif name.strip().lower() == "origin":
                origins.append(value.strip())
    except (OSError, TimeoutError, ValueError):
        writer.close()
        return
    if len(hosts) != 1 or not loopback(hosts[0], origin=False):
        status, body = "421 Misdirected Request", b"loopback Host required"
    elif len(origins) > 1 or (origins and not loopback(origins[0], origin=True)):
        status, body = "403 Forbidden", b"loopback Origin required"
    else:
        # Compact, as the worker's own.
        status, body = "200 OK", json.dumps({"status": "ok", "service": "browser-worker", "revision": REVISION,
                                             "ready": True, "vm": "stopped", "live_sessions": 0},
                                            separators=(",", ":")).encode()
    kind = "application/json" if status == "200 OK" else "text/plain"
    writer.write(f"HTTP/1.1 {status}\r\nContent-Type: {kind}\r\nConnection: close\r\n"
                 f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    with contextlib.suppress(OSError):
        await writer.drain()
    writer.close()


async def main() -> int:
    SOCKET.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # A VM left running when this was killed: it would hold the name and never stop when idle.
    await run("rm", "--force", NAME)
    host = Host()
    server = await asyncio.start_server(host.forward, "127.0.0.1", PORT)
    log(f"serving 127.0.0.1:{PORT}; VM {NAME} starts on first use")
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stopping.set)
    idle = asyncio.create_task(host.stop_when_idle())
    await stopping.wait()
    host.stopping = True
    idle.cancel()
    await host.stop()
    # Last, so the port stays bound (the operator waits for it to free) until the VM is gone.
    server.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
