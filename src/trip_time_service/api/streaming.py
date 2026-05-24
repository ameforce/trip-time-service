from __future__ import annotations

import json
import os
import queue
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime

DEFAULT_STREAM_QUEUE_POLL_SECONDS = 0.5
STREAM_IDLE_TIMEOUT_SECONDS = 45.0
STREAM_STALL_REASON = "stream_stall_timeout"
STREAM_STALL_ERROR_MESSAGE = "추천 계산 워커가 응답하지 않아 스트림을 종료합니다"
DEFAULT_STREAM_EVENT_QUEUE_SIZE = 128
STREAM_BUSY_ERROR_MESSAGE = (
    "현재 추천 계산 요청이 많아 잠시 후 다시 시도해 주세요"
)


def _getenv_positive_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return max(1, parsed)


_DEFAULT_STREAM_WORKER_LIMIT = _getenv_positive_int(
    "TTS_STREAM_WORKERS",
    _getenv_positive_int(
        "TTS_NAVER_SESSION_POOL_SIZE",
        _getenv_positive_int("TTS_RECOMMEND_WORKERS", 2),
    ),
)
_STREAM_WORKER_SEMAPHORE = threading.BoundedSemaphore(_DEFAULT_STREAM_WORKER_LIMIT)


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Not JSON serializable: {type(value)!r}")


def sse_encode(event: str, data: object) -> str:
    payload = json.dumps(data, ensure_ascii=False, default=_json_default)
    return f"event: {event}\ndata: {payload}\n\n"


class CompletedStreamWorker:
    def is_alive(self) -> bool:
        return False


def make_stream_queue() -> queue.Queue[object]:
    return queue.Queue(
        maxsize=max(
            2,
            _getenv_positive_int(
                "TTS_STREAM_EVENT_QUEUE_SIZE",
                DEFAULT_STREAM_EVENT_QUEUE_SIZE,
            ),
        ),
    )


def put_stream_item(
    event_queue: queue.Queue[object],
    item: object,
    *,
    preserve: bool = True,
) -> bool:
    try:
        event_queue.put_nowait(item)
        return True
    except queue.Full:
        if not preserve:
            return False
        try:
            event_queue.get_nowait()
        except queue.Empty:
            return False
        try:
            event_queue.put_nowait(item)
            return True
        except queue.Full:
            return False


def start_bounded_stream_worker(
    *,
    target: Callable[[], None],
    event_queue: queue.Queue[object],
    done_marker: object,
    thread_name: str,
    capacity_error_detail: str = STREAM_BUSY_ERROR_MESSAGE,
) -> CompletedStreamWorker | threading.Thread:
    acquired = _STREAM_WORKER_SEMAPHORE.acquire(blocking=False)
    if not acquired:
        put_stream_item(
            event_queue,
            {
                "event": "busy",
                "data": {
                    "detail": capacity_error_detail,
                    "reason": "capacity_exhausted",
                },
            },
        )
        put_stream_item(event_queue, done_marker)
        return CompletedStreamWorker()

    def _run_with_capacity_release() -> None:
        try:
            target()
        finally:
            _STREAM_WORKER_SEMAPHORE.release()

    worker = threading.Thread(
        target=_run_with_capacity_release,
        name=thread_name,
        daemon=True,
    )
    worker.start()
    return worker


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
                    "data": {
                        "detail": STREAM_STALL_ERROR_MESSAGE,
                        "reason": STREAM_STALL_REASON,
                    },
                }
                break
            continue

        idle_deadline = time.monotonic() + idle_timeout_seconds
        if item is done_marker:
            break
        yield item
