from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import Request

from trip_time_service.core.time_utils import ceil_time_to_minutes, ensure_tzaware
from trip_time_service.services.trip_time_service import TripTimeService


def service_from_request(request: Request) -> TripTimeService:
    return request.app.state.trip_time_service


def ensure_future_time(
    dt: datetime,
    tz: ZoneInfo,
    *,
    step_minutes: int,
) -> datetime:
    normalized = ensure_tzaware(dt, tz)
    now = datetime.now(tz=tz)
    if normalized <= now:
        normalized = ceil_time_to_minutes(now, step_minutes)
    else:
        normalized = ceil_time_to_minutes(normalized, step_minutes)
    return normalized
