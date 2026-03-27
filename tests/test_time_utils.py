from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from trip_time_service.core.time_utils import (
    ensure_tzaware,
    floor_time_to_minutes,
    subtract_hours,
)

KST = ZoneInfo("Asia/Seoul")


class TestEnsureTzaware:
    def test_naive_datetime_gets_timezone(self) -> None:
        naive = datetime(2026, 1, 24, 10, 30)
        result = ensure_tzaware(naive, KST)
        assert result.tzinfo == KST
        assert result.hour == 10

    def test_aware_datetime_converts_to_target(self) -> None:
        utc_dt = datetime(2026, 1, 24, 1, 30, tzinfo=UTC)
        result = ensure_tzaware(utc_dt, KST)
        assert result.tzinfo == KST
        assert result.hour == 10  # UTC+9

    def test_same_timezone_passthrough(self) -> None:
        kst_dt = datetime(2026, 1, 24, 10, 30, tzinfo=KST)
        result = ensure_tzaware(kst_dt, KST)
        assert result == kst_dt


class TestFloorTimeToMinutes:
    def test_floor_to_5_minutes(self) -> None:
        dt = datetime(2026, 1, 24, 10, 37, 45, tzinfo=KST)
        result = floor_time_to_minutes(dt, 5)
        assert result == datetime(2026, 1, 24, 10, 35, 0, tzinfo=KST)

    def test_floor_to_10_minutes(self) -> None:
        dt = datetime(2026, 1, 24, 10, 48, 20, tzinfo=KST)
        result = floor_time_to_minutes(dt, 10)
        assert result == datetime(2026, 1, 24, 10, 40, 0, tzinfo=KST)

    def test_floor_to_1_minute(self) -> None:
        dt = datetime(2026, 1, 24, 10, 37, 45, tzinfo=KST)
        result = floor_time_to_minutes(dt, 1)
        assert result.second == 0
        assert result.microsecond == 0
        assert result.minute == 37

    def test_already_floored(self) -> None:
        dt = datetime(2026, 1, 24, 10, 30, 0, tzinfo=KST)
        result = floor_time_to_minutes(dt, 5)
        assert result == dt

    def test_zero_minutes_raises(self) -> None:
        dt = datetime(2026, 1, 24, 10, 30, tzinfo=KST)
        with pytest.raises(ValueError, match="positive"):
            floor_time_to_minutes(dt, 0)

    def test_negative_minutes_raises(self) -> None:
        dt = datetime(2026, 1, 24, 10, 30, tzinfo=KST)
        with pytest.raises(ValueError, match="positive"):
            floor_time_to_minutes(dt, -5)

    def test_floor_to_15_minutes(self) -> None:
        dt = datetime(2026, 1, 24, 10, 44, 59, tzinfo=KST)
        result = floor_time_to_minutes(dt, 15)
        assert result.minute == 30

    def test_floor_to_30_minutes(self) -> None:
        dt = datetime(2026, 1, 24, 10, 59, 59, tzinfo=KST)
        result = floor_time_to_minutes(dt, 30)
        assert result.minute == 30


class TestSubtractHours:
    def test_subtract_6_hours(self) -> None:
        dt = datetime(2026, 1, 24, 10, 0, tzinfo=KST)
        result = subtract_hours(dt, 6)
        assert result == datetime(2026, 1, 24, 4, 0, tzinfo=KST)

    def test_subtract_crosses_midnight(self) -> None:
        dt = datetime(2026, 1, 24, 2, 0, tzinfo=KST)
        result = subtract_hours(dt, 6)
        assert result.day == 23
        assert result.hour == 20
