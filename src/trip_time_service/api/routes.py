from __future__ import annotations

import json
import logging
import math
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from trip_time_service.api.schemas import (
    ArrivalTimeRequest,
    ArrivalTimeResponse,
    ArrivalWithRecommendationResponse,
    DepartureRecommendationRequest,
    DepartureRecommendationResponse,
    FrontendConfig,
    RecommendationCandidateModel,
    RouteModel,
    SafeDeparturePreviewResponse,
)
from trip_time_service.core.time_utils import ceil_time_to_minutes, ensure_tzaware
from trip_time_service.services.trip_time_service import TripTimeService

router = APIRouter()
_log = logging.getLogger(__name__)

_GEOCODE_UA = "TripTimeService/0.1 (https://triptime.co.kr)"
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_PHOTON_URL = "https://photon.komoot.io/api/"
_OSRM_URL = "https://router.project-osrm.org/route/v1/driving"


_STEP_MINUTES = 10
_STREAM_QUEUE_POLL_SECONDS = 0.5
_STREAM_IDLE_TIMEOUT_SECONDS = 45.0


def _service(request: Request) -> TripTimeService:
    return request.app.state.trip_time_service


def _ensure_future_time(dt: datetime, tz: ZoneInfo) -> datetime:
    dt = ensure_tzaware(dt, tz)
    now = datetime.now(tz=tz)
    if dt <= now:
        dt = ceil_time_to_minutes(now, _STEP_MINUTES)
    else:
        dt = ceil_time_to_minutes(dt, _STEP_MINUTES)
    return dt


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/api/config", response_model=FrontendConfig)
def frontend_config(request: Request) -> FrontendConfig:
    settings = request.app.state.settings
    return FrontendConfig(
        naver_map_client_id=settings.naver_map_client_id,
        timezone=str(settings.timezone),
        provider=settings.provider,
    )


