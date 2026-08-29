from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import urllib.parse
from dataclasses import dataclass

from config import (
    ALLOWED_DESTINATION_PORTS,
    DNS_TIMEOUT_SECONDS,
    MAX_PROXY_HEADER_BYTES,
    MAX_PROXY_STREAM_BYTES,
)

logger = logging.getLogger("browser-worker.egress")


class NetworkPolicyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    hostname: str
    port: int
    addresses: tuple[str, ...]

    @property
    def selected_address(self) -> str:
        return self.addresses[0]


def is_public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    return bool(
        address.is_global
        and not address.is_multicast
        and not address.is_unspecified
        and not address.is_reserved
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_private
    )


def normalize_hostname(hostname: str) -> str:
    host = hostname.rstrip(".").lower()
    if not host or len(host) > 253:
        raise NetworkPolicyError("destination hostname is invalid")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise NetworkPolicyError("destination is not public")
    return host


def parse_public_url(url: str) -> urllib.parse.SplitResult:
    if not isinstance(url, str) or not url.strip() or len(url) > 8192:
        raise NetworkPolicyError("URL must be a nonempty bounded string")
    try:
        parsed = urllib.parse.urlsplit(url.strip())
        port = parsed.port
    except ValueError as exc:
        raise NetworkPolicyError("URL is invalid") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise NetworkPolicyError("only public http(s) URLs are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise NetworkPolicyError("embedded URL credentials are not allowed")
    if not parsed.hostname:
        raise NetworkPolicyError("URL is missing a hostname")
    normalize_hostname(parsed.hostname)
    effective_port = port or (443 if parsed.scheme.lower() == "https" else 80)
    if effective_port not in ALLOWED_DESTINATION_PORTS:
        raise NetworkPolicyError("destination port is not allowed")
    return parsed


class PublicResolver:
    async def resolve(self, hostname: str, port: int) -> ResolvedTarget:
        host = normalize_hostname(hostname)
        try:
            literal = ipaddress.ip_address(host.split("%", 1)[0])
        except ValueError:
            literal = None
        if literal is not None:
            if not is_public_ip(str(literal)):
                raise NetworkPolicyError("destination is not public")
            return ResolvedTarget(host, port, (str(literal),))

        loop = asyncio.get_running_loop()
        try:
            records = await asyncio.wait_for(
                loop.getaddrinfo(host, port, type=socket.SOCK_STREAM),
                timeout=DNS_TIMEOUT_SECONDS,
            )
        except (TimeoutError, OSError, socket.gaierror) as exc:
            raise NetworkPolicyError("destination DNS resolution failed") from exc
        addresses: list[str] = []
        for _family, _kind, _proto, _canon, sockaddr in records:
            address = str(sockaddr[0]).split("%", 1)[0]
            if address not in addresses:
                addresses.append(address)
        if not addresses or any(not is_public_ip(address) for address in addresses):
            raise NetworkPolicyError("destination DNS answers are not exclusively public")
        addresses.sort(key=lambda value: (":" in value, value))
        return ResolvedTarget(host, port, tuple(addresses))


async def resolve_public_url(url: str, resolver: PublicResolver) -> tuple[urllib.parse.SplitResult, ResolvedTarget]:
    parsed = parse_public_url(url)
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    return parsed, await resolver.resolve(parsed.hostname or "", port)


def _authority(value: str, default_port: int) -> tuple[str, int]:
    try:
        parsed = urllib.parse.urlsplit(f"//{value}")
        port = parsed.port or default_port
    except ValueError as exc:
        raise NetworkPolicyError("proxy authority is invalid") from exc
    if parsed.username is not None or parsed.password is not None or not parsed.hostname:
        raise NetworkPolicyError("proxy authority is invalid")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise NetworkPolicyError("proxy authority is invalid")
    if port not in ALLOWED_DESTINATION_PORTS:
        raise NetworkPolicyError("destination port is not allowed")
    return normalize_hostname(parsed.hostname), port


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, limit: int) -> None:
    total = 0
    try:
        while True:
            chunk = await reader.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, OSError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.write_eof()
        except (OSError, RuntimeError):
            pass


async def _relay(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
) -> None:
    tasks = {
        asyncio.create_task(_pipe(client_reader, upstream_writer, MAX_PROXY_STREAM_BYTES)),
        asyncio.create_task(_pipe(upstream_reader, client_writer, MAX_PROXY_STREAM_BYTES)),
    }
    _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


