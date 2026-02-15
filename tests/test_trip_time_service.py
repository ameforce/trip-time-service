from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trip_time_service.config import Settings
from trip_time_service.core.models import (
    DriveDuration,
    RecommendationCandidate,
    Route,
)
from trip_time_service.providers.base import ProviderError
from trip_time_service.services.trip_time_service import (
    NoFeasibleDepartureError,
    TripTimeService,
)

KST = ZoneInfo("Asia/Seoul")


@dataclass(frozen=True, slots=True)
class _FixedProvider:
    name: str
    seconds_by_minute: dict[int, int]

    def get_drive_duration(
        self, route: Route, departure_time: datetime
    ) -> DriveDuration:
        _ = route
        duration_seconds = self.seconds_by_minute.get(departure_time.minute, 2400)
        return DriveDuration(
            duration_seconds=duration_seconds,
            fetched_at=departure_time,
        )

    def close(self) -> None:
        return


class _ConstantProvider:
    name = "constant"

    def __init__(self, seconds: int) -> None:
        self._seconds = seconds

    def get_drive_duration(
        self, route: Route, departure_time: datetime
    ) -> DriveDuration:
        _ = route
        return DriveDuration(
            duration_seconds=self._seconds,
            fetched_at=departure_time,
        )

    def close(self) -> None:
        return


class _CountingProvider:
    """Provider that counts how many times get_drive_duration is called."""

    name = "counting"

    def __init__(self, seconds: int) -> None:
        self._seconds = seconds
        self.call_count = 0

    def get_drive_duration(
        self, route: Route, departure_time: datetime
    ) -> DriveDuration:
        _ = route
        self.call_count += 1
        return DriveDuration(
            duration_seconds=self._seconds,
            fetched_at=departure_time,
        )

    def close(self) -> None:
        return


class _PatternProvider:
    name = "pattern"
    max_parallel_sessions = 16

    def __init__(
        self,
        *,
        seconds_by_slot: dict[tuple[int, int], int],
        default_seconds: int,
    ) -> None:
        self._seconds_by_slot = seconds_by_slot
        self._default_seconds = default_seconds
        self.call_count = 0

    def get_drive_duration(
        self, route: Route, departure_time: datetime
    ) -> DriveDuration:
        _ = route
        self.call_count += 1
        duration_seconds = self._seconds_by_slot.get(
            (departure_time.hour, departure_time.minute),
            self._default_seconds,
        )
        return DriveDuration(
            duration_seconds=duration_seconds,
            fetched_at=departure_time,
        )

    def close(self) -> None:
        return


class _SmoothProvider:
    name = "smooth"

    def __init__(self) -> None:
        self.call_count = 0

    def get_drive_duration(
        self, route: Route, departure_time: datetime
    ) -> DriveDuration:
        _ = route
        self.call_count += 1
        minute_of_day = departure_time.hour * 60 + departure_time.minute
        evening_peak = abs(minute_of_day - (18 * 60))
        duration_seconds = 1200 + evening_peak * 2
        return DriveDuration(
            duration_seconds=duration_seconds,
            fetched_at=departure_time,
        )

    def close(self) -> None:
        return


class _RetryableFailOnceProvider:
    name = "retryable-once"
    max_parallel_sessions = 4

    def __init__(self, seconds: int) -> None:
        self._seconds = seconds
        self.call_count = 0
        self._failed_buckets: set[tuple[int, int]] = set()

    def get_drive_duration(
        self, route: Route, departure_time: datetime
    ) -> DriveDuration:
        _ = route
        self.call_count += 1
        bucket = (departure_time.hour, departure_time.minute)
        if departure_time.minute % 20 == 0 and bucket not in self._failed_buckets:
            self._failed_buckets.add(bucket)
            raise ProviderError("transient selenium read error", is_retryable=True)
        return DriveDuration(
            duration_seconds=self._seconds,
            fetched_at=departure_time,
        )

    def close(self) -> None:
        return


class _FailOnceProvider:
    name = "fail-once"

    def __init__(self, seconds: int) -> None:
        self._seconds = seconds
        self.call_count = 0

    def get_drive_duration(
        self, route: Route, departure_time: datetime
    ) -> DriveDuration:
        _ = route
        self.call_count += 1
        if self.call_count == 1:
            raise ProviderError("transient first-call error", is_retryable=True)
        return DriveDuration(
            duration_seconds=self._seconds,
            fetched_at=departure_time,
        )

    def close(self) -> None:
        return


