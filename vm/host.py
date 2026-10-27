#!/usr/bin/env python3
"""Run browser-worker in a Linux VM (Apple `container`) and serve it on the Mac's loopback.

Chromium runs windowed on the VM's virtual display, so nothing shows on the Mac's screen or plays from
its speakers. The VM publishes the worker's Unix socket to the Mac, and this forwards
127.0.0.1:BROWSER_WORKER_PORT to it: a published TCP port would need macOS's Local Network permission
for `container`'s helper, which a Mac without anyone at it can't grant (apple/container#2029).

launchd keeps this running. It exits when the VM does, and stops the VM when it's told to stop.
Standard library only: it runs on the installer's Python, not the release's environment.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
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
SOCKET = DATA_DIR / "run" / "worker.sock"
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


def log(message: str) -> None:
    print(f"browser-worker host: {message}", file=sys.stderr, flush=True)


async def run(*args: str) -> int:
    process = await asyncio.create_subprocess_exec(
        CONTAINER, *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
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


async def forward(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
    try:
        worker_reader, worker_writer = await asyncio.open_unix_connection(str(SOCKET))
    except OSError:  # the VM is still starting
        client_writer.close()
        return
    try:
        await asyncio.gather(pipe(client_reader, worker_writer), pipe(worker_reader, client_writer))
    finally:
        for writer in (client_writer, worker_writer):
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


async def main() -> int:
    SOCKET.parent.mkdir(mode=0o700, exist_ok=True)
    SOCKET.unlink(missing_ok=True)
    # A VM left by a crash would hold the name and the socket.
    await run("rm", "--force", NAME)
    vm = await asyncio.create_subprocess_exec(*vm_command())
    server = await asyncio.start_server(forward, "127.0.0.1", PORT)
    log(f"VM {NAME} from {IMAGE}; serving 127.0.0.1:{PORT}")

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stopping.set)
    vm_exit = asyncio.create_task(vm.wait())
    stop = asyncio.create_task(stopping.wait())
    await asyncio.wait({vm_exit, stop}, return_when=asyncio.FIRST_COMPLETED)
    if vm_exit.done():
        server.close()
        log(f"VM {NAME} exited ({vm.returncode})")
        return 1
    log(f"stopping VM {NAME}")
    await run("stop", "--time", str(STOP_SECONDS), NAME)
    try:
        await asyncio.wait_for(vm_exit, STOP_SECONDS + 10)
    except TimeoutError:
        await run("kill", NAME)
        await vm_exit
    # Last, so the port stays bound (the operator waits for it to free) until the VM is gone.
    server.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
