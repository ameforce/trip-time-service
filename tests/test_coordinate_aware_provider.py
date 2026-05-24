from __future__ import annotations

from types import SimpleNamespace

from trip_time_service.api.geocode_services import pre_geocode_for_provider


class _CoordinateAwareProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, float, float]] = []

    def set_coords(self, place: str, lat: float, lon: float) -> None:
        self.calls.append((place, lat, lon))


class _NonCoordinateAwareProvider:
    pass


def test_pre_geocode_for_provider_uses_coordinate_aware_protocol() -> None:
    provider = _CoordinateAwareProvider()
    service = SimpleNamespace(_provider=provider)

    pre_geocode_for_provider(
        service,
        "강남역",
        coords_map={"강남역": (37.4979, 127.0276)},
    )

    assert provider.calls == [("강남역", 37.4979, 127.0276)]


def test_pre_geocode_for_provider_skips_non_coordinate_aware_provider() -> None:
    service = SimpleNamespace(_provider=_NonCoordinateAwareProvider())

    pre_geocode_for_provider(
        service,
        "강남역",
        coords_map={"강남역": (37.4979, 127.0276)},
    )
