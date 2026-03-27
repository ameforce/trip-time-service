from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
import urllib.parse
from collections.abc import Callable

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

_log = logging.getLogger(__name__)

_naver_lock = threading.Lock()
_naver_driver: webdriver.Chrome | None = None

_ROAD_CORE_RE = re.compile(r"(.+(?:로|길)\s*\d+(?:-\d+)?)")
_NAVER_ADDRESS_PATH_RE = re.compile(
    r"/(?:address|place)/(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)(?:,([^/?#]+))?"
)
_NAVER_C_RE = re.compile(r"[?&]c=(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)")


def _ensure_naver_driver() -> webdriver.Chrome:
    global _naver_driver
    if _naver_driver is not None:
        return _naver_driver

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--lang=ko-KR")
    opts.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(20)
    driver.set_script_timeout(10)
    _naver_driver = driver
    return driver


def _extract_road_addr_from_body(body: str) -> str | None:
    lines = body.split("\n")
    for i, line in enumerate(lines):
        if line.strip() != "주소":
            continue
        for j in range(i + 1, min(i + 4, len(lines))):
            candidate = lines[j].strip()
            if not candidate:
                continue
            match = _ROAD_CORE_RE.match(candidate)
            if match:
                return match.group(1).strip()
            return candidate
    return None


def _naver_search(driver: webdriver.Chrome, query: str) -> None:
    driver.get("https://map.naver.com/")
    WebDriverWait(driver, 10).until(
        lambda current: current.execute_script("return document.readyState")
        == "complete"
    )
    time.sleep(1)

    search_input = WebDriverWait(driver, 5).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "input.input_search"))
    )
    search_input.clear()
    search_input.send_keys(query)
    time.sleep(0.3)
    search_input.send_keys(Keys.RETURN)


def _naver_read_entry_detail(driver: webdriver.Chrome) -> str | None:
    entry = driver.find_elements(By.ID, "entryIframe")
    if not entry:
        return None
    driver.switch_to.frame(entry[0])
    time.sleep(1.5)
    body = driver.execute_script("return document.body.innerText;")
    driver.switch_to.default_content()
    return _extract_road_addr_from_body(body)


def _mercator_to_wgs84(x: float, y: float) -> tuple[float, float] | None:
    if abs(x) < 1000 or abs(y) < 1000:
        return None

    lon = (x / 20037508.34) * 180
    lat_rad = (y / 20037508.34) * math.pi
    lat = 180.0 / math.pi * (2.0 * math.atan(math.exp(lat_rad)) - math.pi / 2.0)

    if 33 <= lat <= 43 and 124 <= lon <= 132:
        return lat, lon
    return None


def extract_coords_from_naver_url(url: str) -> tuple[float, float] | None:
    path_match = _NAVER_ADDRESS_PATH_RE.search(url)
    if path_match:
        coords = _mercator_to_wgs84(
            float(path_match.group(1)),
            float(path_match.group(2)),
        )
        if coords:
            return coords

    c_match = _NAVER_C_RE.search(url)
    if not c_match:
        return None
    return _mercator_to_wgs84(float(c_match.group(1)), float(c_match.group(2)))


def extract_addr_from_naver_url(url: str) -> str | None:
    path_match = _NAVER_ADDRESS_PATH_RE.search(url)
    if not path_match:
        return None
    encoded_addr = path_match.group(3)
    if not encoded_addr:
        return None
    decoded = urllib.parse.unquote(encoded_addr).strip()
    return decoded or None


def _naver_extract_entry_coords(driver: webdriver.Chrome) -> dict | None:
    entry = driver.find_elements(By.ID, "entryIframe")
    if not entry:
        return None
    driver.switch_to.frame(entry[0])
    try:
        coords_json = driver.execute_script(
            "var scripts = document.querySelectorAll("
            "'script[type=\"application/ld+json\"]');"
            "for (var i = 0; i < scripts.length; i++) {"
            "  try {"
            "    var data = JSON.parse(scripts[i].textContent);"
            "    if (data.geo) return JSON.stringify("
            "      {lat: data.geo.latitude, lon: data.geo.longitude});"
            "  } catch(e) {}"
            "}"
            "return null;"
        )
        if coords_json:
            coords = json.loads(coords_json)
            return {"lat": str(coords["lat"]), "lon": str(coords["lon"])}
    except Exception:
        _log.debug("entryIframe coord extraction failed", exc_info=True)
    finally:
        driver.switch_to.default_content()
    return None


def geocode_naver(
    query: str,
    *,
    fallback_geocode: Callable[[str], dict | None] | None = None,
) -> dict | None:
    with _naver_lock:
        try:
            driver = _ensure_naver_driver()
        except Exception:
            _log.warning("Naver driver init failed", exc_info=True)
            return None

        try:
            _naver_search(driver, query)
            time.sleep(3)

            road_addr = _naver_read_entry_detail(driver)
            if not road_addr:
                search_frames = driver.find_elements(By.ID, "searchIframe")
                if search_frames:
                    driver.switch_to.frame(search_frames[0])
                    WebDriverWait(driver, 5).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "a"))
                    )
                    links = driver.find_elements(By.CSS_SELECTOR, "a")
                    if links:
                        links[0].click()
                    driver.switch_to.default_content()
                    time.sleep(3)
                    road_addr = _naver_read_entry_detail(driver)

            coords = extract_coords_from_naver_url(driver.current_url)
            if coords:
                lat, lon = coords
                if not road_addr:
                    road_addr = extract_addr_from_naver_url(driver.current_url)
                display_name = f"{query} ({road_addr})" if road_addr else query
                _log.info("Naver q=%r → URL coords (%s, %s)", query, lat, lon)
                return {
                    "lat": str(lat),
                    "lon": str(lon),
                    "display_name": display_name,
                }

            entry_coords = _naver_extract_entry_coords(driver)
            if entry_coords:
                display_name = f"{query} ({road_addr})" if road_addr else query
                entry_coords["display_name"] = display_name
                _log.info("Naver q=%r → entry coords", query)
                return entry_coords

            if not road_addr:
                _log.info("Naver: no road address for q=%r", query)
                return None

            _log.info("Naver q=%r → road_addr=%r (fallback geocode)", query, road_addr)
            if fallback_geocode is None:
                return None

            result = fallback_geocode(road_addr)
            if result:
                result["display_name"] = f"{query} ({road_addr})"
                return result
            _log.info("Naver: geocoding failed for addr=%r", road_addr)
        except Exception:
            _log.debug("Naver geocode failed for q=%r", query, exc_info=True)
            try:
                driver.switch_to.default_content()
            except Exception:
                pass
    return None
