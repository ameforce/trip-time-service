from __future__ import annotations

import logging
import math
import os
import re
import subprocess
import threading
import time as _time
import urllib.parse
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from trip_time_service.config import Settings
from trip_time_service.core.models import DriveDuration, Route
from trip_time_service.providers.base import ProviderError

_log = logging.getLogger(__name__)

_MINUTE_RESOLUTION = 10
_CLOSE_LOCK_TIMEOUT_SECONDS = 2.0
_DRIVER_QUIT_TIMEOUT_SECONDS = 4.0
_POOL_CLOSE_TIMEOUT_SECONDS = 6.0
_POOL_BULK_TERMINATE_TIMEOUT_SECONDS = 1.5


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
                    _log.warning("Naver non-retryable: %s", exc)
                    raise
                _log.error("Naver query failed, resetting driver", exc_info=True)
                self._reset_state()
                raise
            except Exception as exc:
                _log.error("Naver query failed, resetting driver", exc_info=True)
                self._reset_state()
                raise ProviderError(
                    "네이버 지도 조회 실패", cause=exc,
                ) from exc

            self._dur_cache[cache_key] = secs
            _log.info(
                "Naver: %s → %s [%02d:%02d] = %d초 (%d분)",
                route.origin, route.destination,
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

        opts = Options()
        if self._settings.headless:
            opts.add_argument("--headless=new")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--lang=ko-KR")
        opts.add_argument("--window-size=1920,1080")
        opts.add_argument(
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
        if self._settings.chrome_binary_path:
            opts.binary_location = self._settings.chrome_binary_path
        if self._settings.chrome_user_data_dir:
            opts.add_argument(
                f"--user-data-dir={self._settings.chrome_user_data_dir}"
            )

        driver = webdriver.Chrome(options=opts)
        driver.set_page_load_timeout(30)
        self._driver = driver
        return driver

    def _close_driver_locked(self) -> None:
        if self._driver is None:
            return
        driver = self._driver
        quit_errors: list[Exception] = []
        quit_done = threading.Event()

        def _run_quit() -> None:
            try:
                driver.quit()
            except Exception as exc:
                quit_errors.append(exc)
            finally:
                quit_done.set()

        try:
            quit_thread = threading.Thread(
                target=_run_quit,
                name="naver-driver-quit",
                daemon=True,
            )
            quit_thread.start()
            if not quit_done.wait(timeout=_DRIVER_QUIT_TIMEOUT_SECONDS):
                self._force_kill_driver_process(driver)
                _log.warning(
                    "Naver driver quit timeout after %.1fs; process killed",
                    _DRIVER_QUIT_TIMEOUT_SECONDS,
                )
            elif quit_errors:
                _log.debug("Naver driver quit raised: %s", quit_errors[0])
        finally:
            self._driver = None
            self._current_route = None
            self._modal_open = False

    def _force_kill_driver_process(self, driver: webdriver.Chrome) -> None:
        service = getattr(driver, "service", None)
        process = getattr(service, "process", None)
        if process is None:
            return
        pid = getattr(process, "pid", None)
        try:
            if process.poll() is not None:
                return
            if isinstance(pid, int) and os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=1.5,
                )
                return
            process.kill()
            process.wait(timeout=1.0)
        except Exception:
            pass

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
            self._navigate_and_search(driver, route)
            self._current_route = route
            self._modal_open = False

        # 2) "나중에 출발" 모달 열기
        if not self._modal_open:
            self._open_later_modal(driver)
            self._modal_open = True

        # 3) 시간 설정 + 결과 읽기
        return self._set_time_and_read(
            driver, departure_time.hour, m10, departure_time,
        )

    # ── 경로 검색 ──

    def _build_directions_url(self, route: Route) -> str | None:
        o_coords = self._coords.get(route.origin)
        d_coords = self._coords.get(route.destination)
        if not o_coords or not d_coords:
            return None

        ox, oy = _latlon_to_epsg3857(o_coords[0], o_coords[1])
        dx, dy = _latlon_to_epsg3857(d_coords[0], d_coords[1])

        o_name = urllib.parse.quote(route.origin)
        d_name = urllib.parse.quote(route.destination)

        # 네이버 지도 URL 형식: /p/directions/{x},{y},{name},,/{x},{y},{name},,/-/car
        url = (
            f"https://map.naver.com/p/directions/"
            f"{ox:.7f},{oy:.7f},{o_name},,/"
            f"{dx:.7f},{dy:.7f},{d_name},,/"
            f"-/car"
        )
        return url

    def _navigate_and_search(
        self, driver: webdriver.Chrome, route: Route,
    ) -> None:
        direct_url = self._build_directions_url(route)
        if direct_url:
            _log.info(
                "Naver: URL 직접 접속 %s → %s", route.origin, route.destination,
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
                self._navigate_and_search_ac(driver, route)
                return
            _time.sleep(0.5)
            return

        self._navigate_and_search_ac(driver, route)

    def _navigate_and_search_ac(
        self, driver: webdriver.Chrome, route: Route,
    ) -> None:
        _log.info("Naver: 자동완성 경로 검색 %s → %s", route.origin, route.destination)
        driver.get("https://map.naver.com/p/directions/-/-/-/car")
        _time.sleep(4)

        inputs = driver.find_elements(By.CSS_SELECTOR, "input.input_search")
        if len(inputs) < 2:
            raise ProviderError("출발지/도착지 입력 필드를 찾을 수 없음")
        self._select_place_ac(driver, inputs[0], route.origin)

        inputs = driver.find_elements(By.CSS_SELECTOR, "input.input_search")
        dest_input = inputs[-1]
        driver.execute_script(
            "arguments[0].focus(); arguments[0].value = '';", dest_input,
        )
        _time.sleep(0.3)
        self._select_place_ac(driver, dest_input, route.destination)

        search_btns = driver.find_elements(
            By.CSS_SELECTOR, "button.btn_direction.search",
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
                "경로 결과 로딩 실패 (timeout)", cause=exc,
            ) from exc
        _time.sleep(1)

    def _select_place_ac(
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
            By.CSS_SELECTOR, ".list_place li.item_place",
        )
        visible = [it for it in place_items if it.is_displayed()]
        if not visible:
            _log.warning("자동완성에 '%s' 결과 없음, Enter로 검색", place_name)
            input_el.send_keys(Keys.ENTER)
            _time.sleep(3)
            return

        input_el.send_keys(Keys.ARROW_DOWN)
        _time.sleep(0.15)
        input_el.send_keys(Keys.ARROW_DOWN)
        _time.sleep(0.15)
        input_el.send_keys(Keys.ENTER)
        _time.sleep(2)

    def _is_modal_open(self, driver: webdriver.Chrome) -> bool:
        """'나중에 출발' 모달이 열려있는지 확인."""
        close_btns = driver.find_elements(
            By.CSS_SELECTOR, "button.later_departure_modal_btn_close",
        )
        if any(b.is_displayed() for b in close_btns):
            return True
        if self._has_picker_dropdowns(driver):
            return True
        if self._find_visible_confirm_buttons(driver):
            return True
        return bool(self._find_visible_change_buttons(driver))

    def _find_visible_confirm_buttons(self, driver: webdriver.Chrome) -> list:
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

    def _find_visible_change_buttons(self, driver: webdriver.Chrome) -> list:
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

    def _has_picker_dropdowns(self, driver: webdriver.Chrome) -> bool:
        visible_count = 0
        for button in driver.find_elements(By.CSS_SELECTOR, "button.dropdown_btn"):
            try:
                if button.is_displayed():
                    visible_count += 1
            except Exception:
                continue
        return visible_count >= 2

    def _wait_picker_controls(self, driver: webdriver.Chrome, timeout: int = 8) -> None:
        try:
            WebDriverWait(driver, timeout).until(
                lambda d: (
                    bool(self._find_visible_confirm_buttons(d))
                    or self._has_picker_dropdowns(d)
                    or bool(self._find_visible_change_buttons(d))
                )
            )
        except Exception:
            return

    def _debug_visible_buttons(
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

    def _open_later_modal(self, driver: webdriver.Chrome) -> None:
        later_btn = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "button.later_departure_btn")
            )
        )

        if self._is_modal_open(driver):
            _log.debug("나중에 출발 모달 이미 열림")
            return

        driver.execute_script("arguments[0].click();", later_btn)

        try:
            WebDriverWait(driver, 6).until(
                lambda d: any(
                    b.is_displayed()
                    for b in d.find_elements(
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
                lambda d: any(
                    b.is_displayed()
                    for b in d.find_elements(
                        By.CSS_SELECTOR,
                        "button.later_departure_modal_btn_close",
                    )
                )
            )
            _time.sleep(0.3)
        except Exception as exc:
            raise ProviderError("'나중에 출발' 모달 열기 실패") from exc

    def _ensure_picker_open(self, driver: webdriver.Chrome) -> None:
        if self._find_visible_confirm_buttons(driver):
            return
        if self._has_picker_dropdowns(driver):
            return

        vis_change = self._find_visible_change_buttons(driver)
        if not vis_change:
            self._wait_picker_controls(driver, timeout=6)
            if self._find_visible_confirm_buttons(driver):
                return
            if self._has_picker_dropdowns(driver):
                return
            vis_change = self._find_visible_change_buttons(driver)

        if not vis_change:
            _log.debug("'출발 시간 변경' 미발견, 모달 재오픈")
            self._modal_open = False
            self._open_later_modal(driver)
            self._modal_open = True
            self._wait_picker_controls(driver, timeout=10)

            if self._find_visible_confirm_buttons(driver):
                return
            if self._has_picker_dropdowns(driver):
                return

            vis_change = self._find_visible_change_buttons(driver)
            if not vis_change:
                visible_buttons = self._debug_visible_buttons(driver)
                raise ProviderError(
                    "'출발 시간 변경' 버튼을 찾을 수 없음 "
                    f"(visible_buttons={visible_buttons})"
                )

        driver.execute_script("arguments[0].click();", vis_change[0])
        try:
            WebDriverWait(driver, 8).until(
                lambda d: (
                    bool(self._find_visible_confirm_buttons(d))
                    or self._has_picker_dropdowns(d)
                )
            )
        except Exception as exc:
            raise ProviderError("시간 선택기 열기 실패", cause=exc) from exc
        _time.sleep(0.3)

    def _set_time_and_read(
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

        self._ensure_picker_open(driver)

        if departure_time is not None:
            self._set_calendar_date(driver, departure_time)

        self._set_dropdown_value(driver, "ampm", ampm_text)
        self._set_dropdown_value(driver, "hour", hour_text)
        self._set_dropdown_value(driver, "minute", minute_text)

        _time.sleep(0.5)

        confirm_btns = driver.find_elements(
            By.CSS_SELECTOR, "button.later_departure_confirm_btn",
        )
        vis_confirms = [b for b in confirm_btns if b.is_displayed()]
        if not vis_confirms:
            raise ProviderError("확인 버튼을 찾을 수 없음")

        confirm = vis_confirms[0]
        btn_text = confirm.text.strip()

        if "미래시간" in btn_text or "선택해주세요" in btn_text:
            _log.warning(
                "과거 시간 선택 불가: %s %s %s → '%s'",
                ampm_text, hour_text, minute_text, btn_text,
            )
            raise ProviderError(
                f"과거 시간 조회 불가 ({ampm_text} {hour_text} {minute_text})",
                is_retryable=False,
            )

        driver.execute_script("arguments[0].click();", confirm)

        try:
            WebDriverWait(driver, 8).until(
                lambda d: not any(
                    b.is_displayed()
                    for b in d.find_elements(
                        By.CSS_SELECTOR,
                        "button.later_departure_confirm_btn",
                    )
                )
            )
            _time.sleep(0.3)
        except Exception:
            pass

        confirm_btns2 = driver.find_elements(
            By.CSS_SELECTOR, "button.later_departure_confirm_btn",
        )
        still_visible = [b for b in confirm_btns2 if b.is_displayed()]
        if still_visible:
            cur_text = still_visible[0].text.strip()
            if "미래시간" in cur_text or "선택해주세요" in cur_text:
                _log.warning("확인 클릭 후 과거 시간 에러: '%s'", cur_text)
                raise ProviderError(
                    f"과거 시간 조회 불가 ({ampm_text} {hour_text} {minute_text})",
                    is_retryable=False,
                )

            _time.sleep(2)
            confirm_btns3 = driver.find_elements(
                By.CSS_SELECTOR, "button.later_departure_confirm_btn",
            )
            vis3 = [b for b in confirm_btns3 if b.is_displayed()]
            if vis3:
                cur_text2 = vis3[0].text.strip()
                if "미래시간" in cur_text2 or "선택해주세요" in cur_text2:
                    _log.warning(
                        "지연 감지 - 과거 시간 에러: '%s'", cur_text2,
                    )
                    raise ProviderError(
                        f"과거 시간 조회 불가 "
                        f"({ampm_text} {hour_text} {minute_text})",
                        is_retryable=False,
                    )
                _log.warning("확인 클릭 실패 (버튼 잔존), 재시도")
                vis3[0].click()
                _time.sleep(3)

                confirm_btns4 = driver.find_elements(
                    By.CSS_SELECTOR, "button.later_departure_confirm_btn",
                )
                vis4 = [b for b in confirm_btns4 if b.is_displayed()]
                if vis4:
                    final_text = vis4[0].text.strip()
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

        duration = self._wait_panel_duration(
            driver,
            ampm_text,
            hour_text,
            minute_text,
            timeout_seconds=8.0,
        )
        if duration is None:
            raise ProviderError(
                "나중에 출발 패널 소요시간 읽기 실패 "
                f"({ampm_text} {hour_text} {minute_text})"
            )

        change_btns = driver.find_elements(
            By.CSS_SELECTOR, "button.later_departure_time_btn",
        )
        if not change_btns or not any(b.is_displayed() for b in change_btns):
            _log.debug("모달이 닫힘 (확인 후), 재오픈 필요")
            self._modal_open = False

        return duration

    def _matches_requested_time(
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
        has_hour_min_kor = (
            f"{hour_12}시" in text
            and f"{minute_num}분" in text
        )
        has_hour_min_colon = (
            f"{hour_12}:{minute_num}" in text
            or f"{hour_12} : {minute_num}" in text
            or f"{hour_24:02d}:{minute_num}" in text
            or f"{hour_24}:{minute_num}" in text
        )
        if has_ampm and (has_hour_min_kor or has_hour_min_colon):
            return True
        return has_hour_min_kor

    def _extract_duration_from_panel_text(
        self,
        text: str,
        ampm: str,
        hour: str,
        minute: str,
    ) -> int | None:
        if "소요" not in text:
            return None
        if not self._matches_requested_time(text, ampm, hour, minute):
            return None
        try:
            return _parse_naver_duration(text)
        except ProviderError:
            return None

    def _read_duration_from_panel_dialog(
        self,
        driver: webdriver.Chrome,
        ampm: str,
        hour: str,
        minute: str,
    ) -> int | None:
        for el in driver.find_elements(By.CSS_SELECTOR, "div.panel_dialog"):
            try:
                if not el.is_displayed():
                    continue
                raw = el.text.strip()
            except Exception:
                continue
            if not raw:
                continue
            duration = self._extract_duration_from_panel_text(
                raw,
                ampm,
                hour,
                minute,
            )
            if duration is not None:
                return duration
        return None

    def _wait_panel_duration(
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
            duration = self._read_duration_from_panel_dialog(
                driver,
                ampm,
                hour,
                minute,
            )
            if duration is not None:
                return duration
            _time.sleep(0.25)
        return self._read_duration_from_panel_dialog(driver, ampm, hour, minute)

    def _visible_calendar_day_buttons(self, driver: webdriver.Chrome) -> list:
        visible_buttons = []
        for button in driver.find_elements(
            By.CSS_SELECTOR, "button.calendar_day_btn",
        ):
            try:
                if button.is_displayed():
                    visible_buttons.append(button)
            except Exception:
                continue
        return visible_buttons

    def _find_calendar_day_button(
        self,
        driver: webdriver.Chrome,
        day: int,
    ):
        day_text = str(day)
        for button in self._visible_calendar_day_buttons(driver):
            if button.text.strip() == day_text:
                return button
        return None

    def _expand_calendar_days_if_needed(self, driver: webdriver.Chrome) -> bool:
        visible_count_before = len(self._visible_calendar_day_buttons(driver))
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
        visible_count_after = len(self._visible_calendar_day_buttons(driver))
        _log.debug(
            "캘린더 확장 클릭: visible_day_count=%d->%d",
            visible_count_before,
            visible_count_after,
        )
        return visible_count_after > visible_count_before

    def _set_calendar_date(
        self, driver: webdriver.Chrome, departure_time: datetime,
    ) -> None:
        """캘린더에서 출발일 선택. 오늘이면 스킵."""
        from datetime import date as _date

        today = _date.today()
        target = departure_time.date()
        if target == today:
            return

        day_button = self._find_calendar_day_button(driver, target.day)
        if day_button is None:
            if self._expand_calendar_days_if_needed(driver):
                day_button = self._find_calendar_day_button(driver, target.day)

        if day_button is not None:
            driver.execute_script("arguments[0].click();", day_button)
            _time.sleep(0.5)
            _log.debug("캘린더 날짜 선택: %s", target.strftime("%Y-%m-%d"))
            return

        visible_days = [
            button.text.strip().replace("\n", " ")
            for button in self._visible_calendar_day_buttons(driver)
        ]
        _log.warning(
            "캘린더에서 %s일을 찾을 수 없음 (target=%s, visible_days=%s)",
            target.day,
            target.strftime("%Y-%m-%d"),
            visible_days,
        )

    def _set_dropdown_value(
        self,
        driver: webdriver.Chrome,
        kind: str,
        target: str,
    ) -> None:
        """드롭다운(오전/오후, 시, 분) 값 설정.

        kind: 'ampm' | 'hour' | 'minute'
        """
        dd_btns = driver.find_elements(By.CSS_SELECTOR, "button.dropdown_btn")
        btn = None

        for b in dd_btns:
            if not b.is_displayed():
                continue
            txt = b.text.strip()
            if kind == "ampm" and txt in ("오전", "오후"):
                btn = b
                break
            elif kind == "hour" and re.match(r"^\d{1,2}시$", txt):
                btn = b
                break
            elif kind == "minute" and re.match(r"^\d{2}분$", txt):
                btn = b
                break

        if btn is None:
            raise ProviderError(f"드롭다운 버튼 미발견: {kind}={target}")

        # 이미 올바른 값이면 스킵
        if btn.text.strip() == target:
            return

        # 드롭다운 열기
        driver.execute_script("arguments[0].click();", btn)
        _time.sleep(0.5)

        # 옵션 목록에서 선택
        options = driver.find_elements(
            By.CSS_SELECTOR, "[role='option']",
        )
        for opt in options:
            if opt.is_displayed() and opt.text.strip() == target:
                driver.execute_script("arguments[0].click();", opt)
                _time.sleep(0.3)
                return

        # 옵션을 못 찾으면 드롭다운 닫기
        driver.execute_script("arguments[0].click();", btn)
        _time.sleep(0.2)
        raise ProviderError(f"드롭다운 옵션 미발견: {kind}={target}")


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
