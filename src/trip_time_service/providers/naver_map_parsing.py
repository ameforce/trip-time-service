"""Naver 지도 소요시간 provider의 순수 파싱/좌표 헬퍼.

Playwright provider가 공유하는 DOM-비의존 로직만 모은다.
좌표 변환, 소요시간 텍스트 파싱, directions URL 조립, 패널 텍스트 매칭 등
브라우저 런타임과 무관한 함수만 둔다.
"""

from __future__ import annotations

import math
import re
import urllib.parse

from trip_time_service.providers.base import ProviderError

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


def latlon_to_epsg3857(lat: float, lon: float) -> tuple[float, float]:
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


def parse_naver_duration(text: str) -> int:
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


def build_directions_url(
    origin: str,
    destination: str,
    o_coords: tuple[float, float] | None,
    d_coords: tuple[float, float] | None,
) -> str | None:
    if not o_coords or not d_coords:
        return None

    ox, oy = latlon_to_epsg3857(o_coords[0], o_coords[1])
    dx, dy = latlon_to_epsg3857(d_coords[0], d_coords[1])

    o_name = urllib.parse.quote(origin)
    d_name = urllib.parse.quote(destination)

    return (
        f"https://map.naver.com/p/directions/"
        f"{ox:.7f},{oy:.7f},{o_name},,/"
        f"{dx:.7f},{dy:.7f},{d_name},,/"
        f"-/car"
    )


def matches_requested_time(
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
    text: str,
    ampm: str,
    hour: str,
    minute: str,
) -> int | None:
    if "소요" not in text:
        return None
    if not matches_requested_time(text, ampm, hour, minute):
        return None
    try:
        return parse_naver_duration(text)
    except ProviderError:
        return None


def duration_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for match in _DUR_WITH_SOYO_RE.finditer(text):
        token = match.group(0).strip()
        if token and token not in tokens:
            tokens.append(token)
        if len(tokens) >= _PANEL_DIAGNOSTIC_TOKEN_LIMIT:
            return tokens
    return tokens
