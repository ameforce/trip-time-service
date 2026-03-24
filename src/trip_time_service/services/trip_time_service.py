from __future__ import annotations

import logging
import math
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from trip_time_service.config import Settings
from trip_time_service.core.cache import LruTtlCache
from trip_time_service.core.models import (
    ArrivalEstimate,
    DepartureRecommendation,
    DriveDuration,
    RecommendationCandidate,
    Route,
)
from trip_time_service.core.time_utils import (
    ceil_time_to_minutes,
    ensure_tzaware,
    floor_time_to_minutes,
    subtract_hours,
)
from trip_time_service.providers.base import ProviderError, TravelTimeProvider

_log = logging.getLogger(__name__)
_ARRIVAL_COARSE_PRIMARY_HOURS = 10
_ARRIVAL_COARSE_EXTENDED_HOURS = 12
_ANALYSIS_SCORE_WEIGHT_DURATION = 0.40
_ANALYSIS_SCORE_WEIGHT_TIME_PROXIMITY = 0.25
_ANALYSIS_SCORE_WEIGHT_NIGHT_DRIVE = 0.10
_ANALYSIS_SCORE_WEIGHT_STABILITY = 0.15
_ANALYSIS_SCORE_WEIGHT_IMPROVEMENT_EFFICIENCY = 0.10
_ANALYSIS_SCORE_DURATION_SPAN_FLOOR_SECONDS = 20 * 60
_ANALYSIS_DYNAMIC_ANCHOR_MAX = 4
_ANALYSIS_DYNAMIC_ANCHOR_TIGHT_GAP = 0.01
_ANALYSIS_DYNAMIC_ANCHOR_LOOSE_GAP = 0.03
_ANALYSIS_SCORE_BAND_MARGIN = 0.01
_ANALYSIS_SCORE_MIN_GAIN = 0.005


class NoFeasibleDepartureError(RuntimeError):
    """정시 도착 가능한 추천 출발 시각이 없는 경우."""


