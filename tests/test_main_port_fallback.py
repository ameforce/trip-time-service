from __future__ import annotations

import socket
from contextlib import closing

import trip_time_service.__main__ as entry


def _reserve_free_port(host: str) -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _bind_port(host: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((host, port))
    return sock


def test_find_available_port_prefers_requested_port() -> None:
    host = "127.0.0.1"
    preferred = _reserve_free_port(host)
    assert entry._find_available_port(host, preferred) == preferred


def test_find_available_port_uses_fallback_range(monkeypatch) -> None:
    monkeypatch.delenv("TTS_PORT_STRICT", raising=False)
    host = "127.0.0.1"
    preferred = _reserve_free_port(host)
    blocker = _bind_port(host, preferred)
    try:
        fallback_port = entry._find_available_port(host, preferred)
    finally:
        blocker.close()

    assert fallback_port != preferred
    assert fallback_port in entry._FALLBACK_RANGE


def test_find_available_port_uses_ephemeral_when_range_exhausted(monkeypatch) -> None:
    monkeypatch.delenv("TTS_PORT_STRICT", raising=False)
    host = "127.0.0.1"
    occupied: list[socket.socket] = []
    try:
        for port in entry._FALLBACK_RANGE:
            if entry._is_port_available(host, port):
                occupied.append(_bind_port(host, port))

        fallback_port = entry._find_available_port(host, entry._FALLBACK_RANGE.start)
        assert fallback_port not in entry._FALLBACK_RANGE
        assert fallback_port > 0
    finally:
        for sock in occupied:
            sock.close()


def test_find_available_port_strict_mode_fails_without_fallback(monkeypatch) -> None:
    host = "127.0.0.1"
    preferred = _reserve_free_port(host)
    blocker = _bind_port(host, preferred)
    monkeypatch.setenv("TTS_PORT_STRICT", "1")
    try:
        try:
            entry._find_available_port(host, preferred)
        except RuntimeError as exc:
            assert str(preferred) in str(exc)
            assert "strict" in str(exc).lower()
        else:
            raise AssertionError("strict mode should fail instead of falling back")
    finally:
        blocker.close()
