from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import sys
import threading
from contextlib import closing
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.util import module_from_spec, spec_from_file_location
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from trip_time_service.accounts import AccountStore
from trip_time_service.api.routes_accounts import configured_account_origin, router

_smoke_file = Path(__file__).resolve().parents[1] / "deploy" / "enm" / "smoke.py"
_smoke_spec = spec_from_file_location("release_smoke", _smoke_file)
assert _smoke_spec is not None and _smoke_spec.loader is not None
smoke = module_from_spec(_smoke_spec)
_smoke_spec.loader.exec_module(smoke)

VERSION = "v0.4.3.0"
PRIVATE_VALUE = "response-value-excluded-from-release-evidence"
ACCOUNT_ID = "9f0c8baa40554aa5ba03db052da6a9f80"
OTHER_ACCOUNT_ID = "07ac7dbf7d334ca29bd5f4e18a68ffee"
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
                *(["--check-accounts"] if version != VERSION else []),
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
    assert "accounts" not in checks
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


def test_account_release_smoke_uses_the_real_secure_session(
    monkeypatch, tmp_path
) -> None:
    origin = "https://trip-time-smoke.test"
    database = tmp_path / "accounts.sqlite3"
    app = FastAPI()
    app.state.account_store = AccountStore(database)
    app.state.account_allowed_origin = configured_account_origin(origin)
    app.include_router(router)
    make_client = httpx.AsyncClient
    mutation_ids: list[bytes] = []

    async def record_mutation_id(scope, receive, send) -> None:
        if account_id := dict(scope["headers"]).get(b"x-triptime-account"):
            mutation_ids.append(account_id)
        await app(scope, receive, send)

    async def check() -> dict:
        transport = httpx.ASGITransport(app=record_mutation_id)

        def in_process_client(**options):
            return make_client(transport=transport, **options)

        monkeypatch.setattr(httpx, "AsyncClient", in_process_client)
        return await smoke._check_accounts(origin)

    report = asyncio.run(check())
    assert report["ok"] and report["roundtrip"]
    assert report["cleanup"] == "deleted"
    assert len(mutation_ids) == 3 and len(set(mutation_ids)) == 1
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute("SELECT count(*) FROM accounts").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM sessions").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM favorites").fetchone() == (0,)


