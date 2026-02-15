from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def ensure_tzaware(value: datetime, timezone: ZoneInfo) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.replace(tzinfo=timezone)
    return value.astimezone(timezone)


def floor_time_to_minutes(value: datetime, minutes: int) -> datetime:
    if minutes <= 0:
        raise ValueError("minutes must be positive")

    value = value.replace(second=0, microsecond=0)
    if minutes == 1:
        return value

    floored_minute = (value.minute // minutes) * minutes
    return value.replace(minute=floored_minute)


def ceil_time_to_minutes(value: datetime, minutes: int) -> datetime:
    if minutes <= 0:
        raise ValueError("minutes must be positive")

    has_sub = value.second > 0 or value.microsecond > 0
    base = value.replace(second=0, microsecond=0)

    remainder = base.minute % minutes
    if remainder == 0 and not has_sub:
        return base
    add = minutes - remainder if remainder else minutes
    return base + timedelta(minutes=add)


def subtract_hours(value: datetime, hours: int) -> datetime:
    return value - timedelta(hours=hours)
