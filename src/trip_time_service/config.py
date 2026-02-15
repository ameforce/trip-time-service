from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo


def _getenv_stripped(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value if value else None


def _getenv_int(name: str, default: int) -> int:
    value = _getenv_stripped(name)
    if value is None:
        return default
    return int(value)


def _getenv_bool(name: str, default: bool) -> bool:
    value = _getenv_stripped(name)
    if value is None:
        return default

    normalized = value.lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False

    raise ValueError(f"Invalid boolean env var: {name}={value!r}")


def _cpu_parallel_target(logical_cpus: int) -> int:
    # CPU logical thread의 80%를 기본 병렬도 목표치로 사용
    return max(1, math.ceil(logical_cpus * 0.8))


@dataclass(frozen=True, slots=True)
class Settings:
    timezone: ZoneInfo
    headless: bool
    cache_ttl: timedelta
    step_minutes: int
    lookback_hours: int
    max_queries: int
    provider: str

    chrome_binary_path: str | None
    chrome_user_data_dir: str | None
    naver_map_client_id: str | None
    recommend_workers: int = 1
    naver_session_pool_size: int = 1


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    timezone_name = _getenv_stripped("TTS_TIMEZONE") or "Asia/Seoul"
    timezone = ZoneInfo(timezone_name)

    headless = _getenv_bool("TTS_HEADLESS", True)
    cache_ttl_seconds = _getenv_int("TTS_CACHE_TTL_SECONDS", 600)
    step_minutes = _getenv_int("TTS_STEP_MINUTES", 10)
    lookback_hours = _getenv_int("TTS_LOOKBACK_HOURS", 3)
    max_queries = _getenv_int("TTS_MAX_QUERIES", 120)
    provider = (_getenv_stripped("TTS_PROVIDER") or "naver_selenium").lower()
    logical_cpus = max(1, os.cpu_count() or 1)
    cpu_parallel_target = _cpu_parallel_target(logical_cpus)

    default_pool_size = (
        cpu_parallel_target
        if provider in {"naver", "naver_selenium"}
        else 1
    )
    naver_session_pool_size = _getenv_int(
        "TTS_NAVER_SESSION_POOL_SIZE",
        default_pool_size,
    )
    recommend_workers = _getenv_int(
        "TTS_RECOMMEND_WORKERS",
        naver_session_pool_size,
    )

    if provider in {"naver", "naver_selenium"}:
        # Naver Selenium은 항상 CPU 80% 병렬도를 확보한다.
        naver_session_pool_size = cpu_parallel_target
        recommend_workers = cpu_parallel_target

    chrome_binary_path = _getenv_stripped("TTS_CHROME_BINARY_PATH")
    chrome_user_data_dir = _getenv_stripped("TTS_CHROME_USER_DATA_DIR")
    naver_map_client_id = _getenv_stripped("TTS_NAVER_MAP_CLIENT_ID")

    if step_minutes <= 0:
        raise ValueError("TTS_STEP_MINUTES must be positive")
    if lookback_hours <= 0:
        raise ValueError("TTS_LOOKBACK_HOURS must be positive")
    if max_queries <= 0:
        raise ValueError("TTS_MAX_QUERIES must be positive")
    if cache_ttl_seconds <= 0:
        raise ValueError("TTS_CACHE_TTL_SECONDS must be positive")
    if recommend_workers <= 0:
        raise ValueError("TTS_RECOMMEND_WORKERS must be positive")
    if naver_session_pool_size <= 0:
        raise ValueError("TTS_NAVER_SESSION_POOL_SIZE must be positive")

    return Settings(
        timezone=timezone,
        headless=headless,
        cache_ttl=timedelta(seconds=cache_ttl_seconds),
        step_minutes=step_minutes,
        lookback_hours=lookback_hours,
        max_queries=max_queries,
        provider=provider,
        chrome_binary_path=chrome_binary_path,
        chrome_user_data_dir=chrome_user_data_dir,
        naver_map_client_id=naver_map_client_id,
        recommend_workers=recommend_workers,
        naver_session_pool_size=naver_session_pool_size,
    )
