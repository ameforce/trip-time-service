from __future__ import annotations

import os
import statistics
import time
from dataclasses import replace
from datetime import datetime, timedelta

from trip_time_service.config import load_settings
from trip_time_service.providers.factory import create_provider
from trip_time_service.services.trip_time_service import TripTimeService


def _run_case(
    *,
    base_settings,
    origin: str,
    destination: str,
    recommend_workers: int,
    session_pool_size: int,
    runs: int,
) -> dict[str, float | int]:
    settings = replace(
        base_settings,
        provider="naver_playwright",
        recommend_workers=recommend_workers,
        naver_session_pool_size=session_pool_size,
        step_minutes=10,
        lookback_hours=6,
        max_queries=120,
    )
    provider = create_provider(settings)
    service = TripTimeService(settings=settings, provider=provider)

    latencies: list[float] = []
    failures = 0

    try:
        now = datetime.now(tz=settings.timezone).replace(second=0, microsecond=0)
        for i in range(runs):
            desired = now + timedelta(hours=2, minutes=(i * settings.step_minutes))
            started = time.perf_counter()
            try:
                service.recommend_departure(
                    origin=origin,
                    destination=destination,
                    desired_arrival_time=desired,
                )
            except Exception:
                failures += 1
                continue
            latencies.append(time.perf_counter() - started)
    finally:
        service.close()

    avg = statistics.mean(latencies) if latencies else float("inf")
    p95 = max(latencies) if latencies else float("inf")
    return {
        "workers": recommend_workers,
        "sessions": session_pool_size,
        "runs": runs,
        "successes": len(latencies),
        "failures": failures,
        "avg_seconds": avg,
        "p95_seconds": p95,
    }


def main() -> None:
    origin = os.getenv("TTS_BENCH_ORIGIN", "강남역")
    destination = os.getenv("TTS_BENCH_DESTINATION", "판교역")
    runs = int(os.getenv("TTS_BENCH_RUNS", "2"))
    logical_cpus = max(1, os.cpu_count() or 1)
    max_workers = int(
        os.getenv("TTS_BENCH_PARALLEL_WORKERS", str(logical_cpus))
    )
    max_workers = max(1, min(max_workers, logical_cpus))

    base_settings = load_settings()
    serial = _run_case(
        base_settings=base_settings,
        origin=origin,
        destination=destination,
        recommend_workers=1,
        session_pool_size=1,
        runs=runs,
    )
    parallel = _run_case(
        base_settings=base_settings,
        origin=origin,
        destination=destination,
        recommend_workers=max_workers,
        session_pool_size=max_workers,
        runs=runs,
    )

    print("SERIAL", serial)
    print("PARALLEL", parallel)


if __name__ == "__main__":
    main()
