from __future__ import annotations

import re
from pathlib import Path


def test_mobile_toggle_stays_above_leaflet_controls() -> None:
    css = Path("src/trip_time_service/static/css/style.css").read_text(
        encoding="utf-8"
    )
    match = re.search(r"\.mobile-toggle\s*\{(?P<body>[^}]+)\}", css)

    assert match is not None
    z_index_match = re.search(r"z-index:\s*(?P<value>\d+)\s*;", match.group("body"))

    assert z_index_match is not None
    assert int(z_index_match.group("value")) > 1000
