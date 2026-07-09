from __future__ import annotations

import queue
from collections.abc import Iterator

from fastapi import APIRouter

from trip_time_service.api.naver_playwright_geo import (
    extract_addr_from_naver_url as _extract_addr_from_naver_url,
)
from trip_time_service.api.naver_playwright_geo import (
    extract_coords_from_naver_url as _extract_coords_from_naver_url,
)
from trip_time_service.api.routes_geo import router as geo_router
from trip_time_service.api.routes_trip import router as trip_router
from trip_time_service.api.streaming import (
    DEFAULT_STREAM_QUEUE_POLL_SECONDS,
    iter_stream_events,
)

router = APIRouter()
router.include_router(geo_router)
router.include_router(trip_router)

_STREAM_QUEUE_POLL_SECONDS = DEFAULT_STREAM_QUEUE_POLL_SECONDS


def _iter_stream_events(
    *,
    event_queue: queue.Queue[object],
    done_marker: object,
    worker: object,
    idle_timeout_seconds: float,
) -> Iterator[dict[str, object]]:
    return iter_stream_events(
        event_queue=event_queue,
        done_marker=done_marker,
        worker=worker,
        idle_timeout_seconds=idle_timeout_seconds,
        poll_seconds=_STREAM_QUEUE_POLL_SECONDS,
    )


__all__ = [
    "router",
    "_iter_stream_events",
    "_extract_coords_from_naver_url",
    "_extract_addr_from_naver_url",
]
