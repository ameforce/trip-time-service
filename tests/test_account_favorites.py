from __future__ import annotations

import sqlite3
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Event

import pytest
from fastapi.testclient import TestClient

from trip_time_service import accounts
from trip_time_service.api.main import create_app
from trip_time_service.config import load_settings

_PASSWORD = " 정확히 그대로 보존되는 passphrase "
_WRONG_PASSWORD = "wrong password for this test"
_LOCAL_URL = "http://127.0.0.1"
_LOCAL_COOKIE = "tts_session"
_GUEST = {"user": None, "favorites": []}
_MUTATION = {"X-Requested-With": "TripTime"}


@pytest.fixture
def database_path(tmp_path, monkeypatch):
    path = tmp_path / "account-data" / "accounts.sqlite3"
    monkeypatch.setenv("TTS_ACCOUNT_DB_PATH", str(path))
    monkeypatch.delenv("TTS_ACCOUNT_ALLOWED_ORIGIN", raising=False)
    load_settings.cache_clear()
    yield path
    load_settings.cache_clear()


def _client(app=None, **kwargs) -> TestClient:
    return TestClient(app or create_app(), base_url=_LOCAL_URL, **kwargs)


def _account_headers(account_id: str) -> dict[str, str]:
    return {**_MUTATION, "X-TripTime-Account": account_id}


def _credentials(username: str, password: str = _PASSWORD) -> dict[str, str]:
    return {"username": username, "password": password}


def _sign_up(client: TestClient, username: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/register",
        headers=_MUTATION,
        json=_credentials(username),
    )
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    snapshot = response.json()
    assert snapshot["favorites"] == []
    assert isinstance(snapshot["user"]["id"], str)
    assert snapshot["user"]["id"]
    return snapshot["user"]


def _place(name: str, address: str = "정상 테스트 주소") -> dict[str, object]:
    return {"name": name, "address": address, "lat": 37.5, "lon": 127.1}


def _save(
    client: TestClient, account_id: str, place: dict[str, object]
) -> dict[str, object]:
    response = client.post(
        "/api/favorites", headers=_account_headers(account_id), json=place
    )
    assert response.status_code == 201
    return response.json()["favorites"][-1]


def test_guest_and_account_flow_persists_across_app_instances(database_path) -> None:
    client = _client()
    guest = client.get("/api/auth/me")
    assert guest.json() == _GUEST
    assert guest.headers["cache-control"] == "no-store"
    assert client.get("/api/favorites").status_code == 401
    guest_logout = client.post("/api/auth/logout", headers=_MUTATION)
    assert guest_logout.status_code == 204
    assert "Max-Age=0" in guest_logout.headers["set-cookie"]
    assert not database_path.exists()

    username = "사용자_a"
    user = _sign_up(client, unicodedata.normalize("NFD", "사용자_A"))
    assert user["username"] == username
    duplicate = client.post(
        "/api/auth/register",
        headers=_MUTATION,
        json=_credentials(username),
    )
    assert duplicate.status_code == 409
    assert client.get("/api/auth/me").json()["user"] == user
    place = _place("집")
    saved = _save(client, user["id"], place)
    assert saved == {"id": saved["id"], **place}

    previous_token = client.cookies[_LOCAL_COOKIE]
    restarted = _client(cookies={_LOCAL_COOKIE: previous_token})
    assert restarted.get("/api/auth/me").json() == {
        "user": user,
        "favorites": [saved],
    }
    wrong = restarted.post(
        "/api/auth/login",
        headers=_MUTATION,
        json=_credentials(username, _PASSWORD.strip()),
    )
    assert wrong.status_code == 401
    invalid_user = restarted.post(
        "/api/auth/login",
        headers=_MUTATION,
        json=_credentials("unknown_name"),
    )
    assert invalid_user.status_code == wrong.status_code
    assert invalid_user.json() == wrong.json()
    login = client.post(
        "/api/auth/login",
        headers=_MUTATION,
        json=_credentials("사용자_A"),
    )
    assert login.status_code == 200
    assert login.json()["user"] == user
    assert login.json()["favorites"] == [saved]
    assert restarted.get("/api/auth/me").json() == _GUEST
    assert client.cookies[_LOCAL_COOKIE] != previous_token

    current_token = client.cookies[_LOCAL_COOKIE]
    with closing(sqlite3.connect(database_path)) as db:
        encoded = db.execute("SELECT password_hash FROM accounts").fetchone()[0]
        digest = db.execute("SELECT token_hash FROM sessions").fetchone()[0]
        assert encoded.startswith("scrypt-1$")
        assert _PASSWORD not in encoded
        assert len(digest) == 32
        assert current_token.encode() not in digest

    replay = _client(cookies={_LOCAL_COOKIE: current_token})
    logout = client.post("/api/auth/logout", headers=_account_headers(user["id"]))
    assert logout.status_code == 204
    assert "Max-Age=0" in logout.headers["set-cookie"]
    assert logout.headers["cache-control"] == "no-store"
    assert replay.get("/api/auth/me").json() == _GUEST
    revoked_logout = replay.post(
        "/api/auth/logout", headers=_account_headers(user["id"])
    )
    assert revoked_logout.status_code == 204
    assert "Max-Age=0" in revoked_logout.headers["set-cookie"]