class TripTimeService:
    def __init__(
        self,
        *,
        settings: Settings,
        provider: TravelTimeProvider,
        cache: LruTtlCache[tuple[Route, datetime], DriveDuration] | None = None,
    ) -> None:
        self._settings = settings
        self._provider = provider
        self._cache = cache or LruTtlCache(
            maxsize=4096,
            ttl_seconds=settings.cache_ttl.total_seconds(),
        )

    @property
    def provider_name(self) -> str:
        return getattr(self._provider, "name", self._provider.__class__.__name__)

    def estimate_arrival(
        self,
        *,
        origin: str,
        destination: str,
        departure_time: datetime,
    ) -> ArrivalEstimate:
        route = Route.of(origin, destination)
        departure_time = ensure_tzaware(departure_time, self._settings.timezone)

        duration, cache_hit, _provider_calls = self._get_duration_cached(
            route=route,
            departure_time=departure_time,
        )
        arrival_time = departure_time + timedelta(seconds=duration.duration_seconds)

        return ArrivalEstimate(
            route=route,
            departure_time=departure_time,
            arrival_time=arrival_time,
            duration=duration,
            provider=self.provider_name,
            cache_hit=cache_hit,
        )

    def recommend_departure(
        self,
        *,
        origin: str,
        destination: str,
        desired_arrival_time: datetime,
        analysis_start_time: datetime | None = None,
        on_search_initialized: (
            Callable[[int, int], None] | None
        ) = None,
        on_candidate_evaluated: (
            Callable[[RecommendationCandidate], None] | None
        ) = None,
    ) -> DepartureRecommendation:
        route = Route.of(origin, destination)
        desired_arrival_time = ensure_tzaware(
            desired_arrival_time, self._settings.timezone
        )
        if analysis_start_time is not None:
            analysis_start_time = ensure_tzaware(
                analysis_start_time,
                desired_arrival_time.tzinfo,
            )

        step_minutes = self._settings.step_minutes
        lookback_hours = self._settings.lookback_hours

        if analysis_start_time is not None:
            return self._recommend_from_departure_analysis(
                route=route,
                desired_arrival_time=desired_arrival_time,
                analysis_start_time=analysis_start_time,
                step_minutes=step_minutes,
                on_search_initialized=on_search_initialized,
                on_candidate_evaluated=on_candidate_evaluated,
            )

        start_limit = subtract_hours(desired_arrival_time, lookback_hours)

        now = datetime.now(tz=desired_arrival_time.tzinfo)
        min_future_departure = ceil_time_to_minutes(now, step_minutes)
        departures = self._build_recommendation_departures(
            desired_arrival_time=desired_arrival_time,
            start_limit=start_limit,
            step_minutes=step_minutes,
            now=now,
        )
        if not departures:
            raise NoFeasibleDepartureError("조회 가능한 미래 출발 후보가 없습니다")

        total_candidates = len(departures)
        planned_queries = total_candidates
        minimum_sample_target = min(
            total_candidates,
            self._settings.max_queries,
            max(1, self._settings.recommend_min_samples),
        )
        if on_search_initialized is not None:
            on_search_initialized(total_candidates, planned_queries)

        provider_calls_total = 0
        candidates_checked = 0

        latest_departure: datetime | None = None
        latest_arrival: datetime | None = None
        latest_dur: DriveDuration | None = None
        candidate_evaluations: list[RecommendationCandidate] = []
        worker_count = self._worker_count()
        coarse_stride = max(1, math.ceil(60 / step_minutes))
        volatility_threshold_seconds = max(15 * 60, step_minutes * 3 * 60)

        evaluated: dict[int, RecommendationCandidate] = {}
        duration_by_index: dict[int, DriveDuration] = {}

        def _evaluate_index(
            index: int,
            *,
            phase: str,
        ) -> tuple[int, DriveDuration, int, RecommendationCandidate]:
            departure = departures[index]
            duration, _cache_hit, provider_calls = self._get_duration_cached(
                route=route,
                departure_time=departure,
            )
            arrival = departure + timedelta(seconds=duration.duration_seconds)
            candidate = RecommendationCandidate(
                departure_time=departure,
                arrival_time=arrival,
                duration_seconds=duration.duration_seconds,
                meets_deadline=arrival <= desired_arrival_time,
                phase=phase,
            )
            return index, duration, provider_calls, candidate

        def _record_candidate(
            *,
            index: int,
            duration: DriveDuration,
            provider_calls: int,
            candidate: RecommendationCandidate,
        ) -> None:
            nonlocal provider_calls_total
            nonlocal candidates_checked
            if index in evaluated:
                return
            evaluated[index] = candidate
            duration_by_index[index] = duration
            provider_calls_total += provider_calls
            candidates_checked += 1
            candidate_evaluations.append(candidate)
            if on_candidate_evaluated is not None:
                on_candidate_evaluated(candidate)

        def _evaluate_indices(indices: list[int], *, phase: str) -> None:
            pending = [idx for idx in indices if idx not in evaluated]
            if not pending:
                return

            if worker_count <= 1 or len(pending) == 1:
                for idx in pending:
                    result = _evaluate_index(idx, phase=phase)
                    _record_candidate(
                        index=result[0],
                        duration=result[1],
                        provider_calls=result[2],
                        candidate=result[3],
                    )
                return

            with ThreadPoolExecutor(
                max_workers=min(worker_count, len(pending)),
            ) as executor:
                futures = {
                    executor.submit(_evaluate_index, idx, phase=phase): idx
                    for idx in pending
                }
                for future in as_completed(futures):
                    result = future.result()
                    _record_candidate(
                        index=result[0],
                        duration=result[1],
                        provider_calls=result[2],
                        candidate=result[3],
                    )

        def _probe_indices(lo: int, hi: int, count: int) -> list[int]:
            gap = hi - lo
            if gap <= 1:
                return []
            probe_count = min(count, gap - 1)
            if probe_count <= 0:
                return []
            step = gap / (probe_count + 1)
            points: set[int] = set()
            for i in range(1, probe_count + 1):
                idx = lo + int(round(step * i))
                idx = max(lo + 1, min(hi - 1, idx))
                points.add(idx)
            return sorted(points)

        def _collect_supplemental_indices(
            *,
            best_idx: int,
            target_count: int,
        ) -> list[int]:
            pending_needed = max(0, target_count - candidates_checked)
            if pending_needed <= 0:
                return []

            selected: list[int] = []
            selected_set: set[int] = set()

            def _queue(index: int) -> None:
                if (
                    index < 0
                    or index >= total_candidates
                    or index in evaluated
                    or index in selected_set
                ):
                    return
                selected_set.add(index)
                selected.append(index)

            boundary_start = max(0, best_idx - (coarse_stride - 1))
            for idx in range(boundary_start, best_idx):
                _queue(idx)
                if len(selected) >= pending_needed:
                    return selected

            radius = 1
            while len(selected) < pending_needed and radius < total_candidates:
                _queue(best_idx - radius)
                if len(selected) >= pending_needed:
                    break
                _queue(best_idx + radius)
                radius += 1

            for idx in coarse_indices:
                _queue(idx)
                if len(selected) >= pending_needed:
                    return selected

            for idx in range(total_candidates):
                _queue(idx)
                if len(selected) >= pending_needed:
                    break

            return selected

        coarse_indices = list(range(0, total_candidates, coarse_stride))
        if coarse_indices[-1] != total_candidates - 1:
            coarse_indices.append(total_candidates - 1)
        coarse_indices = sorted(set(coarse_indices))
        evaluated_coarse: list[int] = [coarse_indices[0]]
        _evaluate_indices([coarse_indices[0]], phase="coarse")

        best_index: int | None = None
        if evaluated[coarse_indices[0]].meets_deadline:
            best_index = coarse_indices[0]
        else:
            remaining_coarse = coarse_indices[1:]
            chunk_size = max(1, worker_count)
            for offset in range(0, len(remaining_coarse), chunk_size):
                chunk = remaining_coarse[offset: offset + chunk_size]
                _evaluate_indices(chunk, phase="coarse")
                evaluated_coarse.extend(chunk)
                feasible_chunk = [
                    idx for idx in chunk if evaluated[idx].meets_deadline
                ]
                if feasible_chunk:
                    best_index = min(feasible_chunk)
                    break

        if best_index is None:
            range_start = departures[-1].strftime("%Y-%m-%d %H:%M")
            range_end = departures[0].strftime("%Y-%m-%d %H:%M")
            raise NoFeasibleDepartureError(
                "기준 도착 시각을 충족하는 출발 후보를 찾지 못했습니다 "
                f"(분석 범위 {range_start}~{range_end}, "
                f"후보 {total_candidates}건)"
            )

        windows: list[tuple[int, int, int]] = []
        for left, right in zip(
            evaluated_coarse,
            evaluated_coarse[1:],
            strict=False,
        ):
            if left >= best_index:
                break

            left_candidate = evaluated[left]
            right_candidate = evaluated[right]
            duration_gap = abs(
                left_candidate.duration_seconds - right_candidate.duration_seconds
            )
            feasibility_flip = (
                left_candidate.meets_deadline != right_candidate.meets_deadline
            )
            high_volatility = duration_gap >= volatility_threshold_seconds

            if feasibility_flip:
                windows.append((0, left, right))
            elif high_volatility:
                windows.append((1, left, right))

        if not windows and best_index > 0:
            left = max(0, best_index - coarse_stride)
            windows.append((0, left, best_index))

        for _priority, left, right in sorted(
            windows,
            key=lambda item: (item[0], item[1]),
        ):
            if left >= best_index:
                break
            refine_end = min(right, best_index)
            refine_indices = list(range(left, refine_end + 1))
            _evaluate_indices(refine_indices, phase="refine")

            feasible_in_window = [
                idx for idx in refine_indices if evaluated[idx].meets_deadline
            ]
            if feasible_in_window:
                best_index = min(feasible_in_window)

        infeasible_before = [
            idx for idx, candidate in evaluated.items()
            if idx < best_index and not candidate.meets_deadline
        ]
        if infeasible_before:
            lo = max(infeasible_before)
            hi = best_index
            while hi - lo > 1:
                probes = _probe_indices(lo, hi, worker_count)
                if not probes:
                    break
                _evaluate_indices(probes, phase="refine")

                feasible = [idx for idx in probes if evaluated[idx].meets_deadline]
                if feasible:
                    hi = min(feasible)
                else:
                    lo = max(probes)
            best_index = hi

        local_refine_start = max(0, best_index - (coarse_stride - 1))
        local_refine_end = best_index
        local_refine_indices = list(range(local_refine_start, local_refine_end + 1))
        _evaluate_indices(local_refine_indices, phase="refine")
        feasible_local = [
            idx for idx in local_refine_indices if evaluated[idx].meets_deadline
        ]
        if feasible_local:
            best_index = min(feasible_local)

        supplemental_indices = _collect_supplemental_indices(
            best_idx=best_index,
            target_count=minimum_sample_target,
        )
        if supplemental_indices:
            _evaluate_indices(supplemental_indices, phase="refine")
            feasible_supplemental = [
                idx
                for idx in supplemental_indices
                if idx < best_index and evaluated[idx].meets_deadline
            ]
            if feasible_supplemental:
                best_index = min(feasible_supplemental)
                boundary_refine_start = max(0, best_index - (coarse_stride - 1))
                boundary_refine_indices = list(
                    range(boundary_refine_start, best_index + 1)
                )
                _evaluate_indices(boundary_refine_indices, phase="refine")
                feasible_boundary = [
                    idx
                    for idx in boundary_refine_indices
                    if evaluated[idx].meets_deadline
                ]
                if feasible_boundary:
                    best_index = min(feasible_boundary)

        if best_index not in evaluated or not evaluated[best_index].meets_deadline:
            range_start = departures[-1].strftime("%Y-%m-%d %H:%M")
            range_end = departures[0].strftime("%Y-%m-%d %H:%M")
            raise NoFeasibleDepartureError(
                "기준 도착 시각을 충족하는 출발 후보를 찾지 못했습니다 "
                f"(분석 범위 {range_start}~{range_end}, "
                f"후보 {total_candidates}건)"
            )

        best_candidate = evaluated[best_index]
        latest_departure = best_candidate.departure_time
        latest_arrival = best_candidate.arrival_time
        latest_dur = duration_by_index[best_index]

        if (
            latest_departure is None
            or latest_arrival is None
            or latest_dur is None
        ):
            range_start = departures[-1].strftime("%Y-%m-%d %H:%M")
            range_end = departures[0].strftime("%Y-%m-%d %H:%M")
            raise NoFeasibleDepartureError(
                "기준 도착 시각을 충족하는 출발 후보를 찾지 못했습니다 "
                f"(분석 범위 {range_start}~{range_end}, "
                f"후보 {total_candidates}건)"
            )

        safe_duration_secs = math.ceil(latest_dur.duration_seconds * 1.25)
        if analysis_start_time is not None:
            # 출발 시각 기준 모드: 출발은 고정, 도착이 늘어나는 형태로 표시
            safe_departure_time = latest_departure
        else:
            computed_safe_departure = desired_arrival_time - timedelta(
                seconds=safe_duration_secs,
            )
            safe_departure_time = max(computed_safe_departure, min_future_departure)

        score_by_index = self._score_deadline_recommendation_candidates(
            evaluated=evaluated,
            tight_departure_time=latest_departure,
            tight_duration_seconds=latest_dur.duration_seconds,
        )
        score_by_departure = {
            evaluated[idx].departure_time: score_by_index[idx]
            for idx in evaluated
            if idx in score_by_index
        }
        normalized_candidates = tuple(
            RecommendationCandidate(
                departure_time=item.departure_time,
                arrival_time=item.arrival_time,
                duration_seconds=item.duration_seconds,
                meets_deadline=item.meets_deadline,
                phase=item.phase,
                score_total=score_by_departure[item.departure_time][0],
                score_duration=score_by_departure[item.departure_time][1],
                score_time_proximity=score_by_departure[item.departure_time][2],
                score_night_drive=score_by_departure[item.departure_time][3],
                score_stability=score_by_departure[item.departure_time][4],
                score_improvement_efficiency=score_by_departure[
                    item.departure_time
                ][5],
            )
            for item in candidate_evaluations
            if item.departure_time in score_by_departure
        )
        tight_score_total = score_by_index.get(best_index, (None,))[0]

        return DepartureRecommendation(
            route=route,
            desired_arrival_time=desired_arrival_time,
            recommended_departure_time=latest_departure,
            expected_arrival_time=latest_arrival,
            duration=latest_dur,
            provider=self.provider_name,
            provider_calls=provider_calls_total,
            candidates_checked=candidates_checked,
            meets_deadline=True,
            planned_queries=planned_queries,
            total_candidates=total_candidates,
            latest_departure_time=latest_departure,
            latest_departure_arrival_time=latest_arrival,
            latest_departure_duration_seconds=latest_dur.duration_seconds,
            safe_departure_time=safe_departure_time,
            safe_departure_duration_seconds=safe_duration_secs,
            recommended_score_total=tight_score_total,
            baseline_score_total=tight_score_total,
            candidate_evaluations=normalized_candidates,
        )

    def _worker_count(self) -> int:
        provider_parallelism = max(
            1,
            int(getattr(self._provider, "max_parallel_sessions", 1)),
        )
        logical_cpus = max(1, os.cpu_count() or 1)
        cpu_parallel_target = max(1, math.ceil(logical_cpus * 0.8))
        configured_workers = max(
            1,
            int(getattr(self._settings, "recommend_workers", 1)),
        )
        return max(
            1,
            min(provider_parallelism, cpu_parallel_target, configured_workers),
        )

    def _build_forward_analysis_departures(
        self,
        *,
        analysis_start_time: datetime,
        step_minutes: int,
        now: datetime,
        horizon_hours: int,
    ) -> list[datetime]:
        step = timedelta(minutes=step_minutes)
        departure = ceil_time_to_minutes(analysis_start_time, step_minutes)
        min_future_departure = ceil_time_to_minutes(now, step_minutes)
        if departure < min_future_departure:
            departure = min_future_departure

        effective_hours = max(1, horizon_hours)
        end_limit = departure + timedelta(hours=effective_hours)

        departures: list[datetime] = []
        cursor = departure
        while cursor <= end_limit:
            departures.append(cursor)
            cursor += step
        return departures

    def _recommend_from_departure_analysis(
        self,
        *,
        route: Route,
        desired_arrival_time: datetime,
        analysis_start_time: datetime,
        step_minutes: int,
        on_search_initialized: (
            Callable[[int, int], None] | None
        ),
        on_candidate_evaluated: (
            Callable[[RecommendationCandidate], None] | None
        ),
    ) -> DepartureRecommendation:
        worker_count = self._worker_count()
        now = datetime.now(tz=desired_arrival_time.tzinfo)
        departures = self._build_forward_analysis_departures(
            analysis_start_time=analysis_start_time,
            step_minutes=step_minutes,
            now=now,
            horizon_hours=_ARRIVAL_COARSE_EXTENDED_HOURS,
        )
        if not departures:
            raise NoFeasibleDepartureError("조회 가능한 미래 출발 후보가 없습니다")

        total_candidates = len(departures)
        coarse_stride = max(1, math.ceil(60 / step_minutes))
        planned_queries = 0

        provider_calls_total = 0
        candidates_checked = 0
        candidate_evaluations: list[RecommendationCandidate] = []
        evaluated: dict[int, RecommendationCandidate] = {}
        duration_by_index: dict[int, DriveDuration] = {}

        def _evaluate_index(
            index: int,
            *,
            phase: str,
        ) -> tuple[int, DriveDuration, int, RecommendationCandidate]:
            departure = departures[index]
            duration, _cache_hit, provider_calls = self._get_duration_cached(
                route=route,
                departure_time=departure,
            )
            arrival = departure + timedelta(seconds=duration.duration_seconds)
            candidate = RecommendationCandidate(
                departure_time=departure,
                arrival_time=arrival,
                duration_seconds=duration.duration_seconds,
                meets_deadline=True,
                phase=phase,
            )
            return index, duration, provider_calls, candidate

        def _record_candidate(
            *,
            index: int,
            duration: DriveDuration,
            provider_calls: int,
            candidate: RecommendationCandidate,
        ) -> None:
            nonlocal provider_calls_total
            nonlocal candidates_checked
            if index in evaluated:
                return
            evaluated[index] = candidate
            duration_by_index[index] = duration
            provider_calls_total += provider_calls
            candidates_checked += 1
            candidate_evaluations.append(candidate)
            if on_candidate_evaluated is not None:
                on_candidate_evaluated(candidate)

        def _evaluate_indices(indices: list[int], *, phase: str) -> None:
            pending = [idx for idx in indices if idx not in evaluated]
            if not pending:
                return

            if worker_count <= 1 or len(pending) == 1:
                for idx in pending:
                    result = _evaluate_index(idx, phase=phase)
                    _record_candidate(
                        index=result[0],
                        duration=result[1],
                        provider_calls=result[2],
                        candidate=result[3],
                    )
                return

            with ThreadPoolExecutor(
                max_workers=min(worker_count, len(pending)),
            ) as executor:
                futures = {
                    executor.submit(_evaluate_index, idx, phase=phase): idx
                    for idx in pending
                }
                for future in as_completed(futures):
                    result = future.result()
                    _record_candidate(
                        index=result[0],
                        duration=result[1],
                        provider_calls=result[2],
                        candidate=result[3],
                    )

        primary_max_index = min(
            total_candidates - 1,
            _ARRIVAL_COARSE_PRIMARY_HOURS * coarse_stride,
        )
        primary_coarse_indices = list(range(0, primary_max_index + 1, coarse_stride))
        planned_queries += len(primary_coarse_indices)
        if on_search_initialized is not None:
            on_search_initialized(total_candidates, planned_queries)
        _evaluate_indices(primary_coarse_indices, phase="coarse")
        baseline_duration_seconds = duration_by_index[0].duration_seconds

        primary_non_baseline = [idx for idx in primary_coarse_indices if idx != 0]
        all_primary_worse_than_baseline = (
            bool(primary_non_baseline)
            and all(
                duration_by_index[idx].duration_seconds > baseline_duration_seconds
                for idx in primary_non_baseline
            )
        )

        coarse_indices = list(primary_coarse_indices)
        if all_primary_worse_than_baseline:
            extended_max_index = min(
                total_candidates - 1,
                _ARRIVAL_COARSE_EXTENDED_HOURS * coarse_stride,
            )
            extended_coarse_indices = list(
                range(
                    primary_max_index + coarse_stride,
                    extended_max_index + 1,
                    coarse_stride,
                )
            )
            planned_queries += len(extended_coarse_indices)
            if on_search_initialized is not None:
                on_search_initialized(total_candidates, planned_queries)
            _evaluate_indices(extended_coarse_indices, phase="coarse")
            coarse_indices.extend(extended_coarse_indices)

        coarse_score_by_index = self._score_departure_analysis_candidates(
            evaluated={idx: evaluated[idx] for idx in coarse_indices},
            analysis_start_time=analysis_start_time,
            baseline_duration_seconds=baseline_duration_seconds,
        )
        baseline_coarse_score = coarse_score_by_index[coarse_indices[0]][0]
        non_baseline_coarse_indices = [
            idx for idx in coarse_indices if idx != coarse_indices[0]
        ]
        improving_non_baseline_indices = [
            idx
            for idx in non_baseline_coarse_indices
            if evaluated[idx].duration_seconds < baseline_duration_seconds
        ]

        def _coarse_score_sort_key(
            idx: int,
        ) -> tuple[float, float, float, float, float, float, int]:
            score = coarse_score_by_index[idx]
            return (
                score[0],
                score[1],
                score[2],
                score[3],
                score[4],
                score[5],
                -idx,
            )

        ranked_non_baseline = sorted(
            improving_non_baseline_indices,
            key=_coarse_score_sort_key,
            reverse=True,
        )
        dynamic_anchor_limit = 0
        if ranked_non_baseline:
            dynamic_anchor_limit = 2
            if len(ranked_non_baseline) >= 2:
                top_score_gap = (
                    coarse_score_by_index[ranked_non_baseline[0]][0]
                    - coarse_score_by_index[ranked_non_baseline[1]][0]
                )
                if top_score_gap <= _ANALYSIS_DYNAMIC_ANCHOR_TIGHT_GAP:
                    dynamic_anchor_limit = _ANALYSIS_DYNAMIC_ANCHOR_MAX
                elif top_score_gap <= _ANALYSIS_DYNAMIC_ANCHOR_LOOSE_GAP:
                    dynamic_anchor_limit = min(_ANALYSIS_DYNAMIC_ANCHOR_MAX, 3)
            dynamic_anchor_limit = max(
                1,
                min(_ANALYSIS_DYNAMIC_ANCHOR_MAX, dynamic_anchor_limit),
            )

        score_band_candidates: list[int] = []
        if ranked_non_baseline and dynamic_anchor_limit > 0:
            cutoff_rank = min(dynamic_anchor_limit, len(ranked_non_baseline)) - 1
            cutoff_score = coarse_score_by_index[ranked_non_baseline[cutoff_rank]][0]
            score_band_threshold = max(
                baseline_coarse_score + _ANALYSIS_SCORE_MIN_GAIN,
                cutoff_score - _ANALYSIS_SCORE_BAND_MARGIN,
            )
            score_band_candidates = sorted(
                idx
                for idx in ranked_non_baseline
                if coarse_score_by_index[idx][0] >= score_band_threshold
            )

        score_bands: list[list[int]] = []
        if score_band_candidates:
            current_band = [score_band_candidates[0]]
            for idx in score_band_candidates[1:]:
                if idx - current_band[-1] == coarse_stride:
                    current_band.append(idx)
                    continue
                score_bands.append(current_band)
                current_band = [idx]
            score_bands.append(current_band)

        def _band_priority_key(
            band: list[int],
        ) -> tuple[float, float, float, float, float, float, int, int]:
            best_idx = max(band, key=_coarse_score_sort_key)
            return (*_coarse_score_sort_key(best_idx), -band[0])

        promising_indices: set[int] = set()
        for pos, idx in enumerate(coarse_indices):
            if pos == 0:
                continue

            current_score = coarse_score_by_index[idx][0]
            prev_idx = coarse_indices[pos - 1]
            prev_score = coarse_score_by_index[prev_idx][0]
            current_duration = evaluated[idx].duration_seconds
            prev_duration = evaluated[prev_idx].duration_seconds
            next_score = None
            next_duration = None
            if pos + 1 < len(coarse_indices):
                next_idx = coarse_indices[pos + 1]
                next_score = coarse_score_by_index[next_idx][0]
                next_duration = evaluated[next_idx].duration_seconds

            is_local_score_peak = (
                next_score is None
                and current_score >= prev_score
            ) or (
                next_score is not None
                and current_score >= prev_score
                and current_score >= next_score
                and (
                    current_score > prev_score
                    or current_score > next_score
                )
            )
            is_local_duration_pocket = (
                next_duration is not None
                and current_duration < baseline_duration_seconds
                and current_duration <= prev_duration
                and current_duration <= next_duration
                and (
                    current_duration < prev_duration
                    or current_duration < next_duration
                )
            )
            # baseline(출발 기준)보다 총점이 높은 국소 최대점을 정밀 탐색 대상으로 선정
            if (
                is_local_score_peak
                and current_score > baseline_coarse_score
                and current_duration < baseline_duration_seconds
            ):
                promising_indices.add(idx)
            # coarse에서 확인된 국소 시간 단축 구간은 점수 잠재 구간으로 간주
            if is_local_duration_pocket:
                promising_indices.add(idx)

        selected_bands: list[list[int]] = []
        if score_bands and dynamic_anchor_limit > 0:
            selected_bands = sorted(
                sorted(
                    score_bands,
                    key=_band_priority_key,
                    reverse=True,
                )[:dynamic_anchor_limit],
                key=lambda band: band[0],
            )

        selected_band_indices = {
            idx for band in selected_bands for idx in band
        }
        if dynamic_anchor_limit > len(selected_bands):
            for idx in sorted(
                promising_indices,
                key=_coarse_score_sort_key,
                reverse=True,
            ):
                if idx in selected_band_indices:
                    continue
                selected_bands.append([idx])
                selected_band_indices.add(idx)
                if len(selected_bands) >= dynamic_anchor_limit:
                    break

        if not selected_bands and ranked_non_baseline:
            best_coarse_index = ranked_non_baseline[0]
            if coarse_score_by_index[best_coarse_index][0] > baseline_coarse_score:
                selected_bands.append([best_coarse_index])

        refine_indices: set[int] = set()
        for band in sorted(selected_bands, key=lambda item: item[0]):
            band_start = band[0]
            band_end = band[-1]
            upper = min(total_candidates - 1, band_end + (coarse_stride - 1))
            for idx in range(band_start, upper + 1):
                refine_indices.add(idx)
        refine_pending = [idx for idx in sorted(refine_indices) if idx not in evaluated]
        if refine_pending:
            planned_queries += len(refine_pending)
            if on_search_initialized is not None:
                on_search_initialized(total_candidates, planned_queries)
            _evaluate_indices(refine_pending, phase="refine")

        if not evaluated:
            range_start = departures[0].strftime("%Y-%m-%d %H:%M")
            range_end = departures[-1].strftime("%Y-%m-%d %H:%M")
            raise NoFeasibleDepartureError(
                "유효한 추천 후보를 찾지 못했습니다 "
                f"(분석 범위 {range_start}~{range_end}, "
                f"후보 {total_candidates}건)"
            )

        baseline_candidate = evaluated[0]
        baseline_duration = duration_by_index[0]
        baseline_duration_seconds = baseline_duration.duration_seconds
        score_by_index = self._score_departure_analysis_candidates(
            evaluated=evaluated,
            analysis_start_time=analysis_start_time,
            baseline_duration_seconds=baseline_duration_seconds,
        )
        eligible_indices = [
            idx
            for idx, candidate in evaluated.items()
            if candidate.duration_seconds <= baseline_duration_seconds
        ]
        if not eligible_indices:
            eligible_indices = list(evaluated.keys())

        best_index = max(
            eligible_indices,
            key=lambda idx: (
                score_by_index[idx][0],
                score_by_index[idx][1],
                score_by_index[idx][2],
                score_by_index[idx][3],
                score_by_index[idx][4],
                score_by_index[idx][5],
                -idx,
            ),
        )
        best_candidate = evaluated[best_index]
        best_duration = duration_by_index[best_index]
        safe_duration_secs = math.ceil(
            baseline_duration.duration_seconds * 1.25
        )
        safe_departure_time = baseline_candidate.departure_time

        score_by_departure = {
            candidate.departure_time: score_by_index[idx]
            for idx, candidate in evaluated.items()
        }

        normalized_candidates = tuple(
            RecommendationCandidate(
                departure_time=item.departure_time,
                arrival_time=item.arrival_time,
                duration_seconds=item.duration_seconds,
                meets_deadline=(
                    item.duration_seconds <= baseline_duration_seconds
                ),
                phase=item.phase,
                score_total=score_by_departure[item.departure_time][0],
                score_duration=score_by_departure[item.departure_time][1],
                score_time_proximity=score_by_departure[item.departure_time][2],
                score_night_drive=score_by_departure[item.departure_time][3],
                score_stability=score_by_departure[item.departure_time][4],
                score_improvement_efficiency=score_by_departure[
                    item.departure_time
                ][5],
            )
            for item in candidate_evaluations
        )

        return DepartureRecommendation(
            route=route,
            desired_arrival_time=baseline_candidate.arrival_time,
            recommended_departure_time=best_candidate.departure_time,
            expected_arrival_time=best_candidate.arrival_time,
            duration=best_duration,
            provider=self.provider_name,
            provider_calls=provider_calls_total,
            candidates_checked=candidates_checked,
            meets_deadline=(
                best_duration.duration_seconds <= baseline_duration_seconds
            ),
            planned_queries=planned_queries,
            total_candidates=total_candidates,
            latest_departure_time=baseline_candidate.departure_time,
            latest_departure_arrival_time=baseline_candidate.arrival_time,
            latest_departure_duration_seconds=baseline_duration.duration_seconds,
            safe_departure_time=safe_departure_time,
            safe_departure_duration_seconds=safe_duration_secs,
            recommended_score_total=score_by_index[best_index][0],
            baseline_score_total=score_by_index[0][0],
            candidate_evaluations=normalized_candidates,
        )

    def _night_drive_score(self, departure_time: datetime) -> float:
        hour = departure_time.hour + (departure_time.minute / 60.0)
        if 0 <= hour < 5:
            return 0.30
        if 5 <= hour < 6:
            return 0.60
        if 22 <= hour < 24:
            return 0.65
        return 1.0

    def _score_deadline_recommendation_candidates(
        self,
        *,
        evaluated: dict[int, RecommendationCandidate],
        tight_departure_time: datetime,
        tight_duration_seconds: int,
    ) -> dict[int, tuple[float, float, float, float, float, float]]:
        if not evaluated:
            return {}

        durations = [candidate.duration_seconds for candidate in evaluated.values()]
        min_duration = min(durations)
        max_improvement = max(0, tight_duration_seconds - min_duration)
        improvement_range_floor = max(
            _ANALYSIS_SCORE_DURATION_SPAN_FLOOR_SECONDS,
            self._settings.step_minutes * 60,
        )
        effective_improvement_range = max(
            max_improvement,
            improvement_range_floor,
        )

        offset_seconds_by_index = {
            idx: abs(
                (
                    candidate.departure_time - tight_departure_time
                ).total_seconds()
            )
            for idx, candidate in evaluated.items()
        }
        max_offset_seconds = max(
            0.0,
            max(offset_seconds_by_index.values(), default=0.0),
        )

        sorted_indices = sorted(
            evaluated.keys(),
            key=lambda idx: evaluated[idx].departure_time,
        )
        base_gap_seconds = float(self._settings.step_minutes * 60)
        volatility_by_index: dict[int, float] = {}
        max_volatility_seconds = 0.0
        for pos, idx in enumerate(sorted_indices):
            adjusted_diffs: list[float] = []
            current_candidate = evaluated[idx]
            current_duration = current_candidate.duration_seconds
            current_departure = current_candidate.departure_time
            if pos > 0:
                prev_idx = sorted_indices[pos - 1]
                prev_candidate = evaluated[prev_idx]
                prev_gap_seconds = max(
                    base_gap_seconds,
                    (
                        current_departure - prev_candidate.departure_time
                    ).total_seconds(),
                )
                prev_gap_scale = min(base_gap_seconds / prev_gap_seconds, 1.0)
                adjusted_diffs.append(
                    abs(current_duration - prev_candidate.duration_seconds)
                    * prev_gap_scale
                )
            if pos + 1 < len(sorted_indices):
                next_idx = sorted_indices[pos + 1]
                next_candidate = evaluated[next_idx]
                next_gap_seconds = max(
                    base_gap_seconds,
                    (
                        next_candidate.departure_time - current_departure
                    ).total_seconds(),
                )
                next_gap_scale = min(base_gap_seconds / next_gap_seconds, 1.0)
                adjusted_diffs.append(
                    abs(current_duration - next_candidate.duration_seconds)
                    * next_gap_scale
                )
            volatility = (
                sum(adjusted_diffs) / len(adjusted_diffs)
                if adjusted_diffs
                else 0.0
            )
            volatility_by_index[idx] = volatility
            max_volatility_seconds = max(max_volatility_seconds, volatility)
        effective_volatility_range = max(
            max_volatility_seconds,
            float(improvement_range_floor),
        )

        efficiency_raw_by_index: dict[int, float] = {}
        max_efficiency_raw = 0.0
        min_wait_seconds = float(self._settings.step_minutes * 60)
        for idx, candidate in evaluated.items():
            improvement_seconds = max(
                0,
                tight_duration_seconds - candidate.duration_seconds,
            )
            wait_seconds = max(
                offset_seconds_by_index[idx],
                min_wait_seconds,
            )
            efficiency_raw = improvement_seconds / wait_seconds
            efficiency_raw_by_index[idx] = efficiency_raw
            max_efficiency_raw = max(max_efficiency_raw, efficiency_raw)

        scored: dict[int, tuple[float, float, float, float, float, float]] = {}
        for idx, candidate in evaluated.items():
            improvement_seconds = max(
                0,
                tight_duration_seconds - candidate.duration_seconds,
            )
            duration_score = min(
                improvement_seconds / effective_improvement_range,
                1.0,
            )

            if max_offset_seconds <= 0:
                time_proximity_score = 1.0
            else:
                offset_seconds = offset_seconds_by_index[idx]
                time_proximity_score = 1.0 - min(
                    offset_seconds / max_offset_seconds,
                    1.0,
                )

            night_drive_score = self._night_drive_score(
                candidate.departure_time
            )
            stability_score = 1.0 - min(
                volatility_by_index[idx] / effective_volatility_range,
                1.0,
            )
            if max_efficiency_raw <= 0:
                improvement_efficiency_score = 1.0
            else:
                improvement_efficiency_score = min(
                    efficiency_raw_by_index[idx] / max_efficiency_raw,
                    1.0,
                )
            total_score = (
                duration_score * _ANALYSIS_SCORE_WEIGHT_DURATION
                + time_proximity_score
                * _ANALYSIS_SCORE_WEIGHT_TIME_PROXIMITY
                + night_drive_score * _ANALYSIS_SCORE_WEIGHT_NIGHT_DRIVE
                + stability_score * _ANALYSIS_SCORE_WEIGHT_STABILITY
                + improvement_efficiency_score
                * _ANALYSIS_SCORE_WEIGHT_IMPROVEMENT_EFFICIENCY
            )
            if not candidate.meets_deadline:
                total_score *= 0.45
            scored[idx] = (
                total_score,
                duration_score,
                time_proximity_score,
                night_drive_score,
                stability_score,
                improvement_efficiency_score,
            )
        return scored

    def _score_departure_analysis_candidates(
        self,
        *,
        evaluated: dict[int, RecommendationCandidate],
        analysis_start_time: datetime,
        baseline_duration_seconds: int,
    ) -> dict[int, tuple[float, float, float, float, float, float]]:
        if not evaluated:
            return {}

        durations = [candidate.duration_seconds for candidate in evaluated.values()]
        min_duration = min(durations)
        max_improvement = max(0, baseline_duration_seconds - min_duration)
        improvement_range_floor = max(
            _ANALYSIS_SCORE_DURATION_SPAN_FLOOR_SECONDS,
            self._settings.step_minutes * 60,
        )
        effective_improvement_range = max(
            max_improvement,
            improvement_range_floor,
        )

        offset_seconds_by_index = {
            idx: max(
                0.0,
                (
                    candidate.departure_time - analysis_start_time
                ).total_seconds(),
            )
            for idx, candidate in evaluated.items()
        }
        max_offset_seconds = max(
            0.0,
            max(offset_seconds_by_index.values(), default=0.0),
        )

        sorted_indices = sorted(
            evaluated.keys(),
            key=lambda idx: evaluated[idx].departure_time,
        )
        base_gap_seconds = float(self._settings.step_minutes * 60)
        volatility_by_index: dict[int, float] = {}
        max_volatility_seconds = 0.0
        for pos, idx in enumerate(sorted_indices):
            adjusted_diffs: list[float] = []
            current_candidate = evaluated[idx]
            current_duration = current_candidate.duration_seconds
            current_departure = current_candidate.departure_time
            if pos > 0:
                prev_idx = sorted_indices[pos - 1]
                prev_candidate = evaluated[prev_idx]
                prev_gap_seconds = max(
                    base_gap_seconds,
                    (
                        current_departure - prev_candidate.departure_time
                    ).total_seconds(),
                )
                prev_gap_scale = min(base_gap_seconds / prev_gap_seconds, 1.0)
                adjusted_diffs.append(
                    abs(current_duration - prev_candidate.duration_seconds)
                    * prev_gap_scale
                )
            if pos + 1 < len(sorted_indices):
                next_idx = sorted_indices[pos + 1]
                next_candidate = evaluated[next_idx]
                next_gap_seconds = max(
                    base_gap_seconds,
                    (
                        next_candidate.departure_time - current_departure
                    ).total_seconds(),
                )
                next_gap_scale = min(base_gap_seconds / next_gap_seconds, 1.0)
                adjusted_diffs.append(
                    abs(current_duration - next_candidate.duration_seconds)
                    * next_gap_scale
                )
            # coarse/refine 혼합으로 발생하는 간격 편향을 줄이기 위해
            # 실제 간격(Δtime) 대비 step 기준 변동으로 안정성을 산출한다.
            volatility = (
                sum(adjusted_diffs) / len(adjusted_diffs)
                if adjusted_diffs
                else 0.0
            )
            volatility_by_index[idx] = volatility
            max_volatility_seconds = max(max_volatility_seconds, volatility)
        effective_volatility_range = max(
            max_volatility_seconds,
            float(improvement_range_floor),
        )

        efficiency_raw_by_index: dict[int, float] = {}
        max_efficiency_raw = 0.0
        min_wait_seconds = float(self._settings.step_minutes * 60)
        for idx, candidate in evaluated.items():
            improvement_seconds = max(
                0,
                baseline_duration_seconds - candidate.duration_seconds,
            )
            wait_seconds = max(
                offset_seconds_by_index[idx],
                min_wait_seconds,
            )
            efficiency_raw = improvement_seconds / wait_seconds
            efficiency_raw_by_index[idx] = efficiency_raw
            max_efficiency_raw = max(max_efficiency_raw, efficiency_raw)

        scored: dict[int, tuple[float, float, float, float, float, float]] = {}
        for idx, candidate in evaluated.items():
            improvement_seconds = max(
                0,
                baseline_duration_seconds - candidate.duration_seconds,
            )
            duration_score = min(
                improvement_seconds / effective_improvement_range,
                1.0,
            )

            if max_offset_seconds <= 0:
                time_proximity_score = 1.0
            else:
                offset_seconds = offset_seconds_by_index[idx]
                time_proximity_score = 1.0 - min(
                    offset_seconds / max_offset_seconds,
                    1.0,
                )

            night_drive_score = self._night_drive_score(
                candidate.departure_time
            )
            stability_score = 1.0 - min(
                volatility_by_index[idx] / effective_volatility_range,
                1.0,
            )
            if max_efficiency_raw <= 0:
                improvement_efficiency_score = 1.0
            else:
                improvement_efficiency_score = min(
                    efficiency_raw_by_index[idx] / max_efficiency_raw,
                    1.0,
                )
            total_score = (
                duration_score * _ANALYSIS_SCORE_WEIGHT_DURATION
                + time_proximity_score
                * _ANALYSIS_SCORE_WEIGHT_TIME_PROXIMITY
                + night_drive_score * _ANALYSIS_SCORE_WEIGHT_NIGHT_DRIVE
                + stability_score * _ANALYSIS_SCORE_WEIGHT_STABILITY
                + improvement_efficiency_score
                * _ANALYSIS_SCORE_WEIGHT_IMPROVEMENT_EFFICIENCY
            )
            scored[idx] = (
                total_score,
                duration_score,
                time_proximity_score,
                night_drive_score,
                stability_score,
                improvement_efficiency_score,
            )
        return scored

    def close(self) -> None:
        self._provider.close()

    def _build_recommendation_departures(
        self,
        *,
        desired_arrival_time: datetime,
        start_limit: datetime,
        step_minutes: int,
        now: datetime,
    ) -> list[datetime]:
        step = timedelta(minutes=step_minutes)
        departure = floor_time_to_minutes(desired_arrival_time, step_minutes)
        departures: list[datetime] = []

        while departure >= start_limit:
            if departure > now:
                departures.append(departure)
            departure -= step

        return departures

    def _get_duration_cached(
        self,
        *,
        route: Route,
        departure_time: datetime,
    ) -> tuple[DriveDuration, bool, int]:
        bucketed_departure = floor_time_to_minutes(
            departure_time, self._settings.step_minutes
        )
        key = (route, bucketed_departure)

        cached = self._cache.get(key)
        if cached is not None:
            return cached, True, 0

        retryable_attempts = 2
        for attempt in range(1, retryable_attempts + 1):
            try:
                one_shot = getattr(self._provider, "get_drive_duration_once", None)
                if callable(one_shot):
                    duration = one_shot(route, bucketed_departure)
                else:
                    duration = self._provider.get_drive_duration(
                        route,
                        bucketed_departure,
                    )
                self._cache.set(key, duration)
                return duration, False, attempt
            except ProviderError as exc:
                if not exc.is_retryable or attempt >= retryable_attempts:
                    raise
                _log.warning(
                    "provider retryable 에러 (재시도 %d/%d): %s @ %s",
                    attempt,
                    retryable_attempts,
                    exc,
                    bucketed_departure.strftime("%H:%M"),
                )
