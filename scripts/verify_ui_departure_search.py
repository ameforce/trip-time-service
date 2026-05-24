#!/usr/bin/env python3
"""
실제 provider 기준 departure-mode UI 수동 검증 스크립트.

- `tests/live/data/routes-blocking.json`에서 departure 시나리오를 읽는다.
- autocomplete/route/mock interception 없이 실제 UI와 실제 API만 사용한다.
- 초기 진입, 입력 완료, 분석 진행, 최종 결과 스크린샷과 JSON report를 남긴다.

예시:
  uv run --no-sync python scripts/verify_ui_departure_search.py
  uv run --no-sync python scripts/verify_ui_departure_search.py --headed --case-index 1
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

try:
    from selenium import webdriver
    from selenium.webdriver import ActionChains
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
except ImportError:
    print("Selenium 필요: uv sync --extra dev 또는 pip install selenium")
    sys.exit(1)

DEFAULT_BASE_URL = "http://127.0.0.1:8500"
DEFAULT_DATASET = Path("tests/live/data/routes-blocking.json")
DEFAULT_ARTIFACT_ROOT = Path(".artifacts/live/manual-ui")
DATETIME_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")


@dataclass
class RouteCase:
    origin_query: str
    destination_query: str
    mode: str
    future_offset_days: int
    time_hhmm: str
    selection_kind: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate the real departure-mode UI using a live dataset scenario."
        ),
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Service base URL (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--dataset",
        default=str(DEFAULT_DATASET),
        help=f"Route dataset JSON path (default: {DEFAULT_DATASET})",
    )
    parser.add_argument(
        "--case-index",
        type=int,
        default=0,
        help="Zero-based index among departure scenarios in the dataset",
    )
    parser.add_argument(
        "--artifact-root",
        default=str(DEFAULT_ARTIFACT_ROOT),
        help=f"Artifact output directory (default: {DEFAULT_ARTIFACT_ROOT})",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run Chrome with a visible window",
    )
    return parser.parse_args()


def load_departure_case(path: Path, case_index: int) -> RouteCase:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise SystemExit(f"dataset must be a JSON array: {path}")
    cases = [RouteCase(**item) for item in payload if item.get("mode") == "departure"]
    if not cases:
        raise SystemExit(f"no departure case found in dataset: {path}")
    if case_index < 0 or case_index >= len(cases):
        raise SystemExit(f"case-index out of range: {case_index} >= {len(cases)}")
    return cases[case_index]


def clear_autocomplete_cache(base_url: str) -> None:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/debug/autocomplete/cache-clear",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15):
            return
    except urllib.error.URLError as exc:
        raise SystemExit(f"cache clear failed: {exc}") from exc


def normalize_text(value: str) -> str:
    return "".join(value.lower().split())


def build_query_tokens(query: str) -> list[str]:
    tokens = [query, *query.split()]
    unique: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        trimmed = token.strip()
        normalized = normalize_text(trimmed)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(trimmed)
    return unique


def save_screenshot(driver: webdriver.Chrome, path: Path) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    png = driver.get_screenshot_as_png()
    path.write_bytes(png)
    return path.exists() and path.stat().st_size > 0


def set_future_datetime(
    driver: webdriver.Chrome,
    *,
    offset_days: int,
    hhmm: str,
) -> str:
    hours_text, minutes_text = hhmm.split(":")
    target = (datetime.now() + timedelta(days=offset_days)).replace(
        hour=int(hours_text),
        minute=int(minutes_text),
        second=0,
        microsecond=0,
    )
    value = target.strftime("%Y-%m-%dT%H:%M")
    driver.execute_script(
        "const el = document.getElementById('datetime-input');"
        "el.value = arguments[0];"
        "el.dispatchEvent(new Event('input', { bubbles: true }));"
        "el.dispatchEvent(new Event('change', { bubbles: true }));",
        value,
    )
    return value


def select_autocomplete_entry(
    driver: webdriver.Chrome,
    wait: WebDriverWait,
    *,
    input_id: str,
    dropdown_id: str,
    query: str,
    base_url: str,
) -> dict[str, Any]:
    tokens = build_query_tokens(query)
    input_el = driver.find_element(By.ID, input_id)

    last_error: Exception | None = None
    for attempt in range(1, 3):
        input_el.clear()
        input_el.send_keys(query)
        try:
            wait.until(
                lambda current: len(
                    [
                        item
                        for item in current.find_elements(
                            By.CSS_SELECTOR, f"#{dropdown_id} .ac-item"
                        )
                        if item.is_displayed()
                    ]
                )
                > 0
            )
            items = [
                item
                for item in driver.find_elements(
                    By.CSS_SELECTOR, f"#{dropdown_id} .ac-item"
                )
                if item.is_displayed()
            ]
            texts = [item.text.strip() for item in items[:5]]
            selected_index = 0
            matched_token = None
            for idx, text in enumerate(texts):
                normalized_text = normalize_text(text)
                token = next(
                    (
                        candidate
                        for candidate in tokens
                        if normalize_text(candidate) in normalized_text
                    ),
                    None,
                )
                if token:
                    selected_index = idx
                    matched_token = token
                    break

            items[selected_index].click()
            wait.until(
                lambda current: bool(
                    current.find_element(By.ID, input_id).get_attribute("value")
                )
            )
            return {
                "query": query,
                "attempts_used": attempt,
                "retried": attempt > 1,
                "matched_token": matched_token,
                "clicked_text": texts[selected_index] if texts else "",
                "top_dropdown_texts": texts,
            }
        except Exception as exc:  # pragma: no cover - live UI retry path
            last_error = exc
            if attempt == 1:
                clear_autocomplete_cache(base_url)
                continue
            raise

    raise RuntimeError(f"autocomplete selection failed: {query}: {last_error}")


def collect_tooltip_state(driver: webdriver.Chrome) -> dict[str, Any]:
    badge = WebDriverWait(driver, 20).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, ".candidate-badge"))
    )
    ActionChains(driver).move_to_element(badge).perform()
    WebDriverWait(driver, 10).until(
        EC.visibility_of_element_located(
            (By.CSS_SELECTOR, ".candidate-tooltip-portal.is-visible")
        )
    )
    tooltip_panel = driver.find_element(
        By.CSS_SELECTOR, ".candidate-tooltip-portal.is-visible"
    )
    tooltip_rows = tooltip_panel.find_elements(
        By.CSS_SELECTOR, ".candidate-tooltip-row"
    )
    row_texts = [row.text.strip() for row in tooltip_rows[:6]]
    departures: list[datetime] = []
    for row in row_texts[1:]:
        match = DATETIME_RE.search(row)
        if match:
            departures.append(datetime.strptime(match.group(0), "%Y-%m-%d %H:%M"))

    geometry = driver.execute_script(
        """
        const sidebar = document.getElementById('sidebar');
        const panel = document.querySelector('.candidate-tooltip-portal.is-visible');
        if (!sidebar || !panel) return null;
        const sidebarRect = sidebar.getBoundingClientRect();
        const panelRect = panel.getBoundingClientRect();
        return {
          sidebar_left: sidebarRect.left,
          sidebar_right: sidebarRect.right,
          panel_left: panelRect.left,
          panel_right: panelRect.right,
          panel_width: panelRect.width
        };
        """
    )
    if not isinstance(geometry, dict):
        geometry = {}

    return {
        "status": "success",
        "rows_preview": row_texts,
        "datetime_pattern_ok": any(
            len(DATETIME_RE.findall(row)) >= 2 for row in row_texts[1:6]
        ),
        "order_ascending_ok": all(
            departures[index] <= departures[index + 1]
            for index in range(len(departures) - 1)
        )
        if len(departures) >= 2
        else True,
        "overflow_left_px": round(
            max(
                0.0,
                float(geometry.get("sidebar_left", 0))
                - float(geometry.get("panel_left", 0)),
            ),
            2,
        ),
        "overflow_right_px": round(
            max(
                0.0,
                float(geometry.get("panel_right", 0))
                - float(geometry.get("sidebar_right", 0)),
            ),
            2,
        ),
        "panel_width_px": round(float(geometry.get("panel_width", 0)), 2),
    }


def main() -> int:
    args = parse_args()
    base_url = args.base_url.rstrip("/")
    dataset_path = Path(args.dataset)
    artifact_root = Path(args.artifact_root)
    artifact_root.mkdir(parents=True, exist_ok=True)

    scenario = load_departure_case(dataset_path, args.case_index)
    clear_autocomplete_cache(base_url)

    screenshots = {
        "initial": artifact_root / "01_initial.png",
        "ready": artifact_root / "02_ready.png",
        "progress": artifact_root / "03_progress.png",
        "final": artifact_root / "04_final.png",
    }
    report_path = artifact_root / "verify-ui-departure-search.json"
    report: dict[str, Any] = {
        "base_url": base_url,
        "dataset": str(dataset_path),
        "case_index": args.case_index,
        "scenario": asdict(scenario),
        "screenshots": {name: str(path) for name, path in screenshots.items()},
        "steps": {},
        "tooltip": {},
        "errors": [],
    }

    options = webdriver.ChromeOptions()
    if not args.headed:
        options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1440,1080")
    driver = webdriver.Chrome(options=options)
    wait = WebDriverWait(driver, 30)

    try:
        driver.get(base_url)
        wait.until(EC.presence_of_element_located((By.ID, "origin")))
        report["steps"]["initial_loaded"] = save_screenshot(
            driver, screenshots["initial"]
        )

        departure_tab = driver.find_element(By.ID, "tab-departure")
        if "active" not in departure_tab.get_attribute("class"):
            departure_tab.click()
        departure_tab_class = departure_tab.get_attribute("class")
        report["steps"]["departure_tab_active"] = "active" in departure_tab_class
        report["steps"]["departure_tab_class"] = departure_tab_class

        report["steps"]["origin"] = select_autocomplete_entry(
            driver,
            wait,
            input_id="origin",
            dropdown_id="origin-ac",
            query=scenario.origin_query,
            base_url=base_url,
        )
        report["steps"]["destination"] = select_autocomplete_entry(
            driver,
            wait,
            input_id="destination",
            dropdown_id="dest-ac",
            query=scenario.destination_query,
            base_url=base_url,
        )
        report["steps"]["datetime_value"] = set_future_datetime(
            driver,
            offset_days=scenario.future_offset_days,
            hhmm=scenario.time_hhmm,
        )
        report["steps"]["ready_capture_ok"] = save_screenshot(
            driver, screenshots["ready"]
        )

        driver.find_element(By.ID, "search-btn").click()
        wait.until(
            lambda current: "출발 시각 분석"
            in current.find_element(By.ID, "results").text
            and "추천 출발 시각 계산 중"
            in current.find_element(By.ID, "results").text
        )
        report["steps"]["analysis_stage_visible"] = True
        report["steps"]["progress_capture_ok"] = save_screenshot(
            driver, screenshots["progress"]
        )

        wait.until(
            lambda current: "추천 출발 시각"
            in current.find_element(By.ID, "results").text
            and "추천 출발 시각 계산 중"
            not in current.find_element(By.ID, "results").text
        )
        results_text = driver.find_element(By.ID, "results").text
        report["steps"]["final_results_preview"] = results_text[:800]
        report["steps"]["final_capture_ok"] = save_screenshot(
            driver, screenshots["final"]
        )
        report["tooltip"] = collect_tooltip_state(driver)
    except Exception as exc:  # pragma: no cover - live UI failure path
        report["errors"].append(str(exc))
        for name, path in screenshots.items():
            if not path.exists():
                save_screenshot(driver, path)
                report["steps"][f"{name}_capture_ok"] = path.exists()
    finally:
        driver.quit()

    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["errors"]:
        return 1
    if report["tooltip"].get("status") != "success":
        return 1
    if not report["tooltip"].get("datetime_pattern_ok", False):
        return 1
    if not report["tooltip"].get("order_ascending_ok", False):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
