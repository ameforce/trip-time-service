from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache

from trip_time_service.api.naver_geo import geocode_naver
from trip_time_service.services.trip_time_service import TripTimeService

_log = logging.getLogger(__name__)

_GEOCODE_UA = "TripTimeService/0.1 (https://triptime.co.kr)"
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_PHOTON_URL = "https://photon.komoot.io/api/"
_OSRM_URL = "https://router.project-osrm.org/route/v1/driving"

_NAVER_MAP_SEARCH_URL = "https://map.naver.com/p/api/search/allSearch"
_NAVER_MAP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_geo_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="geo")


def geocode_nominatim(query: str) -> dict | None:
    qs = urllib.parse.urlencode(
        {"format": "json", "countrycodes": "kr", "limit": "1", "q": query}
    )
    request = urllib.request.Request(
        f"{_NOMINATIM_URL}?{qs}",
        headers={"Accept-Language": "ko", "User-Agent": _GEOCODE_UA},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            data = json.loads(response.read())
        if data:
            return {
                "lat": data[0]["lat"],
                "lon": data[0]["lon"],
                "display_name": data[0].get("display_name", ""),
            }
    except Exception:
        _log.debug("Nominatim failed for q=%r", query, exc_info=True)
    return None


def geocode_photon(query: str) -> dict | None:
    qs = urllib.parse.urlencode({"q": query, "limit": "1"})
    request = urllib.request.Request(
        f"{_PHOTON_URL}?{qs}",
        headers={"User-Agent": _GEOCODE_UA},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            data = json.loads(response.read())
        features = data.get("features", [])
        if features:
            coords = features[0]["geometry"]["coordinates"]
            props = features[0]["properties"]
            parts = [
                props.get("name", ""),
                props.get("city", ""),
                props.get("state", ""),
            ]
            display_name = ", ".join(part for part in parts if part)
            return {
                "lat": str(coords[1]),
                "lon": str(coords[0]),
                "display_name": display_name,
            }
    except Exception:
        _log.debug("Photon failed for q=%r", query, exc_info=True)
    return None


def _fallback_address_geocode(address: str) -> dict | None:
    result = geocode_nominatim(address)
    if not result:
        result = geocode_photon(address)
    return result


def geocode_one(place: str) -> dict | None:
    result = geocode_nominatim(place)
    if not result:
        result = geocode_photon(place)
    if not result:
        result = geocode_naver(place, fallback_geocode=_fallback_address_geocode)
    return result


def autocomplete_naver_map_raw(query: str, limit: int = 5) -> tuple[dict, ...]:
    qs = urllib.parse.urlencode(
        {
            "query": query,
            "type": "all",
            "searchCoord": "126.978;37.5665",
            "boundary": "",
        }
    )
    request = urllib.request.Request(
        f"{_NAVER_MAP_SEARCH_URL}?{qs}",
        headers={
            "User-Agent": _NAVER_MAP_UA,
            "Referer": "https://map.naver.com/",
            "Accept": "application/json, text/plain, */*",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            raw = response.read()
            _log.info("Naver map resp status=%d len=%d", response.status, len(raw))
            data = json.loads(raw)
    except urllib.error.HTTPError as exc:
        body = exc.read()[:300] if hasattr(exc, "read") else b""
        _log.warning(
            "Naver map search HTTP %d q=%r body=%s",
            exc.code,
            query,
            body.decode("utf-8", errors="replace"),
        )
        return ()
    except Exception:
        _log.warning("Naver map search failed q=%r", query, exc_info=True)
        return ()

    result_root = data.get("result") or {}
    _log.info(
        "Naver map AC q=%r keys=%s",
        query,
        (
            list(result_root.keys())
            if isinstance(result_root, dict)
            else type(result_root).__name__
        ),
    )
    results: list[dict] = []

    place_section = result_root.get("place") or {}
    for item in place_section.get("list") or []:
        if len(results) >= limit:
            break
        name = item.get("name", "")
        road = item.get("roadAddress", "") or item.get("address", "")
        x_coord = item.get("x", "")
        y_coord = item.get("y", "")
        if not x_coord or not y_coord:
            continue
        categories = item.get("category") or []
        if isinstance(categories, list):
            category = categories[-1] if categories else ""
        else:
            category = str(categories).split(">")[-1].strip()
        results.append(
            {
                "lat": y_coord,
                "lon": x_coord,
                "display_name": name,
                "address": road,
                "type": category,
            }
        )

    addr_section = result_root.get("address") or {}
    for item in addr_section.get("list") or []:
        if len(results) >= limit:
            break
        name = item.get("name", "") or item.get("roadAddress", "")
        road = item.get("roadAddress", "") or item.get("address", "")
        x_coord = item.get("x", "")
        y_coord = item.get("y", "")
        if not x_coord or not y_coord:
            continue
        results.append(
            {
                "lat": y_coord,
                "lon": x_coord,
                "display_name": name,
                "address": road,
                "type": "주소",
            }
        )

    return tuple(results)


@lru_cache(maxsize=512)
def autocomplete_naver_map(query: str, limit: int = 5) -> tuple[dict, ...]:
    return autocomplete_naver_map_raw(query, limit)


def fetch_osrm_route(
    *,
    olat: float,
    olon: float,
    dlat: float,
    dlon: float,
) -> dict:
    url = f"{_OSRM_URL}/{olon},{olat};{dlon},{dlat}?overview=full&geometries=geojson"
    request = urllib.request.Request(url, headers={"User-Agent": _GEOCODE_UA})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())
    except Exception:
        _log.exception("OSRM route failed")
        return {"code": "Error", "routes": []}


def pre_geocode_for_provider(
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
            _log.info("Pre-geocoded (frontend coords) %r → (%s, %s)", place, lat, lon)
            continue
        need_geocode.append(place)

    if not need_geocode:
        return

    def _do_geocode(place_name: str) -> None:
        result = geocode_one(place_name)
        if result:
            provider.set_coords(place_name, float(result["lat"]), float(result["lon"]))
            _log.info(
                "Pre-geocoded %r → (%s, %s)",
                place_name,
                result["lat"],
                result["lon"],
            )

    if len(need_geocode) == 1:
        _do_geocode(need_geocode[0])
        return

    futures = [_geo_pool.submit(_do_geocode, place) for place in need_geocode]
    for future in as_completed(futures, timeout=30):
        future.result()
