from __future__ import annotations

import asyncio

import pytest

import security
from browser_runtime import chromium_hardening_args
from security import (
    NetworkPolicyError,
    PublicEgressProxy,
    PublicResolver,
    ResolvedTarget,
    is_public_ip,
    parse_public_url,
)


def test_public_ip_classification() -> None:
    assert is_public_ip("8.8.8.8")
    assert is_public_ip("2606:4700:4700::1111")
    for value in ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1", "fe80::1", "0.0.0.0"]:
        assert not is_public_ip(value)


def test_chromium_forces_proxy_dns_and_disables_nonproxied_protocols() -> None:
    args = chromium_hardening_args()
    assert "--disable-quic" in args
    assert "--proxy-bypass-list=<-loopback>" in args
    assert any(value.startswith("--host-resolver-rules=MAP * ~NOTFOUND") for value in args)
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in args


def test_url_shape_rejects_credentials_schemes_and_ports() -> None:
    with pytest.raises(NetworkPolicyError):
        parse_public_url("file:///etc/passwd")
    with pytest.raises(NetworkPolicyError):
        parse_public_url("http://user:pass@example.com/")
    with pytest.raises(NetworkPolicyError):
        parse_public_url("http://example.com:22/")
    assert parse_public_url("https://example.com/path").hostname == "example.com"


@pytest.mark.asyncio
async def test_literal_private_addresses_fail_before_connection() -> None:
    resolver = PublicResolver()
    for host in ["127.0.0.1", "10.0.0.1", "::1", "169.254.169.254"]:
        with pytest.raises(NetworkPolicyError, match="not public"):
            await resolver.resolve(host, 443)


class FixtureResolver(PublicResolver):
    def __init__(self, port: int) -> None:
        self.port = port
        self.calls: list[tuple[str, int]] = []

    async def resolve(self, hostname: str, port: int) -> ResolvedTarget:
        self.calls.append((hostname, port))
        if hostname == "public.test" and port == self.port:
            return ResolvedTarget(hostname, port, ("127.0.0.1",))
        return await super().resolve(hostname, port)


@pytest.mark.asyncio
async def test_proxy_rewrites_public_http_to_the_resolved_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    received = b""

    async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal received
        received = await reader.read(65536)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    port = int((server.sockets or [])[0].getsockname()[1])
    monkeypatch.setattr(security, "ALLOWED_DESTINATION_PORTS", frozenset({80, 443, port}))
    resolver = FixtureResolver(port)
    proxy = PublicEgressProxy(resolver)
    proxy_port = await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        writer.write(
            f"GET http://public.test:{port}/hello?q=secret HTTP/1.1\r\nHost: public.test:{port}\r\n\r\n".encode()
        )
        await writer.drain()
        response = await reader.read()
        assert b"200 OK" in response and response.endswith(b"OK")
        assert received.startswith(b"GET /hello?q=secret HTTP/1.1")
        assert resolver.calls == [("public.test", port)]

        reader2, writer2 = await asyncio.open_connection("127.0.0.1", proxy_port)
        writer2.write(
            f"GET http://public.test:{port}/again HTTP/1.1\r\nHost: public.test:{port}\r\n\r\n".encode()
        )
        await writer2.drain()
        assert b"200 OK" in await reader2.read()
        assert resolver.calls == [("public.test", port), ("public.test", port)]
    finally:
        await proxy.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_mixed_public_private_dns_answers_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = asyncio.get_running_loop()

    async def fake_getaddrinfo(*_args, **_kwargs):
        return [
            (2, 1, 6, "", ("8.8.8.8", 443)),
            (2, 1, 6, "", ("127.0.0.1", 443)),
        ]

    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(NetworkPolicyError, match="exclusively public"):
        await PublicResolver().resolve("mixed.example", 443)


@pytest.mark.asyncio
async def test_connect_tunnel_uses_the_resolver_selected_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(await reader.read(4))
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    target_port = int((upstream.sockets or [])[0].getsockname()[1])
    monkeypatch.setattr(
        security, "ALLOWED_DESTINATION_PORTS", frozenset({80, 443, target_port})
    )
    resolver = FixtureResolver(target_port)
    proxy = PublicEgressProxy(resolver)
    port = await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            f"CONNECT public.test:{target_port} HTTP/1.1\r\nHost: public.test:{target_port}\r\n\r\n".encode()
        )
        await writer.drain()
        head = await reader.readuntil(b"\r\n\r\n")
        assert b"200 Connection Established" in head
        writer.write(b"PING")
        await writer.drain()
        assert await reader.readexactly(4) == b"PING"
        assert resolver.calls == [("public.test", target_port)]
    finally:
        await proxy.close()
        upstream.close()
        await upstream.wait_closed()


@pytest.mark.asyncio
async def test_proxy_blocks_private_connect() -> None:
    proxy = PublicEgressProxy()
    port = await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"CONNECT 127.0.0.1:443 HTTP/1.1\r\nHost: 127.0.0.1:443\r\n\r\n")
        await writer.drain()
        response = await reader.read()
        assert b"403 Browser Worker Policy" in response

        ws_reader, ws_writer = await asyncio.open_connection("127.0.0.1", port)
        ws_writer.write(
            b"GET http://127.0.0.1/socket HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Connection: Upgrade\r\nUpgrade: websocket\r\n\r\n"
        )
        await ws_writer.drain()
        assert b"403 Browser Worker Policy" in await ws_reader.read()
    finally:
        await proxy.close()
