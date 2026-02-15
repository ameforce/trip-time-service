from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

from trip_time_service.config import Settings
from trip_time_service.providers.naver_selenium import (
    NaverMapsSeleniumProvider,
    _parse_naver_duration,
)

KST = ZoneInfo("Asia/Seoul")


def _settings() -> Settings:
    return Settings(
        timezone=KST,
        headless=True,
        cache_ttl=timedelta(seconds=300),
        step_minutes=10,
        lookback_hours=3,
        max_queries=120,
        provider="naver_selenium",
        chrome_binary_path=None,
        chrome_user_data_dir=None,
        naver_map_client_id=None,
        recommend_workers=1,
        naver_session_pool_size=1,
    )


def test_extract_duration_from_panel_text_matches_requested_departure() -> None:
    provider = NaverMapsSeleniumProvider(_settings())
    text = (
        "\ub098\uc911\uc5d0 \ucd9c\ubc1c\n"
        "\ub0b4\uc77c \uc624\uc804 10\uc2dc 00\ubd84 \ucd9c\ubc1c\ud558\uba74\n"
        "4\uc2dc\uac04 33\ubd84 \uc18c\uc694 \uc608\uc0c1"
    )

    duration = provider._extract_duration_from_panel_text(
        text,
        "\uc624\uc804",
        "10\uc2dc",
        "00\ubd84",
    )

    assert duration == (4 * 60 + 33) * 60


def test_extract_duration_from_panel_text_rejects_mismatched_departure() -> None:
    provider = NaverMapsSeleniumProvider(_settings())
    text = (
        "\ub098\uc911\uc5d0 \ucd9c\ubc1c\n"
        "\ub0b4\uc77c \uc624\uc804 09\uc2dc 00\ubd84 \ucd9c\ubc1c\ud558\uba74\n"
        "4\uc2dc\uac04 33\ubd84 \uc18c\uc694 \uc608\uc0c1"
    )

    duration = provider._extract_duration_from_panel_text(
        text,
        "\uc624\uc804",
        "10\uc2dc",
        "00\ubd84",
    )

    assert duration is None


def test_extract_duration_from_panel_text_rejects_route_summary_like_text() -> None:
    provider = NaverMapsSeleniumProvider(_settings())
    text = "2\uc2dc\uac04 45\ubd84231km"

    duration = provider._extract_duration_from_panel_text(
        text,
        "\uc624\uc804",
        "10\uc2dc",
        "00\ubd84",
    )

    assert duration is None


def test_parse_duration_prefers_hour_only_before_soyo() -> None:
    text = (
        "\ub0b4\uc77c \uc624\uc804 10\uc2dc 40\ubd84 \ucd9c\ubc1c\ud558\uba74\n"
        "5\uc2dc\uac04 \uc18c\uc694 \uc608\uc0c1\n"
        "+9\ubd84\n"
        "30\ubd84 \ud6c4\n"
        "+14\ubd84\n"
        "2\uc2dc\uac04 \ud6c4"
    )

    duration = _parse_naver_duration(text)

    assert duration == 5 * 3600
