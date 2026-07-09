from __future__ import annotations

from trip_time_service.config import Settings
from trip_time_service.providers.base import TravelTimeProvider
from trip_time_service.providers.mock import MockTravelTimeProvider
from trip_time_service.providers.osrm import OsrmTravelTimeProvider


def create_provider(settings: Settings) -> TravelTimeProvider:
    provider_name = settings.provider

    if provider_name in {"osrm"}:
        return OsrmTravelTimeProvider()
    if provider_name in {"mock"}:
        return MockTravelTimeProvider()
    if provider_name in {"naver", "naver_playwright"}:
        # Playwright provider/pool (기본 경로).
        from trip_time_service.providers.naver_playwright import (
            NaverMapsPlaywrightPoolProvider,
            NaverMapsPlaywrightProvider,
        )

        if settings.naver_session_pool_size > 1:
            return NaverMapsPlaywrightPoolProvider(settings)
        return NaverMapsPlaywrightProvider(settings)
    if provider_name in {"naver_selenium"}:
        # Task 6 삭제 전까지 유지되는 임시 Selenium 경로. selenium 패키지가
        # deps에서 제거되었으므로, 모듈이 존재해도 import 시점에 실패할 수 있다.
        # 지연 import로 두어 Playwright 기본 경로에는 영향을 주지 않는다.
        try:
            from trip_time_service.providers.naver_selenium import (
                NaverMapsSeleniumPoolProvider,
                NaverMapsSeleniumProvider,
            )
        except ImportError as exc:
            raise ValueError(
                "naver_selenium provider is no longer supported "
                "(selenium dependency removed); use 'naver_playwright'"
            ) from exc

        if settings.naver_session_pool_size > 1:
            return NaverMapsSeleniumPoolProvider(settings)
        return NaverMapsSeleniumProvider(settings)

    raise ValueError(f"Unsupported provider: {provider_name!r}")
