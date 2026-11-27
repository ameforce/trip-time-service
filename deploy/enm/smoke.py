"""Check the newly deployed public Trip Time API and record safe evidence."""

from __future__ import annotations

import argparse
import http.client
import json
import queue
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

PROVIDER = "naver_playwright"
KST = timezone(timedelta(hours=9))
MAX_BODY_BYTES = 2 * 1024 * 1024
VERSION_PATTERN = re.compile(r"v\d+\.\d+\.\d+\.\d+(?:-[a-zA-Z0-9._-]+)?")
PROVIDER_BUCKETS = {
    "coords_unresolved",
    "ncaptcha_backoff",
    "panel_parse_timeout",
    "provider_retry_exhausted",
}
STREAM_REASONS = {
    "capacity_exhausted",
    "no_feasible_departure",
    "provider_degraded",
    "stream_stall_timeout",
    "worker_failed",
}
ROUTE = {
    "origin": "서울역",
    "origin_coords": {"lat": 37.554648, "lon": 126.970633},
    "destination": "서울시청",
    "dest_coords": {"lat": 37.566295, "lon": 126.977945},
}


class SmokeFailure(Exception):
    def __init__(self, bucket: str, **evidence: str | int) -> None:
        super().__init__(bucket)
        self.evidence = {"bucket": bucket, **evidence}


def _object(value: object) -> dict:
    if not isinstance(value, dict):
        raise SmokeFailure("invalid_response")
    return value


def _json_body(response: http.client.HTTPResponse) -> object:
    data = response.read(MAX_BODY_BYTES + 1)
    if len(data) > MAX_BODY_BYTES:
        raise SmokeFailure("response_too_large")
    return json.loads(data.decode("utf-8"))


def _response(
    conn: http.client.HTTPConnection,
    path: str,
    *,
    payload: dict | None = None,
) -> http.client.HTTPResponse:
    headers = {
        "Accept": "application/json" if payload is None else "text/event-stream",
        "Cache-Control": "no-cache",
    }
    if payload is None:
        conn.request("GET", path, headers=headers)
    else:
        headers["Content-Type"] = "application/json"
        conn.request("POST", path, json.dumps(payload).encode("utf-8"), headers)
    response = conn.getresponse()
    if response.status != 200:
        response.close()
        raise SmokeFailure("http_error", http_status=response.status)
    return response


def _check_config(conn: http.client.HTTPConnection, expected_version: str) -> dict:
    with _response(conn, "/api/config") as response:
        config = _object(_json_body(response))
    version = config.get("version")
    if version != expected_version:
        evidence = {}
        if (
            isinstance(version, str)
            and len(version) <= 128
            and VERSION_PATTERN.fullmatch(version)
        ):
            evidence["observed_version"] = version
        raise SmokeFailure("version_mismatch", **evidence)
    if config.get("provider") != PROVIDER:
        raise SmokeFailure("provider_mismatch")
    return {"version": version, "provider": PROVIDER}


def _check_autocomplete(conn: http.client.HTTPConnection) -> dict:
    query = ROUTE["origin"]
    with _response(conn, "/api/autocomplete?" + urlencode({"q": query})) as response:
        if 'desc="live"' not in response.getheader("Server-Timing", ""):
            raise SmokeFailure("autocomplete_mode_mismatch")
        results = _json_body(response)
    if not isinstance(results, list):
        raise SmokeFailure("invalid_response")
    matches = [
        item
        for value in results
        if (item := _object(value)).get("coords_ready") is True
        and isinstance(item.get("source"), str)
        and item["source"].startswith("naver_")
        and query in str(item.get("canonical_query", ""))
    ]
    if not matches:
        raise SmokeFailure("autocomplete_missing_match", result_count=len(results))
    return {"result_count": len(results), "matching_count": len(matches)}


def _events(response: http.client.HTTPResponse):
    event = ""
    data: list[str] = []
    remaining = MAX_BODY_BYTES
    while raw_line := response.readline(remaining + 1):
        remaining -= len(raw_line)
        if remaining < 0:
            raise SmokeFailure("response_too_large")
        line = raw_line.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                yield event, _object(json.loads("\n".join(data)))
            event, data = "", []
        elif line.startswith("event:"):
            event = line[6:].lstrip(" ")
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))


def _route_result(data: dict, departure_key: str, arrival_key: str) -> None:
    duration = data.get("duration_seconds")
    if data.get("provider") != PROVIDER:
        raise SmokeFailure("provider_mismatch")
    if type(duration) is not int or duration <= 0:
        raise SmokeFailure("invalid_route_result")
    try:
        departure = datetime.fromisoformat(data[departure_key])
        arrival = datetime.fromisoformat(data[arrival_key])
        if departure.tzinfo is None or arrival.tzinfo is None or arrival <= departure:
            raise SmokeFailure("invalid_route_result")
    except (KeyError, TypeError, ValueError) as exc:
        raise SmokeFailure("invalid_route_result") from exc


