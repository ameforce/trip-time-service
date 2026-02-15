from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from datetime import datetime
from threading import Lock

from trip_time_service.core.models import DriveDuration, Route

_log = logging.getLogger(__name__)

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_PHOTON_URL = "https://photon.komoot.io/api/"
_OSRM_URL = "https://router.project-osrm.org/route/v1/driving"
_UA = "TripTimeService/0.1 (https://triptime.co.kr)"

# OSRM 기본값은 제한속도 기반 이론치이므로
# 실제 한국 도심 교통(신호대기, 정체)을 반영하여 보정.
_TRAFFIC_TABLE: list[tuple[int, float]] = [
    (0, 1.05),   # 심야 (신호 대기 최소)
    (5, 1.05),
    (6, 1.20),   # 새벽 → 아침
    (7, 1.70),   # 오전 러시아워 시작
    (8, 2.00),   # 오전 피크
    (9, 1.60),   # 러시아워 완화
    (10, 1.30),
    (12, 1.25),  # 평시 (신호/교차로 대기)
    (14, 1.25),
    (16, 1.40),  # 오후 러시아워 진입
    (17, 1.75),
    (18, 2.00),  # 오후 피크
    (19, 1.65),  # 완화
    (20, 1.30),
    (22, 1.15),
    (23, 1.05),
]


def _traffic_multiplier(hour: int, minute: int = 0) -> float:
    t = hour + minute / 60.0
    prev_h, prev_m = _TRAFFIC_TABLE[-1]
    for cur_h, cur_m in _TRAFFIC_TABLE:
        if t <= cur_h:
            if cur_h == prev_h:
                return cur_m
            ratio = (t - prev_h) / (cur_h - prev_h)
            return prev_m + ratio * (cur_m - prev_m)
        prev_h, prev_m = cur_h, cur_m
    return _TRAFFIC_TABLE[-1][1]


def _geocode_place(query: str) -> tuple[float, float] | None:
    # 1) Nominatim
    qs = urllib.parse.urlencode({
        "format": "json", "countrycodes": "kr", "limit": "1", "q": query,
    })
    req = urllib.request.Request(
        f"{_NOMINATIM_URL}?{qs}",
        headers={"Accept-Language": "ko", "User-Agent": _UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception:
        _log.debug("OSRM geocode Nominatim failed: %r", query, exc_info=True)

    # 2) Photon
    qs = urllib.parse.urlencode({
        "q": query, "limit": "1", "lat": "37.5665", "lon": "126.978",
    })
    req = urllib.request.Request(
        f"{_PHOTON_URL}?{qs}",
        headers={"User-Agent": _UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        features = data.get("features", [])
        if features:
            coords = features[0]["geometry"]["coordinates"]
            return float(coords[1]), float(coords[0])
    except Exception:
        _log.debug("OSRM geocode Photon failed: %r", query, exc_info=True)

    return None


def _osrm_duration(
    olat: float, olon: float, dlat: float, dlon: float,
) -> float | None:
    url = f"{_OSRM_URL}/{olon},{olat};{dlon},{dlat}?overview=false"
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        routes = data.get("routes", [])
        if routes:
            return float(routes[0]["duration"])
    except Exception:
        _log.exception("OSRM route query failed")
    return None


_NOT_CACHED = object()


class OsrmTravelTimeProvider:
    name = "osrm"

    def __init__(self) -> None:
        self._lock = Lock()
        self._coord_cache: dict[str, tuple[float, float] | None] = {}
        self._base_cache: dict[Route, float] = {}

    def set_coords(self, place: str, lat: float, lon: float) -> None:
        key = " ".join(place.strip().split())
        with self._lock:
            self._coord_cache[key] = (lat, lon)

    def get_drive_duration(
        self, route: Route, departure_time: datetime,
    ) -> DriveDuration:
        base_sec = self._get_base_duration(route)
        mult = _traffic_multiplier(departure_time.hour, departure_time.minute)
        adjusted = max(60, int(base_sec * mult))

        return DriveDuration(
            duration_seconds=adjusted,
            fetched_at=datetime.now(tz=departure_time.tzinfo),
            raw_text=(
                f"OSRM {base_sec:.0f}s × {mult:.2f} = {adjusted}s"
            ),
        )

    def close(self) -> None:
        pass

    # ── internal ──

    def _get_base_duration(self, route: Route) -> float:
        with self._lock:
            cached = self._base_cache.get(route)
            if cached is not None:
                return cached

        o = self._geocode(route.origin)
        d = self._geocode(route.destination)
        if o is None or d is None:
            _log.warning(
                "OSRM: geocode failed, origin=%r dest=%r", route.origin, route.destination,
            )
            return 1800.0

        dur = _osrm_duration(o[0], o[1], d[0], d[1])
        if dur is None:
            _log.warning("OSRM: route query failed, using fallback")
            return 1800.0

        with self._lock:
            self._base_cache[route] = dur
        _log.info(
            "OSRM: %s → %s = %.0f초 (%.1f분)",
            route.origin, route.destination, dur, dur / 60,
        )
        return dur

    def _geocode(self, query: str) -> tuple[float, float] | None:
        with self._lock:
            cached = self._coord_cache.get(query, _NOT_CACHED)
            if cached is not _NOT_CACHED:
                return cached

        result = _geocode_place(query)
        with self._lock:
            self._coord_cache[query] = result
        return result
