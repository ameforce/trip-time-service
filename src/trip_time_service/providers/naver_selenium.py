from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time as _time
import urllib.parse
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from trip_time_service.chrome_driver import (
    build_chrome_options,
    close_webdriver_with_timeout,
    force_kill_webdriver_process,
)
from trip_time_service.config import Settings
from trip_time_service.core.models import DriveDuration, Route
from trip_time_service.privacy import redact_route, redact_text
from trip_time_service.providers.base import ProviderError

_log = logging.getLogger(__name__)

_MINUTE_RESOLUTION = 10
_CLOSE_LOCK_TIMEOUT_SECONDS = 2.0
_DRIVER_QUIT_TIMEOUT_SECONDS = 4.0
_POOL_CLOSE_TIMEOUT_SECONDS = 6.0
_POOL_BULK_TERMINATE_TIMEOUT_SECONDS = 1.5
_PANEL_DIAGNOSTIC_SAMPLE_LIMIT = 5
_PANEL_DIAGNOSTIC_TOKEN_LIMIT = 5
_PANEL_DIAGNOSTIC_SELECTORS = (
    "div.panel_dialog",
    "div.summary_content",
    "p.summary_departure_time_text",
    "p.summary_duration_text",
    "em.later_depature_time_text",
    "div.later_departure_current_time",
    "button.later_departure_time_btn",
    "button.later_departure_confirm_btn",
    "button.dropdown_btn",
)
_PANEL_DURATION_SELECTORS = (
    "div.panel_dialog",
    # Naver currently renders the confirmed later-departure result in the
    # map-side summary while keeping the legacy panel_dialog node hidden.
    "div.summary_content",
)


def _latlon_to_epsg3857(lat: float, lon: float) -> tuple[float, float]:
    x = lon * 20037508.34 / 180.0
    lat_rad = math.log(math.tan((90.0 + lat) * math.pi / 360.0))
    y = lat_rad * 20037508.34 / math.pi
    return x, y


_DUR_RE = re.compile(
    r"(?:(\d+)\s*시간\s*)?(\d+)\s*분",
)
_DUR_WITH_SOYO_RE = re.compile(
    r"(?:(\d+)\s*시간\s*)?(?:(\d+)\s*분)?\s*소요",
)


def _parse_naver_duration(text: str) -> int:
    if "소요" in text:
        soyo_matches = list(_DUR_WITH_SOYO_RE.finditer(text))
        if not soyo_matches:
            raise ProviderError(
                f"소요시간 파싱 실패: {text!r}",
                is_retryable=False,
            )
        soyo_match = soyo_matches[-1]
        hours = int(soyo_match.group(1)) if soyo_match.group(1) else 0
        minutes = int(soyo_match.group(2)) if soyo_match.group(2) else 0
        return (hours * 60 + minutes) * 60

    matches = list(_DUR_RE.finditer(text))
    if not matches:
        raise ProviderError(
            f"소요시간 파싱 실패: {text!r}",
            is_retryable=False,
        )
    match = matches[-1]
    hours = int(match.group(1)) if match.group(1) else 0
    minutes = int(match.group(2))
    return (hours * 60 + minutes) * 60