class _DelayedProvider:
    name = "delayed"
    max_parallel_sessions = 16

    def __init__(self, *, seconds: int, delay_seconds: float) -> None:
        self._seconds = seconds
        self._delay_seconds = delay_seconds
        self.call_count = 0
        self._lock = threading.Lock()

    def get_drive_duration(
        self, route: Route, departure_time: datetime
    ) -> DriveDuration:
        _ = route, departure_time
        with self._lock:
            self.call_count += 1
        time.sleep(self._delay_seconds)
        return DriveDuration(
            duration_seconds=self._seconds,
            fetched_at=datetime.now(tz=KST),
        )

    def close(self) -> None:
        return


def _settings(*, step_minutes: int = 5, lookback_hours: int = 1) -> Settings:
    return Settings(
        timezone=KST,
        headless=True,
        cache_ttl=timedelta(seconds=3600),
        step_minutes=step_minutes,
        lookback_hours=lookback_hours,
        max_queries=1000,
        provider="test",
        chrome_binary_path=None,
        chrome_user_data_dir=None,
        naver_map_client_id=None,
        recommend_workers=1,
        naver_session_pool_size=1,
    )


def _future_time(*, hours_ahead: int, minute: int) -> datetime:
    base = datetime.now(tz=KST).replace(second=0, microsecond=0)
    future = base + timedelta(hours=hours_ahead)
    return future.replace(minute=minute)


# ── estimate_arrival ────────────────────────────────────────────


class TestEstimateArrival:
    def test_basic_arrival_calculation(self) -> None:
        provider = _ConstantProvider(seconds=1800)
        service = TripTimeService(settings=_settings(), provider=provider)

        departure = datetime(2026, 1, 24, 9, 0, tzinfo=KST)
        result = service.estimate_arrival(
            origin="강남역",
            destination="판교역",
            departure_time=departure,
        )

        assert result.duration.duration_seconds == 1800
        assert result.arrival_time == departure + timedelta(seconds=1800)
        assert result.provider == "constant"

    def test_cache_hit_on_second_call(self) -> None:
        provider = _CountingProvider(seconds=1200)
        service = TripTimeService(settings=_settings(), provider=provider)

        departure = datetime(2026, 1, 24, 9, 0, tzinfo=KST)
        r1 = service.estimate_arrival(
            origin="A", destination="B", departure_time=departure
        )
        r2 = service.estimate_arrival(
            origin="A", destination="B", departure_time=departure
        )

        assert r1.cache_hit is False
        assert r2.cache_hit is True
        assert provider.call_count == 1

    def test_naive_datetime_treated_as_kst(self) -> None:
        provider = _ConstantProvider(seconds=600)
        service = TripTimeService(settings=_settings(), provider=provider)

        naive_dt = datetime(2026, 1, 24, 9, 0)
        result = service.estimate_arrival(
            origin="A", destination="B", departure_time=naive_dt
        )

        assert result.departure_time.tzinfo == KST

    def test_route_normalization(self) -> None:
        provider = _CountingProvider(seconds=600)
        service = TripTimeService(settings=_settings(), provider=provider)

        departure = datetime(2026, 1, 24, 9, 0, tzinfo=KST)
        service.estimate_arrival(
            origin="  강남역  ", destination="  판교역 ", departure_time=departure
        )
        r2 = service.estimate_arrival(
            origin="강남역", destination="판교역", departure_time=departure
        )

        # same route → cache hit
        assert r2.cache_hit is True
        assert provider.call_count == 1

    def test_retryable_provider_error_is_retried(self) -> None:
        provider = _FailOnceProvider(seconds=900)
        service = TripTimeService(settings=_settings(), provider=provider)

        departure = datetime(2026, 1, 24, 9, 0, tzinfo=KST)
        result = service.estimate_arrival(
            origin="A",
            destination="B",
            departure_time=departure,
        )

        assert result.duration.duration_seconds == 900
        assert provider.call_count == 2


# ── recommend_departure ─────────────────────────────────────────