def _geocode_nominatim(q: str) -> dict | None:
    qs = urllib.parse.urlencode({
        "format": "json", "countrycodes": "kr", "limit": "1", "q": q,
    })
    req = urllib.request.Request(
        f"{_NOMINATIM_URL}?{qs}",
        headers={"Accept-Language": "ko", "User-Agent": _GEOCODE_UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        if data:
            return {
                "lat": data[0]["lat"],
                "lon": data[0]["lon"],
                "display_name": data[0].get("display_name", ""),
            }
    except Exception:
        _log.debug("Nominatim failed for q=%r", q, exc_info=True)
    return None


def _geocode_photon(q: str) -> dict | None:
    qs = urllib.parse.urlencode({"q": q, "limit": "1"})
    req = urllib.request.Request(
        f"{_PHOTON_URL}?{qs}",
        headers={"User-Agent": _GEOCODE_UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        features = data.get("features", [])
        if features:
            coords = features[0]["geometry"]["coordinates"]
            props = features[0]["properties"]
            parts = [
                props.get("name", ""),
                props.get("city", ""),
                props.get("state", ""),
            ]
            display = ", ".join(p for p in parts if p)
            return {
                "lat": str(coords[1]),
                "lon": str(coords[0]),
                "display_name": display,
            }
    except Exception:
        _log.debug("Photon failed for q=%r", q, exc_info=True)
    return None


_naver_lock = threading.Lock()
_naver_driver: webdriver.Chrome | None = None


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


_ROAD_CORE_RE = re.compile(r"(.+(?:로|길)\s*\d+(?:-\d+)?)")


def _extract_road_addr_from_body(body: str) -> str | None:
    lines = body.split("\n")
    for i, line in enumerate(lines):
        if line.strip() == "주소":
            for j in range(i + 1, min(i + 4, len(lines))):
                candidate = lines[j].strip()
                if not candidate:
                    continue
                m = _ROAD_CORE_RE.match(candidate)
                if m:
                    return m.group(1).strip()
                return candidate
    return None



def _naver_search(driver: webdriver.Chrome, q: str) -> None:
    driver.get("https://map.naver.com/")
    WebDriverWait(driver, 10).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )
    time.sleep(1)

    search_input = WebDriverWait(driver, 5).until(
        EC.presence_of_element_located(
            (By.CSS_SELECTOR, "input.input_search")
        )
    )
    search_input.clear()
    search_input.send_keys(q)
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


_NAVER_ADDRESS_PATH_RE = re.compile(
    r"/(?:address|place)/(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)(?:,([^/?#]+))?"
)
_NAVER_C_RE = re.compile(r"[?&]c=(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)")


def _mercator_to_wgs84(x: float, y: float) -> tuple[float, float] | None:
    if abs(x) < 1000 or abs(y) < 1000:
        return None
    lon = (x / 20037508.34) * 180
    lat_rad = (y / 20037508.34) * math.pi
    lat = 180.0 / math.pi * (2.0 * math.atan(math.exp(lat_rad)) - math.pi / 2.0)
    if 33 <= lat <= 43 and 124 <= lon <= 132:
        return lat, lon
    return None


def _extract_coords_from_naver_url(url: str) -> tuple[float, float] | None:
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


def _extract_addr_from_naver_url(url: str) -> str | None:
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
            "var s = document.querySelectorAll("
            "'script[type=\"application/ld+json\"]');"
            "for (var i = 0; i < s.length; i++) {"
            "  try {"
            "    var d = JSON.parse(s[i].textContent);"
            "    if (d.geo) return JSON.stringify("
            "      {lat: d.geo.latitude, lon: d.geo.longitude});"
            "  } catch(e) {}"
            "}"
            "return null;"
        )
        if coords_json:
            c = json.loads(coords_json)
            return {"lat": str(c["lat"]), "lon": str(c["lon"])}
    except Exception:
        _log.debug("entryIframe coord extraction failed", exc_info=True)
    finally:
        driver.switch_to.default_content()
    return None


def _geocode_naver(q: str) -> dict | None:

    with _naver_lock:
        try:
            driver = _ensure_naver_driver()
        except Exception:
            _log.warning("Naver driver init failed", exc_info=True)
            return None
        try:
            _naver_search(driver, q)
            time.sleep(3)

            road_addr: str | None = None

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

            coords = _extract_coords_from_naver_url(driver.current_url)
            if coords:
                lat, lon = coords
                if not road_addr:
                    road_addr = _extract_addr_from_naver_url(driver.current_url)
                display = f"{q} ({road_addr})" if road_addr else q
                _log.info("Naver q=%r → URL coords (%s, %s)", q, lat, lon)
                return {
                    "lat": str(lat), "lon": str(lon),
                    "display_name": display,
                }

            entry_coords = _naver_extract_entry_coords(driver)
            if entry_coords:
                display = f"{q} ({road_addr})" if road_addr else q
                entry_coords["display_name"] = display
                _log.info("Naver q=%r → entry coords", q)
                return entry_coords

            if not road_addr:
                _log.info("Naver: no road address for q=%r", q)
                return None

            _log.info("Naver q=%r → road_addr=%r (fallback geocode)", q, road_addr)
            result = _geocode_nominatim(road_addr)
            if not result:
                result = _geocode_photon(road_addr)
            if result:
                result["display_name"] = f"{q} ({road_addr})"
                return result

            _log.info("Naver: geocoding failed for addr=%r", road_addr)
        except Exception:
            _log.debug("Naver geocode failed for q=%r", q, exc_info=True)
            try:
                driver.switch_to.default_content()
            except Exception:
                pass
    return None


_NAVER_MAP_SEARCH_URL = "https://map.naver.com/p/api/search/allSearch"
_NAVER_MAP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def _autocomplete_naver_map_raw(q: str, limit: int = 5) -> tuple[dict, ...]:
    qs = urllib.parse.urlencode({
        "query": q,
        "type": "all",
        "searchCoord": "126.978;37.5665",
        "boundary": "",
    })
    req = urllib.request.Request(
        f"{_NAVER_MAP_SEARCH_URL}?{qs}",
        headers={
            "User-Agent": _NAVER_MAP_UA,
            "Referer": "https://map.naver.com/",
            "Accept": "application/json, text/plain, */*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            raw = resp.read()
            _log.info("Naver map resp status=%d len=%d", resp.status, len(raw))
            data = json.loads(raw)
    except urllib.error.HTTPError as e:
        body = e.read()[:300] if hasattr(e, "read") else b""
        _log.warning(
            "Naver map search HTTP %d q=%r body=%s",
            e.code, q, body.decode("utf-8", errors="replace"),
        )
        return ()
    except Exception:
        _log.warning("Naver map search failed q=%r", q, exc_info=True)
        return ()

    result_root = data.get("result") or {}
    _log.info(
        "Naver map AC q=%r keys=%s",
        q,
        (
            list(result_root.keys())
            if isinstance(result_root, dict)
            else type(result_root).__name__
        ),
    )
    results: list[dict] = []

    place_section = result_root.get("place") or {}
    for item in (place_section.get("list") or []):
        if len(results) >= limit:
            break
        name = item.get("name", "")
        road = item.get("roadAddress", "") or item.get("address", "")
        x = item.get("x", "")
        y = item.get("y", "")
        if not x or not y:
            continue
        cats = item.get("category") or []
        if isinstance(cats, list):
            cat = cats[-1] if cats else ""
        else:
            cat = str(cats).split(">")[-1].strip()
        results.append({
            "lat": y,
            "lon": x,
            "display_name": name,
            "address": road,
            "type": cat,
        })

    addr_section = result_root.get("address") or {}
    for item in (addr_section.get("list") or []):
        if len(results) >= limit:
            break
        name = item.get("name", "") or item.get("roadAddress", "")
        road = item.get("roadAddress", "") or item.get("address", "")
        x = item.get("x", "")
        y = item.get("y", "")
        if not x or not y:
            continue
        results.append({
            "lat": y,
            "lon": x,
            "display_name": name,
            "address": road,
            "type": "주소",
        })

    return tuple(results)


@lru_cache(maxsize=512)
def _autocomplete_naver_map(q: str, limit: int = 5) -> tuple[dict, ...]:
    return _autocomplete_naver_map_raw(q, limit)


@router.get("/api/autocomplete")
def autocomplete(
    q: str = Query(..., min_length=1, max_length=200),
) -> JSONResponse:
    results = _autocomplete_naver_map(q, 5)
    return JSONResponse(content=list(results))


@router.get("/api/geocode")
def geocode(q: str = Query(..., min_length=1, max_length=200)) -> JSONResponse:
    """Multi-source geocode: Nominatim → Photon → Naver Maps fallback."""
    result = _geocode_nominatim(q)
    if not result:
        result = _geocode_photon(q)
    if not result:
        result = _geocode_naver(q)
    if result:
        _log.info("geocode q=%r → %s", q, result["display_name"])
        return JSONResponse(content=[result])
    _log.warning("geocode q=%r → no results from any source", q)
    return JSONResponse(content=[])


@router.get("/api/route")
def route_between(
    olat: float = Query(...),
    olon: float = Query(...),
    dlat: float = Query(...),
    dlon: float = Query(...),
) -> JSONResponse:
    """OSRM proxy – returns driving route GeoJSON."""
    url = (
        f"{_OSRM_URL}/{olon},{olat};{dlon},{dlat}"
        f"?overview=full&geometries=geojson"
    )
    req = urllib.request.Request(url, headers={"User-Agent": _GEOCODE_UA})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return JSONResponse(content=json.loads(resp.read()))
    except Exception:
        _log.exception("OSRM route failed")
        return JSONResponse(content={"code": "Error", "routes": []})


_geo_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="geo")


def _geocode_one(place: str) -> dict | None:
    result = _geocode_nominatim(place)
    if not result:
        result = _geocode_photon(place)
    if not result:
        result = _geocode_naver(place)
    return result


def _pre_geocode_for_provider(
    service: TripTimeService,
    *places: str,
    coords_map: dict[str, tuple[float, float]] | None = None,
) -> None:
    provider = service._provider
    if not hasattr(provider, "set_coords"):
        return

    coords_map = coords_map or {}
    need_geocode: list[str] = []

    for place in places:
        if place in coords_map:
            lat, lon = coords_map[place]
            provider.set_coords(place, lat, lon)
            _log.info(
                "Pre-geocoded (frontend coords) %r → (%s, %s)",
                place, lat, lon,
            )
        else:
            need_geocode.append(place)

    if not need_geocode:
        return

    def _do_geocode(p: str) -> None:
        result = _geocode_one(p)
        if result:
            provider.set_coords(p, float(result["lat"]), float(result["lon"]))
            _log.info("Pre-geocoded %r → (%s, %s)", p, result["lat"], result["lon"])

    if len(need_geocode) == 1:
        _do_geocode(need_geocode[0])
    else:
        futs = [_geo_pool.submit(_do_geocode, p) for p in need_geocode]
        for f in as_completed(futs, timeout=30):
            f.result()


def _extract_coords_map(
    origin: str,
    destination: str,
    origin_coords: object | None,
    dest_coords: object | None,
) -> dict[str, tuple[float, float]]:
    coords_map: dict[str, tuple[float, float]] = {}
    if origin_coords:
        coords_map[origin] = (origin_coords.lat, origin_coords.lon)
    if dest_coords:
        coords_map[destination] = (dest_coords.lat, dest_coords.lon)
    return coords_map


def _to_arrival_response(result: object) -> ArrivalTimeResponse:
    return ArrivalTimeResponse(
        route=RouteModel(
            origin=result.route.origin,
            destination=result.route.destination,
        ),
        departure_time=result.departure_time,
        arrival_time=result.arrival_time,
        duration_seconds=result.duration.duration_seconds,
        provider=result.provider,
        cache_hit=result.cache_hit,
    )


def _to_departure_response(result: object) -> DepartureRecommendationResponse:
    candidates = [
        RecommendationCandidateModel(
            departure_time=item.departure_time,
            arrival_time=item.arrival_time,
            duration_seconds=item.duration_seconds,
            meets_deadline=item.meets_deadline,
            phase=item.phase,
            score_total=item.score_total,
            score_duration=item.score_duration,
            score_time_proximity=item.score_time_proximity,
            score_night_drive=item.score_night_drive,
            score_stability=item.score_stability,
            score_improvement_efficiency=item.score_improvement_efficiency,
        )
        for item in result.candidate_evaluations
    ]
    return DepartureRecommendationResponse(
        route=RouteModel(
            origin=result.route.origin,
            destination=result.route.destination,
        ),
        desired_arrival_time=result.desired_arrival_time,
        recommended_departure_time=result.recommended_departure_time,
        expected_arrival_time=result.expected_arrival_time,
        duration_seconds=result.duration.duration_seconds,
        meets_deadline=result.meets_deadline,
        provider=result.provider,
        provider_calls=result.provider_calls,
        candidates_checked=result.candidates_checked,
        planned_queries=result.planned_queries,
        total_candidates=result.total_candidates,
        latest_departure_time=result.latest_departure_time,
        latest_departure_arrival_time=result.latest_departure_arrival_time,
        latest_departure_duration_seconds=result.latest_departure_duration_seconds,
        safe_departure_time=result.safe_departure_time,
        safe_departure_duration_seconds=result.safe_departure_duration_seconds,
        recommended_score_total=result.recommended_score_total,
        baseline_score_total=result.baseline_score_total,
        candidate_evaluations=candidates,
    )


def _compute_safe_preview(
    *,
    desired_arrival_time: datetime,
    base_duration_seconds: int,
    tz: ZoneInfo,
) -> SafeDeparturePreviewResponse:
    safe_duration = math.ceil(base_duration_seconds * 1.25)
    baseline_departure = desired_arrival_time - timedelta(seconds=base_duration_seconds)
    now_floor = _ensure_future_time(datetime.now(tz=tz), tz)
    clamped = baseline_departure < now_floor
    safe_departure = now_floor if clamped else baseline_departure
    return SafeDeparturePreviewResponse(
        safe_departure_time=safe_departure,
        safe_duration_seconds=safe_duration,
        clamped_to_now=clamped,
    )


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Not JSON serializable: {type(value)!r}")


def _sse_encode(event: str, data: object) -> str:
    payload = json.dumps(data, ensure_ascii=False, default=_json_default)
    return f"event: {event}\ndata: {payload}\n\n"


def _iter_stream_events(
    *,
    event_queue: queue.Queue[object],
    done_marker: object,
    worker: threading.Thread,
    idle_timeout_seconds: float,
) -> object:
    idle_deadline = time.monotonic() + idle_timeout_seconds
    while True:
        try:
            item = event_queue.get(timeout=_STREAM_QUEUE_POLL_SECONDS)
        except queue.Empty:
            if not worker.is_alive():
                break
            if time.monotonic() >= idle_deadline:
                yield {
                    "event": "error",
                    "data": {
                        "detail": (
                            "추천 계산 워커가 응답하지 않아 스트림을 종료합니다"
                        )
                    },
                }
                break
            continue

        idle_deadline = time.monotonic() + idle_timeout_seconds
        if item is done_marker:
            break
        yield item


@router.post("/v1/trip/arrival-time", response_model=ArrivalTimeResponse)
def estimate_arrival_time(
    payload: ArrivalTimeRequest,
    request: Request,
) -> ArrivalTimeResponse:
    service = _service(request)
    tz = request.app.state.settings.timezone
    departure = _ensure_future_time(payload.departure_time, tz)

    coords_map = _extract_coords_map(
        payload.origin,
        payload.destination,
        payload.origin_coords,
        payload.dest_coords,
    )
    _pre_geocode_for_provider(
        service, payload.origin, payload.destination,
        coords_map=coords_map,
    )

    result = service.estimate_arrival(
        origin=payload.origin,
        destination=payload.destination,
        departure_time=departure,
    )

    return _to_arrival_response(result)


@router.post(
    "/v1/trip/recommended-departure-time",
    response_model=DepartureRecommendationResponse,
)
def recommend_departure_time(
    payload: DepartureRecommendationRequest,
    request: Request,
) -> DepartureRecommendationResponse:
    service = _service(request)
    tz = request.app.state.settings.timezone
    desired = _ensure_future_time(payload.desired_arrival_time, tz)

    coords_map = _extract_coords_map(
        payload.origin,
        payload.destination,
        payload.origin_coords,
        payload.dest_coords,
    )
    _pre_geocode_for_provider(
        service, payload.origin, payload.destination,
        coords_map=coords_map,
    )

    result = service.recommend_departure(
        origin=payload.origin,
        destination=payload.destination,
        desired_arrival_time=desired,
    )

    return _to_departure_response(result)


@router.post(
    "/v1/trip/arrival-time-with-recommendation",
    response_model=ArrivalWithRecommendationResponse,
)
def estimate_arrival_with_recommendation(
    payload: ArrivalTimeRequest,
    request: Request,
) -> ArrivalWithRecommendationResponse:
    service = _service(request)
    tz = request.app.state.settings.timezone
    departure = _ensure_future_time(payload.departure_time, tz)

    coords_map = _extract_coords_map(
        payload.origin,
        payload.destination,
        payload.origin_coords,
        payload.dest_coords,
    )
    _pre_geocode_for_provider(
        service, payload.origin, payload.destination,
        coords_map=coords_map,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        recommendation_future = executor.submit(
            service.recommend_departure,
            origin=payload.origin,
            destination=payload.destination,
            desired_arrival_time=departure,
            analysis_start_time=departure,
        )
        arrival = service.estimate_arrival(
            origin=payload.origin,
            destination=payload.destination,
            departure_time=departure,
        )
        recommendation = recommendation_future.result()

    immediate_safe = _compute_safe_preview(
        desired_arrival_time=arrival.arrival_time,
        base_duration_seconds=arrival.duration.duration_seconds,
        tz=tz,
    )
    return ArrivalWithRecommendationResponse(
        arrival=_to_arrival_response(arrival),
        recommendation=_to_departure_response(recommendation),
        immediate_safe_departure=immediate_safe,
    )


@router.post("/v1/trip/arrival-time-with-recommendation/stream")
def stream_arrival_with_recommendation(
    payload: ArrivalTimeRequest,
    request: Request,
) -> StreamingResponse:
    service = _service(request)
    tz = request.app.state.settings.timezone
    departure = _ensure_future_time(payload.departure_time, tz)

    coords_map = _extract_coords_map(
        payload.origin,
        payload.destination,
        payload.origin_coords,
        payload.dest_coords,
    )
    _pre_geocode_for_provider(
        service, payload.origin, payload.destination,
        coords_map=coords_map,
    )

    done_marker = object()
    event_queue: queue.Queue[object] = queue.Queue()
    progress = {
        "checked": 0,
        "planned": 0,
        "remaining": 0,
        "total_candidates": 0,
    }

    def _on_initialized(total_candidates: int, planned_queries: int) -> None:
        progress["total_candidates"] = total_candidates
        progress["planned"] = planned_queries
        progress["remaining"] = max(
            0,
            planned_queries - progress["checked"],
        )
        event_queue.put({
            "event": "plan",
            "data": progress.copy(),
        })

    def _on_candidate(candidate: object) -> None:
        progress["checked"] += 1
        progress["remaining"] = max(
            0,
            progress["planned"] - progress["checked"],
        )
        event_queue.put({
            "event": "candidate",
            "data": {
                "candidate": RecommendationCandidateModel(
                    departure_time=candidate.departure_time,
                    arrival_time=candidate.arrival_time,
                    duration_seconds=candidate.duration_seconds,
                    meets_deadline=candidate.meets_deadline,
                    phase=candidate.phase,
                    score_total=candidate.score_total,
                    score_duration=candidate.score_duration,
                    score_time_proximity=candidate.score_time_proximity,
                    score_night_drive=candidate.score_night_drive,
                    score_stability=candidate.score_stability,
                    score_improvement_efficiency=(
                        candidate.score_improvement_efficiency
                    ),
                ).model_dump(mode="json"),
                "progress": progress.copy(),
            },
        })

    def _worker() -> None:
        try:
            recommendation = service.recommend_departure(
                origin=payload.origin,
                destination=payload.destination,
                desired_arrival_time=departure,
                analysis_start_time=departure,
                on_search_initialized=_on_initialized,
                on_candidate_evaluated=_on_candidate,
            )
            event_queue.put({
                "event": "recommendation",
                "data": _to_departure_response(recommendation).model_dump(mode="json"),
            })
        except Exception as exc:
            event_queue.put({
                "event": "error",
                "data": {"detail": str(exc)},
            })
        finally:
            event_queue.put(done_marker)

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()

    arrival = service.estimate_arrival(
        origin=payload.origin,
        destination=payload.destination,
        departure_time=departure,
    )
    immediate_safe = _compute_safe_preview(
        desired_arrival_time=arrival.arrival_time,
        base_duration_seconds=arrival.duration.duration_seconds,
        tz=tz,
    )

    def _stream() -> object:
        first_payload = {
            "arrival": _to_arrival_response(arrival).model_dump(mode="json"),
            "immediate_safe_departure": immediate_safe.model_dump(mode="json"),
            "progress": progress.copy(),
        }
        yield _sse_encode("arrival", first_payload)
        for item in _iter_stream_events(
            event_queue=event_queue,
            done_marker=done_marker,
            worker=worker,
            idle_timeout_seconds=_STREAM_IDLE_TIMEOUT_SECONDS,
        ):
            yield _sse_encode(item["event"], item["data"])
        yield _sse_encode("end", {"ok": True})

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