class _NaverDirectionsSearchAdapter:
    def __init__(self, provider: NaverMapsSeleniumProvider) -> None:
        self._provider = provider

    def build_directions_url(self, route: Route) -> str | None:
        o_coords = self._provider._coords.get(route.origin)
        d_coords = self._provider._coords.get(route.destination)
        if not o_coords or not d_coords:
            return None

        ox, oy = _latlon_to_epsg3857(o_coords[0], o_coords[1])
        dx, dy = _latlon_to_epsg3857(d_coords[0], d_coords[1])

        o_name = urllib.parse.quote(route.origin)
        d_name = urllib.parse.quote(route.destination)

        return (
            f"https://map.naver.com/p/directions/"
            f"{ox:.7f},{oy:.7f},{o_name},,/"
            f"{dx:.7f},{dy:.7f},{d_name},,/"
            f"-/car"
        )

    def navigate_and_search(
        self,
        driver: webdriver.Chrome,
        route: Route,
    ) -> None:
        direct_url = self.build_directions_url(route)
        if direct_url:
            _log.info(
                "Naver: URL direct route=%s",
                redact_route(route.origin, route.destination),
            )
            driver.get(direct_url)

            try:
                WebDriverWait(driver, 20).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "button.later_departure_btn")
                    )
                )
            except Exception:
                _log.warning("URL 직접 접속 후 결과 로딩 실패, 자동완성으로 전환")
                self.navigate_and_search_ac(driver, route)
                return
            _time.sleep(0.5)
            return

        self.navigate_and_search_ac(driver, route)

    def navigate_and_search_ac(
        self,
        driver: webdriver.Chrome,
        route: Route,
    ) -> None:
        _log.info(
            "Naver: autocomplete route search route=%s",
            redact_route(route.origin, route.destination),
        )
        driver.get("https://map.naver.com/p/directions/-/-/-/car")
        _time.sleep(4)

        inputs = driver.find_elements(By.CSS_SELECTOR, "input.input_search")
        if len(inputs) < 2:
            raise ProviderError("출발지/도착지 입력 필드를 찾을 수 없음")
        self.select_place_ac(driver, inputs[0], route.origin)

        inputs = driver.find_elements(By.CSS_SELECTOR, "input.input_search")
        dest_input = inputs[-1]
        driver.execute_script(
            "arguments[0].focus(); arguments[0].value = '';",
            dest_input,
        )
        _time.sleep(0.3)
        self.select_place_ac(driver, dest_input, route.destination)

        search_btns = driver.find_elements(
            By.CSS_SELECTOR,
            "button.btn_direction.search",
        )
        if not search_btns or not search_btns[0].is_displayed():
            raise ProviderError("'길찾기' 버튼을 찾을 수 없음")
        driver.execute_script("arguments[0].click();", search_btns[0])

        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "button.later_departure_btn")
                )
            )
        except Exception as exc:
            raise ProviderError(
                "경로 결과 로딩 실패 (timeout)",
                cause=exc,
            ) from exc
        _time.sleep(1)

    def select_place_ac(
        self,
        driver: webdriver.Chrome,
        input_el,
        place_name: str,
    ) -> None:
        driver.execute_script("arguments[0].focus();", input_el)
        _time.sleep(0.2)
        input_el.send_keys(place_name)
        _time.sleep(2)

        place_items = driver.find_elements(
            By.CSS_SELECTOR,
            ".list_place li.item_place",
        )
        visible = [it for it in place_items if it.is_displayed()]
        if not visible:
            _log.warning(
                "Naver autocomplete has no visible result query=%s; pressing Enter",
                redact_text(place_name),
            )
            input_el.send_keys(Keys.ENTER)
            _time.sleep(3)
            return

        input_el.send_keys(Keys.ARROW_DOWN)
        _time.sleep(0.15)
        input_el.send_keys(Keys.ARROW_DOWN)
        _time.sleep(0.15)
        input_el.send_keys(Keys.ENTER)
        _time.sleep(2)