def test_account_isolation_stale_tab_and_owner_scoped_removal(database_path) -> None:
    first, second = _client(), _client()
    owner_a_id = _sign_up(first, "owner_a")["id"]
    owner_b_id = _sign_up(second, "owner_b")["id"]
    saved_a = _save(first, owner_a_id, _place("집"))
    saved_b = _save(second, owner_b_id, _place("집", "다른 주소"))
    assert first.get("/api/favorites").json()["favorites"] == [saved_a]
    assert second.get("/api/favorites").json()["favorites"] == [saved_b]
    assert (
        first.delete(
            f"/api/favorites/{saved_b['id']}",
            headers=_account_headers(owner_a_id),
        ).status_code
        == 404
    )
    assert (
        second.post(
            "/api/favorites",
            headers=_account_headers(owner_a_id),
            json=_place("오래된 탭"),
        ).status_code
        == 409
    )
    second_cookie = second.cookies[_LOCAL_COOKIE]
    stale_logout = second.post("/api/auth/logout", headers=_account_headers(owner_a_id))
    assert stale_logout.status_code == 409
    assert "set-cookie" not in stale_logout.headers
    assert second.cookies[_LOCAL_COOKIE] == second_cookie
    assert (
        second.request(
            "DELETE",
            "/api/auth/account",
            headers=_account_headers(owner_a_id),
            json={"password": _PASSWORD},
        ).status_code
        == 409
    )
    response = first.delete(
        f"/api/favorites/{saved_a['id']}",
        headers=_account_headers(owner_a_id),
    )
    assert response.status_code == 200
    assert response.json()["favorites"] == []
    assert second.get("/api/favorites").json()["favorites"] == [saved_b]


def test_account_removal_checks_password_and_revokes_every_session(
    database_path,
) -> None:
    owner, other_device, different_owner = _client(), _client(), _client()
    owner_id = _sign_up(owner, "remove_me")["id"]
    different_owner_id = _sign_up(different_owner, "keep_me")["id"]
    assert (
        other_device.post(
            "/api/auth/login",
            headers=_MUTATION,
            json=_credentials("remove_me"),
        ).status_code
        == 200
    )
    saved = _save(owner, owner_id, _place("삭제할 항목"))
    retained = _save(different_owner, different_owner_id, _place("유지할 항목"))

    response = owner.request(
        "DELETE",
        "/api/auth/account",
        headers=_account_headers(owner_id),
        json={"password": _WRONG_PASSWORD},
    )
    assert response.status_code == 401
    assert owner.get("/api/auth/me").json()["favorites"] == [saved]
    assert other_device.get("/api/auth/me").json()["favorites"] == [saved]

    removed = owner.request(
        "DELETE",
        "/api/auth/account",
        headers=_account_headers(owner_id),
        json={"password": _PASSWORD},
    )
    assert removed.status_code == 204
    assert "Max-Age=0" in removed.headers["set-cookie"]
    assert owner.get("/api/auth/me").json() == _GUEST
    assert other_device.get("/api/auth/me").json() == _GUEST
    assert different_owner.get("/api/favorites").json()["favorites"] == [retained]
    with closing(sqlite3.connect(database_path)) as db:
        assert db.execute("SELECT username FROM accounts").fetchall() == [("keep_me",)]
        assert db.execute("SELECT count(*) FROM sessions").fetchone() == (1,)
        assert db.execute("SELECT id FROM favorites").fetchall() == [(retained["id"],)]


