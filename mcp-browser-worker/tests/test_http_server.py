from __future__ import annotations

from http_server import _loopback_authority, _loopback_origin


def test_loopback_host_and_origin_guards() -> None:
    assert _loopback_authority("127.0.0.1:8890")
    assert _loopback_authority("localhost:8890")
    assert _loopback_origin("http://127.0.0.1:8890")
    assert not _loopback_authority("example.com")
    assert not _loopback_authority("127.0.0.1@evil.example")
    assert not _loopback_origin("https://evil.example")
    assert not _loopback_origin("http://127.0.0.1:8890/path")
