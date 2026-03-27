from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import Request

from trip_time_service.core.time_utils import ceil_time_to_minutes, ensure_tzaware
from trip_time_service.services.trip_time_service import TripTimeService

_STEP_MINUTES = 10


def service_from_request(request: Request) -> TripTimeService:
    return request.app.state.trip_time_service


def ensure_future_time(dt: datetime, tz: ZoneInfo) -> datetime:
    normalized = ensure_tzaware(dt, tz)
    now = datetime.now(tz=tz)
    if normalized <= now:
        normalized = ceil_time_to_minutes(now, _STEP_MINUTES)
    else:
        normalized = ceil_time_to_minutes(normalized, _STEP_MINUTES)
    return normalized
