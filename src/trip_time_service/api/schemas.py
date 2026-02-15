from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class RouteModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    origin: str = Field(min_length=1)
    destination: str = Field(min_length=1)


class CoordsInput(BaseModel):
    lat: float
    lon: float


class ArrivalTimeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    origin: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    departure_time: datetime
    origin_coords: CoordsInput | None = None
    dest_coords: CoordsInput | None = None


class ArrivalTimeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route: RouteModel
    departure_time: datetime
    arrival_time: datetime
    duration_seconds: int
    provider: str
    cache_hit: bool


class DepartureRecommendationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    origin: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    desired_arrival_time: datetime
    origin_coords: CoordsInput | None = None
    dest_coords: CoordsInput | None = None


class RecommendationCandidateModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

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


class DepartureRecommendationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route: RouteModel
    desired_arrival_time: datetime
    recommended_departure_time: datetime
    expected_arrival_time: datetime
    duration_seconds: int
    meets_deadline: bool
    provider: str
    provider_calls: int
    candidates_checked: int
    planned_queries: int
    total_candidates: int
    # 마지노선 출발 (타이트)
    latest_departure_time: datetime | None = None
    latest_departure_arrival_time: datetime | None = None
    latest_departure_duration_seconds: int | None = None
    # 안정적 출발 (× 1.25)
    safe_departure_time: datetime | None = None
    safe_departure_duration_seconds: int | None = None
    recommended_score_total: float | None = None
    baseline_score_total: float | None = None
    candidate_evaluations: list[RecommendationCandidateModel] = Field(
        default_factory=list
    )


class SafeDeparturePreviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    safe_departure_time: datetime
    safe_duration_seconds: int
    clamped_to_now: bool


class ArrivalWithRecommendationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    arrival: ArrivalTimeResponse
    recommendation: DepartureRecommendationResponse
    immediate_safe_departure: SafeDeparturePreviewResponse


class FrontendConfig(BaseModel):
    naver_map_client_id: str | None = None
    timezone: str = "Asia/Seoul"
    provider: str = "unknown"
