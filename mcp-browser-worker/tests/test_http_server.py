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


def test_the_open_file_limit_is_raised_toward_what_busy_pages_need(monkeypatch):
    import resource

    import http_server

    calls = []
    monkeypatch.setattr(resource, "getrlimit", lambda _which: (256, resource.RLIM_INFINITY))
    monkeypatch.setattr(resource, "setrlimit", lambda _which, limits: calls.append(limits))
    assert http_server.raise_open_file_limit() == http_server.OPEN_FILES_WANTED
    assert calls == [(http_server.OPEN_FILES_WANTED, resource.RLIM_INFINITY)]
    calls.clear()
    monkeypatch.setattr(resource, "getrlimit", lambda _which: (20_000, 30_000))
    assert http_server.raise_open_file_limit() == 20_000 and calls == []  # never lowered
