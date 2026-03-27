from __future__ import annotations

from trip_time_service.config import Settings
from trip_time_service.providers.base import TravelTimeProvider
from trip_time_service.providers.mock import MockTravelTimeProvider
from trip_time_service.providers.naver_selenium import (
    NaverMapsSeleniumPoolProvider,
    NaverMapsSeleniumProvider,
)
from trip_time_service.providers.osrm import OsrmTravelTimeProvider


def create_provider(settings: Settings) -> TravelTimeProvider:
    provider_name = settings.provider

    if provider_name in {"osrm"}:
        return OsrmTravelTimeProvider()
    if provider_name in {"mock"}:
        return MockTravelTimeProvider()
    if provider_name in {"naver", "naver_selenium"}:
        if settings.naver_session_pool_size > 1:
            return NaverMapsSeleniumPoolProvider(settings)
        return NaverMapsSeleniumProvider(settings)

    raise ValueError(f"Unsupported provider: {provider_name!r}")
