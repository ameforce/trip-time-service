from __future__ import annotations

import re
import sqlite3
import unicodedata
from collections.abc import Awaitable, Callable
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Path, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from starlette.exceptions import HTTPException
from starlette.types import Message

from trip_time_service.accounts import (
    FAVORITE_LIMIT,
    SESSION_MAX_AGE,
    AccountError,
    AccountStore,
    Favorite,
    ImportSummary,
    Place,
    Snapshot,
    User,
)

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_MAX_BODY_BYTES = 40 * 1024
_USERNAME = re.compile(r"[a-zA-Z0-9_가-힣]{3,32}")


def _origin(value: str) -> tuple[str, str, int] | None:
    try:
        url = urlsplit(value)
        if (
            url.scheme not in {"http", "https"}
            or url.hostname is None
            or url.username is not None
            or url.path
            or url.query
            or url.fragment
        ):
            return None
        return (
            url.scheme,
            url.hostname,
            url.port
            if url.port is not None
            else (443 if url.scheme == "https" else 80),
        )
    except ValueError:
        return None


def configured_account_origin(value: str | None) -> tuple[str, str, int] | None:
    if value is None:
        return None
    origin = _origin(value.removesuffix("/"))
    if origin is None or (origin[0] == "http" and origin[1] not in _LOCAL_HOSTS):
        raise ValueError(
            "TTS_ACCOUNT_ALLOWED_ORIGIN must be an HTTPS or loopback origin"
        )
    return origin


def _expected_origin(request: Request) -> tuple[str, str, int] | None:
    configured = request.app.state.account_allowed_origin
    if configured is not None:
        return configured
    host = request.headers.get("host", "")
    # Traefik preserves Host. The ASGI connection itself can be HTTP behind TLS.
    https_origin = _origin(f"https://{host}")
    if (
        https_origin is not None
        and https_origin[1] in _LOCAL_HOSTS
        and request.url.scheme == "http"
    ):
        return _origin(f"http://{host}")
    return https_origin


def _check_mutation(request: Request) -> None:
    origin = request.headers.get("origin")
    expected = _expected_origin(request)
    if (
        request.headers.get("x-requested-with") != "TripTime"
        or request.headers.get("sec-fetch-site") not in {None, "same-origin"}
        or expected is None
        or (origin is not None and _origin(origin) != expected)
    ):
        raise HTTPException(403, "요청을 확인할 수 없습니다.")


class _AccountRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                if request.method in {"POST", "DELETE"}:
                    _check_mutation(request)
                    receive = request.receive
                    size = 0

                    async def receive_bounded() -> Message:
                        nonlocal size
                        message = await receive()
                        size += len(message.get("body", b""))
                        if size > _MAX_BODY_BYTES:
                            raise HTTPException(413, "요청 데이터가 너무 큽니다.")
                        return message

                    request = Request(request.scope, receive_bounded)
                return await handler(request)
            except RequestValidationError:
                # The default FastAPI error payload can include untrusted inputs.
                return JSONResponse(
                    status_code=422,
                    content={"detail": "입력 내용을 확인해 주세요."},
                )
            except AccountError as exc:
                return JSONResponse(
                    status_code=exc.status, content={"detail": exc.detail}
                )
            except OSError, sqlite3.OperationalError:
                return JSONResponse(
                    status_code=503,
                    content={"detail": "계정 정보를 잠시 사용할 수 없습니다."},
                )

        return handle


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _PasswordPayload(_Payload):
    password: SecretStr = Field(min_length=10, max_length=128)

    @field_validator("password")
    @classmethod
    def _valid_unicode(cls, value: SecretStr) -> SecretStr:
        value.get_secret_value().encode("utf-8")
        return value


class _Credentials(_PasswordPayload):
    username: str = Field(strict=True, max_length=96)

    @field_validator("username")
    @classmethod
    def _canonical_username(cls, value: str) -> str:
        value = unicodedata.normalize("NFC", value)
        if _USERNAME.fullmatch(value) is None:
            raise ValueError("invalid username")
        return value.lower()


