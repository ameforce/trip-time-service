from __future__ import annotations

import zlib
from datetime import datetime, timedelta

from trip_time_service.core.models import DriveDuration, Route


def _triangular_peak(
    *,
    minute_of_day: int,
    center: int,
    half_width: int,
    peak_seconds: int,
) -> int:
    distance = abs(minute_of_day - center)
    if distance >= half_width:
        return 0
    return int(peak_seconds * (1.0 - (distance / half_width)))


class MockTravelTimeProvider:
    name = "mock"

    def get_drive_duration(
        self, route: Route, departure_time: datetime
    ) -> DriveDuration:
        seed_bytes = f"{route.origin}|{route.destination}".encode()
        route_hash = zlib.adler32(seed_bytes)

        base_seconds = 900 + (route_hash % 1801)  # 15~45분 범위

        minute_of_day = departure_time.hour * 60 + departure_time.minute
        extra = 0
        extra += _triangular_peak(
            minute_of_day=minute_of_day,
            center=8 * 60,
            half_width=120,
            peak_seconds=20 * 60,
        )
        extra += _triangular_peak(
            minute_of_day=minute_of_day,
            center=18 * 60,
            half_width=150,
            peak_seconds=25 * 60,
        )

        jitter = ((route_hash ^ minute_of_day) % 61) - 30  # -30~+30초
        duration_seconds = max(60, base_seconds + extra + jitter)

        fetched_at = datetime.now(tz=departure_time.tzinfo)

        return DriveDuration(
            duration_seconds=duration_seconds,
            fetched_at=fetched_at,
            raw_text=f"{timedelta(seconds=duration_seconds)}",
        )

    def close(self) -> None:
        return
