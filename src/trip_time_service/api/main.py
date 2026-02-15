from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from trip_time_service import __version__
from trip_time_service.api.routes import router
from trip_time_service.config import load_settings
from trip_time_service.providers.base import ProviderError
from trip_time_service.providers.factory import create_provider
from trip_time_service.services.trip_time_service import (
    NoFeasibleDepartureError,
    TripTimeService,
)

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_log = logging.getLogger(__name__)


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
    try:
        yield
    finally:
        service.close()


def create_app() -> FastAPI:
    app = FastAPI(
        title="trip-time-service",
        version=__version__,
        lifespan=lifespan,
    )
    app.include_router(router)

    @app.exception_handler(ProviderError)
    async def _provider_error_handler(
        request: Request,
        exc: ProviderError,
    ) -> JSONResponse:
        status = 503 if exc.is_retryable else 502
        return JSONResponse(
            status_code=status,
            content={"detail": str(exc), "retryable": exc.is_retryable},
        )

    @app.exception_handler(NoFeasibleDepartureError)
    async def _no_feasible_departure_error_handler(
        request: Request,
        exc: NoFeasibleDepartureError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"detail": str(exc), "retryable": False},
        )

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html", media_type="text/html")

    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    return app


app = create_app()
