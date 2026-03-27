from __future__ import annotations

import json
import queue
import time
from collections.abc import Iterator
from datetime import datetime

DEFAULT_STREAM_QUEUE_POLL_SECONDS = 0.5
STREAM_IDLE_TIMEOUT_SECONDS = 45.0
STREAM_STALL_ERROR_MESSAGE = "추천 계산 워커가 응답하지 않아 스트림을 종료합니다"


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Not JSON serializable: {type(value)!r}")


def sse_encode(event: str, data: object) -> str:
    payload = json.dumps(data, ensure_ascii=False, default=_json_default)
    return f"event: {event}\ndata: {payload}\n\n"


def iter_stream_events(
    *,
    event_queue: queue.Queue[object],
    done_marker: object,
    worker: object,
    idle_timeout_seconds: float,
    poll_seconds: float = DEFAULT_STREAM_QUEUE_POLL_SECONDS,
) -> Iterator[dict[str, object]]:
    idle_deadline = time.monotonic() + idle_timeout_seconds
    while True:
        try:
            item = event_queue.get(timeout=poll_seconds)
        except queue.Empty:
            worker_is_alive = getattr(worker, "is_alive", None)
            if callable(worker_is_alive) and not worker_is_alive():
                break
            if time.monotonic() >= idle_deadline:
                yield {
                    "event": "error",
                    "data": {"detail": STREAM_STALL_ERROR_MESSAGE},
                }
                break
            continue

        idle_deadline = time.monotonic() + idle_timeout_seconds
        if item is done_marker:
            break
        yield item
