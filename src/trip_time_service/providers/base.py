from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from trip_time_service.core.models import DriveDuration, Route


class ProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        is_retryable: bool = True,
        cause: Exception | None = None,
        code: str | None = None,
        bucket: str | None = None,
    ) -> None:
        super().__init__(message)
        self.is_retryable = is_retryable
        self.code = code
        self.bucket = bucket if bucket is not None else code
        self.__cause__ = cause


class TravelTimeProvider(Protocol):
    name: str

    def get_drive_duration(
        self, route: Route, departure_time: datetime
    ) -> DriveDuration: ...

    def close(self) -> None: ...


@runtime_checkable
class CoordinateAwareProvider(Protocol):
    def set_coords(
        self,
        place: str,
        lat: float,
        lon: float,
    ) -> None: ...
