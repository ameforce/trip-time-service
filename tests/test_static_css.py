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


def test_loading_status_is_not_full_sidebar_overlay() -> None:
    css = Path("src/trip_time_service/static/css/style.css").read_text(
        encoding="utf-8"
    )
    match = re.search(r"\.loading\s*\{(?P<body>[^}]+)\}", css)

    assert match is not None
    loading_body = match.group("body")
    assert "position: absolute" not in loading_body
    assert "inset: 0" not in loading_body
    assert "position: sticky" in loading_body or "position: relative" in loading_body


def test_touch_viewport_meta_does_not_disable_zoom() -> None:
    html = Path("src/trip_time_service/static/index.html").read_text(
        encoding="utf-8"
    )
    match = re.search(
        r"<meta\s+name=[\"']viewport[\"']\s+content=[\"'](?P<content>[^\"']+)[\"']",
        html,
    )

    assert match is not None
    content = match.group("content")
    # iOS Safari 포커스 자동 확대를 maximum-scale 로 억제하면 핀치줌까지
    # 막혀 WCAG 2.1 1.4.4 를 위반한다. 폰트 크기로 해결해야 한다.
    assert not re.search(r"user-scalable\s*=\s*no", content, re.IGNORECASE)
    assert "maximum-scale" not in content


def test_touch_form_controls_meet_ios_zoom_safe_font_size() -> None:
    """iOS Safari 는 포커스된 폼 컨트롤 계산 폰트 크기가 16px 미만이면 페이지를
    자동 확대한다. 터치 환경에서 모든 포커스 가능 폼 컨트롤이 16px 이상인지
    검증한다."""
    css = Path("src/trip_time_service/static/css/style.css").read_text(
        encoding="utf-8"
    )
    match = re.search(
        r"@media\s*\(max-width:\s*768px\)\s*,\s*\(pointer:\s*coarse\)\s*\{"
        r"(?P<body>(?:[^{}]|\{[^{}]*\})*)\}\s*$",
        css,
    )

    assert match is not None, (
        "터치 환경(iOS Safari 포커스 자동 확대 차단)용 16px 폼 컨트롤 규칙이 "
        "style.css 끝에 존재해야 한다"
    )

    body = match.group("body")
    for selector in (
        ".input-wrap input",
        ".favorites-add-form input",
        ".datetime-input-wrap input",
        ".datetime-ymd-inputs input",
        ".datetime-time-col select",
    ):
        assert selector in body, f"{selector} 규칙이 누락되었다"

    sizes = re.findall(r"font-size:\s*(?P<value>[\d.]+)px", body)
    assert sizes, "16px 폰트 크기 선언이 누락되었다"
    assert all(float(value) >= 16 for value in sizes)
