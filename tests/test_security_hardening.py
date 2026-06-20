from __future__ import annotations

from fastapi.testclient import TestClient

from trip_time_service.api.main import create_app
from trip_time_service.config import load_settings


def _client() -> TestClient:
    load_settings.cache_clear()
    return TestClient(create_app())


def test_security_headers_are_applied_by_default(monkeypatch) -> None:
    monkeypatch.delenv("TTS_ENABLE_DOCS", raising=False)

    response = _client().get("/healthz")

    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "geolocation=()" in response.headers["permissions-policy"]
    assert response.headers["strict-transport-security"].startswith(
        "max-age=31536000",
    )


def test_openapi_docs_are_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("TTS_ENABLE_DOCS", raising=False)

    client = _client()

    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_static_entrypoints_force_revalidation(monkeypatch) -> None:
    monkeypatch.delenv("TTS_ENABLE_DOCS", raising=False)

    client = _client()

    index_response = client.get("/")
    app_js_response = client.get("/static/js/app.js")

    assert index_response.status_code == 200
    assert app_js_response.status_code == 200
    assert index_response.headers["cache-control"] == "no-cache"
    assert app_js_response.headers["cache-control"] == "no-cache"
    assert '/static/js/app.js?v=' in index_response.text
    assert '/static/js/autocomplete-controller.js?v=' in index_response.text
    assert '/static/css/style.css?v=' in index_response.text
