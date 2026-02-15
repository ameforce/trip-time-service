#!/usr/bin/env python3
"""
출발 시각 기준 검색 UI 검증 스크립트.

목표:
1) 검색 직후 "출발 시각 분석" 카드(기준/안정적 x1.25)가 먼저 보이는지 확인
2) 추천 완료 후 분석 카드 유지 + "추천 출발 시각" 카드 추가 표시 확인
3) 두 시점 스크린샷 확보

실행: python scripts/verify_ui_departure_search.py
(앱이 http://127.0.0.1:8500 에서 실행 중이어야 함)
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    from selenium import webdriver
    from selenium.webdriver import ActionChains
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
except ImportError:
    print("Selenium 필요: pip install selenium")
    sys.exit(1)

BASE_URL = "http://127.0.0.1:8500"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "ui_verification_screenshots"
PATH_1 = Path("C:/Workspace/Daeng/Git/Project/trip-time-service/ui_verification_screenshots/01_analysis_and_progress.png")
PATH_2 = Path("C:/Workspace/Daeng/Git/Project/trip-time-service/ui_verification_screenshots/02_analysis_and_recommendation.png")
PATH_3 = Path("C:/Workspace/Daeng/Git/Project/trip-time-service/ui_verification_screenshots/03_candidate_tooltip_hover.png")
SCREENSHOT_1 = "01_analysis_and_progress.png"
SCREENSHOT_2 = "02_analysis_and_recommendation.png"
SCREENSHOT_3 = "03_candidate_tooltip_hover.png"
DATETIME_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")


def _save_screenshot(driver, path: Path) -> bool:
    """PNG 바이트로 직접 저장하여 파일 생성 보장."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        png_bytes = driver.get_screenshot_as_png()
        path.write_bytes(png_bytes)
        return path.exists() and path.stat().st_size > 0
    except Exception:
        return False


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    results = {
        "step1_analysis_first": "unknown",
        "step2_recommendation_added": "unknown",
        "screenshots": [],
        "screenshot_1_ok": False,
        "screenshot_2_ok": False,
        "screenshot_3_ok": False,
        "observed_text": {},
        "tooltip_check": {
            "status": "unknown",
            "overflow_left_px": None,
            "overflow_right_px": None,
            "datetime_pattern_ok": False,
            "hover_persist_ok": False,
            "header_scrolls_ok": False,
            "order_ascending_ok": False,
            "rows_preview": [],
        },
        "errors": [],
    }

    opts = webdriver.ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1280,900")

    driver = webdriver.Chrome(options=opts)
    driver.implicitly_wait(3)

    try:
        # 1. 페이지 로드
        driver.get(BASE_URL)
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "origin"))
        )
        results["observed_text"]["page_title"] = driver.title

        # 2. 출발/도착 입력 (최근 검색 있으면 클릭, 없으면 직접 입력)
        origin_el = driver.find_element(By.ID, "origin")
        dest_el = driver.find_element(By.ID, "destination")

        recent_items = driver.find_elements(By.CSS_SELECTOR, ".recent-item")
        if recent_items:
            recent_items[0].click()
            WebDriverWait(driver, 3).until(
                lambda d: bool(d.find_element(By.ID, "origin").get_attribute("value"))
            )
        else:
            origin_el.clear()
            origin_el.send_keys("강남역")
            try:
                WebDriverWait(driver, 5).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, ".ac-item"))
                )
                driver.find_element(By.CSS_SELECTOR, ".ac-item").click()
            except Exception:
                pass  # geocode on search
            dest_el.clear()
            dest_el.send_keys("판교역")
            try:
                WebDriverWait(driver, 5).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, ".ac-item"))
                )
                driver.find_elements(By.CSS_SELECTOR, ".ac-item")[0].click()
            except Exception:
                pass

        # 3. 탭 "출발 시각 기준" 확인 (기본 활성)
        tab_arrival = driver.find_element(By.ID, "tab-arrival")
        if "active" not in tab_arrival.get_attribute("class"):
            tab_arrival.click()

        # 4. 시간 설정 2026-02-17 22:00 (datetime-local은 send_keys가 불안정하므로 JS 사용)
        datetime_value = "2026-02-17T22:00"
        driver.execute_script(
            f"document.getElementById('datetime-input').value = '{datetime_value}';"
        )
        driver.execute_script(
            "document.getElementById('datetime-input').dispatchEvent(new Event('change', { bubbles: true }));"
        )

        # 5. 검색 클릭
        actual_datetime = driver.find_element(By.ID, "datetime-input").get_attribute("value")
        if not actual_datetime:
            results["errors"].append("datetime-input 값이 비어 있음")
        results["observed_text"]["datetime_set"] = actual_datetime or "(empty)"
        search_btn = driver.find_element(By.ID, "search-btn")
        search_btn.click()

        # 6. 첫 단계: 분석 카드 + 진행 카드 대기 (최대 90초)
        try:
            WebDriverWait(driver, 90).until(
                lambda d: (
                    "출발 시각 분석" in d.find_element(By.ID, "results").text
                    and "추천 출발 시각 계산 중" in d.find_element(By.ID, "results").text
                )
            )
            results["step1_analysis_first"] = "success"
            results["observed_text"]["analysis_card"] = "출발 시각 분석"
            results["observed_text"]["progress_card"] = "추천 출발 시각 계산 중"
            results_area = driver.find_element(By.ID, "results")
            if "기준" in results_area.text:
                results["observed_text"]["tight_tag"] = "기준"
            if "안정적" in results_area.text:
                results["observed_text"]["safe_tag"] = "안정적"
            if "1.25" in results_area.text or "×1.25" in results_area.text:
                results["observed_text"]["safe_multiplier"] = "x1.25"

            ok1 = _save_screenshot(driver, PATH_1)
            results["screenshots"].append(str(PATH_1))
            results["screenshot_1_ok"] = ok1

        except Exception as e:
            results["step1_analysis_first"] = "fail"
            results["errors"].append(f"Step1: {e!s}")
            try:
                results["observed_text"]["results_at_fail"] = driver.find_element(
                    By.ID, "results"
                ).text[:500]
            except Exception:
                pass
            try:
                err_el = driver.find_element(By.ID, "error-msg")
                if err_el and err_el.text:
                    results["observed_text"]["error_message"] = err_el.text
            except Exception:
                pass
            ok1 = _save_screenshot(driver, PATH_1)
            results["screenshots"].append(str(PATH_1))
            results["screenshot_1_ok"] = ok1

        # 7. 추천 완료 대기 (최대 180초)
        try:
            WebDriverWait(driver, 180).until(
                lambda d: (
                    "추천 출발 시각" in d.find_element(By.ID, "results").text
                    and "추천 출발 시각 계산 중" not in d.find_element(By.ID, "results").text
                )
            )
            results["step2_recommendation_added"] = "success"
            results["observed_text"]["recommendation_card"] = "추천 출발 시각"

            ok2 = _save_screenshot(driver, PATH_2)
            results["screenshots"].append(str(PATH_2))
            results["screenshot_2_ok"] = ok2

        except Exception as e:
            results["step2_recommendation_added"] = "fail"
            results["errors"].append(f"Step2: {e!s}")
            try:
                results["observed_text"]["results_at_fail"] = driver.find_element(
                    By.ID, "results"
                ).text[:500]
            except Exception:
                pass
            ok2 = _save_screenshot(driver, PATH_2)
            results["screenshots"].append(str(PATH_2))
            results["screenshot_2_ok"] = ok2

        # 8. 후보 배지 hover tooltip 검증
        try:
            candidate_badge = WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".candidate-badge"))
            )
            ActionChains(driver).move_to_element(candidate_badge).perform()
            WebDriverWait(driver, 5).until(
                lambda d: float(
                    d.execute_script(
                        "const p=document.querySelector('.candidate-tooltip-portal.is-visible');"
                        "return p ? getComputedStyle(p).opacity : 0;"
                    )
                )
                > 0.9
            )
            time.sleep(0.35)

            tooltip_panel = driver.find_element(
                By.CSS_SELECTOR,
                ".candidate-tooltip-portal.is-visible",
            )
            tooltip_items = tooltip_panel.find_elements(By.CSS_SELECTOR, ".candidate-tooltip-item")
            if tooltip_items:
                ActionChains(driver).move_to_element(tooltip_items[0]).perform()
            else:
                ActionChains(driver).move_to_element(tooltip_panel).perform()
            time.sleep(0.3)

            hover_persist_opacity = float(
                driver.execute_script(
                    "const p=document.querySelector('.candidate-tooltip-portal.is-visible');"
                    "return p ? parseFloat(getComputedStyle(p).opacity || '0') : 0;"
                )
            )
            hover_persist_ok = hover_persist_opacity > 0.9

            header_scroll_metrics = driver.execute_script(
                """
                const panel = arguments[0];
                if (!panel) return null;
                const header = panel.querySelector('.candidate-tooltip-header');
                if (!header) return null;
                const beforeTop = header.getBoundingClientRect().top;
                const maxScroll = Math.max(0, panel.scrollHeight - panel.clientHeight);
                const targetScroll = Math.min(maxScroll, 140);
                panel.scrollTop = targetScroll;
                const afterTop = header.getBoundingClientRect().top;
                return {
                  before_top: beforeTop,
                  after_top: afterTop,
                  moved_px: beforeTop - afterTop,
                  scroll_top: panel.scrollTop,
                  max_scroll: maxScroll
                };
                """,
                tooltip_panel,
            )
            if not isinstance(header_scroll_metrics, dict):
                header_scroll_metrics = {
                    "before_top": None,
                    "after_top": None,
                    "moved_px": 0.0,
                    "scroll_top": 0.0,
                    "max_scroll": 0.0,
                }
            moved_px = float(header_scroll_metrics.get("moved_px") or 0.0)
            max_scroll = float(header_scroll_metrics.get("max_scroll") or 0.0)
            header_scrolls_ok = moved_px > 20.0 if max_scroll > 30.0 else True

            tooltip_state = driver.execute_script(
                """
                const sidebar = document.getElementById('sidebar');
                const badge = document.querySelector('.candidate-badge');
                if (!sidebar || !badge) return null;
                const panel = document.querySelector('.candidate-tooltip-portal.is-visible');
                if (!panel) return null;
                const sidebarRect = sidebar.getBoundingClientRect();
                const panelRect = panel.getBoundingClientRect();
                const rows = Array.from(
                  panel.querySelectorAll('.candidate-tooltip-row')
                ).map((el) => el.textContent.trim());
                return {
                  sidebar_left: sidebarRect.left,
                  sidebar_right: sidebarRect.right,
                  sidebar_width: sidebarRect.width,
                  panel_left: panelRect.left,
                  panel_right: panelRect.right,
                  panel_width: panelRect.width,
                  panel_left_style: panel.style.left,
                  panel_transform: getComputedStyle(panel).transform,
                  rows: rows
                };
                """
            )

            if not tooltip_state:
                raise RuntimeError("tooltip_state 수집 실패")

            overflow_left = max(
                0.0,
                float(tooltip_state["sidebar_left"]) - float(tooltip_state["panel_left"]),
            )
            overflow_right = max(
                0.0,
                float(tooltip_state["panel_right"]) - float(tooltip_state["sidebar_right"]),
            )
            rows = tooltip_state.get("rows", [])
            row_list = rows if isinstance(rows, list) else []
            rows_preview = row_list[:6]
            has_two_datetimes = any(
                len(DATETIME_RE.findall(line)) >= 2 for line in row_list[1:6]
            )
            departures = []
            for line in row_list[1:]:
                match = DATETIME_RE.search(line)
                if not match:
                    continue
                departures.append(
                    datetime.strptime(match.group(0), "%Y-%m-%d %H:%M")
                )
            order_ascending_ok = True
            if len(departures) >= 2:
                order_ascending_ok = all(
                    departures[idx] <= departures[idx + 1]
                    for idx in range(len(departures) - 1)
                )

            results["tooltip_check"] = {
                "status": "success",
                "overflow_left_px": round(overflow_left, 2),
                "overflow_right_px": round(overflow_right, 2),
                "datetime_pattern_ok": has_two_datetimes,
                "hover_persist_ok": hover_persist_ok,
                "header_scrolls_ok": header_scrolls_ok,
                "order_ascending_ok": order_ascending_ok,
                "rows_preview": rows_preview,
                "sidebar_width_px": round(float(tooltip_state.get("sidebar_width", 0)), 2),
                "panel_width_px": round(float(tooltip_state.get("panel_width", 0)), 2),
                "panel_left_style": tooltip_state.get("panel_left_style"),
                "panel_transform": tooltip_state.get("panel_transform"),
                "header_before_top": header_scroll_metrics.get("before_top"),
                "header_after_top": header_scroll_metrics.get("after_top"),
                "header_moved_px": round(moved_px, 2),
                "panel_scroll_top": header_scroll_metrics.get("scroll_top"),
                "panel_max_scroll": header_scroll_metrics.get("max_scroll"),
            }

            ok3 = _save_screenshot(driver, PATH_3)
            results["screenshots"].append(str(PATH_3))
            results["screenshot_3_ok"] = ok3

            if (
                overflow_left > 1.0
                or overflow_right > 1.0
                or not has_two_datetimes
                or not hover_persist_ok
                or not header_scrolls_ok
                or not order_ascending_ok
            ):
                results["tooltip_check"]["status"] = "fail"
                results["errors"].append(
                    "Step3: tooltip overflow/datetime/hover/header-scroll/order 검증 실패"
                )
        except Exception as e:
            results["tooltip_check"]["status"] = "fail"
            results["errors"].append(f"Step3: {e!s}")
            ok3 = _save_screenshot(driver, PATH_3)
            results["screenshots"].append(str(PATH_3))
            results["screenshot_3_ok"] = ok3

    finally:
        driver.quit()

    # 결과 출력
    report_path = OUTPUT_DIR / "verification_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print("\n=== UI verification result ===\n")
    print(f"1) Analysis card first: {results['step1_analysis_first']}")
    print(f"2) Recommendation card added: {results['step2_recommendation_added']}")
    print(f"3) Screenshot 1 saved OK: {results.get('screenshot_1_ok', False)}")
    print(f"4) Screenshot 2 saved OK: {results.get('screenshot_2_ok', False)}")
    print(f"5) Tooltip check: {results.get('tooltip_check', {}).get('status')}")
    print(f"6) Screenshot 3 saved OK: {results.get('screenshot_3_ok', False)}")
    print("\nScreenshot paths:")
    for p in results["screenshots"]:
        print(f"  - {p}")
    print("\nReport:", str(report_path))
    # File existence check
    for i, p in enumerate([str(PATH_1), str(PATH_2), str(PATH_3)], 1):
        exists = Path(p).exists()
        size = Path(p).stat().st_size if exists else 0
        print(f"  File {i} exists: {exists}, size: {size} bytes")

    if (
        results["step1_analysis_first"] == "fail"
        or results["step2_recommendation_added"] == "fail"
        or results.get("tooltip_check", {}).get("status") == "fail"
    ):
        sys.exit(1)


if __name__ == "__main__":
    main()
