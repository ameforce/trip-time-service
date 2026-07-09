from __future__ import annotations

from trip_time_service.api import naver_playwright_geo
from trip_time_service.browser.playwright_runtime import PlaywrightCloseResult


class _FakeSession:
    def __init__(self) -> None:
        self.close_calls: list[float] = []

    def close(self, *, close_timeout_seconds: float = 5.0) -> PlaywrightCloseResult:
        self.close_calls.append(close_timeout_seconds)
        return PlaywrightCloseResult(timed_out=False)


def test_shutdown_naver_driver_closes_global_session(monkeypatch) -> None:
    session = _FakeSession()
    monkeypatch.setattr(naver_playwright_geo, "_naver_session", session)

    naver_playwright_geo.shutdown_naver_driver()

    assert len(session.close_calls) == 1
    assert naver_playwright_geo._naver_session is None


def test_shutdown_naver_driver_is_noop_without_session(monkeypatch) -> None:
    monkeypatch.setattr(naver_playwright_geo, "_naver_session", None)

    naver_playwright_geo.shutdown_naver_driver()

    assert naver_playwright_geo._naver_session is None
