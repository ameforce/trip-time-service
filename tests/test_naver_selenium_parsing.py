from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from trip_time_service.config import Settings
from trip_time_service.providers.naver_selenium import (
    NaverMapsSeleniumProvider,
    _parse_naver_duration,
)

KST = ZoneInfo("Asia/Seoul")


def _settings() -> Settings:
    return Settings(
        timezone=KST,
        headless=True,
        cache_ttl=timedelta(seconds=300),
        step_minutes=10,
        lookback_hours=3,
        max_queries=120,
        provider="naver_selenium",
        chrome_binary_path=None,
        chrome_user_data_dir=None,
        naver_map_client_id=None,
        recommend_workers=1,
        naver_session_pool_size=1,
    )


class _FakeButton:
    def __init__(
        self,
        text: str,
        *,
        displayed: bool = True,
        css_class: str = "",
    ) -> None:
        self.text = text
        self._displayed = displayed
        self._css_class = css_class
        self.clicks = 0

    def is_displayed(self) -> bool:
        return self._displayed

    def get_attribute(self, name: str) -> str | None:
        if name == "class":
            return self._css_class
        return None


class _FakeCalendarDriver:
    def __init__(
        self,
        *,
        collapsed_days: list[str],
        expanded_days: list[str] | None = None,
        show_expand_button: bool = True,
    ) -> None:
        self._collapsed_buttons = [
            _FakeButton(day, css_class="calendar_day_btn")
            for day in collapsed_days
        ]
        expanded_values = expanded_days if expanded_days is not None else collapsed_days
        self._expanded_buttons = [
            _FakeButton(day, css_class="calendar_day_btn")
            for day in expanded_values
        ]
        self._expand_button = _FakeButton(
            "펼치기",
            css_class="calendar_expand_btn",
            displayed=show_expand_button,
        )
        self.is_expanded = False
        self.clicked_elements: list[_FakeButton] = []

    def _active_day_buttons(self) -> list[_FakeButton]:
        if self.is_expanded:
            return self._expanded_buttons
        return self._collapsed_buttons

    def find_elements(self, by: str, selector: str) -> list[_FakeButton]:
        if selector == "button.calendar_day_btn":
            return self._active_day_buttons()
        if selector == "button.calendar_expand_btn":
            return [self._expand_button]
        if "calendar_expand_btn" in selector:
            return [self._expand_button]
        if "펼치기" in selector:
            return [self._expand_button]
        return []

    def execute_script(self, script: str, element: _FakeButton) -> None:
        del script
        element.clicks += 1
        self.clicked_elements.append(element)
        if element is self._expand_button:
            self.is_expanded = True


def test_extract_duration_from_panel_text_matches_requested_departure() -> None:
    provider = NaverMapsSeleniumProvider(_settings())
    text = (
        "\ub098\uc911\uc5d0 \ucd9c\ubc1c\n"
        "\ub0b4\uc77c \uc624\uc804 10\uc2dc 00\ubd84 \ucd9c\ubc1c\ud558\uba74\n"
        "4\uc2dc\uac04 33\ubd84 \uc18c\uc694 \uc608\uc0c1"
    )

    duration = provider._extract_duration_from_panel_text(
        text,
        "\uc624\uc804",
        "10\uc2dc",
        "00\ubd84",
    )

    assert duration == (4 * 60 + 33) * 60


def test_extract_duration_from_panel_text_rejects_mismatched_departure() -> None:
    provider = NaverMapsSeleniumProvider(_settings())
    text = (
        "\ub098\uc911\uc5d0 \ucd9c\ubc1c\n"
        "\ub0b4\uc77c \uc624\uc804 09\uc2dc 00\ubd84 \ucd9c\ubc1c\ud558\uba74\n"
        "4\uc2dc\uac04 33\ubd84 \uc18c\uc694 \uc608\uc0c1"
    )

    duration = provider._extract_duration_from_panel_text(
        text,
        "\uc624\uc804",
        "10\uc2dc",
        "00\ubd84",
    )

    assert duration is None


def test_extract_duration_from_panel_text_rejects_route_summary_like_text() -> None:
    provider = NaverMapsSeleniumProvider(_settings())
    text = "2\uc2dc\uac04 45\ubd84231km"

    duration = provider._extract_duration_from_panel_text(
        text,
        "\uc624\uc804",
        "10\uc2dc",
        "00\ubd84",
    )

    assert duration is None


def test_parse_duration_prefers_hour_only_before_soyo() -> None:
    text = (
        "\ub0b4\uc77c \uc624\uc804 10\uc2dc 40\ubd84 \ucd9c\ubc1c\ud558\uba74\n"
        "5\uc2dc\uac04 \uc18c\uc694 \uc608\uc0c1\n"
        "+9\ubd84\n"
        "30\ubd84 \ud6c4\n"
        "+14\ubd84\n"
        "2\uc2dc\uac04 \ud6c4"
    )

    duration = _parse_naver_duration(text)

    assert duration == 5 * 3600


def test_set_calendar_date_clicks_visible_day_without_expand(monkeypatch) -> None:
    provider = NaverMapsSeleniumProvider(_settings())
    driver = _FakeCalendarDriver(
        collapsed_days=[str(day) for day in range(8, 22)],
        expanded_days=[str(day) for day in range(1, 32)],
    )
    departure_time = datetime(2099, 3, 15, 9, 0, tzinfo=KST)
    monkeypatch.setattr(
        "trip_time_service.providers.naver_selenium._time.sleep",
        lambda _seconds: None,
    )

    provider._set_calendar_date(driver, departure_time)

    assert driver._expand_button.clicks == 0
    assert any(button.text == "15" for button in driver.clicked_elements)


def test_set_calendar_date_expands_calendar_when_target_day_missing(
    monkeypatch,
) -> None:
    provider = NaverMapsSeleniumProvider(_settings())
    driver = _FakeCalendarDriver(
        collapsed_days=[str(day) for day in range(8, 22)],
        expanded_days=[str(day) for day in range(1, 32)],
    )
    departure_time = datetime(2099, 3, 27, 9, 0, tzinfo=KST)
    monkeypatch.setattr(
        "trip_time_service.providers.naver_selenium._time.sleep",
        lambda _seconds: None,
    )

    provider._set_calendar_date(driver, departure_time)

    assert driver._expand_button.clicks == 1
    assert any(button.text == "27" for button in driver.clicked_elements)
