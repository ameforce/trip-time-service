from __future__ import annotations

import logging
import os
import socket

import uvicorn

from trip_time_service.config import _getenv_bool, _getenv_int

logger = logging.getLogger("trip_time_service")

_DEFAULT_PORT = 8500
_FALLBACK_RANGE = range(8500, 8600)


def _is_port_available(host: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, port))
        return True
    except OSError:
        return False


def _reserve_ephemeral_port(host: str) -> int:
    bind_host = host
    if host in {"0.0.0.0", "::"}:
        # Wildcard host uses an empty bind target.
        bind_host = ""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((bind_host, 0))
        return int(sock.getsockname()[1])


def _find_available_port(host: str, preferred: int) -> int:
    if _is_port_available(host, preferred):
        return preferred

    logger.warning(
        "포트 %d 사용 불가 (OS 예약 또는 점유). 대체 포트 탐색 중...",
        preferred,
    )

    for port in _FALLBACK_RANGE:
        if port != preferred and _is_port_available(host, port):
            logger.info("대체 포트 %d 사용", port)
            return port

    fallback_port = _reserve_ephemeral_port(host)
    logger.warning(
        "포트 %d 및 fallback 범위 %d-%d 모두 사용 불가. "
        "OS 임시 가용 포트 %d를 사용합니다.",
        preferred,
        _FALLBACK_RANGE.start,
        _FALLBACK_RANGE.stop - 1,
        fallback_port,
    )
    return fallback_port


def main() -> None:
    logging.basicConfig(level=logging.INFO)

    host = os.getenv("TTS_HOST", "127.0.0.1")
    port = _getenv_int("TTS_PORT", _DEFAULT_PORT)
    reload = _getenv_bool("TTS_RELOAD", True)

    port = _find_available_port(host, port)

    uvicorn.run(
        "trip_time_service.api.main:app",
        host=host,
        port=port,
        reload=reload,
    )


if __name__ == "__main__":
    main()
