from __future__ import annotations

from pathlib import Path

APP_JS = Path("src/trip_time_service/static/js/app.js").read_text(encoding="utf-8")


def _between(start: str, end: str) -> str:
    start_index = APP_JS.index(start)
    end_index = APP_JS.index(end, start_index)
    return APP_JS[start_index:end_index]


def test_route_mutation_handlers_invalidate_search_generation() -> None:
    recent_click = _between(
        '$list.querySelectorAll(".recent-item").forEach(function (el) {',
        "function renderFavorites()",
    )
    favorite_click = _between(
        '$list.querySelectorAll(".fav-chip").forEach(function (el) {',
        '$list.querySelectorAll(".fav-chip-del").forEach(function (el) {',
    )
    swap_click = _between(
        '$swapBtn.addEventListener("click", function () {',
        "/* Live marker update on input change */",
    )

    assert "invalidateRouteInputState();" in recent_click
    assert "invalidateRouteInputState();" in favorite_click
    assert "invalidateRouteInputState();" in swap_click


def test_mode_and_datetime_changes_invalidate_search_without_clearing_markers() -> None:
    mode_switch = _between(
        "function switchMode(mode) {",
        "$tabArrival.addEventListener",
    )
    datetime_change = _between(
        '$datetimeInput.addEventListener("change", function () {',
        "// Enter key in inputs triggers search",
    )

    assert "invalidateSearchOnlyState();" in mode_switch
    assert "invalidateSearchOnlyState();" in datetime_change


def test_search_cleanup_uses_full_current_snapshot_guard() -> None:
    finally_block = _between("  } finally {", "\n  }\n}\n\n$searchBtn")

    assert "if (isSearchStillCurrent()) {" in finally_block
    assert "if (_routeInputRevision === searchRouteInputRevision)" not in finally_block
