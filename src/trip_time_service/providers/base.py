from __future__ import annotations

from datetime import datetime
from typing import Protocol

from trip_time_service.core.models import DriveDuration, Route


class ProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        is_retryable: bool = True,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.is_retryable = is_retryable
        self.__cause__ = cause


class TravelTimeProvider(Protocol):
    name: str

    def get_drive_duration(
        self, route: Route, departure_time: datetime
    ) -> DriveDuration: ...

    def close(self) -> None: ...