def test_reused_username_cannot_authorize_writes_from_old_account(
    database_path,
) -> None:
    client = _client()
    username = "reusable"
    old_user = _sign_up(client, username)
    stale_headers = _account_headers(old_user["id"])
    _save(client, old_user["id"], _place("이전 계정 항목"))
    response = client.request(
        "DELETE",
        "/api/auth/account",
        headers=stale_headers,
        json={"password": _PASSWORD},
    )
    assert response.status_code == 204

    new_user = _sign_up(client, username)
    assert new_user["username"] == old_user["username"]
    assert new_user["id"] != old_user["id"]
    replacement_favorite = _save(client, new_user["id"], _place("새 계정 항목"))
    favorite_id = replacement_favorite["id"]
    for method, path, kwargs in (
        ("POST", "/api/favorites", {"json": _place("오래된 탭")}),
        (
            "POST",
            "/api/favorites/import",
            {"json": {"favorites": [_place("이전 기기")]}},
        ),
        ("DELETE", f"/api/favorites/{favorite_id}", {}),
        ("POST", "/api/auth/logout", {}),
        ("DELETE", "/api/auth/account", {"json": {"password": _PASSWORD}}),
    ):
        stale = client.request(method, path, headers=stale_headers, **kwargs)
        assert stale.status_code == 409

    no_fallback = client.post(
        "/api/favorites",
        headers=_account_headers(username),
        json=_place("사용자 이름으로 지정"),
    )
    assert no_fallback.status_code == 409
    assert client.get("/api/auth/me").json() == {
        "user": new_user,
        "favorites": [replacement_favorite],
    }


def test_account_removal_rechecks_session_after_password_verification(
    database_path,
    monkeypatch,
) -> None:
    app = create_app()
    client = _client(app)
    account_id = _sign_up(client, "changing")["id"]
    retained = _save(client, account_id, _place("유지할 항목"))
    device = _client(app, cookies={_LOCAL_COOKIE: client.cookies[_LOCAL_COOKIE]})
    headers = _account_headers(account_id)
    started, finish = Event(), Event()
    verify_password = accounts._verify_password

    def paused_verification(password: str, encoded: str) -> bool:
        started.set()
        assert finish.wait(10)
        return verify_password(password, encoded)

    with monkeypatch.context() as patch:
        patch.setattr(accounts, "_verify_password", paused_verification)
        with ThreadPoolExecutor(max_workers=1) as pool:
            removing = pool.submit(
                device.request,
                "DELETE",
                "/api/auth/account",
                headers=headers,
                json={"password": _PASSWORD},
            )
            try:
                assert started.wait(5)
                assert (
                    client.post("/api/auth/logout", headers=headers).status_code == 204
                )
            finally:
                finish.set()
            assert removing.result(timeout=10).status_code == 401

    signed_in = client.post(
        "/api/auth/login",
        headers=_MUTATION,
        json=_credentials("changing"),
    )
    assert signed_in.status_code == 200
    assert signed_in.json()["favorites"] == [retained]


def test_repeated_invalid_login_is_rate_limited(database_path, monkeypatch) -> None:
    client = _client()
    verifications = 0

    def reject_password(password: str, encoded: str) -> bool:
        nonlocal verifications
        verifications += 1
        return False

    monkeypatch.setattr(accounts, "_verify_password", reject_password)
    statuses = [
        client.post(
            "/api/auth/login", headers=_MUTATION, json=_credentials(username)
        ).status_code
        for username in ("invalid_name", "INVALID_NAME") * 6
    ]
    assert statuses == [401] * 10 + [429] * 2
    assert verifications == 10


def test_import_preserves_existing_names_order_and_skips_capacity(
    database_path,
) -> None:
    client = _client()
    account_id = _sign_up(client, "importer")["id"]
    headers = _account_headers(account_id)
    saved = _save(client, account_id, _place("저장됨"))
    entries = [
        _place("저장됨", "덮어쓰면 안 되는 주소"),
        _place("두 번째"),
        _place("두 번째"),
    ]
    entries.extend(_place(f"추가 {n}") for n in range(17))
    first = client.post(
        "/api/favorites/import", headers=headers, json={"favorites": entries}
    )
    assert first.status_code == 200
    assert first.json()["import"] == {"imported": 18, "skipped": 2}
    favorites = first.json()["favorites"]
    assert favorites[0] == saved
    assert [item["name"] for item in favorites[1:]] == [
        "두 번째",
        *(f"추가 {n}" for n in range(17)),
    ]
    again = client.post(
        "/api/favorites/import", headers=headers, json={"favorites": entries}
    )
    assert again.json()["import"] == {"imported": 0, "skipped": 20}
    assert again.json()["favorites"] == favorites
    filled = client.post(
        "/api/favorites/import",
        headers=headers,
        json={"favorites": [_place("마지막"), _place("한도 초과")]},
    )
    assert filled.json()["import"] == {"imported": 1, "skipped": 1}
    assert filled.json()["favorites"][-1]["name"] == "마지막"


