from __future__ import annotations

from trip_time_service.api import naver_geo
from trip_time_service.chrome_driver import ChromeDriverCloseResult


class _FakeDriver:
    pass


def test_shutdown_naver_driver_closes_global_driver(monkeypatch) -> None:
    driver = _FakeDriver()
    closed: list[object] = []
    monkeypatch.setattr(naver_geo, "_naver_driver", driver)

    def close_driver(current_driver, **_kwargs):
        closed.append(current_driver)
        return ChromeDriverCloseResult(timed_out=False)

    monkeypatch.setattr(naver_geo, "close_webdriver_with_timeout", close_driver)

    naver_geo.shutdown_naver_driver()

    assert closed == [driver]
    assert naver_geo._naver_driver is None


def test_shutdown_naver_driver_is_noop_without_driver(monkeypatch) -> None:
    monkeypatch.setattr(naver_geo, "_naver_driver", None)
    monkeypatch.setattr(
        naver_geo,
        "close_webdriver_with_timeout",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected")),
    )

    naver_geo.shutdown_naver_driver()

    assert naver_geo._naver_driver is None
