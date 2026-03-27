from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


def normalize_place(value: str) -> str:
    return " ".join(value.strip().split())


@dataclass(frozen=True, slots=True)
class Route:
    origin: str
    destination: str

    @classmethod
    def of(cls, origin: str, destination: str) -> Route:
        return cls(
            origin=normalize_place(origin),
            destination=normalize_place(destination),
        )


@dataclass(frozen=True, slots=True)
class DriveDuration:
    duration_seconds: int
    fetched_at: datetime
    raw_text: str | None = None

    def __post_init__(self) -> None:
        if self.duration_seconds < 0:
            raise ValueError("duration_seconds must be non-negative")


@dataclass(frozen=True, slots=True)
class ArrivalEstimate:
    route: Route
    departure_time: datetime
    arrival_time: datetime
    duration: DriveDuration
    provider: str
    cache_hit: bool


@dataclass(frozen=True, slots=True)
class DepartureRecommendation:
    route: Route
    desired_arrival_time: datetime
    recommended_departure_time: datetime
    expected_arrival_time: datetime
    duration: DriveDuration
    provider: str
    provider_calls: int
    candidates_checked: int
    meets_deadline: bool
    planned_queries: int = 0
    total_candidates: int = 0
    # 마지노선 출발 (타이트): 가장 늦게 출발해도 정시 도착 가능한 시각
    latest_departure_time: datetime | None = None
    latest_departure_arrival_time: datetime | None = None
    latest_departure_duration_seconds: int | None = None
    # 안정적 출발: 마지노선 소요시간 × 1.25 여유 적용
    safe_departure_time: datetime | None = None
    safe_departure_duration_seconds: int | None = None
    recommended_score_total: float | None = None
    baseline_score_total: float | None = None
    candidate_evaluations: tuple[RecommendationCandidate, ...] = ()


@dataclass(frozen=True, slots=True)
class RecommendationCandidate:
    departure_time: datetime
    arrival_time: datetime
    duration_seconds: int
    meets_deadline: bool
    phase: str
    score_total: float | None = None
    score_duration: float | None = None
    score_time_proximity: float | None = None
    score_night_drive: float | None = None
    score_stability: float | None = None
    score_improvement_efficiency: float | None = None