def test_concurrent_requests_cannot_exceed_account_capacity(database_path) -> None:
    app, restarted = create_app(), create_app()
    client = _client(app)
    account_id = _sign_up(client, "capacity")["id"]
    headers = _account_headers(account_id)
    token = client.cookies[_LOCAL_COOKIE]
    prefill = client.post(
        "/api/favorites/import",
        headers=headers,
        json={"favorites": [_place(f"기존 {n}") for n in range(19)]},
    )
    assert prefill.json()["import"]["imported"] == 19

    def add(index: int) -> int:
        return (
            _client(
                app if index % 2 == 0 else restarted,
                cookies={_LOCAL_COOKIE: token},
            )
            .post("/api/favorites", headers=headers, json=_place(f"동시 {index}"))
            .status_code
        )

    with ThreadPoolExecutor(max_workers=5) as pool:
        statuses = list(pool.map(add, range(5)))
    assert sorted(statuses) == [201, 409, 409, 409, 409]
    assert len(client.get("/api/favorites").json()["favorites"]) == 20


def test_request_guards_secure_proxy_cookie_and_hidden_validation_inputs(
    database_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("TTS_ACCOUNT_ALLOWED_ORIGIN", "https://public.example")
    load_settings.cache_clear()
    app = create_app()
    behind_tls = TestClient(app, base_url="http://public.example")
    payload = _credentials("hidden_user")
    for headers in (
        {"Origin": "https://public.example"},
        {
            **_MUTATION,
            "Origin": "https://other.example",
            "X-Forwarded-Host": "other.example",
        },
        {**_MUTATION, "Origin": "http://public.example", "X-Forwarded-Proto": "http"},
        {
            **_MUTATION,
            "Origin": "https://public.example",
            "Sec-Fetch-Site": "cross-site",
        },
    ):
        denied = behind_tls.post("/api/auth/register", headers=headers, json=payload)
        assert denied.status_code == 403
        assert denied.headers["cache-control"] == "no-store"
    assert not database_path.exists()
    accepted = behind_tls.post(
        "/api/auth/register",
        headers={**_MUTATION, "Origin": "https://public.example"},
        json=payload,
    )
    assert accepted.status_code == 201
    cookie = accepted.headers["set-cookie"]
    assert cookie.startswith("__Host-tts_session=")
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert "Path=/" in cookie and "Domain=" not in cookie
    secret = "validation-plaintext"
    for kwargs in (
        {"json": {"username": "!@#", "password": {"hidden": secret}}},
        {"json": {"username": "valid_user", "password": secret, "extra": secret}},
        {"content": '{"password":"' + secret},
    ):
        invalid = behind_tls.post(
            "/api/auth/login",
            headers={**_MUTATION, "Content-Type": "application/json"},
            **kwargs,
        )
        assert invalid.status_code == 422
        assert secret not in invalid.text
        assert invalid.headers["cache-control"] == "no-store"
    oversized = behind_tls.post(
        "/api/auth/login",
        headers={**_MUTATION, "Content-Type": "application/json"},
        content=" " * (41 * 1024),
    )
    assert oversized.status_code == 413


def test_unconfigured_origin_uses_direct_host_not_forwarding_headers(
    database_path,
) -> None:
    client = TestClient(create_app(), base_url="http://public.example")
    denied = client.post(
        "/api/auth/register",
        json=_credentials("local_proxy"),
        headers={
            **_MUTATION,
            "Origin": "https://other.example",
            "X-Forwarded-Host": "other.example",
        },
    )
    assert denied.status_code == 403
    accepted = client.post(
        "/api/auth/register",
        json=_credentials("local_proxy"),
        headers={**_MUTATION, "Origin": "https://public.example"},
    )
    assert accepted.status_code == 201
    assert "Secure" in accepted.headers["set-cookie"]


def test_invalid_import_is_atomic_and_expired_session_is_rejected(
    database_path,
) -> None:
    client = _client()
    account_id = _sign_up(client, "expired")["id"]
    headers = _account_headers(account_id)
    invalid_import = client.post(
        "/api/favorites/import",
        headers=headers,
        json={"favorites": [_place("정상"), {**_place("잘못된 위도"), "lat": 91}]},
    )
    assert invalid_import.status_code == 422
    assert "잘못된 위도" not in invalid_import.text
    assert client.get("/api/favorites").json()["favorites"] == []
    with closing(sqlite3.connect(database_path)) as db, db:
        db.execute("UPDATE sessions SET expires_at = 1")
    assert client.get("/api/auth/me").json() == _GUEST
    assert (
        client.post(
            "/api/favorites", headers=headers, json=_place("새 위치")
        ).status_code
        == 401
    )
    expired_logout = client.post("/api/auth/logout", headers=headers)
    assert expired_logout.status_code == 204
    assert "Max-Age=0" in expired_logout.headers["set-cookie"]
    assert _LOCAL_COOKIE not in client.cookies
