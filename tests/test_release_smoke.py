from __future__ import annotations

import json
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.util import module_from_spec, spec_from_file_location
from io import BytesIO
from pathlib import Path

import pytest

_smoke_file = Path(__file__).resolve().parents[1] / "deploy" / "enm" / "smoke.py"
_smoke_spec = spec_from_file_location("release_smoke", _smoke_file)
assert _smoke_spec is not None and _smoke_spec.loader is not None
smoke = module_from_spec(_smoke_spec)
_smoke_spec.loader.exec_module(smoke)

VERSION = "v0.4.3.0"
PRIVATE_VALUE = "response-value-excluded-from-release-evidence"
RECOMMENDATION = {
    "provider": smoke.PROVIDER,
    "duration_seconds": 650,
    "recommended_departure_time": "2026-11-29T12:00:00+09:00",
    "expected_arrival_time": "2026-11-29T12:11:00+09:00",
    "route": {"origin": PRIVATE_VALUE},
}
ARRIVAL = {
    "provider": smoke.PROVIDER,
    "duration_seconds": 650,
    "departure_time": "2026-11-29T12:00:00+09:00",
    "arrival_time": "2026-11-29T12:11:00+09:00",
    "route": {"destination": PRIVATE_VALUE},
}


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


@pytest.mark.parametrize(
    ("version", "trip_failure", "expected_bucket"),
    [
        (VERSION, None, None),
        ("v0.4.2.0", None, "version_mismatch"),
        (VERSION, "missing_final", "missing_final_event"),
        (VERSION, "captcha", "ncaptcha_backoff"),
        (VERSION, "bad_result", "invalid_route_result"),
        (VERSION, "no_end", "missing_end_event"),
    ],
)
def test_release_smoke_reports_bounded_public_api_evidence(
    monkeypatch, tmp_path, version, trip_failure, expected_bucket
) -> None:
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def reply(self, media_type: str, body: bytes, **headers: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", media_type)
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            requests.append(("GET", self.path, None))
            if self.path == "/api/config":
                data = {
                    "version": version,
                    "provider": smoke.PROVIDER,
                    "naver_map_client_id": PRIVATE_VALUE,
                }
                self.reply("application/json", json.dumps(data).encode("utf-8"))
            else:
                data = [
                    {
                        "coords_ready": True,
                        "source": "naver_browser",
                        "canonical_query": f"{smoke.ROUTE['origin']} {PRIVATE_VALUE}",
                    }
                ]
                self.reply(
                    "application/json",
                    json.dumps(data).encode("utf-8"),
                    **{"Server-Timing": 'autocomplete;dur=25.0;desc="live"'},
                )

        def do_POST(self) -> None:
            content_length = int(self.headers["Content-Length"])
            request_data = json.loads(self.rfile.read(content_length))
            requests.append(("POST", self.path, request_data))
            body = _sse("plan", {"planned": 4})
            if self.path.endswith("/arrival-time-with-recommendation/stream"):
                body += _sse("arrival", {"arrival": ARRIVAL})
            if trip_failure == "captcha":
                body += _sse(
                    "error",
                    {
                        "provider_bucket": "ncaptcha_backoff",
                        "reason": "provider_degraded",
                        "detail": PRIVATE_VALUE,
                    },
                )
            elif trip_failure == "bad_result":
                body += _sse(
                    "recommendation", {**RECOMMENDATION, "duration_seconds": 0}
                )
            elif trip_failure != "missing_final":
                body += _sse("recommendation", RECOMMENDATION)
            if trip_failure != "no_end":
                body += _sse("end", {"ok": True})
            self.reply("text/event-stream; charset=utf-8", body)

    artifact = tmp_path / "release-smoke.json"
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "smoke.py",
                "--base-url",
                f"http://127.0.0.1:{server.server_port}",
                "--expected-version",
                VERSION,
                "--artifact",
                str(artifact),
            ],
        )
        try:
            assert smoke.main() == (1 if expected_bucket else 0)
        finally:
            server.shutdown()
            server_thread.join(timeout=2)

    raw_report = artifact.read_text(encoding="utf-8")
    report = json.loads(raw_report)
    assert PRIVATE_VALUE not in raw_report
    assert smoke.ROUTE["origin"] not in raw_report
    assert report["ok"] is (expected_bucket is None)
    assert report["expected_version"] == VERSION
    checks = report["checks"]
    if version != VERSION:
        assert requests == [("GET", "/api/config", None)]
        assert checks["config"]["bucket"] == "version_mismatch"
        assert checks["config"]["observed_version"] == version
        return
    assert checks["config"]["version"] == VERSION
    assert checks["autocomplete"]["matching_count"] == 1
    assert [method for method, _, _ in requests] == ["GET", "GET", "POST", "POST"]
    assert requests[1][1].startswith("/api/autocomplete?q=")
    assert requests[2][1] == "/v1/trip/arrival-time-with-recommendation/stream"
    assert requests[3][1] == "/v1/trip/recommended-departure-time/stream"
    for (_, _, payload), field in zip(
        requests[2:], ("departure_time", "desired_arrival_time"), strict=True
    ):
        assert payload["origin_coords"] == smoke.ROUTE["origin_coords"]
        requested = datetime.fromisoformat(payload[field])
        assert requested.tzinfo == smoke.KST
        assert requested > datetime.now(smoke.KST)
    for mode in ("depart_at", "arrive_by"):
        if expected_bucket:
            assert checks[mode]["bucket"] == expected_bucket
        else:
            assert checks[mode]["recommendation_received"] is True
            first = checks[mode]["first_usable_ms"]
            recommendation = checks[mode]["recommendation_ms"]
            assert 0 <= first <= recommendation <= checks[mode]["elapsed_ms"]
    if not expected_bucket:
        assert checks["depart_at"]["arrival_received"] is True


def test_stream_reader_limits_unterminated_response_body() -> None:
    oversized_line = BytesIO(b"x" * (smoke.MAX_BODY_BYTES + 1))
    with pytest.raises(smoke.SmokeFailure, match="response_too_large"):
        list(smoke._events(oversized_line))