class PublicEgressProxy:
    def __init__(self, resolver: PublicResolver | None = None) -> None:
        self.resolver = resolver or PublicResolver()
        self._server: asyncio.AbstractServer | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self.port: int | None = None

    async def start(self) -> int:
        if self._server is not None and self.port is not None:
            return self.port
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0, limit=MAX_PROXY_HEADER_BYTES)
        sockets = self._server.sockets or []
        if not sockets:
            raise RuntimeError("egress proxy failed to bind")
        self.port = int(sockets[0].getsockname()[1])
        return self.port

    async def close(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            await server.wait_closed()
        for writer in tuple(self._writers):
            writer.close()
        await asyncio.gather(
            *(writer.wait_closed() for writer in tuple(self._writers)), return_exceptions=True
        )
        self._writers.clear()
        self.port = None

    async def _read_head(self, reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = await reader.read(min(4096, MAX_PROXY_HEADER_BYTES + 1 - len(data)))
            if not chunk:
                raise NetworkPolicyError("incomplete proxy request")
            data.extend(chunk)
            if len(data) > MAX_PROXY_HEADER_BYTES:
                raise NetworkPolicyError("proxy request headers are too large")
        head, rest = bytes(data).split(b"\r\n\r\n", 1)
        return head, rest

    @staticmethod
    def _error(status: int, message: str) -> bytes:
        body = message.encode("utf-8")
        return (
            f"HTTP/1.1 {status} Browser Worker Policy\r\nContent-Type: text/plain\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        ).encode("ascii") + body

    async def _connect(self, target: ResolvedTarget) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        last_error: OSError | None = None
        for address in target.addresses:
            try:
                return await asyncio.wait_for(
                    asyncio.open_connection(address, target.port), timeout=10
                )
            except (TimeoutError, OSError) as exc:
                if isinstance(exc, OSError):
                    last_error = exc
        raise NetworkPolicyError("public destination connection failed") from last_error

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            head, rest = await self._read_head(reader)
            lines = head.split(b"\r\n")
            try:
                method_b, target_b, version_b = lines[0].split(b" ", 2)
                method = method_b.decode("ascii").upper()
                target_text = target_b.decode("ascii")
                version = version_b.decode("ascii")
            except (ValueError, UnicodeDecodeError) as exc:
                raise NetworkPolicyError("invalid proxy request line") from exc
            if version not in {"HTTP/1.0", "HTTP/1.1"}:
                raise NetworkPolicyError("unsupported proxy protocol")

            if method == "CONNECT":
                host, port = _authority(target_text, 443)
                resolved = await self.resolver.resolve(host, port)
                upstream_reader, upstream_writer = await self._connect(resolved)
                self._writers.add(upstream_writer)
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                if rest:
                    upstream_writer.write(rest)
                    await upstream_writer.drain()
                await _relay(reader, writer, upstream_reader, upstream_writer)
                return

            parsed, resolved = await resolve_public_url(target_text, self.resolver)
            if parsed.scheme.lower() != "http":
                raise NetworkPolicyError("HTTPS requires CONNECT")
            upstream_reader, upstream_writer = await self._connect(resolved)
            self._writers.add(upstream_writer)
            path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            output_headers: list[bytes] = []
            has_host = False
            upgrade = False
            for raw in lines[1:]:
                if not raw:
                    continue
                if b":" not in raw:
                    raise NetworkPolicyError("invalid proxy header")
                name, value = raw.split(b":", 1)
                lower = name.strip().lower()
                if lower in {b"proxy-authorization", b"proxy-connection", b"connection"}:
                    if lower == b"connection" and b"upgrade" in value.lower():
                        upgrade = True
                    continue
                if lower == b"host":
                    has_host = True
                    expected = parsed.hostname or ""
                    if parsed.port and parsed.port != 80:
                        expected = f"{expected}:{parsed.port}"
                    if value.decode("latin-1").strip().rstrip(".").lower() != expected.rstrip(".").lower():
                        raise NetworkPolicyError("Host header does not match proxy target")
                output_headers.append(name + b":" + value)
            if not has_host:
                host_value = parsed.hostname or ""
                if parsed.port and parsed.port != 80:
                    host_value = f"{host_value}:{parsed.port}"
                output_headers.append(f"Host: {host_value}".encode("ascii"))
            output_headers.append(b"Connection: Upgrade" if upgrade else b"Connection: close")
            rebuilt = (
                f"{method} {path} {version}\r\n".encode("ascii")
                + b"\r\n".join(output_headers)
                + b"\r\n\r\n"
                + rest
            )
            upstream_writer.write(rebuilt)
            await upstream_writer.drain()
            await _relay(reader, writer, upstream_reader, upstream_writer)
        except NetworkPolicyError as exc:
            writer.write(self._error(403, str(exc)))
            await writer.drain()
        except Exception as exc:  # noqa: BLE001 - connection boundary must fail closed
            logger.warning("browser egress connection failed: %s", type(exc).__name__)
            writer.write(self._error(502, "browser egress failed"))
            await writer.drain()
        finally:
            if upstream_writer is not None:
                self._writers.discard(upstream_writer)
                upstream_writer.close()
                await upstream_writer.wait_closed()
            self._writers.discard(writer)
            writer.close()
            await writer.wait_closed()