class _NaverDeparturePickerAdapter:
    def __init__(self, provider: NaverMapsSeleniumProvider) -> None:
        self._provider = provider

    def is_modal_open(self, driver: webdriver.Chrome) -> bool:
        close_btns = driver.find_elements(
            By.CSS_SELECTOR,
            "button.later_departure_modal_btn_close",
        )
        if any(button.is_displayed() for button in close_btns):
            return True
        if self.has_picker_dropdowns(driver):
            return True
        if self.find_visible_confirm_buttons(driver):
            return True
        return bool(self.find_visible_change_buttons(driver))

    def find_visible_confirm_buttons(self, driver: webdriver.Chrome) -> list:
        buttons = []
        for by, selector in (
            (By.CSS_SELECTOR, "button.later_departure_confirm_btn"),
            (By.XPATH, "//button[contains(normalize-space(.), '확인')]"),
        ):
            for button in driver.find_elements(by, selector):
                try:
                    if button.is_displayed():
                        buttons.append(button)
                except Exception:
                    continue
        return buttons

    def find_visible_change_buttons(self, driver: webdriver.Chrome) -> list:
        buttons = []
        for by, selector in (
            (By.CSS_SELECTOR, "button.later_departure_time_btn"),
            (
                By.XPATH,
                "//button[contains(normalize-space(.), '출발 시간 변경')]",
            ),
            (By.XPATH, "//button[contains(normalize-space(.), '시간 변경')]"),
        ):
            for button in driver.find_elements(by, selector):
                try:
                    if button.is_displayed():
                        buttons.append(button)
                except Exception:
                    continue
        return buttons

    def has_picker_dropdowns(self, driver: webdriver.Chrome) -> bool:
        visible_count = 0
        for button in driver.find_elements(By.CSS_SELECTOR, "button.dropdown_btn"):
            try:
                if button.is_displayed():
                    visible_count += 1
            except Exception:
                continue
        return visible_count >= 2

    def wait_picker_controls(
        self,
        driver: webdriver.Chrome,
        timeout: int = 8,
    ) -> None:
        try:
            WebDriverWait(driver, timeout).until(
                lambda current_driver: (
                    bool(self.find_visible_confirm_buttons(current_driver))
                    or self.has_picker_dropdowns(current_driver)
                    or bool(self.find_visible_change_buttons(current_driver))
                )
            )
        except Exception:
            return

    def debug_visible_buttons(
        self,
        driver: webdriver.Chrome,
        *,
        limit: int = 8,
    ) -> str:
        texts: list[str] = []
        for button in driver.find_elements(By.CSS_SELECTOR, "button"):
            if len(texts) >= limit:
                break
            try:
                if not button.is_displayed():
                    continue
                text = button.text.strip()
                if not text:
                    continue
                texts.append(text)
            except Exception:
                continue
        if not texts:
            return "none"
        return ", ".join(texts)

    def open_later_modal(self, driver: webdriver.Chrome) -> None:
        later_btn = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "button.later_departure_btn")
            )
        )

        if self.is_modal_open(driver):
            _log.debug("나중에 출발 모달 이미 열림")
            return

        driver.execute_script("arguments[0].click();", later_btn)

        try:
            WebDriverWait(driver, 6).until(
                lambda current_driver: any(
                    button.is_displayed()
                    for button in current_driver.find_elements(
                        By.CSS_SELECTOR,
                        "button.later_departure_modal_btn_close",
                    )
                )
            )
            _time.sleep(0.3)
            return
        except Exception:
            pass

        _log.debug("나중에 출발 토글 재시도")
        driver.execute_script("arguments[0].click();", later_btn)

        try:
            WebDriverWait(driver, 6).until(
                lambda current_driver: any(
                    button.is_displayed()
                    for button in current_driver.find_elements(
                        By.CSS_SELECTOR,
                        "button.later_departure_modal_btn_close",
                    )
                )
            )
            _time.sleep(0.3)
        except Exception as exc:
            raise ProviderError("'나중에 출발' 모달 열기 실패") from exc

    def ensure_picker_open(self, driver: webdriver.Chrome) -> None:
        if self.find_visible_confirm_buttons(driver):
            return
        if self.has_picker_dropdowns(driver):
            return

        visible_change_buttons = self.find_visible_change_buttons(driver)
        if not visible_change_buttons:
            self.wait_picker_controls(driver, timeout=6)
            if self.find_visible_confirm_buttons(driver):
                return
            if self.has_picker_dropdowns(driver):
                return
            visible_change_buttons = self.find_visible_change_buttons(driver)

        if not visible_change_buttons:
            _log.debug("'출발 시간 변경' 미발견, 모달 재오픈")
            self._provider._modal_open = False
            self.open_later_modal(driver)
            self._provider._modal_open = True
            self.wait_picker_controls(driver, timeout=10)

            if self.find_visible_confirm_buttons(driver):
                return
            if self.has_picker_dropdowns(driver):
                return

            visible_change_buttons = self.find_visible_change_buttons(driver)
            if not visible_change_buttons:
                visible_buttons = self.debug_visible_buttons(driver)
                raise ProviderError(
                    "'출발 시간 변경' 버튼을 찾을 수 없음 "
                    f"(visible_buttons={visible_buttons})"
                )

        driver.execute_script("arguments[0].click();", visible_change_buttons[0])
        try:
            WebDriverWait(driver, 8).until(
                lambda current_driver: (
                    bool(self.find_visible_confirm_buttons(current_driver))
                    or self.has_picker_dropdowns(current_driver)
                )
            )
        except Exception as exc:
            raise ProviderError("시간 선택기 열기 실패", cause=exc) from exc
        _time.sleep(0.3)

    def set_time_and_read(
        self,
        driver: webdriver.Chrome,
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

        self.ensure_picker_open(driver)

        if departure_time is not None:
            self.set_calendar_date(driver, departure_time)

        self.set_dropdown_value(driver, "ampm", ampm_text)
        self.set_dropdown_value(driver, "hour", hour_text)
        self.set_dropdown_value(driver, "minute", minute_text)

        _time.sleep(0.5)

        confirm_btns = driver.find_elements(
            By.CSS_SELECTOR,
            "button.later_departure_confirm_btn",
        )
        visible_confirms = [button for button in confirm_btns if button.is_displayed()]
        if not visible_confirms:
            raise ProviderError("확인 버튼을 찾을 수 없음")

        confirm = visible_confirms[0]
        button_text = confirm.text.strip()

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

        driver.execute_script("arguments[0].click();", confirm)

        try:
            WebDriverWait(driver, 8).until(
                lambda current_driver: not any(
                    button.is_displayed()
                    for button in current_driver.find_elements(
                        By.CSS_SELECTOR,
                        "button.later_departure_confirm_btn",
                    )
                )
            )
            _time.sleep(0.3)
        except Exception:
            pass

        confirm_btns = driver.find_elements(
            By.CSS_SELECTOR,
            "button.later_departure_confirm_btn",
        )
        still_visible = [button for button in confirm_btns if button.is_displayed()]
        if still_visible:
            current_text = still_visible[0].text.strip()
            if "미래시간" in current_text or "선택해주세요" in current_text:
                _log.warning("확인 클릭 후 과거 시간 에러: '%s'", current_text)
                raise ProviderError(
                    f"과거 시간 조회 불가 ({ampm_text} {hour_text} {minute_text})",
                    is_retryable=False,
                )

            _time.sleep(2)
            confirm_btns = driver.find_elements(
                By.CSS_SELECTOR,
                "button.later_departure_confirm_btn",
            )
            still_visible = [button for button in confirm_btns if button.is_displayed()]
            if still_visible:
                current_text = still_visible[0].text.strip()
                if "미래시간" in current_text or "선택해주세요" in current_text:
                    _log.warning("지연 감지 - 과거 시간 에러: '%s'", current_text)
                    raise ProviderError(
                        f"과거 시간 조회 불가 "
                        f"({ampm_text} {hour_text} {minute_text})",
                        is_retryable=False,
                    )
                _log.warning("확인 클릭 실패 (버튼 잔존), 재시도")
                still_visible[0].click()
                _time.sleep(3)

                confirm_btns = driver.find_elements(
                    By.CSS_SELECTOR,
                    "button.later_departure_confirm_btn",
                )
                still_visible = [
                    button for button in confirm_btns if button.is_displayed()
                ]
                if still_visible:
                    final_text = still_visible[0].text.strip()
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
            driver,
            ampm_text,
            hour_text,
            minute_text,
            timeout_seconds=8.0,
        )
        if duration is None:
            self.raise_panel_parse_timeout(
                driver,
                ampm_text,
                hour_text,
                minute_text,
            )

        change_btns = driver.find_elements(
            By.CSS_SELECTOR,
            "button.later_departure_time_btn",
        )
        if not change_btns or not any(button.is_displayed() for button in change_btns):
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
        hour_num = hour.replace("시", "").strip()
        minute_num = minute.replace("분", "").strip()
        if not hour_num.isdigit() or not minute_num.isdigit():
            return False

        hour_12 = int(hour_num)
        minute_int = int(minute_num)
        minute_num = f"{minute_int:02d}"
        hour_24 = hour_12 % 12
        if ampm == "오후":
            hour_24 += 12
        elif ampm == "오전" and hour_12 == 12:
            hour_24 = 0

        has_ampm = ampm in text
        has_hour_min_kor = f"{hour_12}시" in text and f"{minute_num}분" in text
        has_hour_min_colon = (
            f"{hour_12}:{minute_num}" in text
            or f"{hour_12} : {minute_num}" in text
            or f"{hour_24:02d}:{minute_num}" in text
            or f"{hour_24}:{minute_num}" in text
        )
        if has_ampm and (has_hour_min_kor or has_hour_min_colon):
            return True
        return has_hour_min_kor

    def extract_duration_from_panel_text(
        self,
        text: str,
        ampm: str,
        hour: str,
        minute: str,
    ) -> int | None:
        if "소요" not in text:
            return None
        if not self.matches_requested_time(text, ampm, hour, minute):
            return None
        try:
            return _parse_naver_duration(text)
        except ProviderError:
            return None

    def read_duration_from_panel_dialog(
        self,
        driver: webdriver.Chrome,
        ampm: str,
        hour: str,
        minute: str,
    ) -> int | None:
        for selector in _PANEL_DURATION_SELECTORS:
            for el in driver.find_elements(By.CSS_SELECTOR, selector):
                try:
                    if not el.is_displayed():
                        continue
                    raw = el.text.strip()
                except Exception:
                    continue
                if not raw:
                    continue
                duration = self.extract_duration_from_panel_text(
                    raw,
                    ampm,
                    hour,
                    minute,
                )
                if duration is not None:
                    return duration
        return None

    def _duration_tokens(self, text: str) -> list[str]:
        tokens: list[str] = []
        for match in _DUR_WITH_SOYO_RE.finditer(text):
            token = match.group(0).strip()
            if token and token not in tokens:
                tokens.append(token)
            if len(tokens) >= _PANEL_DIAGNOSTIC_TOKEN_LIMIT:
                return tokens
        return tokens

    def panel_parse_diagnostics(
        self,
        driver: webdriver.Chrome,
        ampm: str,
        hour: str,
        minute: str,
    ) -> dict[str, object]:
        selectors: dict[str, dict[str, int]] = {}
        panel_samples: list[dict[str, object]] = []
        for selector in _PANEL_DIAGNOSTIC_SELECTORS:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                selectors[selector] = {"count": 0, "visible": 0}
                continue

            visible_count = 0
            for element in elements:
                try:
                    is_visible = bool(element.is_displayed())
                except Exception:
                    is_visible = False
                if is_visible:
                    visible_count += 1

                if selector in _PANEL_DURATION_SELECTORS and len(
                    panel_samples
                ) < _PANEL_DIAGNOSTIC_SAMPLE_LIMIT:
                    try:
                        raw_text = element.text.strip()
                    except Exception:
                        raw_text = ""
                    panel_samples.append(
                        {
                            "visible": is_visible,
                            "text": redact_text(raw_text),
                            "has_requested_time": self.matches_requested_time(
                                raw_text,
                                ampm,
                                hour,
                                minute,
                            ),
                            "has_duration_word": "소요" in raw_text,
                            "duration_tokens": self._duration_tokens(raw_text),
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
        driver: webdriver.Chrome,
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
            payload = self.panel_parse_diagnostics(driver, ampm, hour, minute)
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
        driver: webdriver.Chrome,
        ampm: str,
        hour: str,
        minute: str,
    ) -> None:
        artifact_path = self.write_panel_parse_diagnostics(driver, ampm, hour, minute)
        artifact_hint = f" diagnostics={artifact_path.name}" if artifact_path else ""
        raise ProviderError(
            "나중에 출발 패널 소요시간 읽기 실패 "
            f"({ampm} {hour} {minute}){artifact_hint}",
            code="panel_parse_timeout",
            bucket="panel_parse_timeout",
        )

    def wait_panel_duration(
        self,
        driver: webdriver.Chrome,
        ampm: str,
        hour: str,
        minute: str,
        *,
        timeout_seconds: float,
    ) -> int | None:
        deadline = _time.monotonic() + timeout_seconds
        while _time.monotonic() < deadline:
            duration = self.read_duration_from_panel_dialog(
                driver,
                ampm,
                hour,
                minute,
            )
            if duration is not None:
                return duration
            _time.sleep(0.25)
        return self.read_duration_from_panel_dialog(driver, ampm, hour, minute)

    def visible_calendar_day_buttons(self, driver: webdriver.Chrome) -> list:
        visible_buttons = []
        for button in driver.find_elements(
            By.CSS_SELECTOR,
            "button.calendar_day_btn",
        ):
            try:
                if button.is_displayed():
                    visible_buttons.append(button)
            except Exception:
                continue
        return visible_buttons

    def find_calendar_day_button(self, driver: webdriver.Chrome, target_date):
        day_text = str(target_date.day)
        current_year_month = self.read_calendar_display_year_month(driver)
        fallback_button = None
        for button in self.visible_calendar_day_buttons(driver):
            try:
                if not button.is_enabled():
                    continue
                text_lines = [
                    line.strip()
                    for line in button.text.splitlines()
                    if line.strip()
                ]
            except Exception:
                continue
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

    def read_calendar_display_year_month(
        self,
        driver: webdriver.Chrome,
    ) -> tuple[int, int] | None:
        for button in driver.find_elements(
            By.CSS_SELECTOR,
            "button.calendar_date_btn",
        ):
            try:
                if not button.is_displayed():
                    continue
                text = button.text.strip()
            except Exception:
                continue
            match = re.search(r"(\d{4})\.(\d{2})$", text)
            if match:
                return int(match.group(1)), int(match.group(2))
            match = re.search(r"(\d{1,2})월", text)
            if match:
                return datetime.now().year, int(match.group(1))
        return None

    def select_calendar_month(self, driver: webdriver.Chrome, target_date) -> bool:
        target_text = target_date.strftime("%Y.%m")
        target_year_month = (target_date.year, target_date.month)
        if self.read_calendar_display_year_month(driver) == target_year_month:
            return True

        month_buttons = []
        for button in driver.find_elements(
            By.CSS_SELECTOR,
            "button.calendar_date_btn",
        ):
            try:
                if button.is_displayed() and button.is_enabled():
                    month_buttons.append(button)
            except Exception:
                continue
        if not month_buttons:
            return False

        driver.execute_script("arguments[0].click();", month_buttons[0])
        _time.sleep(0.25)

        visible_options = []
        for selector in ("button.list_item_btn", "button[role='option']"):
            for option in driver.find_elements(By.CSS_SELECTOR, selector):
                try:
                    if not option.is_displayed() or not option.is_enabled():
                        continue
                    option_text = option.text.strip()
                except Exception:
                    continue
                visible_options.append(option_text)
                if option_text != target_text:
                    continue
                driver.execute_script("arguments[0].click();", option)
                _time.sleep(0.35)
                return True
            if visible_options:
                break

        _log.warning(
            "캘린더 월 옵션 '%s'을 찾을 수 없음 (visible_options=%s)",
            target_text,
            visible_options,
        )
        return False

    def expand_calendar_days_if_needed(self, driver: webdriver.Chrome) -> bool:
        visible_count_before = len(self.visible_calendar_day_buttons(driver))
        if visible_count_before >= 28:
            return True

        expand_buttons = []
        for by, selector in (
            (By.CSS_SELECTOR, "button.calendar_expand_btn"),
            (
                By.XPATH,
                "//button[contains(@class, 'calendar_expand_btn')]",
            ),
            (
                By.XPATH,
                "//button[contains(normalize-space(.), '펼치기')]",
            ),
        ):
            for button in driver.find_elements(by, selector):
                try:
                    if button.is_displayed():
                        expand_buttons.append(button)
                except Exception:
                    continue
            if expand_buttons:
                break

        if not expand_buttons:
            return False

        driver.execute_script("arguments[0].click();", expand_buttons[0])
        _time.sleep(0.35)
        visible_count_after = len(self.visible_calendar_day_buttons(driver))
        _log.debug(
            "캘린더 확장 클릭: visible_day_count=%d->%d",
            visible_count_before,
            visible_count_after,
        )
        return visible_count_after > visible_count_before

    def set_calendar_date(
        self,
        driver: webdriver.Chrome,
        departure_time: datetime,
    ) -> None:
        from datetime import date as _date

        today = _date.today()
        target = departure_time.date()
        if target == today:
            return

        day_button = self.find_calendar_day_button(driver, target)
        if day_button is None:
            displayed_year_month = self.read_calendar_display_year_month(driver)
            if displayed_year_month != (target.year, target.month):
                if self.select_calendar_month(driver, target):
                    day_button = self.find_calendar_day_button(driver, target)

        if day_button is None and self.expand_calendar_days_if_needed(driver):
            day_button = self.find_calendar_day_button(driver, target)

        if day_button is not None:
            driver.execute_script("arguments[0].click();", day_button)
            _time.sleep(0.5)
            _log.debug("캘린더 날짜 선택: %s", target.strftime("%Y-%m-%d"))
            return

        visible_days = [
            button.text.strip().replace("\n", " ")
            for button in self.visible_calendar_day_buttons(driver)
        ]
        _log.warning(
            "캘린더에서 %s일을 찾을 수 없음 (target=%s, visible_days=%s)",
            target.day,
            target.strftime("%Y-%m-%d"),
            visible_days,
        )

    def set_dropdown_value(
        self,
        driver: webdriver.Chrome,
        kind: str,
        target: str,
    ) -> None:
        dd_btns = driver.find_elements(By.CSS_SELECTOR, "button.dropdown_btn")
        button = None

        for candidate in dd_btns:
            if not candidate.is_displayed():
                continue
            text = candidate.text.strip()
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
        if button.text.strip() == target:
            return

        driver.execute_script("arguments[0].click();", button)
        _time.sleep(0.5)

        options = driver.find_elements(By.CSS_SELECTOR, "[role='option']")
        for option in options:
            if option.is_displayed() and option.text.strip() == target:
                driver.execute_script("arguments[0].click();", option)
                _time.sleep(0.3)
                return

        driver.execute_script("arguments[0].click();", button)
        _time.sleep(0.2)
        raise ProviderError(f"드롭다운 옵션 미발견: {kind}={target}")


class NaverMapsSeleniumProvider:
    name = "naver_selenium"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._driver: webdriver.Chrome | None = None
        self._current_route: Route | None = None
        self._modal_open = False
        # (origin, dest, date_iso, hour24, min10) → duration_seconds
        self._dur_cache: dict[tuple, int] = {}
        self._coords: dict[str, tuple[float, float]] = {}
        self._search_adapter = _NaverDirectionsSearchAdapter(self)
        self._departure_picker = _NaverDeparturePickerAdapter(self)

    # ── public API ──

    def set_coords(
        self, place: str, lat: float, lon: float,
    ) -> None:
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

            driver = self._ensure_driver_locked()
            try:
                secs = self._query_locked(driver, route, departure_time, m10)
            except ProviderError as exc:
                if not exc.is_retryable:
                    _log.warning(
                        "Naver non-retryable provider error route=%s error=%s",
                        redact_route(route.origin, route.destination),
                        exc,
                    )
                    raise
                _log.warning(
                    "Naver retryable provider error; resetting driver route=%s "
                    "error=%s",
                    redact_route(route.origin, route.destination),
                    exc,
                )
                self._reset_state()
                raise
            except Exception as exc:
                _log.warning(
                    "Naver query failed; resetting driver route=%s",
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
            self._close_driver_locked()
        finally:
            self._lock.release()

    def close(self) -> None:
        acquired = self._lock.acquire(timeout=_CLOSE_LOCK_TIMEOUT_SECONDS)
        if not acquired:
            _log.warning(
                "Naver provider close skipped after %.1fs (busy query lock)",
                _CLOSE_LOCK_TIMEOUT_SECONDS,
            )
            driver = self._driver
            if driver is not None:
                self._force_kill_driver_process(driver)
            return
        try:
            self._close_driver_locked()
        finally:
            self._lock.release()

    # ── driver 관리 ──

    def _ensure_driver_locked(self) -> webdriver.Chrome:
        if self._driver is not None:
            return self._driver

        opts = build_chrome_options(
            headless=self._settings.headless,
            window_size="1920,1080",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            chrome_binary_path=self._settings.chrome_binary_path,
            chrome_user_data_dir=self._settings.chrome_user_data_dir,
            no_sandbox=self._settings.chrome_no_sandbox,
        )

        driver = webdriver.Chrome(options=opts)
        driver.set_page_load_timeout(30)
        self._driver = driver
        return driver

    def _close_driver_locked(self) -> None:
        if self._driver is None:
            return
        driver = self._driver
        try:
            result = close_webdriver_with_timeout(
                driver,
                quit_timeout_seconds=_DRIVER_QUIT_TIMEOUT_SECONDS,
                quit_thread_name="naver-driver-quit",
            )
            if result.timed_out:
                _log.warning(
                    "Naver driver quit timeout after %.1fs; process killed",
                    _DRIVER_QUIT_TIMEOUT_SECONDS,
                )
            elif result.quit_error is not None:
                _log.debug("Naver driver quit raised: %s", result.quit_error)
        finally:
            self._driver = None
            self._current_route = None
            self._modal_open = False

    def _force_kill_driver_process(self, driver: webdriver.Chrome) -> None:
        force_kill_webdriver_process(driver)

    def terminate_session(self) -> None:
        acquired = self._lock.acquire(timeout=_CLOSE_LOCK_TIMEOUT_SECONDS)
        if not acquired:
            driver = self._driver
            if driver is not None:
                self._force_kill_driver_process(driver)
            self._driver = None
            self._current_route = None
            self._modal_open = False
            return
        try:
            self._close_driver_locked()
        finally:
            self._lock.release()

    def _reset_state(self) -> None:
        self._close_driver_locked()

    # ── 핵심 로직 ──

    def _query_locked(
        self,
        driver: webdriver.Chrome,
        route: Route,
        departure_time: datetime,
        m10: int,
    ) -> int:
        # 1) 경로가 바뀌면 새로 검색
        if self._current_route != route:
            self._search_adapter.navigate_and_search(driver, route)
            self._current_route = route
            self._modal_open = False

        # 2) "나중에 출발" 모달 열기
        if not self._modal_open:
            self._departure_picker.open_later_modal(driver)
            self._modal_open = True

        # 3) 시간 설정 + 결과 읽기
        return self._departure_picker.set_time_and_read(
            driver, departure_time.hour, m10, departure_time,
        )

    # ── 경로 검색 ──

    def _build_directions_url(self, route: Route) -> str | None:
        return self._search_adapter.build_directions_url(route)

    def _navigate_and_search(
        self, driver: webdriver.Chrome, route: Route,
    ) -> None:
        self._search_adapter.navigate_and_search(driver, route)

    def _navigate_and_search_ac(
        self, driver: webdriver.Chrome, route: Route,
    ) -> None:
        self._search_adapter.navigate_and_search_ac(driver, route)

    def _select_place_ac(
        self,
        driver: webdriver.Chrome,
        input_el,
        place_name: str,
    ) -> None:
        self._search_adapter.select_place_ac(driver, input_el, place_name)

    def _is_modal_open(self, driver: webdriver.Chrome) -> bool:
        return self._departure_picker.is_modal_open(driver)

    def _find_visible_confirm_buttons(self, driver: webdriver.Chrome) -> list:
        return self._departure_picker.find_visible_confirm_buttons(driver)

    def _find_visible_change_buttons(self, driver: webdriver.Chrome) -> list:
        return self._departure_picker.find_visible_change_buttons(driver)

    def _has_picker_dropdowns(self, driver: webdriver.Chrome) -> bool:
        return self._departure_picker.has_picker_dropdowns(driver)

    def _wait_picker_controls(self, driver: webdriver.Chrome, timeout: int = 8) -> None:
        self._departure_picker.wait_picker_controls(driver, timeout)

    def _debug_visible_buttons(
        self,
        driver: webdriver.Chrome,
        *,
        limit: int = 8,
    ) -> str:
        return self._departure_picker.debug_visible_buttons(
            driver,
            limit=limit,
        )

    def _open_later_modal(self, driver: webdriver.Chrome) -> None:
        self._departure_picker.open_later_modal(driver)

    def _ensure_picker_open(self, driver: webdriver.Chrome) -> None:
        self._departure_picker.ensure_picker_open(driver)

    def _set_time_and_read(
        self,
        driver: webdriver.Chrome,
        hour_24: int,
        minute_10: int,
        departure_time: datetime | None = None,
    ) -> int:
        return self._departure_picker.set_time_and_read(
            driver,
            hour_24,
            minute_10,
            departure_time,
        )

    def _matches_requested_time(
        self,
        text: str,
        ampm: str,
        hour: str,
        minute: str,
    ) -> bool:
        return self._departure_picker.matches_requested_time(
            text,
            ampm,
            hour,
            minute,
        )

    def _extract_duration_from_panel_text(
        self,
        text: str,
        ampm: str,
        hour: str,
        minute: str,
    ) -> int | None:
        return self._departure_picker.extract_duration_from_panel_text(
            text,
            ampm,
            hour,
            minute,
        )

    def _read_duration_from_panel_dialog(
        self,
        driver: webdriver.Chrome,
        ampm: str,
        hour: str,
        minute: str,
    ) -> int | None:
        return self._departure_picker.read_duration_from_panel_dialog(
            driver,
            ampm,
            hour,
            minute,
        )

    def _wait_panel_duration(
        self,
        driver: webdriver.Chrome,
        ampm: str,
        hour: str,
        minute: str,
        *,
        timeout_seconds: float,
    ) -> int | None:
        return self._departure_picker.wait_panel_duration(
            driver,
            ampm,
            hour,
            minute,
            timeout_seconds=timeout_seconds,
        )

    def _raise_panel_parse_timeout(
        self,
        driver: webdriver.Chrome,
        ampm: str,
        hour: str,
        minute: str,
    ) -> None:
        self._departure_picker.raise_panel_parse_timeout(
            driver,
            ampm,
            hour,
            minute,
        )

    def _visible_calendar_day_buttons(self, driver: webdriver.Chrome) -> list:
        return self._departure_picker.visible_calendar_day_buttons(driver)

    def _find_calendar_day_button(
        self,
        driver: webdriver.Chrome,
        target_date,
    ):
        return self._departure_picker.find_calendar_day_button(
            driver,
            target_date,
        )

    def _read_calendar_display_year_month(
        self,
        driver: webdriver.Chrome,
    ) -> tuple[int, int] | None:
        return self._departure_picker.read_calendar_display_year_month(driver)

    def _select_calendar_month(
        self,
        driver: webdriver.Chrome,
        target_date,
    ) -> bool:
        return self._departure_picker.select_calendar_month(driver, target_date)

    def _expand_calendar_days_if_needed(self, driver: webdriver.Chrome) -> bool:
        return self._departure_picker.expand_calendar_days_if_needed(driver)

    def _set_calendar_date(
        self, driver: webdriver.Chrome, departure_time: datetime,
    ) -> None:
        self._departure_picker.set_calendar_date(driver, departure_time)

    def _set_dropdown_value(
        self,
        driver: webdriver.Chrome,
        kind: str,
        target: str,
    ) -> None:
        self._departure_picker.set_dropdown_value(driver, kind, target)


class NaverMapsSeleniumPoolProvider:
    name = "naver_selenium"

    def __init__(self, settings: Settings) -> None:
        pool_size = max(1, settings.naver_session_pool_size)
        self.max_parallel_sessions = pool_size
        self._index_lock = threading.Lock()
        self._next_index = 0
        self._workers = tuple(
            NaverMapsSeleniumProvider(
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
        worker: NaverMapsSeleniumProvider,
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
        worker: NaverMapsSeleniumProvider,
    ) -> None:
        try:
            worker.close()
        except Exception:
            _log.exception("Naver worker close failed (worker=%d)", index)

    def _pick_worker(self) -> NaverMapsSeleniumProvider:
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
