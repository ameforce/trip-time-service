"""Check the newly deployed public Trip Time API and record safe evidence."""

from __future__ import annotations

import argparse
import asyncio
import http.client
import json
import queue
import re
import secrets
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
ACCOUNT_REQUEST_SECONDS = 8
MAX_ACCOUNT_BODY_BYTES = 64 * 1024
ACCOUNT_FAVORITE = {
    "name": "서울역",
    "address": "서울특별시 용산구 한강대로 405",
    **ROUTE["origin_coords"],
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
    started = time.monotonic()
    arrival_ms: int | None = None
    recommendation_ms: int | None = None
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
                if arrival_ms is not None:
                    raise SmokeFailure("duplicate_final_event")
                _route_result(
                    _object(data.get("arrival")), "departure_time", "arrival_time"
                )
                arrival_ms = round((time.monotonic() - started) * 1000)
            if event == "recommendation":
                if recommendation_ms is not None:
                    raise SmokeFailure("duplicate_final_event")
                _route_result(
                    data, "recommended_departure_time", "expected_arrival_time"
                )
                recommendation_ms = round((time.monotonic() - started) * 1000)
            if event == "end":
                if recommendation_ms is None or (depart_at and arrival_ms is None):
                    raise SmokeFailure("missing_final_event")
                return {
                    "provider": PROVIDER,
                    "arrival_received": arrival_ms is not None,
                    "recommendation_received": True,
                    "first_usable_ms": (
                        recommendation_ms
                        if arrival_ms is None
                        else min(arrival_ms, recommendation_ms)
                    ),
                    "recommendation_ms": recommendation_ms,
                }
    if recommendation_ms is None or (depart_at and arrival_ms is None):
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


def _account_snapshot(snapshot: object) -> tuple[object, list]:
    data = _object(snapshot)
    favorites = data.get("favorites")
    if "user" not in data or not isinstance(favorites, list):
        raise SmokeFailure("account_state_mismatch")
    return data["user"], favorites


def _signed_in_snapshot(
    snapshot: object,
    username: str,
    expected_id: str | None = None,
) -> tuple[str, list]:
    user, favorites = _account_snapshot(snapshot)
    if not isinstance(user, dict) or user.get("username") != username:
        raise SmokeFailure("account_state_mismatch")
    account_id = user.get("id")
    if (
        not isinstance(account_id, str)
        or re.fullmatch(r"[!-~]+", account_id) is None
        or (expected_id is not None and account_id != expected_id)
    ):
        raise SmokeFailure("account_state_mismatch")
    return account_id, favorites


def _favorite_id(favorites: list) -> int:
    if len(favorites) != 1:
        raise SmokeFailure("favorite_mismatch")
    favorite = _object(favorites[0])
    favorite_id = favorite.get("id")
    if type(favorite_id) is not int or any(
        favorite.get(key) != value for key, value in ACCOUNT_FAVORITE.items()
    ):
        raise SmokeFailure("favorite_mismatch")
    return favorite_id


async def _check_accounts(origin: str) -> dict:
    # The account check is opt-in for callers without the development dependencies.
    import httpx

    started = time.monotonic()
    username = "smoke_" + secrets.token_hex(12)
    password = "Aa1!" + secrets.token_urlsafe(32)
    credentials = {"username": username, "password": password}

    def open_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=origin,
            trust_env=False,
            follow_redirects=False,
            timeout=ACCOUNT_REQUEST_SECONDS,
        )

    async def request(
        client: httpx.AsyncClient,
        method: str,
        path: str,
        status: int,
        *,
        payload: dict | None = None,
        account_id: str | None = None,
        snapshot: bool = False,
    ) -> object:
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache",
        }
        if method != "GET":
            headers.update({"Origin": origin, "X-Requested-With": "TripTime"})
        if account_id is not None:
            headers["X-TripTime-Account"] = account_id
        try:
            async with (
                asyncio.timeout(ACCOUNT_REQUEST_SECONDS),
                client.stream(method, path, headers=headers, json=payload) as response,
            ):
                if response.status_code != status:
                    raise SmokeFailure("http_error", http_status=response.status_code)
                if not snapshot:
                    return None
                body = bytearray()
                async for chunk in response.aiter_raw():
                    if len(body) + len(chunk) > MAX_ACCOUNT_BODY_BYTES:
                        raise SmokeFailure("response_too_large")
                    body.extend(chunk)
                return json.loads(body.decode("utf-8"))
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise SmokeFailure("request_timeout") from exc
        except (OSError, httpx.HTTPError) as exc:
            raise SmokeFailure("network_error") from exc
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise SmokeFailure("invalid_response") from exc

    async def delete(client: httpx.AsyncClient, account_id: str) -> None:
        await request(
            client,
            "DELETE",
            "/api/auth/account",
            204,
            payload={"password": password},
            account_id=account_id,
        )

    async def cleanup(
        client: httpx.AsyncClient | None, known_id: str | None
    ) -> None:
        if client is not None and known_id is not None:
            try:
                await delete(client, known_id)
                return
            except SmokeFailure:
                pass
        async with open_client() as recovery:
            logged_in = await request(
                recovery,
                "POST",
                "/api/auth/login",
                200,
                payload=credentials,
                snapshot=True,
            )
            recovered_id, _ = _signed_in_snapshot(logged_in, username, known_id)
            await delete(recovery, recovered_id)

    async with open_client() as original, open_client() as fresh:
        stage = "register"
        account_id: str | None = None
        active: httpx.AsyncClient | None = None
        # Registration can commit before the client receives the response.
        cleanup_required = True
        outcome: dict = {"ok": True}
        try:
            registered = await request(
                original,
                "POST",
                "/api/auth/register",
                201,
                payload=credentials,
                snapshot=True,
            )
            account_id, favorites = _signed_in_snapshot(registered, username)
            active = original
            if favorites:
                raise SmokeFailure("account_state_mismatch")

            stage = "save_favorite"
            saved = await request(
                original,
                "POST",
                "/api/favorites",
                201,
                payload=ACCOUNT_FAVORITE,
                account_id=account_id,
                snapshot=True,
            )
            active = None
            _, favorites = _signed_in_snapshot(saved, username, account_id)
            active = original
            favorite_id = _favorite_id(favorites)

            stage = "logout"
            await request(
                original,
                "POST",
                "/api/auth/logout",
                204,
                account_id=account_id,
            )
            active = None
            stage = "verify_anonymous"
            guest = await request(original, "GET", "/api/auth/me", 200, snapshot=True)
            user, favorites = _account_snapshot(guest)
            if user is not None or favorites:
                raise SmokeFailure("account_state_mismatch")

            stage = "login"
            logged_in = await request(
                fresh,
                "POST",
                "/api/auth/login",
                200,
                payload=credentials,
                snapshot=True,
            )
            _signed_in_snapshot(logged_in, username, account_id)
            active = fresh
            stage = "verify_favorite"
            persisted = await request(fresh, "GET", "/api/auth/me", 200, snapshot=True)
            active = None
            _, favorites = _signed_in_snapshot(persisted, username, account_id)
            active = fresh
            if _favorite_id(favorites) != favorite_id:
                raise SmokeFailure("favorite_mismatch")

            stage = "delete_account"
            await delete(fresh, account_id)
            cleanup_required = False
            outcome.update(roundtrip=True, cleanup="deleted")
        except SmokeFailure as exc:
            outcome = {"ok": False, "stage": stage, **exc.evidence}
            if stage == "register" and exc.evidence.get("http_status") == 409:
                cleanup_required = False
                outcome["cleanup"] = "not_needed"
        finally:
            if cleanup_required:
                try:
                    await cleanup(
                        None if stage == "delete_account" else active, account_id
                    )
                    outcome["cleanup"] = "deleted"
                except SmokeFailure as exc:
                    outcome["cleanup"] = "unconfirmed"
                    outcome["cleanup_bucket"] = exc.evidence["bucket"]
                    if "http_status" in exc.evidence:
                        outcome["cleanup_http_status"] = exc.evidence["http_status"]

    return {**outcome, "elapsed_ms": round((time.monotonic() - started) * 1000)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument(
        "--check-accounts",
        action="store_true",
        help="verify favorite persistence using a temporary account",
    )
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
        args.check_accounts
        and base.scheme == "http"
        and base.hostname not in {"localhost", "127.0.0.1", "::1"}
    ):
        parser.error("--check-accounts requires HTTPS or a loopback origin")
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
        if args.check_accounts:
            host = base.hostname
            host = f"[{host}]" if ":" in host else host.encode("idna").decode("ascii")
            default_port = 443 if base.scheme == "https" else 80
            port = "" if base.port in (None, default_port) else f":{base.port}"
            checks["accounts"] = asyncio.run(
                _check_accounts(f"{base.scheme}://{host}{port}")
            )
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