class _FavoritePayload(_Payload):
    name: str = Field(min_length=1, max_length=20, strict=True)
    address: str = Field(min_length=1, max_length=100, strict=True)
    lat: float = Field(ge=-90, le=90, strict=True, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, strict=True, allow_inf_nan=False)

    @field_validator("name", "address")
    @classmethod
    def _valid_text(cls, value: str) -> str:
        value.encode("utf-8")
        value = value.strip()
        if not value or "\x00" in value:
            raise ValueError("invalid favorite text")
        return value

    def as_place(self) -> Place:
        return Place(name=self.name, address=self.address, lat=self.lat, lon=self.lon)


class _ImportPayload(_Payload):
    favorites: list[_FavoritePayload] = Field(max_length=FAVORITE_LIMIT)


class _ImportSnapshot(BaseModel):
    user: User | None
    favorites: list[Favorite]
    summary: ImportSummary = Field(alias="import")


def _session_cookie(request: Request) -> tuple[str, bool]:
    origin = _expected_origin(request)
    host = _origin(f"https://{request.headers.get('host', '')}")
    secure = not (
        origin is not None
        and origin[0] == "http"
        and host is not None
        and host[1] in _LOCAL_HOSTS
        and request.url.scheme == "http"
    )
    return ("__Host-tts_session" if secure else "tts_session", secure)


def _session_token(request: Request) -> str | None:
    name, _ = _session_cookie(request)
    return request.cookies.get(name)


def _set_session(request: Request, response: Response, token: str) -> None:
    name, secure = _session_cookie(request)
    response.set_cookie(
        name,
        token,
        max_age=SESSION_MAX_AGE,
        secure=secure,
        httponly=True,
        samesite="lax",
    )


def _clear_session(request: Request, response: Response) -> None:
    name, secure = _session_cookie(request)
    response.delete_cookie(name, secure=secure, httponly=True, samesite="lax")


def _store(request: Request) -> AccountStore:
    return request.app.state.account_store


def _expected_account_id(request: Request) -> str | None:
    return request.headers.get("x-triptime-account")


router = APIRouter(prefix="/api", route_class=_AccountRoute)


@router.post("/auth/register", status_code=201)
def register(payload: _Credentials, request: Request, response: Response) -> Snapshot:
    token, result = _store(request).register(
        payload.username,
        payload.password.get_secret_value(),
        _session_token(request),
    )
    _set_session(request, response, token)
    return result


@router.post("/auth/login")
def login(payload: _Credentials, request: Request, response: Response) -> Snapshot:
    token, result = _store(request).login(
        payload.username,
        payload.password.get_secret_value(),
        _session_token(request),
    )
    _set_session(request, response, token)
    return result


@router.get("/auth/me")
def me(request: Request) -> Snapshot:
    return _store(request).snapshot(_session_token(request))


@router.post("/auth/logout", status_code=204)
def logout(request: Request) -> Response:
    _store(request).logout(_session_token(request), _expected_account_id(request))
    response = Response(status_code=204)
    _clear_session(request, response)
    return response


@router.delete("/auth/account", status_code=204)
def delete_account(payload: _PasswordPayload, request: Request) -> Response:
    _store(request).delete_account(
        _session_token(request),
        _expected_account_id(request),
        payload.password.get_secret_value(),
    )
    response = Response(status_code=204)
    _clear_session(request, response)
    return response


@router.get("/favorites")
def favorites(request: Request) -> Snapshot:
    return _store(request).snapshot(_session_token(request), require_account=True)


@router.post("/favorites", status_code=201)
def add_favorite(payload: _FavoritePayload, request: Request) -> Snapshot:
    snapshot, _ = _store(request).add_favorites(
        _session_token(request),
        _expected_account_id(request),
        [payload.as_place()],
    )
    return snapshot


@router.post("/favorites/import", response_model=_ImportSnapshot)
def import_favorites(payload: _ImportPayload, request: Request) -> dict[str, object]:
    snapshot, summary = _store(request).add_favorites(
        _session_token(request),
        _expected_account_id(request),
        [favorite.as_place() for favorite in payload.favorites],
        importing=True,
    )
    return {"user": snapshot.user, "favorites": snapshot.favorites, "import": summary}


@router.delete("/favorites/{favorite_id}")
def delete_favorite(
    favorite_id: Annotated[int, Path(gt=0, le=2**63 - 1)],
    request: Request,
) -> Snapshot:
    return _store(request).delete_favorite(
        _session_token(request),
        _expected_account_id(request),
        favorite_id,
    )
