from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime

from trip_time_service.config import load_settings
from trip_time_service.providers.mock import MockTravelTimeProvider
from trip_time_service.services.trip_time_service import TripTimeService


def main() -> None:
    settings = load_settings()
    settings = replace(settings, provider="mock")

    provider = MockTravelTimeProvider()
    service = TripTimeService(settings=settings, provider=provider)

    iters = int(os.getenv("TTS_BENCH_ITERS", "1"))
    origin = os.getenv("TTS_BENCH_ORIGIN", "강남역")
    destination = os.getenv("TTS_BENCH_DESTINATION", "판교역")

    departure_time = datetime.now(tz=settings.timezone).replace(second=0, microsecond=0)

    last = None
    for _ in range(max(1, iters)):
        last = service.estimate_arrival(
            origin=origin,
            destination=destination,
            departure_time=departure_time,
        )

    if last is not None:
        print(
            {
                "origin": last.route.origin,
                "destination": last.route.destination,
                "departure_time": last.departure_time.isoformat(),
                "arrival_time": last.arrival_time.isoformat(),
                "duration_seconds": last.duration.duration_seconds,
                "provider": last.provider,
                "cache_hit": last.cache_hit,
            }
        )


if __name__ == "__main__":
    main()
