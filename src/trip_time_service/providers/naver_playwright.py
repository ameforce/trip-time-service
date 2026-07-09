"""Playwright 기반 Naver 지도(차량) 소요시간 provider.

`naver_selenium.py`의 외부 계약(공개 API, 캐시, 좌표 duck typing,
pool round-robin)을 보존하면서 브라우저 자동화만 Selenium에서 Playwright로
이식한다. 순수 파싱/좌표 헬퍼는 `naver_map_parsing`에서 공유한다.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time as _time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from trip_time_service.browser.playwright_runtime import (
    PlaywrightBrowserSession,
    PlaywrightLaunchOptions,
    force_kill_playwright_process,
    launch_browser_session,
)
from trip_time_service.config import Settings
from trip_time_service.core.models import DriveDuration, Route
from trip_time_service.privacy import redact_route, redact_text
from trip_time_service.providers.base import ProviderError
from trip_time_service.providers.naver_map_parsing import (
    _PANEL_DIAGNOSTIC_SAMPLE_LIMIT,
    _PANEL_DIAGNOSTIC_SELECTORS,
    _PANEL_DURATION_SELECTORS,
    build_directions_url,
    duration_tokens,
    extract_duration_from_panel_text,
    matches_requested_time,
)

_log = logging.getLogger(__name__)

_MINUTE_RESOLUTION = 10
_CLOSE_LOCK_TIMEOUT_SECONDS = 2.0
_SESSION_CLOSE_TIMEOUT_SECONDS = 4.0
_POOL_CLOSE_TIMEOUT_SECONDS = 6.0
_POOL_BULK_TERMINATE_TIMEOUT_SECONDS = 1.5
_PAGE_DEFAULT_TIMEOUT_MS = 30000


# ── low-level element/page helpers ──
#
# Playwright ElementHandle / Page 호출은 요소가 detach되면 예외를 던질 수 있어,
# Selenium의 관용적 방어 패턴(`is_displayed()` try/except)과 동일하게 감싼다.


def _sleep(seconds: float) -> None:
    _time.sleep(seconds)


def _safe_visible(element: Any) -> bool:
    try:
        return bool(element.is_visible())
    except Exception:
        return False


def _safe_enabled(element: Any) -> bool:
    try:
        return bool(element.is_enabled())
    except Exception:
        return False


def _safe_text(element: Any) -> str:
    try:
        text = element.inner_text()
    except Exception:
        return ""
    return text.strip() if text else ""


def _click(element: Any) -> None:
    element.click()


def _query_all(page: Any, selector: str) -> list:
    try:
        return list(page.query_selector_all(selector))
    except Exception:
        return []


def _wait_until(
    predicate,
    *,
    timeout: float,
    poll: float = 0.25,
) -> bool:
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        _sleep(poll)
    try:
        return bool(predicate())
    except Exception:
        return False


class _NaverDirectionsSearchAdapter:
    def __init__(self, provider: NaverMapsPlaywrightProvider) -> None:
        self._provider = provider

    def build_directions_url(self, route: Route) -> str | None:
        return build_directions_url(
            route.origin,
            route.destination,
            self._provider._coords.get(route.origin),
            self._provider._coords.get(route.destination),
        )

    def navigate_and_search(self, page: Any, route: Route) -> None:
        direct_url = self.build_directions_url(route)
        if direct_url:
            _log.info(
                "Naver: URL direct route=%s",
                redact_route(route.origin, route.destination),
            )
            try:
                page.goto(
                    direct_url,
                    wait_until="domcontentloaded",
                    timeout=_PAGE_DEFAULT_TIMEOUT_MS,
                )
            except Exception:
                _log.warning("URL 직접 접속 실패, 자동완성으로 전환")
                self.navigate_and_search_ac(page, route)
                return

            found = _wait_until(
                lambda: bool(page.query_selector("button.later_departure_btn")),
                timeout=20,
            )
            if not found:
                _log.warning("URL 직접 접속 후 결과 로딩 실패, 자동완성으로 전환")
                self.navigate_and_search_ac(page, route)
                return
            _sleep(0.5)
            return

        self.navigate_and_search_ac(page, route)

    def navigate_and_search_ac(self, page: Any, route: Route) -> None:
        _log.info(
            "Naver: autocomplete route search route=%s",
            redact_route(route.origin, route.destination),
        )
        page.goto(
            "https://map.naver.com/p/directions/-/-/-/car",
            wait_until="domcontentloaded",
            timeout=_PAGE_DEFAULT_TIMEOUT_MS,
        )
        _sleep(4)

        inputs = _query_all(page, "input.input_search")
        if len(inputs) < 2:
            raise ProviderError("출발지/도착지 입력 필드를 찾을 수 없음")
        self.select_place_ac(page, inputs[0], route.origin)

        inputs = _query_all(page, "input.input_search")
        if not inputs:
            raise ProviderError("도착지 입력 필드를 찾을 수 없음")
        dest_input = inputs[-1]
        try:
            dest_input.focus()
            dest_input.fill("")
        except Exception:
            pass
        _sleep(0.3)
        self.select_place_ac(page, dest_input, route.destination)

        search_btns = [
            btn
            for btn in _query_all(page, "button.btn_direction.search")
            if _safe_visible(btn)
        ]
        if not search_btns:
            raise ProviderError("'길찾기' 버튼을 찾을 수 없음")
        _click(search_btns[0])

        found = _wait_until(
            lambda: bool(page.query_selector("button.later_departure_btn")),
            timeout=15,
        )
        if not found:
            raise ProviderError("경로 결과 로딩 실패 (timeout)")
        _sleep(1)

    def select_place_ac(self, page: Any, input_el: Any, place_name: str) -> None:
        # Selenium 버전은 결과 확인 후에도 ARROW_DOWN×2 + ENTER를 blind로 보냈다.
        # Playwright 버전은 실제로 보이는 DOM item을 클릭해 "무엇을 선택했는지"를
        # 명시적으로 만들고, 보이는 item이 없을 때만 Enter fallback을 쓴다.
        try:
            input_el.focus()
        except Exception:
            pass
        try:
            input_el.fill("")
        except Exception:
            pass
        input_el.type(place_name)
        _sleep(2)

        place_items = _query_all(page, ".list_place li.item_place")
        visible = [item for item in place_items if _safe_visible(item)]
        if not visible:
            _log.warning(
                "Naver autocomplete has no visible result query=%s; pressing Enter",
                redact_text(place_name),
            )
            input_el.press("Enter")
            _sleep(3)
            return

        _log.debug(
            "Naver autocomplete DOM item click query=%s (visible=%d)",
            redact_text(place_name),
            len(visible),
        )
        _click(visible[0])
        _sleep(2)


class _NaverDeparturePickerAdapter:
    def __init__(self, provider: NaverMapsPlaywrightProvider) -> None:
        self._provider = provider

    def is_modal_open(self, page: Any) -> bool:
        close_btns = _query_all(page, "button.later_departure_modal_btn_close")
        if any(_safe_visible(button) for button in close_btns):
            return True
        if self.has_picker_dropdowns(page):
            return True
        if self.find_visible_confirm_buttons(page):
            return True
        return bool(self.find_visible_change_buttons(page))

    def find_visible_confirm_buttons(self, page: Any) -> list:
        buttons = []
        for button in _query_all(page, "button.later_departure_confirm_btn"):
            if _safe_visible(button):
                buttons.append(button)
        for button in _query_all(page, "button:has-text('확인')"):
            if _safe_visible(button) and button not in buttons:
                buttons.append(button)
        return buttons

    def find_visible_change_buttons(self, page: Any) -> list:
        buttons = []
        for selector in (
            "button.later_departure_time_btn",
            "button:has-text('출발 시간 변경')",
            "button:has-text('시간 변경')",
        ):
            for button in _query_all(page, selector):
                if _safe_visible(button) and button not in buttons:
                    buttons.append(button)
        return buttons

    def has_picker_dropdowns(self, page: Any) -> bool:
        visible_count = 0
        for button in _query_all(page, "button.dropdown_btn"):
            if _safe_visible(button):
                visible_count += 1
        return visible_count >= 2

    def wait_picker_controls(self, page: Any, timeout: int = 8) -> None:
        _wait_until(
            lambda: (
                bool(self.find_visible_confirm_buttons(page))
                or self.has_picker_dropdowns(page)
                or bool(self.find_visible_change_buttons(page))
            ),
            timeout=timeout,
        )

    def debug_visible_buttons(self, page: Any, *, limit: int = 8) -> str:
        texts: list[str] = []
        for button in _query_all(page, "button"):
            if len(texts) >= limit:
                break
            if not _safe_visible(button):
                continue
            text = _safe_text(button)
            if not text:
                continue
            texts.append(text)
        if not texts:
            return "none"
        return ", ".join(texts)

    def open_later_modal(self, page: Any) -> None:
        found = _wait_until(
            lambda: any(
                _safe_visible(button) and _safe_enabled(button)
                for button in _query_all(page, "button.later_departure_btn")
            ),
            timeout=10,
        )
        later_btns = [
            button
            for button in _query_all(page, "button.later_departure_btn")
            if _safe_visible(button)
        ]
        if not found or not later_btns:
            raise ProviderError("'나중에 출발' 버튼을 찾을 수 없음")
        later_btn = later_btns[0]

        if self.is_modal_open(page):
            _log.debug("나중에 출발 모달 이미 열림")
            return

        _click(later_btn)

        if _wait_until(
            lambda: any(
                _safe_visible(button)
                for button in _query_all(
                    page, "button.later_departure_modal_btn_close"
                )
            ),
            timeout=6,
        ):
            _sleep(0.3)
            return

        _log.debug("나중에 출발 토글 재시도")
        _click(later_btn)

        if _wait_until(
            lambda: any(
                _safe_visible(button)
                for button in _query_all(
                    page, "button.later_departure_modal_btn_close"
                )
            ),
            timeout=6,
        ):
            _sleep(0.3)
            return
        raise ProviderError("'나중에 출발' 모달 열기 실패")

    def ensure_picker_open(self, page: Any) -> None:
        if self.find_visible_confirm_buttons(page):
            return
        if self.has_picker_dropdowns(page):
            return

        visible_change_buttons = self.find_visible_change_buttons(page)
        if not visible_change_buttons:
            self.wait_picker_controls(page, timeout=6)
            if self.find_visible_confirm_buttons(page):
                return
            if self.has_picker_dropdowns(page):
                return
            visible_change_buttons = self.find_visible_change_buttons(page)

        if not visible_change_buttons:
            _log.debug("'출발 시간 변경' 미발견, 모달 재오픈")
            self._provider._modal_open = False
            self.open_later_modal(page)
            self._provider._modal_open = True
            self.wait_picker_controls(page, timeout=10)

            if self.find_visible_confirm_buttons(page):
                return
            if self.has_picker_dropdowns(page):
                return

            visible_change_buttons = self.find_visible_change_buttons(page)
            if not visible_change_buttons:
                visible_buttons = self.debug_visible_buttons(page)
                raise ProviderError(
                    "'출발 시간 변경' 버튼을 찾을 수 없음 "
                    f"(visible_buttons={visible_buttons})"
                )

        _click(visible_change_buttons[0])
        opened = _wait_until(
            lambda: (
                bool(self.find_visible_confirm_buttons(page))
                or self.has_picker_dropdowns(page)
            ),
            timeout=8,
        )
        if not opened:
            raise ProviderError("시간 선택기 열기 실패")
        _sleep(0.3)

    def set_time_and_read(
        self,
        page: Any,
        hour_24: int,
        minute_10: int,
        departure_time: datetime | None = None,
    ) -> int:
        is_pm = hour_24 >= 12
        hour_12 = hour_24 % 12
        if hour_12 == 0:
            hour_12 = 12

        ampm_text = "오후" if is_pm else "오전"
        hour_text = f"{hour_12}시"
        minute_text = f"{minute_10:02d}분"

        self.ensure_picker_open(page)

        if departure_time is not None:
            self.set_calendar_date(page, departure_time)

        self.set_dropdown_value(page, "ampm", ampm_text)
        self.set_dropdown_value(page, "hour", hour_text)
        self.set_dropdown_value(page, "minute", minute_text)

        _sleep(0.5)

        visible_confirms = [
            button
            for button in _query_all(page, "button.later_departure_confirm_btn")
            if _safe_visible(button)
        ]
        if not visible_confirms:
            raise ProviderError("확인 버튼을 찾을 수 없음")

        confirm = visible_confirms[0]
        button_text = _safe_text(confirm)

        if "미래시간" in button_text or "선택해주세요" in button_text:
            _log.warning(
                "과거 시간 선택 불가: %s %s %s → '%s'",
                ampm_text,
                hour_text,
                minute_text,
                button_text,
            )
            raise ProviderError(
                f"과거 시간 조회 불가 ({ampm_text} {hour_text} {minute_text})",
                is_retryable=False,
            )

        _click(confirm)

        _wait_until(
            lambda: not any(
                _safe_visible(button)
                for button in _query_all(
                    page, "button.later_departure_confirm_btn"
                )
            ),
            timeout=8,
        )
        _sleep(0.3)

        still_visible = [
            button
            for button in _query_all(page, "button.later_departure_confirm_btn")
            if _safe_visible(button)
        ]
        if still_visible:
            current_text = _safe_text(still_visible[0])
            if "미래시간" in current_text or "선택해주세요" in current_text:
                _log.warning("확인 클릭 후 과거 시간 에러: '%s'", current_text)
                raise ProviderError(
                    f"과거 시간 조회 불가 ({ampm_text} {hour_text} {minute_text})",
                    is_retryable=False,
                )

            _sleep(2)
            still_visible = [
                button
                for button in _query_all(page, "button.later_departure_confirm_btn")
                if _safe_visible(button)
            ]
            if still_visible:
                current_text = _safe_text(still_visible[0])
                if "미래시간" in current_text or "선택해주세요" in current_text:
                    _log.warning("지연 감지 - 과거 시간 에러: '%s'", current_text)
                    raise ProviderError(
                        f"과거 시간 조회 불가 "
                        f"({ampm_text} {hour_text} {minute_text})",
                        is_retryable=False,
                    )
                _log.warning("확인 클릭 실패 (버튼 잔존), 재시도")
                _click(still_visible[0])
                _sleep(3)

                still_visible = [
                    button
                    for button in _query_all(
                        page, "button.later_departure_confirm_btn"
                    )
                    if _safe_visible(button)
                ]
                if still_visible:
                    final_text = _safe_text(still_visible[0])
                    if "미래시간" in final_text or "선택해주세요" in final_text:
                        raise ProviderError(
                            f"과거 시간 조회 불가 "
                            f"({ampm_text} {hour_text} {minute_text})",
                            is_retryable=False,
                        )
                    raise ProviderError(
                        f"확인 버튼 클릭 불가 "
                        f"({ampm_text} {hour_text} {minute_text})",
                        is_retryable=False,
                    )

        duration = self.wait_panel_duration(
            page,
            ampm_text,
            hour_text,
            minute_text,
            timeout_seconds=8.0,
        )
        if duration is None:
            self.raise_panel_parse_timeout(page, ampm_text, hour_text, minute_text)

        change_btns = _query_all(page, "button.later_departure_time_btn")
        if not change_btns or not any(_safe_visible(button) for button in change_btns):
            _log.debug("모달이 닫힘 (확인 후), 재오픈 필요")
            self._provider._modal_open = False

        return duration

    def matches_requested_time(
        self,
        text: str,
        ampm: str,
        hour: str,
        minute: str,
    ) -> bool:
        return matches_requested_time(text, ampm, hour, minute)

    def extract_duration_from_panel_text(
        self,
        text: str,
        ampm: str,
        hour: str,
        minute: str,
    ) -> int | None:
        return extract_duration_from_panel_text(text, ampm, hour, minute)

    def read_duration_from_panel_dialog(
        self,
        page: Any,
        ampm: str,
        hour: str,
        minute: str,
    ) -> int | None:
        for selector in _PANEL_DURATION_SELECTORS:
            for element in _query_all(page, selector):
                if not _safe_visible(element):
                    continue
                raw = _safe_text(element)
                if not raw:
                    continue
                duration = extract_duration_from_panel_text(
                    raw,
                    ampm,
                    hour,
                    minute,
                )
                if duration is not None:
                    return duration
        return None

    def _duration_tokens(self, text: str) -> list[str]:
        return duration_tokens(text)

    def panel_parse_diagnostics(
        self,
        page: Any,
        ampm: str,
        hour: str,
        minute: str,
    ) -> dict[str, object]:
        selectors: dict[str, dict[str, int]] = {}
        panel_samples: list[dict[str, object]] = []
        for selector in _PANEL_DIAGNOSTIC_SELECTORS:
            elements = _query_all(page, selector)

            visible_count = 0
            for element in elements:
                is_visible = _safe_visible(element)
                if is_visible:
                    visible_count += 1

                if selector in _PANEL_DURATION_SELECTORS and (
                    len(panel_samples) < _PANEL_DIAGNOSTIC_SAMPLE_LIMIT
                ):
                    raw_text = _safe_text(element)
                    panel_samples.append(
                        {
                            "visible": is_visible,
                            "text": redact_text(raw_text),
                            "has_requested_time": matches_requested_time(
                                raw_text,
                                ampm,
                                hour,
                                minute,
                            ),
                            "has_duration_word": "소요" in raw_text,
                            "duration_tokens": duration_tokens(raw_text),
                        }
                    )

            selectors[selector] = {"count": len(elements), "visible": visible_count}

        return {
            "code": "panel_parse_timeout",
            "requested_time": {"ampm": ampm, "hour": hour, "minute": minute},
            "selectors": selectors,
            "panel_samples": panel_samples,
        }

    def write_panel_parse_diagnostics(
        self,
        page: Any,
        ampm: str,
        hour: str,
        minute: str,
    ) -> Path | None:
        artifacts_dir = os.environ.get("TTS_E2E_ARTIFACTS_DIR")
        if not artifacts_dir:
            return None
        try:
            output_dir = Path(artifacts_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = (
                output_dir / f"naver-panel-diagnostics-{_time.time_ns()}.json"
            )
            payload = self.panel_parse_diagnostics(page, ampm, hour, minute)
            output_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            return output_path
        except Exception:
            _log.warning("Naver panel diagnostics artifact write failed", exc_info=True)
            return None

    def raise_panel_parse_timeout(
        self,
        page: Any,
        ampm: str,
        hour: str,
        minute: str,
    ) -> None:
        artifact_path = self.write_panel_parse_diagnostics(page, ampm, hour, minute)
        artifact_hint = f" diagnostics={artifact_path.name}" if artifact_path else ""
        raise ProviderError(
            "나중에 출발 패널 소요시간 읽기 실패 "
            f"({ampm} {hour} {minute}){artifact_hint}",
            code="panel_parse_timeout",
            bucket="panel_parse_timeout",
        )

    def wait_panel_duration(
        self,
        page: Any,
        ampm: str,
        hour: str,
        minute: str,
        *,
        timeout_seconds: float,
    ) -> int | None:
        deadline = _time.monotonic() + timeout_seconds
        while _time.monotonic() < deadline:
            duration = self.read_duration_from_panel_dialog(page, ampm, hour, minute)
            if duration is not None:
                return duration
            _sleep(0.25)
        return self.read_duration_from_panel_dialog(page, ampm, hour, minute)

    def visible_calendar_day_buttons(self, page: Any) -> list:
        visible_buttons = []
        for button in _query_all(page, "button.calendar_day_btn"):
            if _safe_visible(button):
                visible_buttons.append(button)
        return visible_buttons

    def find_calendar_day_button(self, page: Any, target_date):
        day_text = str(target_date.day)
        current_year_month = self.read_calendar_display_year_month(page)
        fallback_button = None
        for button in self.visible_calendar_day_buttons(page):
            if not _safe_enabled(button):
                continue
            text_lines = [
                line.strip()
                for line in _safe_text(button).splitlines()
                if line.strip()
            ]
            if not text_lines:
                continue
            month_label_match = re.search(r"(\d{1,2})월", text_lines[0])
            if month_label_match:
                label_month = int(month_label_match.group(1))
                if current_year_month is None:
                    current_year_month = (target_date.year, label_month)
                else:
                    current_year, current_month = current_year_month
                    if label_month < current_month:
                        current_year += 1
                    current_year_month = (current_year, label_month)
            button_day_text = text_lines[-1]
            if button_day_text != day_text:
                continue
            if current_year_month == (target_date.year, target_date.month):
                return button
            if current_year_month is None and fallback_button is None:
                fallback_button = button
        if fallback_button is not None:
            return fallback_button
        return None

    def read_calendar_display_year_month(self, page: Any) -> tuple[int, int] | None:
        for button in _query_all(page, "button.calendar_date_btn"):
            if not _safe_visible(button):
                continue
            text = _safe_text(button)
            match = re.search(r"(\d{4})\.(\d{2})$", text)
            if match:
                return int(match.group(1)), int(match.group(2))
            match = re.search(r"(\d{1,2})월", text)
            if match:
                return datetime.now().year, int(match.group(1))
        return None

    def select_calendar_month(self, page: Any, target_date) -> bool:
        target_text = target_date.strftime("%Y.%m")
        target_year_month = (target_date.year, target_date.month)
        if self.read_calendar_display_year_month(page) == target_year_month:
            return True

        month_buttons = [
            button
            for button in _query_all(page, "button.calendar_date_btn")
            if _safe_visible(button) and _safe_enabled(button)
        ]
        if not month_buttons:
            return False

        _click(month_buttons[0])
        _sleep(0.25)

        visible_options: list[str] = []
        for selector in ("button.list_item_btn", "button[role='option']"):
            for option in _query_all(page, selector):
                if not _safe_visible(option) or not _safe_enabled(option):
                    continue
                option_text = _safe_text(option)
                visible_options.append(option_text)
                if option_text != target_text:
                    continue
                _click(option)
                _sleep(0.35)
                return True
            if visible_options:
                break

        _log.warning(
            "캘린더 월 옵션 '%s'을 찾을 수 없음 (visible_options=%s)",
            target_text,
            visible_options,
        )
        return False

    def expand_calendar_days_if_needed(self, page: Any) -> bool:
        visible_count_before = len(self.visible_calendar_day_buttons(page))
        if visible_count_before >= 28:
            return True

        expand_buttons: list = []
        for selector in (
            "button.calendar_expand_btn",
            "button:has-text('펼치기')",
        ):
            for button in _query_all(page, selector):
                if _safe_visible(button) and button not in expand_buttons:
                    expand_buttons.append(button)
            if expand_buttons:
                break

        if not expand_buttons:
            return False

        _click(expand_buttons[0])
        _sleep(0.35)
        visible_count_after = len(self.visible_calendar_day_buttons(page))
        _log.debug(
            "캘린더 확장 클릭: visible_day_count=%d->%d",
            visible_count_before,
            visible_count_after,
        )
        return visible_count_after > visible_count_before

    def set_calendar_date(self, page: Any, departure_time: datetime) -> None:
        from datetime import date as _date

        today = _date.today()
        target = departure_time.date()
        if target == today:
            return

        day_button = self.find_calendar_day_button(page, target)
        if day_button is None:
            displayed_year_month = self.read_calendar_display_year_month(page)
            if displayed_year_month != (target.year, target.month):
                if self.select_calendar_month(page, target):
                    day_button = self.find_calendar_day_button(page, target)

        if day_button is None and self.expand_calendar_days_if_needed(page):
            day_button = self.find_calendar_day_button(page, target)

        if day_button is not None:
            _click(day_button)
            _sleep(0.5)
            _log.debug("캘린더 날짜 선택: %s", target.strftime("%Y-%m-%d"))
            return

        visible_days = [
            _safe_text(button).replace("\n", " ")
            for button in self.visible_calendar_day_buttons(page)
        ]
        _log.warning(
            "캘린더에서 %s일을 찾을 수 없음 (target=%s, visible_days=%s)",
            target.day,
            target.strftime("%Y-%m-%d"),
            visible_days,
        )

    def set_dropdown_value(self, page: Any, kind: str, target: str) -> None:
        button = None
        for candidate in _query_all(page, "button.dropdown_btn"):
            if not _safe_visible(candidate):
                continue
            text = _safe_text(candidate)
            if kind == "ampm" and text in ("오전", "오후"):
                button = candidate
                break
            if kind == "hour" and re.match(r"^\d{1,2}시$", text):
                button = candidate
                break
            if kind == "minute" and re.match(r"^\d{2}분$", text):
                button = candidate
                break

        if button is None:
            raise ProviderError(f"드롭다운 버튼 미발견: {kind}={target}")
        if _safe_text(button) == target:
            return

        _click(button)
        _sleep(0.5)

        for option in _query_all(page, "[role='option']"):
            if _safe_visible(option) and _safe_text(option) == target:
                _click(option)
                _sleep(0.3)
                return

        _click(button)
        _sleep(0.2)
        raise ProviderError(f"드롭다운 옵션 미발견: {kind}={target}")


class NaverMapsPlaywrightProvider:
    name = "naver_playwright"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._session: PlaywrightBrowserSession | None = None
        self._current_route: Route | None = None
        self._modal_open = False
        # (origin, dest, date_iso, hour24, min10) → duration_seconds
        self._dur_cache: dict[tuple, int] = {}
        self._coords: dict[str, tuple[float, float]] = {}
        self._search_adapter = _NaverDirectionsSearchAdapter(self)
        self._departure_picker = _NaverDeparturePickerAdapter(self)

    # ── public API ──

    def set_coords(self, place: str, lat: float, lon: float) -> None:
        self._coords[place] = (lat, lon)

    def get_drive_duration(
        self, route: Route, departure_time: datetime,
    ) -> DriveDuration:
        m10 = (departure_time.minute // _MINUTE_RESOLUTION) * _MINUTE_RESOLUTION
        cache_key = (
            route.origin,
            route.destination,
            departure_time.date().isoformat(),
            departure_time.hour,
            m10,
        )

        with self._lock:
            if cache_key in self._dur_cache:
                secs = self._dur_cache[cache_key]
                return DriveDuration(
                    duration_seconds=secs,
                    fetched_at=datetime.now(tz=departure_time.tzinfo),
                    raw_text=f"네이버 지도 (캐시): {secs // 60}분",
                )

            session = self._ensure_session_locked()
            try:
                secs = self._query_locked(
                    session.page, route, departure_time, m10
                )
            except ProviderError as exc:
                if not exc.is_retryable:
                    _log.warning(
                        "Naver non-retryable provider error route=%s error=%s",
                        redact_route(route.origin, route.destination),
                        exc,
                    )
                    raise
                _log.warning(
                    "Naver retryable provider error; resetting session route=%s "
                    "error=%s",
                    redact_route(route.origin, route.destination),
                    exc,
                )
                self._reset_state()
                raise
            except Exception as exc:
                _log.warning(
                    "Naver query failed; resetting session route=%s",
                    redact_route(route.origin, route.destination),
                    exc_info=True,
                )
                self._reset_state()
                raise ProviderError(
                    "네이버 지도 조회 실패", cause=exc,
                ) from exc

            self._dur_cache[cache_key] = secs
            _log.info(
                "Naver: route=%s [%02d:%02d] = %d초 (%d분)",
                redact_route(route.origin, route.destination),
                departure_time.hour, m10,
                secs, secs // 60,
            )
            return DriveDuration(
                duration_seconds=secs,
                fetched_at=datetime.now(tz=departure_time.tzinfo),
                raw_text=f"네이버 지도: {secs // 60}분",
            )

    def get_drive_duration_once(
        self,
        route: Route,
        departure_time: datetime,
    ) -> DriveDuration:
        try:
            return self.get_drive_duration(route, departure_time)
        finally:
            self.release_session()

    def release_session(self) -> None:
        self._lock.acquire()
        try:
            self._close_session_locked()
        finally:
            self._lock.release()

    def close(self) -> None:
        acquired = self._lock.acquire(timeout=_CLOSE_LOCK_TIMEOUT_SECONDS)
        if not acquired:
            _log.warning(
                "Naver provider close skipped after %.1fs (busy query lock)",
                _CLOSE_LOCK_TIMEOUT_SECONDS,
            )
            session = self._session
            if session is not None:
                self._force_kill_session(session)
            return
        try:
            self._close_session_locked()
        finally:
            self._lock.release()

    def terminate_session(self) -> None:
        acquired = self._lock.acquire(timeout=_CLOSE_LOCK_TIMEOUT_SECONDS)
        if not acquired:
            session = self._session
            if session is not None:
                self._force_kill_session(session)
            self._session = None
            self._current_route = None
            self._modal_open = False
            return
        try:
            self._close_session_locked()
        finally:
            self._lock.release()

    # ── session 관리 ──

    def _ensure_session_locked(self) -> PlaywrightBrowserSession:
        if self._session is not None:
            return self._session

        options = PlaywrightLaunchOptions(
            headless=self._settings.headless,
            user_data_dir=self._settings.chrome_user_data_dir,
        )
        session = launch_browser_session(options)
        try:
            session.page.set_default_navigation_timeout(_PAGE_DEFAULT_TIMEOUT_MS)
            session.page.set_default_timeout(_PAGE_DEFAULT_TIMEOUT_MS)
        except Exception:
            pass
        self._session = session
        return session

    def _close_session_locked(self) -> None:
        if self._session is None:
            return
        session = self._session
        try:
            result = session.close(
                close_timeout_seconds=_SESSION_CLOSE_TIMEOUT_SECONDS,
            )
            if result.timed_out:
                _log.warning(
                    "Naver session close timeout after %.1fs; process killed",
                    _SESSION_CLOSE_TIMEOUT_SECONDS,
                )
            elif result.close_error is not None:
                _log.debug("Naver session close raised: %s", result.close_error)
        finally:
            self._session = None
            self._current_route = None
            self._modal_open = False

    def _force_kill_session(self, session: PlaywrightBrowserSession) -> None:
        target = session.browser if session.browser is not None else session.context
        force_kill_playwright_process(target)

    def _reset_state(self) -> None:
        self._close_session_locked()

    # ── 핵심 로직 ──

    def _query_locked(
        self,
        page: Any,
        route: Route,
        departure_time: datetime,
        m10: int,
    ) -> int:
        # 1) 경로가 바뀌면 새로 검색
        if self._current_route != route:
            self._search_adapter.navigate_and_search(page, route)
            self._current_route = route
            self._modal_open = False

        # 2) "나중에 출발" 모달 열기
        if not self._modal_open:
            self._departure_picker.open_later_modal(page)
            self._modal_open = True

        # 3) 시간 설정 + 결과 읽기
        return self._departure_picker.set_time_and_read(
            page, departure_time.hour, m10, departure_time,
        )

    # ── seam wrappers (테스트/호환용) ──

    def _build_directions_url(self, route: Route) -> str | None:
        return self._search_adapter.build_directions_url(route)

    def _navigate_and_search(self, page: Any, route: Route) -> None:
        self._search_adapter.navigate_and_search(page, route)

    def _navigate_and_search_ac(self, page: Any, route: Route) -> None:
        self._search_adapter.navigate_and_search_ac(page, route)

    def _select_place_ac(self, page: Any, input_el: Any, place_name: str) -> None:
        self._search_adapter.select_place_ac(page, input_el, place_name)

    def _open_later_modal(self, page: Any) -> None:
        self._departure_picker.open_later_modal(page)

    def _ensure_picker_open(self, page: Any) -> None:
        self._departure_picker.ensure_picker_open(page)

    def _set_time_and_read(
        self,
        page: Any,
        hour_24: int,
        minute_10: int,
        departure_time: datetime | None = None,
    ) -> int:
        return self._departure_picker.set_time_and_read(
            page,
            hour_24,
            minute_10,
            departure_time,
        )

    def _read_duration_from_panel_dialog(
        self,
        page: Any,
        ampm: str,
        hour: str,
        minute: str,
    ) -> int | None:
        return self._departure_picker.read_duration_from_panel_dialog(
            page,
            ampm,
            hour,
            minute,
        )

    def _raise_panel_parse_timeout(
        self,
        page: Any,
        ampm: str,
        hour: str,
        minute: str,
    ) -> None:
        self._departure_picker.raise_panel_parse_timeout(page, ampm, hour, minute)

    def _set_calendar_date(self, page: Any, departure_time: datetime) -> None:
        self._departure_picker.set_calendar_date(page, departure_time)

    def _set_dropdown_value(self, page: Any, kind: str, target: str) -> None:
        self._departure_picker.set_dropdown_value(page, kind, target)


class NaverMapsPlaywrightPoolProvider:
    name = "naver_playwright"

    def __init__(self, settings: Settings) -> None:
        pool_size = max(1, settings.naver_session_pool_size)
        self.max_parallel_sessions = pool_size
        self._index_lock = threading.Lock()
        self._next_index = 0
        self._workers = tuple(
            NaverMapsPlaywrightProvider(
                _build_worker_settings(settings, idx, pool_size)
            )
            for idx in range(pool_size)
        )

    def set_coords(self, place: str, lat: float, lon: float) -> None:
        for worker in self._workers:
            worker.set_coords(place, lat, lon)

    def get_drive_duration(
        self,
        route: Route,
        departure_time: datetime,
    ) -> DriveDuration:
        worker = self._pick_worker()
        return worker.get_drive_duration(route, departure_time)

    def get_drive_duration_once(
        self,
        route: Route,
        departure_time: datetime,
    ) -> DriveDuration:
        worker = self._pick_worker()
        try:
            return worker.get_drive_duration(route, departure_time)
        finally:
            worker.release_session()

    def close(self) -> None:
        self._terminate_all_sessions()

        close_threads: list[threading.Thread] = []
        for index, worker in enumerate(self._workers, start=1):
            thread = threading.Thread(
                target=self._close_worker_safely,
                args=(index, worker),
                name=f"naver-pool-close-{index}",
                daemon=True,
            )
            thread.start()
            close_threads.append(thread)

        deadline = _time.monotonic() + _POOL_CLOSE_TIMEOUT_SECONDS
        for thread in close_threads:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)

        alive_threads = sum(1 for thread in close_threads if thread.is_alive())
        if alive_threads:
            _log.warning(
                "Naver pool close timeout after %.1fs; %d workers still active",
                _POOL_CLOSE_TIMEOUT_SECONDS,
                alive_threads,
            )

    def _terminate_all_sessions(self) -> None:
        terminate_threads: list[threading.Thread] = []
        for index, worker in enumerate(self._workers, start=1):
            thread = threading.Thread(
                target=self._terminate_worker_session_safely,
                args=(index, worker),
                name=f"naver-pool-terminate-{index}",
                daemon=True,
            )
            thread.start()
            terminate_threads.append(thread)

        deadline = _time.monotonic() + _POOL_BULK_TERMINATE_TIMEOUT_SECONDS
        for thread in terminate_threads:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)

    def _terminate_worker_session_safely(
        self,
        index: int,
        worker: NaverMapsPlaywrightProvider,
    ) -> None:
        terminate = getattr(worker, "terminate_session", None)
        if terminate is None:
            return
        try:
            terminate()
        except Exception:
            _log.exception("Naver worker terminate failed (worker=%d)", index)

    def _close_worker_safely(
        self,
        index: int,
        worker: NaverMapsPlaywrightProvider,
    ) -> None:
        try:
            worker.close()
        except Exception:
            _log.exception("Naver worker close failed (worker=%d)", index)

    def _pick_worker(self) -> NaverMapsPlaywrightProvider:
        with self._index_lock:
            worker = self._workers[self._next_index]
            self._next_index = (self._next_index + 1) % len(self._workers)
            return worker


def _build_worker_settings(
    settings: Settings,
    index: int,
    pool_size: int,
) -> Settings:
    user_data_dir = settings.chrome_user_data_dir
    if user_data_dir and pool_size > 1:
        base = Path(user_data_dir)
        worker_dir = base.parent / f"{base.name}-worker-{index + 1}"
        worker_dir.mkdir(parents=True, exist_ok=True)
        user_data_dir = str(worker_dir)

    return replace(
        settings,
        chrome_user_data_dir=user_data_dir,
        naver_session_pool_size=1,
    )
