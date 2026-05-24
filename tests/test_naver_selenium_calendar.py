from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from trip_time_service.config import Settings
from trip_time_service.providers.naver_selenium import NaverMapsSeleniumProvider


@dataclass
class _FakeCalendarButton:
    text: str
    displayed: bool = True
    enabled: bool = True
    clicked: bool = False

    def is_displayed(self) -> bool:
        return self.displayed

    def is_enabled(self) -> bool:
        return self.enabled


class _FakeDriver:
    def __init__(
        self,
        buttons: list[_FakeCalendarButton] | None = None,
        *,
        calendar_month_text: str = "2026.03",
        month_views: dict[str, dict[bool, list[_FakeCalendarButton]]] | None = None,
        month_options: list[_FakeCalendarButton] | None = None,
        expand_button: _FakeCalendarButton | None = None,
    ) -> None:
        self.buttons = buttons or []
        self.clicked_button: _FakeCalendarButton | None = None
        self.click_history: list[_FakeCalendarButton] = []
        self.calendar_month_button = _FakeCalendarButton(text=calendar_month_text)
        self.month_views = month_views
        self.month_options = month_options or []
        self.expand_button = expand_button or _FakeCalendarButton(text="펼치기")
        self.dropdown_open = False
        self.expanded = False

    def _current_day_buttons(self) -> list[_FakeCalendarButton]:
        if not self.month_views:
            return self.buttons
        month_state = self.month_views.get(self.calendar_month_button.text, {})
        if self.expanded in month_state:
            return month_state[self.expanded]
        return month_state.get(False, [])

    def find_elements(self, by: object, selector: str) -> list[_FakeCalendarButton]:
        if selector == "button.calendar_day_btn":
            return self._current_day_buttons()
        if selector == "button.calendar_date_btn":
            return [self.calendar_month_button]
        if selector == "button.list_item_btn":
            return self.month_options if self.dropdown_open else []
        if (
            selector == "button.calendar_expand_btn"
            and self.month_views
            and True in self.month_views.get(self.calendar_month_button.text, {})
        ):
            return [self.expand_button]
        return []

    def execute_script(self, script: str, button: _FakeCalendarButton) -> None:
        self.clicked_button = button
        self.click_history.append(button)
        button.clicked = True
        if button is self.calendar_month_button:
            self.dropdown_open = True
            return
        if button in self.month_options:
            self.calendar_month_button.text = button.text
            self.dropdown_open = False
            self.expanded = False
            return
        if button is self.expand_button:
            self.expanded = True
            self.expand_button.text = "접기"


def _make_settings() -> Settings:
    return Settings(
        timezone=ZoneInfo("Asia/Seoul"),
        headless=True,
        cache_ttl=timedelta(seconds=600),
        step_minutes=10,
        lookback_hours=3,
        max_queries=120,
        provider="naver_selenium",
        chrome_binary_path=None,
        chrome_user_data_dir=None,
        naver_map_client_id=None,
        recommend_workers=1,
        naver_session_pool_size=1,
        cors_allowed_origins=(),
        recommend_min_samples=12,
    )


def test_set_calendar_date_prefers_enabled_next_month_button_for_rollover(
    monkeypatch,
) -> None:
    real_date = dt.date
    provider = NaverMapsSeleniumProvider(_make_settings())
    driver = _FakeDriver(
        [
            _FakeCalendarButton(text="31", enabled=True),
            _FakeCalendarButton(text="1", enabled=False),
            _FakeCalendarButton(text="4월\n1", enabled=True),
            _FakeCalendarButton(text="2", enabled=True),
        ]
    )

    class _FakeDate:
        @classmethod
        def today(cls) -> dt.date:
            return real_date(2026, 3, 31)

    monkeypatch.setattr(dt, "date", _FakeDate)

    provider._set_calendar_date(
        driver,
        datetime(2026, 4, 1, 9, 0, 0),
    )

    assert driver.clicked_button is driver.buttons[2]


def test_set_calendar_date_tracks_next_month_plain_day_after_month_label(
    monkeypatch,
) -> None:
    real_date = dt.date
    provider = NaverMapsSeleniumProvider(_make_settings())
    driver = _FakeDriver(
        [
            _FakeCalendarButton(text="31", enabled=True),
            _FakeCalendarButton(text="1", enabled=True),
            _FakeCalendarButton(text="2", enabled=True),
            _FakeCalendarButton(text="4월\n1", enabled=True),
            _FakeCalendarButton(text="2", enabled=True),
        ],
        calendar_month_text="2026.03",
    )

    class _FakeDate:
        @classmethod
        def today(cls) -> dt.date:
            return real_date(2026, 3, 31)

    monkeypatch.setattr(dt, "date", _FakeDate)

    provider._set_calendar_date(
        driver,
        datetime(2026, 4, 2, 9, 0, 0),
    )

    assert driver.clicked_button is driver.buttons[4]


