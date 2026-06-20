from __future__ import annotations

import logging
import threading
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from trip_time_service import __version__
from trip_time_service.api.e2e_fixtures import is_fixture_mode_enabled
from trip_time_service.api.geocode_services import (
    shutdown_autocomplete_runtime,
    startup_autocomplete_runtime,
    warmup_autocomplete_runtime,
)
from trip_time_service.api.naver_geo import shutdown_naver_driver
from trip_time_service.api.routes import router
from trip_time_service.api.routes_trip import (
    RouteInputContractError,
    route_input_contract_exception_handler,
)
from trip_time_service.config import load_settings
from trip_time_service.providers.base import ProviderError
from trip_time_service.providers.factory import create_provider
from trip_time_service.services.trip_time_service import (
    NoFeasibleDepartureError,
    TripTimeService,
)
from trip_time_service.versioning import resolve_display_version

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_log = logging.getLogger(__name__)


def _static_cache_needs_revalidation(path: str) -> bool:
    return path == "/" or path.startswith("/static/")


def _versioned_index_html() -> str:
    version = quote(resolve_display_version(), safe="._-")
    html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    replacements = {
        '/static/css/style.css"': f'/static/css/style.css?v={version}"',
        '/static/js/app.js"': f'/static/js/app.js?v={version}"',
        '/static/js/autocomplete-controller.js"': (
            f'/static/js/autocomplete-controller.js?v={version}"'
        ),
    }
    for source, target in replacements.items():
        html = html.replace(source, target)
    return html


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = load_settings()
    provider = create_provider(settings)
    _log.info(
        "TripTimeService starting with provider=%s, timezone=%s",
        settings.provider,
        settings.timezone,
    )
    service = TripTimeService(settings=settings, provider=provider)
    app.state.trip_time_service = service
    app.state.settings = settings
    fixture_mode = is_fixture_mode_enabled()
    startup_autocomplete_runtime()
    warmup_thread: threading.Thread | None = None
    if not fixture_mode:
        warmup_thread = threading.Thread(
            target=warmup_autocomplete_runtime,
            name="autocomplete-browser-warmup",
            daemon=True,
        )
        warmup_thread.start()
    app.state.autocomplete_warmup_thread = warmup_thread
    try:
        yield
    finally:
        shutdown_autocomplete_runtime(startup_thread=warmup_thread)
        shutdown_naver_driver()
        if warmup_thread is not None and warmup_thread.is_alive():
            _log.warning("autocomplete warmup thread still alive during shutdown")
        service.close()


def create_app() -> FastAPI:
    settings = load_settings()
    app = FastAPI(
        title="trip-time-service",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if settings.enable_docs else None,
        redoc_url="/redoc" if settings.enable_docs else None,
        openapi_url="/openapi.json" if settings.enable_docs else None,
    )
    if settings.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_allowed_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def _security_headers_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        csp = " ".join(
            (
                "default-src 'self';",
                "script-src 'self' https://unpkg.com;",
                "style-src 'self' 'unsafe-inline' https://unpkg.com "
                "https://fonts.googleapis.com;",
                "font-src 'self' https://fonts.gstatic.com;",
                "img-src 'self' data: https://unpkg.com "
                "https://tile.openstreetmap.org https://*.tile.openstreetmap.org;",
                "connect-src 'self';",
                "base-uri 'self';",
                "form-action 'self';",
                "frame-ancestors 'none';",
            )
        )
        headers = response.headers
        headers.setdefault("Content-Security-Policy", csp)
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        headers.setdefault(
            "Permissions-Policy",
            "geolocation=(), microphone=(), camera=()",
        )
        headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
        if _static_cache_needs_revalidation(request.url.path):
            headers["Cache-Control"] = "no-cache"
        return response

    app.include_router(router)

    app.add_exception_handler(
        RouteInputContractError,
        route_input_contract_exception_handler,
    )

    @app.exception_handler(ProviderError)
    async def _provider_error_handler(
        request: Request,
        exc: ProviderError,
    ) -> JSONResponse:
        _log.warning(
            "ProviderError handled retryable=%s error=%s",
            exc.is_retryable,
            exc,
        )
        status = 503 if exc.is_retryable else 502
        return JSONResponse(
            status_code=status,
            content={
                "detail": "교통 정보 제공자 호출 중 오류가 발생했습니다.",
                "reason": "provider_degraded",
                "retryable": exc.is_retryable,
            },
        )

    @app.exception_handler(NoFeasibleDepartureError)
    async def _no_feasible_departure_error_handler(
        request: Request,
        exc: NoFeasibleDepartureError,
    ) -> JSONResponse:
        _log.info("NoFeasibleDepartureError handled: %s", exc)
        return JSONResponse(
            status_code=422,
            content={
                "detail": "입력 조건에서 유효한 추천 출발 후보를 찾지 못했습니다.",
                "reason": "no_feasible_departure",
                "retryable": False,
            },
        )

    @app.get("/", include_in_schema=False)
    async def index() -> HTMLResponse:
        return HTMLResponse(_versioned_index_html())

    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    return app


app = create_app()
