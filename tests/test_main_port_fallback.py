from __future__ import annotations

import trip_time_service.__main__ as entry


def test_find_available_port_prefers_requested_port(monkeypatch) -> None:
    monkeypatch.setattr(entry, "_is_port_available", lambda host, port: port == 8500)
    assert entry._find_available_port("127.0.0.1", 8500) == 8500


def test_find_available_port_uses_fallback_range(monkeypatch) -> None:
    monkeypatch.setattr(entry, "_FALLBACK_RANGE", range(8500, 8504))
    monkeypatch.setattr(entry, "_is_port_available", lambda host, port: port == 8502)
    assert entry._find_available_port("127.0.0.1", 8500) == 8502


def test_find_available_port_uses_ephemeral_when_range_exhausted(monkeypatch) -> None:
    monkeypatch.setattr(entry, "_FALLBACK_RANGE", range(8500, 8503))
    monkeypatch.setattr(entry, "_is_port_available", lambda host, port: False)
    monkeypatch.setattr(entry, "_reserve_ephemeral_port", lambda host: 39080)
    assert entry._find_available_port("127.0.0.1", 8500) == 39080