def test_set_calendar_date_selects_month_then_expands_for_future_day(
    monkeypatch,
) -> None:
    real_date = dt.date
    provider = NaverMapsSeleniumProvider(_make_settings())
    target_day_button = _FakeCalendarButton(text="19", enabled=True)
    april_expanded_buttons = [
        _FakeCalendarButton(text="29", enabled=False),
        _FakeCalendarButton(text="30", enabled=False),
        _FakeCalendarButton(text="31", enabled=False),
        _FakeCalendarButton(text="1", enabled=True),
        _FakeCalendarButton(text="2", enabled=True),
        _FakeCalendarButton(text="3", enabled=True),
        _FakeCalendarButton(text="4", enabled=True),
        _FakeCalendarButton(text="5", enabled=True),
        _FakeCalendarButton(text="6", enabled=True),
        _FakeCalendarButton(text="7", enabled=True),
        _FakeCalendarButton(text="8", enabled=True),
        _FakeCalendarButton(text="9", enabled=True),
        _FakeCalendarButton(text="10", enabled=True),
        _FakeCalendarButton(text="11", enabled=True),
        _FakeCalendarButton(text="12", enabled=True),
        _FakeCalendarButton(text="13", enabled=True),
        _FakeCalendarButton(text="14", enabled=True),
        _FakeCalendarButton(text="15", enabled=True),
        _FakeCalendarButton(text="16", enabled=True),
        _FakeCalendarButton(text="17", enabled=True),
        _FakeCalendarButton(text="18", enabled=True),
        target_day_button,
    ]
    april_option = _FakeCalendarButton(text="2026.04")
    expand_button = _FakeCalendarButton(text="펼치기", enabled=True)
    driver = _FakeDriver(
        calendar_month_text="2026.03",
        month_views={
            "2026.03": {
                False: [
                    _FakeCalendarButton(text="29", enabled=False),
                    _FakeCalendarButton(text="30", enabled=False),
                    _FakeCalendarButton(text="31", enabled=True),
                    _FakeCalendarButton(text="4월\n1", enabled=True),
                    _FakeCalendarButton(text="2", enabled=True),
                    _FakeCalendarButton(text="3", enabled=True),
                    _FakeCalendarButton(text="4", enabled=True),
                    _FakeCalendarButton(text="5", enabled=True),
                    _FakeCalendarButton(text="6", enabled=True),
                    _FakeCalendarButton(text="7", enabled=True),
                    _FakeCalendarButton(text="8", enabled=True),
                    _FakeCalendarButton(text="9", enabled=True),
                    _FakeCalendarButton(text="10", enabled=True),
                    _FakeCalendarButton(text="11", enabled=True),
                ]
            },
            "2026.04": {
                False: [
                    _FakeCalendarButton(text="29", enabled=False),
                    _FakeCalendarButton(text="30", enabled=False),
                    _FakeCalendarButton(text="31", enabled=False),
                    _FakeCalendarButton(text="1", enabled=True),
                    _FakeCalendarButton(text="2", enabled=True),
                    _FakeCalendarButton(text="3", enabled=True),
                    _FakeCalendarButton(text="4", enabled=True),
                    _FakeCalendarButton(text="5", enabled=True),
                    _FakeCalendarButton(text="6", enabled=True),
                    _FakeCalendarButton(text="7", enabled=True),
                    _FakeCalendarButton(text="8", enabled=True),
                    _FakeCalendarButton(text="9", enabled=True),
                    _FakeCalendarButton(text="10", enabled=True),
                    _FakeCalendarButton(text="11", enabled=True),
                ],
                True: april_expanded_buttons,
            },
        },
        month_options=[
            _FakeCalendarButton(text="2026.03"),
            april_option,
        ],
        expand_button=expand_button,
    )

    class _FakeDate:
        @classmethod
        def today(cls) -> dt.date:
            return real_date(2026, 3, 31)

    monkeypatch.setattr(dt, "date", _FakeDate)

    provider._set_calendar_date(
        driver,
        datetime(2026, 4, 19, 6, 30, 0),
    )

    assert april_option.clicked is True
    assert expand_button.clicked is True
    assert driver.calendar_month_button.text == "2026.04"
    assert driver.clicked_button is target_day_button