@pytest.mark.parametrize(
    ("case", "stage", "bucket", "cleanup"),
    [
        ("success", None, None, "deleted"),
        ("duplicate", "register", "http_error", "not_needed"),
        ("lost_registration_reply", "register", "network_error", "deleted"),
        ("registration_timeout", "register", "request_timeout", "deleted"),
        ("lost_cookie", "save_favorite", "http_error", "deleted"),
        ("cleanup_failed", "save_favorite", "http_error", "unconfirmed"),
        ("foreign_recovery", "register", "network_error", "unconfirmed"),
        ("changed_id_at_login", "login", "account_state_mismatch", "unconfirmed"),
        ("changed_id_in_state", "verify_favorite", "account_state_mismatch", "deleted"),
        ("incorrect_favorite", "verify_favorite", "favorite_mismatch", "deleted"),
        ("redirect", "save_favorite", "http_error", "deleted"),
        ("wrong_provider", None, "provider_mismatch", None),
    ],
)
def test_release_smoke_checks_private_account_roundtrip(
    monkeypatch, tmp_path, capsys, case, stage, bucket, cleanup
) -> None:
    calls: list[tuple[str, str]] = []
    credentials: dict[str, str] = {}
    favorites: list[dict] = []
    session_token = ""
    deleted = False
    release_registration = threading.Event()
    if case == "registration_timeout":
        monkeypatch.setattr(smoke, "ACCOUNT_REQUEST_SECONDS", 0.5)

    class Handler(BaseHTTPRequestHandler):
        def reply(self, status: int, result=None, cookie: str | None = None) -> None:
            body = b"" if result is None else json.dumps(result).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if cookie is not None:
                self.send_header("Set-Cookie", cookie)
            if status == 307:
                self.send_header(
                    "Location", f"http://localhost:{self.server.server_port}/unexpected"
                )
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:
            pass

        def snapshot(self, signed_in: bool) -> dict:
            return {
                "user": {"id": ACCOUNT_ID, "username": credentials["username"]}
                if signed_in
                else None,
                "favorites": favorites if signed_in else [],
            }

        def authed(self) -> bool:
            return bool(session_token) and (
                self.headers.get("Cookie") == f"tts_session={session_token}"
            )

        def do_GET(self) -> None:
            calls.append(("GET", self.path))
            if self.path == "/api/config":
                provider = "fixture" if case == "wrong_provider" else smoke.PROVIDER
                self.reply(200, {"version": VERSION, "provider": provider})
                return
            assert self.path == "/api/auth/me"
            snapshot = self.snapshot(self.authed())
            if case == "incorrect_favorite" and self.authed():
                snapshot["favorites"] = [{**favorites[0], "lon": 127.0}]
            if case == "changed_id_in_state" and self.authed():
                snapshot["user"]["id"] = OTHER_ACCOUNT_ID
            self.reply(200, snapshot)

        def do_POST(self) -> None:
            self.mutate()

        def do_DELETE(self) -> None:
            self.mutate()

        def mutate(self) -> None:
            nonlocal session_token, deleted
            calls.append((self.command, self.path))
            assert (
                self.headers["Origin"] == f"http://127.0.0.1:{self.server.server_port}"
            )
            assert self.headers["X-Requested-With"] == "TripTime"
            body = self.rfile.read(int(self.headers["Content-Length"]))
            payload = json.loads(body) if body else None
            if self.path == "/api/auth/register":
                credentials.update(payload)
                assert re.fullmatch(r"[a-z0-9_]{3,32}", credentials["username"])
                assert len(credentials["password"]) >= 32
                if case == "duplicate":
                    self.reply(409, {"detail": PRIVATE_VALUE})
                    return
                if case in {"lost_registration_reply", "foreign_recovery"}:
                    return
                if case == "registration_timeout":
                    release_registration.wait(timeout=3)
                    return
                session_token = "registration-session-test-token"
                self.reply(
                    201, self.snapshot(True), f"tts_session={session_token}; Path=/"
                )
                return
            if self.path == "/api/auth/login":
                release_registration.set()
                assert self.headers.get("Cookie") is None
                if payload != credentials:
                    raise AssertionError("unexpected recovery credentials")
                if case == "cleanup_failed":
                    self.reply(429, {"detail": PRIVATE_VALUE})
                    return
                session_token = "login-session-test-token"
                snapshot = self.snapshot(True)
                if case == "foreign_recovery":
                    snapshot["user"]["username"] = "foreign_account"
                if case == "changed_id_at_login":
                    snapshot["user"]["id"] = OTHER_ACCOUNT_ID
                self.reply(200, snapshot, f"tts_session={session_token}; Path=/")
                return
            assert self.headers["X-TripTime-Account"] == ACCOUNT_ID
            if not self.authed():
                self.reply(401)
                return
            if self.path == "/api/favorites":
                if case in {"lost_cookie", "cleanup_failed"}:
                    session_token = ""
                    self.reply(503, {"detail": PRIVATE_VALUE})
                elif case == "redirect":
                    self.reply(307)
                else:
                    assert payload == smoke.ACCOUNT_FAVORITE
                    favorites.append({"id": 1, **payload})
                    self.reply(201, self.snapshot(True))
            elif self.path == "/api/auth/logout":
                session_token = ""
                self.reply(204, cookie="tts_session=; Max-Age=0; Path=/")
            else:
                assert self.command == "DELETE" and self.path == "/api/auth/account"
                if payload != {"password": credentials["password"]}:
                    raise AssertionError("unexpected password confirmation")
                deleted = True
                favorites.clear()
                session_token = ""
                self.reply(204, cookie="tts_session=; Max-Age=0; Path=/")

    def skip_provider_call(*_args, **_kwargs) -> dict:
        return {}

    artifact = tmp_path / "account-release-smoke.json"
    monkeypatch.setattr(smoke, "_check_autocomplete", skip_provider_call)
    monkeypatch.setattr(smoke, "_check_trip", skip_provider_call)
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "smoke.py",
                "--base-url",
                f"http://127.0.0.1:{server.server_port}/",
                "--expected-version",
                VERSION,
                "--check-accounts",
                "--artifact",
                str(artifact),
            ],
        )
        try:
            assert smoke.main() == (0 if bucket is None else 1)
        finally:
            server.shutdown()
            thread.join(timeout=2)

    raw = artifact.read_text(encoding="utf-8")
    captured = capsys.readouterr()
    assert captured.out == raw
    assert not captured.err
    report = json.loads(raw)
    assert report["ok"] is (bucket is None)
    for value in (
        *credentials.values(),
        PRIVATE_VALUE,
        ACCOUNT_ID,
        OTHER_ACCOUNT_ID,
        "foreign_account",
        "session-test-token",
        "tts_session",
        smoke.ACCOUNT_FAVORITE["name"],
        smoke.ACCOUNT_FAVORITE["address"],
    ):
        assert value not in raw and json.dumps(value).strip('"') not in raw
    if case == "wrong_provider":
        assert calls == [("GET", "/api/config")]
        assert report["checks"]["config"]["bucket"] == bucket
        assert "accounts" not in report["checks"]
        return

    result = report["checks"]["accounts"]
    assert result["ok"] is (bucket is None)
    assert result.get("stage") == stage
    assert result.get("bucket") == bucket
    assert result["cleanup"] == cleanup
    assert result["elapsed_ms"] >= 0
    assert deleted is (cleanup == "deleted")
    if case == "success":
        assert result["roundtrip"] is True
        assert calls == [
            ("GET", "/api/config"),
            ("POST", "/api/auth/register"),
            ("POST", "/api/favorites"),
            ("POST", "/api/auth/logout"),
            ("GET", "/api/auth/me"),
            ("POST", "/api/auth/login"),
            ("GET", "/api/auth/me"),
            ("DELETE", "/api/auth/account"),
        ]
    elif case == "duplicate":
        assert calls == [("GET", "/api/config"), ("POST", "/api/auth/register")]
    elif case in {
        "lost_registration_reply",
        "registration_timeout",
        "lost_cookie",
        "changed_id_in_state",
    }:
        assert calls[-2:] == [
            ("POST", "/api/auth/login"),
            ("DELETE", "/api/auth/account"),
        ]
    elif case == "cleanup_failed":
        assert calls[-1] == ("POST", "/api/auth/login")
        assert result["http_status"] == 503
        assert result["cleanup_http_status"] == 429
    elif case in {"foreign_recovery", "changed_id_at_login"}:
        assert calls[-1] == ("POST", "/api/auth/login")
        assert ("DELETE", "/api/auth/account") not in calls
        assert result["cleanup_bucket"] == "account_state_mismatch"
    elif case == "redirect":
        assert all(path != "/unexpected" for _, path in calls)
