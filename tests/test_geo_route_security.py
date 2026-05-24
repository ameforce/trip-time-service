from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from trip_time_service.api import routes_geo


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(routes_geo.router)
    return TestClient(app)


def test_warmup_rejects_too_many_queries(monkeypatch) -> None:
    called = False

    def warmup(*_args, **_kwargs) -> int:
        nonlocal called
        called = True
        return 0

    monkeypatch.setattr(routes_geo, "warmup_autocomplete_cache", warmup)
    response = _client().post(
        "/api/autocomplete/warmup",
        json={"queries": [f"장소{i}" for i in range(9)]},
    )

    assert response.status_code == 422
    assert called is False


def test_warmup_rejects_long_query(monkeypatch) -> None:
    monkeypatch.setattr(routes_geo, "warmup_autocomplete_cache", lambda *_a, **_k: 0)
    response = _client().post(
        "/api/autocomplete/warmup",
        json={"queries": ["가" * 121]},
    )

    assert response.status_code == 422


def test_warmup_rejects_excessive_duplicate_queries(monkeypatch) -> None:
    monkeypatch.setattr(routes_geo, "warmup_autocomplete_cache", lambda *_a, **_k: 0)
    response = _client().post(
        "/api/autocomplete/warmup",
        json={"queries": ["강남역", " 강남역 ", "강남 역"]},
    )

    assert response.status_code == 422


def test_public_blocking_warmup_requires_debug_authorization(monkeypatch) -> None:
    called = False

    def warmup(*_args, **_kwargs) -> int:
        nonlocal called
        called = True
        return 1

    monkeypatch.delenv("TTS_ENABLE_DEBUG_ROUTES", raising=False)
    monkeypatch.delenv("TTS_DEBUG_TOKEN", raising=False)
    monkeypatch.delenv("TTS_ALLOW_LOCAL_DEBUG_ROUTES", raising=False)
    monkeypatch.setattr(routes_geo, "warmup_autocomplete_cache", warmup)

    response = _client().post(
        "/api/autocomplete/warmup",
        json={"queries": ["강남역"], "blocking": True},
    )

    assert response.status_code == 403
    assert called is False


def test_public_nonblocking_live_warmup_requires_debug_authorization(
    monkeypatch,
) -> None:
    called = False

    def warmup(*_args, **_kwargs) -> int:
        nonlocal called
        called = True
        return 1

    monkeypatch.delenv("TTS_E2E_FIXTURE_MODE", raising=False)
    monkeypatch.delenv("TTS_ENABLE_DEBUG_ROUTES", raising=False)
    monkeypatch.delenv("TTS_DEBUG_TOKEN", raising=False)
    monkeypatch.delenv("TTS_ALLOW_LOCAL_DEBUG_ROUTES", raising=False)
    monkeypatch.setattr(routes_geo, "warmup_autocomplete_cache", warmup)

    response = _client().post(
        "/api/autocomplete/warmup",
        json={"queries": ["강남역"], "blocking": False},
    )

    assert response.status_code == 403
    assert called is False


def test_blocking_warmup_accepts_enabled_debug_token(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def warmup(*_args, **kwargs) -> int:
        calls.append(kwargs)
        return 1

    monkeypatch.setenv("TTS_ENABLE_DEBUG_ROUTES", "1")
    monkeypatch.setenv("TTS_DEBUG_TOKEN", "secret")
    monkeypatch.delenv("TTS_ALLOW_LOCAL_DEBUG_ROUTES", raising=False)
    monkeypatch.setattr(routes_geo, "warmup_autocomplete_cache", warmup)

    response = _client().post(
        "/api/autocomplete/warmup",
        headers={"X-TTS-Debug-Token": "secret"},
        json={"queries": ["강남역"], "blocking": True},
    )

    assert response.status_code == 200
    assert response.json() == {"queued": 1}
    assert calls == [{"search_coord": None, "limit": 12, "background": False}]


def test_debug_routes_require_token_when_enabled(monkeypatch) -> None:
    monkeypatch.setenv("TTS_ENABLE_DEBUG_ROUTES", "1")
    monkeypatch.setenv("TTS_DEBUG_TOKEN", "secret")
    monkeypatch.delenv("TTS_ALLOW_LOCAL_DEBUG_ROUTES", raising=False)
    monkeypatch.setattr(
        routes_geo,
        "get_autocomplete_runtime_metrics",
        lambda: {"ok": True},
    )

    client = _client()
    rejected = client.get("/api/debug/autocomplete/runtime")
    accepted = client.get(
        "/api/debug/autocomplete/runtime",
        headers={"X-TTS-Debug-Token": "secret"},
    )

    assert rejected.status_code == 403
    assert accepted.status_code == 200
    assert accepted.json() == {"ok": True}