def _check_trip(
    conn: http.client.HTTPConnection,
    *,
    depart_at: bool,
    requested_time: datetime,
) -> dict:
    if depart_at:
        entrypoint = "arrival-time-with-recommendation"
        time_key = "departure_time"
    else:
        entrypoint = "recommended-departure-time"
        time_key = "desired_arrival_time"
    payload = {**ROUTE, time_key: requested_time.isoformat()}
    saw_arrival = saw_recommendation = False
    with _response(conn, f"/v1/trip/{entrypoint}/stream", payload=payload) as response:
        media_type = response.getheader("Content-Type", "").partition(";")[0].strip()
        if media_type != "text/event-stream":
            raise SmokeFailure("stream_content_type")
        for event, data in _events(response):
            if event in ("busy", "error", "timeout") or (
                event == "end" and data.get("ok") is not True
            ):
                bucket = data.get("provider_bucket")
                reason = data.get("reason")
                if isinstance(bucket, str) and bucket in PROVIDER_BUCKETS:
                    raise SmokeFailure(bucket)
                if isinstance(reason, str) and reason in STREAM_REASONS:
                    raise SmokeFailure(reason)
                raise SmokeFailure("stream_failed")
            if event == "arrival" and depart_at:
                if saw_arrival:
                    raise SmokeFailure("duplicate_final_event")
                _route_result(
                    _object(data.get("arrival")), "departure_time", "arrival_time"
                )
                saw_arrival = True
            if event == "recommendation":
                if saw_recommendation:
                    raise SmokeFailure("duplicate_final_event")
                _route_result(
                    data, "recommended_departure_time", "expected_arrival_time"
                )
                saw_recommendation = True
            if event == "end":
                if not saw_recommendation or (depart_at and not saw_arrival):
                    raise SmokeFailure("missing_final_event")
                return {
                    "provider": PROVIDER,
                    "arrival_received": saw_arrival,
                    "recommendation_received": saw_recommendation,
                }
    if not saw_recommendation or (depart_at and not saw_arrival):
        raise SmokeFailure("missing_final_event")
    raise SmokeFailure("missing_end_event")


def _run_check(
    connect: Callable[[int], http.client.HTTPConnection],
    check: Callable[[http.client.HTTPConnection], dict],
    deadline: int,
) -> dict:
    result: queue.Queue[dict] = queue.Queue(maxsize=1)

    def run() -> None:
        conn = connect(min(deadline, 60))
        try:
            outcome = {"ok": True, **check(conn)}
        except SmokeFailure as exc:
            outcome = {"ok": False, **exc.evidence}
        except TimeoutError:
            outcome = {"ok": False, "bucket": "request_timeout"}
        except (OSError, http.client.HTTPException):
            outcome = {"ok": False, "bucket": "network_error"}
        except (UnicodeError, ValueError):
            outcome = {"ok": False, "bucket": "invalid_response"}
        finally:
            conn.close()
        result.put(outcome)

    # Each socket has an inactivity timeout. Also bound total elapsed time,
    # since a streaming server can keep a socket active without finishing.
    started = time.monotonic()
    threading.Thread(target=run, daemon=True).start()
    try:
        outcome = result.get(timeout=deadline)
    except queue.Empty:
        outcome = {"ok": False, "bucket": "request_deadline"}
    return {**outcome, "elapsed_ms": round((time.monotonic() - started) * 1000)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--artifact", required=True, type=Path)
    args = parser.parse_args()
    base = urlsplit(args.base_url)
    if (
        base.scheme not in ("https", "http")
        or not base.hostname
        or base.username is not None
        or base.path not in ("", "/")
        or base.query
        or base.fragment
    ):
        parser.error("--base-url must be an HTTP(S) origin")
    if (
        len(args.expected_version) > 128
        or not VERSION_PATTERN.fullmatch(args.expected_version)
    ):
        parser.error("--expected-version must be the rendered deployment version")
    connection_cls = (
        http.client.HTTPSConnection
        if base.scheme == "https"
        else http.client.HTTPConnection
    )

    def connect(timeout: int) -> http.client.HTTPConnection:
        return connection_cls(base.hostname, base.port, timeout=timeout)

    tomorrow = datetime.now(KST).replace(hour=12, minute=0, second=0, microsecond=0)
    tomorrow += timedelta(days=1)
    checks = {
        "config": _run_check(
            connect, lambda conn: _check_config(conn, args.expected_version), 20
        )
    }
    if checks["config"]["ok"]:
        checks["autocomplete"] = _run_check(connect, _check_autocomplete, 30)
        checks["depart_at"] = _run_check(
            connect,
            lambda conn: _check_trip(conn, depart_at=True, requested_time=tomorrow),
            240,
        )
        checks["arrive_by"] = _run_check(
            connect,
            lambda conn: _check_trip(
                conn, depart_at=False, requested_time=tomorrow + timedelta(hours=4)
            ),
            240,
        )
    ok = all(check["ok"] for check in checks.values())
    report = {"ok": ok, "expected_version": args.expected_version, "checks": checks}
    rendered = json.dumps(report, indent=2) + "\n"
    args.artifact.parent.mkdir(parents=True, exist_ok=True)
    args.artifact.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