class TestRecommendDeparture:
    def test_latest_feasible_coarse_short_circuits_queries(self) -> None:
        provider = _CountingProvider(seconds=120)
        service = TripTimeService(
            settings=_settings(step_minutes=10, lookback_hours=3),
            provider=provider,
        )

        desired = _future_time(hours_ahead=3, minute=3)
        result = service.recommend_departure(
            origin="A",
            destination="B",
            desired_arrival_time=desired,
        )

        assert result.meets_deadline is True
        assert result.candidates_checked == 1
        assert provider.call_count == 1
        assert {item.phase for item in result.candidate_evaluations} == {"coarse"}

    def test_picks_latest_feasible_departure(self) -> None:
        provider = _FixedProvider(
            name="fixed",
            seconds_by_minute={
                20: 1800,  # 09:20 → 30min
                30: 1800,  # 09:30 → 30min (tie → later preferred)
            },
        )
        service = TripTimeService(
            settings=_settings(step_minutes=10), provider=provider
        )

        desired = _future_time(hours_ahead=2, minute=0)
        result = service.recommend_departure(
            origin="A", destination="B", desired_arrival_time=desired
        )

        assert result.meets_deadline is True
        assert result.recommended_departure_time == desired - timedelta(minutes=30)
        assert result.expected_arrival_time == desired
        assert result.duration.duration_seconds == 1800

    def test_fallback_when_deadline_impossible(self) -> None:
        provider = _ConstantProvider(seconds=2 * 3600)
        service = TripTimeService(
            settings=_settings(step_minutes=30, lookback_hours=1),
            provider=provider,
        )

        desired = _future_time(hours_ahead=2, minute=0)
        with pytest.raises(NoFeasibleDepartureError):
            service.recommend_departure(
                origin="A",
                destination="B",
                desired_arrival_time=desired,
            )

    def test_provider_calls_are_counted(self) -> None:
        provider = _CountingProvider(seconds=1800)
        service = TripTimeService(
            settings=_settings(step_minutes=30, lookback_hours=1),
            provider=provider,
        )

        desired = _future_time(hours_ahead=2, minute=0)
        result = service.recommend_departure(
            origin="A", destination="B", desired_arrival_time=desired
        )

        assert result.provider_calls > 0
        assert result.candidates_checked > 0

    def test_analysis_start_time_expands_search_window(self) -> None:
        provider = _CountingProvider(seconds=2 * 3600)
        service = TripTimeService(
            settings=_settings(step_minutes=30, lookback_hours=1),
            provider=provider,
        )

        desired = _future_time(hours_ahead=4, minute=0)
        baseline_departure = desired - timedelta(hours=2)
        result = service.recommend_departure(
            origin="A",
            destination="B",
            desired_arrival_time=desired,
            analysis_start_time=baseline_departure,
        )

        assert result.meets_deadline is True
        assert result.recommended_departure_time == baseline_departure
        assert result.expected_arrival_time == desired
        assert result.safe_departure_time == baseline_departure
        assert provider.call_count == 11
        assert result.candidates_checked == 11

    def test_analysis_mode_hourly_coarse_then_refine(self) -> None:
        provider = _PatternProvider(
            seconds_by_slot={
                (10, 0): 3600,
                (11, 0): 3900,
                (12, 0): 4200,
                (13, 0): 4500,
                (14, 0): 4800,
                (15, 0): 3300,
                (15, 10): 3200,
                (15, 20): 3100,
                (15, 30): 3000,
                (15, 40): 2900,
                (15, 50): 2800,
            },
            default_seconds=5400,
        )
        service = TripTimeService(
            settings=_settings(step_minutes=10, lookback_hours=3),
            provider=provider,
        )

        analysis_start = datetime(2099, 1, 24, 10, 0, tzinfo=KST)
        desired = datetime(2099, 1, 24, 23, 0, tzinfo=KST)
        result = service.recommend_departure(
            origin="A",
            destination="B",
            desired_arrival_time=desired,
            analysis_start_time=analysis_start,
        )

        assert result.meets_deadline is True
        assert result.recommended_departure_time == datetime(
            2099, 1, 24, 15, 40, tzinfo=KST
        )
        assert result.safe_departure_time == analysis_start
        assert provider.call_count == 16
        assert result.candidates_checked == 16
        phases = {item.phase for item in result.candidate_evaluations}
        assert phases == {"coarse", "refine"}

    def test_analysis_mode_extends_coarse_to_plus_12_hours_when_all_worse(self) -> None:
        provider = _PatternProvider(
            seconds_by_slot={
                (10, 0): 3600,
                (11, 0): 4200,
                (12, 0): 4300,
                (13, 0): 4400,
                (14, 0): 4500,
                (15, 0): 4600,
                (16, 0): 4700,
                (17, 0): 4800,
                (18, 0): 4900,
            },
            default_seconds=5400,
        )
        service = TripTimeService(
            settings=_settings(step_minutes=10, lookback_hours=3),
            provider=provider,
        )

        analysis_start = datetime(2099, 1, 24, 10, 0, tzinfo=KST)
        desired = datetime(2099, 1, 24, 23, 0, tzinfo=KST)
        result = service.recommend_departure(
            origin="A",
            destination="B",
            desired_arrival_time=desired,
            analysis_start_time=analysis_start,
        )

        assert result.recommended_departure_time == analysis_start
        assert provider.call_count == 13
        coarse_times = {
            candidate.departure_time
            for candidate in result.candidate_evaluations
            if candidate.phase == "coarse"
        }
        assert datetime(2099, 1, 24, 21, 0, tzinfo=KST) in coarse_times
        assert datetime(2099, 1, 24, 22, 0, tzinfo=KST) in coarse_times

    def test_analysis_mode_ignores_deadline_and_picks_min_duration(self) -> None:
        provider = _PatternProvider(
            seconds_by_slot={
                (10, 0): 4 * 3600,
                (11, 0): 4 * 3600 + 600,
                (12, 0): 4 * 3600 + 900,
                (13, 0): 4 * 3600 + 1200,
                (14, 0): 4 * 3600 + 1500,
                (15, 0): 4 * 3600 + 1800,
                (16, 0): 4 * 3600 - 300,
                (17, 0): 3 * 3600,
                (18, 0): 4 * 3600 + 2400,
            },
            default_seconds=5 * 3600,
        )
        service = TripTimeService(
            settings=_settings(step_minutes=10, lookback_hours=3),
            provider=provider,
        )

        analysis_start = datetime(2099, 1, 24, 10, 0, tzinfo=KST)
        desired = datetime(2099, 1, 24, 14, 0, tzinfo=KST)
        result = service.recommend_departure(
            origin="A",
            destination="B",
            desired_arrival_time=desired,
            analysis_start_time=analysis_start,
        )

        assert result.recommended_departure_time == datetime(
            2099, 1, 24, 17, 0, tzinfo=KST
        )
        assert result.duration.duration_seconds == 3 * 3600

    def test_analysis_mode_scores_balance_proximity_and_night_penalty(self) -> None:
        provider = _PatternProvider(
            seconds_by_slot={
                (16, 30): 3 * 3600 + 30 * 60,
                (17, 30): 3 * 3600 + 7 * 60,
                (18, 30): 2 * 3600 + 49 * 60,
                (19, 30): 2 * 3600 + 40 * 60,
                (20, 30): 2 * 3600 + 34 * 60,
                (21, 30): 2 * 3600 + 28 * 60,
                (22, 30): 2 * 3600 + 19 * 60,
                (23, 30): 2 * 3600 + 15 * 60,
                (0, 30): 2 * 3600 + 15 * 60,
                (1, 30): 2 * 3600 + 14 * 60,
                (2, 30): 2 * 3600 + 16 * 60,
            },
            default_seconds=4 * 3600,
        )
        service = TripTimeService(
            settings=_settings(step_minutes=10, lookback_hours=3),
            provider=provider,
        )

        analysis_start = datetime(2099, 1, 24, 16, 30, tzinfo=KST)
        desired = datetime(2099, 1, 24, 20, 0, tzinfo=KST)
        result = service.recommend_departure(
            origin="A",
            destination="B",
            desired_arrival_time=desired,
            analysis_start_time=analysis_start,
        )

        min_duration_candidate = min(
            result.candidate_evaluations,
            key=lambda item: item.duration_seconds,
        )
        assert min_duration_candidate.departure_time == datetime(
            2099, 1, 25, 1, 30, tzinfo=KST
        )
        assert result.recommended_departure_time <= datetime(
            2099, 1, 24, 23, 30, tzinfo=KST
        )
        assert (
            result.duration.duration_seconds
            > min_duration_candidate.duration_seconds
        )

        recommended_candidates = [
            item
            for item in result.candidate_evaluations
            if item.departure_time == result.recommended_departure_time
        ]
        assert len(recommended_candidates) == 1
        recommended_candidate = recommended_candidates[0]
        baseline_candidates = [
            item
            for item in result.candidate_evaluations
            if item.departure_time == result.latest_departure_time
        ]
        assert len(baseline_candidates) == 1
        baseline_candidate = baseline_candidates[0]
        assert result.recommended_score_total == recommended_candidate.score_total
        assert result.baseline_score_total == baseline_candidate.score_total

        assert all(
            candidate.score_total is not None
            and candidate.score_duration is not None
            and candidate.score_time_proximity is not None
            and candidate.score_night_drive is not None
            and candidate.score_stability is not None
            and candidate.score_improvement_efficiency is not None
            for candidate in result.candidate_evaluations
        )
        assert (
            recommended_candidate.score_total
            > min_duration_candidate.score_total
        )
        assert (
            recommended_candidate.score_night_drive
            > min_duration_candidate.score_night_drive
        )

    def test_analysis_mode_refine_anchors_prioritize_high_score_windows(self) -> None:
        provider = _PatternProvider(
            seconds_by_slot={
                (10, 0): 3600,
                (11, 0): 3420,
                (11, 10): 3410,
                (11, 20): 3405,
                (11, 30): 3400,
                (11, 40): 3395,
                (11, 50): 3390,
                (12, 0): 3410,
                (13, 0): 3405,
                (14, 0): 3400,
                (15, 0): 3395,
                (16, 0): 3390,
                (17, 0): 3385,
                (18, 0): 3380,
                (19, 0): 3375,
                (20, 0): 3370,
            },
            default_seconds=3410,
        )
        service = TripTimeService(
            settings=_settings(step_minutes=10, lookback_hours=3),
            provider=provider,
        )

        analysis_start = datetime(2099, 1, 24, 10, 0, tzinfo=KST)
        desired = datetime(2099, 1, 24, 23, 0, tzinfo=KST)
        result = service.recommend_departure(
            origin="A",
            destination="B",
            desired_arrival_time=desired,
            analysis_start_time=analysis_start,
        )

        refine_times = {
            candidate.departure_time
            for candidate in result.candidate_evaluations
            if candidate.phase == "refine"
        }
        assert datetime(2099, 1, 24, 11, 50, tzinfo=KST) in refine_times
        assert datetime(2099, 1, 24, 20, 10, tzinfo=KST) not in refine_times
        min_duration_candidate = min(
            result.candidate_evaluations,
            key=lambda item: item.duration_seconds,
        )
        assert min_duration_candidate.departure_time == datetime(
            2099, 1, 24, 20, 0, tzinfo=KST
        )
        assert result.recommended_departure_time == datetime(
            2099, 1, 24, 11, 0, tzinfo=KST
        )
        assert (
            result.duration.duration_seconds
            > min_duration_candidate.duration_seconds
        )

    def test_analysis_mode_refine_expands_multiple_score_bands_on_tight_top_gap(
        self,
    ) -> None:
        provider = _PatternProvider(
            seconds_by_slot={
                (10, 0): 3600,
                (11, 0): 3360,
                (11, 10): 3340,
                (11, 20): 3330,
                (11, 30): 3325,
                (11, 40): 3320,
                (11, 50): 3315,
                (12, 0): 3500,
                (13, 0): 3420,
                (14, 0): 3000,
                (14, 10): 2985,
                (14, 20): 2975,
                (14, 30): 2970,
                (14, 40): 2965,
                (14, 50): 2960,
                (15, 0): 3450,
                (16, 0): 3300,
                (17, 0): 3220,
                (18, 0): 2860,
                (18, 10): 2850,
                (18, 20): 2845,
                (18, 30): 2840,
                (18, 40): 2835,
                (18, 50): 2830,
                (19, 0): 3360,
                (20, 0): 3400,
            },
            default_seconds=3460,
        )
        service = TripTimeService(
            settings=_settings(step_minutes=10, lookback_hours=3),
            provider=provider,
        )

        analysis_start = datetime(2099, 1, 24, 10, 0, tzinfo=KST)
        desired = datetime(2099, 1, 24, 23, 0, tzinfo=KST)
        result = service.recommend_departure(
            origin="A",
            destination="B",
            desired_arrival_time=desired,
            analysis_start_time=analysis_start,
        )

        refine_times = {
            candidate.departure_time
            for candidate in result.candidate_evaluations
            if candidate.phase == "refine"
        }
        assert datetime(2099, 1, 24, 11, 50, tzinfo=KST) in refine_times
        assert datetime(2099, 1, 24, 14, 50, tzinfo=KST) in refine_times
        assert datetime(2099, 1, 24, 18, 50, tzinfo=KST) in refine_times
        assert datetime(2099, 1, 24, 20, 10, tzinfo=KST) not in refine_times

    def test_stability_score_normalizes_sparse_gap_bias(self) -> None:
        service = TripTimeService(
            settings=_settings(step_minutes=10, lookback_hours=3),
            provider=_ConstantProvider(seconds=1800),
        )
        analysis_start = datetime(2099, 1, 24, 17, 20, tzinfo=KST)
        baseline_duration_seconds = 3 * 3600 + 21 * 60

        def _candidate(
            departure_time: datetime,
            duration_seconds: int,
            phase: str,
        ) -> RecommendationCandidate:
            return RecommendationCandidate(
                departure_time=departure_time,
                arrival_time=departure_time + timedelta(seconds=duration_seconds),
                duration_seconds=duration_seconds,
                meets_deadline=True,
                phase=phase,
            )

        evaluated = {
            0: _candidate(
                datetime(2099, 1, 24, 22, 20, tzinfo=KST),
                2 * 3600 + 20 * 60,
                "coarse",
            ),
            1: _candidate(
                datetime(2099, 1, 24, 23, 20, tzinfo=KST),
                2 * 3600 + 15 * 60,
                "coarse",
            ),
            2: _candidate(
                datetime(2099, 1, 24, 23, 30, tzinfo=KST),
                2 * 3600 + 15 * 60,
                "refine",
            ),
            3: _candidate(
                datetime(2099, 1, 24, 23, 40, tzinfo=KST),
                2 * 3600 + 15 * 60,
                "refine",
            ),
        }
        scores = service._score_departure_analysis_candidates(
            evaluated=evaluated,
            analysis_start_time=analysis_start,
            baseline_duration_seconds=baseline_duration_seconds,
        )
        coarse_candidate_stability = scores[1][4]
        refine_candidate_stability = scores[2][4]

        # 1시간 coarse 간격 + 10분 refine 간격의 혼합에서도
        # 동일 소요시간 후보의 안정성 차이가 과도하게 벌어지면 안 된다.
        assert abs(coarse_candidate_stability - refine_candidate_stability) <= 0.03

    def test_boundary_search_uses_fewer_queries_than_full_scan(self) -> None:
        provider = _CountingProvider(seconds=1800)
        service = TripTimeService(
            settings=_settings(step_minutes=10, lookback_hours=12),
            provider=provider,
        )

        desired = _future_time(hours_ahead=13, minute=0)
        result = service.recommend_departure(
            origin="A",
            destination="B",
            desired_arrival_time=desired,
        )

        full_scan_candidates = int((12 * 60) / 10) + 1
        assert result.meets_deadline is True
        assert provider.call_count < full_scan_candidates
        phases = {item.phase for item in result.candidate_evaluations}
        assert "coarse" in phases
        assert "refine" in phases

    def test_parallel_probe_reduces_wall_time(self) -> None:
        provider = _DelayedProvider(seconds=1800, delay_seconds=0.05)
        base_settings = _settings(step_minutes=10, lookback_hours=12)
        tuned = Settings(
            timezone=base_settings.timezone,
            headless=base_settings.headless,
            cache_ttl=base_settings.cache_ttl,
            step_minutes=base_settings.step_minutes,
            lookback_hours=base_settings.lookback_hours,
            max_queries=base_settings.max_queries,
            provider=base_settings.provider,
            chrome_binary_path=base_settings.chrome_binary_path,
            chrome_user_data_dir=base_settings.chrome_user_data_dir,
            naver_map_client_id=base_settings.naver_map_client_id,
            recommend_workers=8,
            naver_session_pool_size=1,
        )
        service = TripTimeService(settings=tuned, provider=provider)

        desired = _future_time(hours_ahead=13, minute=0)
        started = time.monotonic()
        result = service.recommend_departure(
            origin="A",
            destination="B",
            desired_arrival_time=desired,
        )
        elapsed = time.monotonic() - started
        serial_cost = provider.call_count * 0.05

        assert result.meets_deadline is True
        assert provider.call_count > 0
        assert elapsed < serial_cost

    def test_retryable_candidate_errors_do_not_abort_recommendation(self) -> None:
        provider = _RetryableFailOnceProvider(seconds=1800)
        settings = _settings(step_minutes=10, lookback_hours=2)
        service = TripTimeService(settings=settings, provider=provider)

        desired = _future_time(hours_ahead=3, minute=0)
        result = service.recommend_departure(
            origin="A",
            destination="B",
            desired_arrival_time=desired,
        )

        assert result.duration.duration_seconds == 1800
        assert result.candidates_checked > 0
        assert result.provider_calls > 0
