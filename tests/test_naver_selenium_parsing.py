from __future__ import annotations

import json
from datetime import timedelta
from zoneinfo import ZoneInfo

from trip_time_service.config import Settings
from trip_time_service.providers.base import ProviderError
from trip_time_service.providers.naver_selenium import NaverMapsSeleniumProvider

_TZ = ZoneInfo("Asia/Seoul")


def _settings() -> Settings:
    return Settings(
        timezone=_TZ,
        headless=True,
        cache_ttl=timedelta(seconds=600),
        step_minutes=10,
        lookback_hours=3,
        max_queries=120,
        provider="naver_selenium",
        chrome_binary_path=None,
        chrome_user_data_dir=None,
        naver_map_client_id=None,
        recommend_workers=1,
        naver_session_pool_size=1,
        cors_allowed_origins=(),
        recommend_min_samples=2,
    )


class _Element:
    def __init__(self, text: str = "", displayed: bool = True) -> None:
        self.text = text
        self._displayed = displayed

    def is_displayed(self) -> bool:
        return self._displayed


class _PanelTimeoutDriver:
    def __init__(self) -> None:
        self.elements = {
            "div.panel_dialog": [
                _Element("강남역에서 판교역까지 오전 9시 00분 출발 정보 로딩 중"),
                _Element("오전 8시 50분 출발 35분 소요"),
            ],
            "button.later_departure_time_btn": [_Element("출발 시간 변경")],
            "button.later_departure_confirm_btn": [],
            "button.dropdown_btn": [_Element("오전"), _Element("9시")],
        }

    def find_elements(self, by: object, selector: str) -> list[_Element]:
        return list(self.elements.get(selector, []))


class _SummaryDurationDriver:
    def __init__(self) -> None:
        self.elements = {
            "div.panel_dialog": [_Element("", displayed=False)],
            "div.summary_content": [
                _Element("내일 오전 10시 00분에 출발하면 37분 소요 예상")
            ],
        }

    def find_elements(self, by: object, selector: str) -> list[_Element]:
        return list(self.elements.get(selector, []))


def test_reads_later_departure_duration_from_summary_content() -> None:
    provider = NaverMapsSeleniumProvider(_settings())

    duration = provider._read_duration_from_panel_dialog(
        _SummaryDurationDriver(),
        "오전",
        "10시",
        "00분",
    )

    assert duration == 37 * 60


def test_panel_parse_timeout_writes_bounded_redacted_diagnostics(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("TTS_E2E_ARTIFACTS_DIR", str(tmp_path))
    provider = NaverMapsSeleniumProvider(_settings())
    driver = _PanelTimeoutDriver()

    try:
        provider._raise_panel_parse_timeout(driver, "오전", "9시", "00분")
    except ProviderError as exc:
        assert exc.code == "panel_parse_timeout"
        assert exc.bucket == "panel_parse_timeout"
    else:  # pragma: no cover
        raise AssertionError("expected ProviderError")

    artifacts = list(tmp_path.glob("naver-panel-diagnostics-*.json"))
    assert len(artifacts) == 1
    raw_artifact = artifacts[0].read_text(encoding="utf-8")
    data = json.loads(raw_artifact)

    assert data["code"] == "panel_parse_timeout"
    assert data["requested_time"] == {"ampm": "오전", "hour": "9시", "minute": "00분"}
    assert data["selectors"]["div.panel_dialog"] == {"count": 2, "visible": 2}
    assert data["selectors"]["div.summary_content"] == {"count": 0, "visible": 0}
    assert data["panel_samples"][0]["text"].startswith("len=")
    assert "sha256=" in data["panel_samples"][0]["text"]
    assert data["panel_samples"][0]["has_requested_time"] is True
    assert data["panel_samples"][1]["duration_tokens"] == ["35분 소요"]
    assert "강남역" not in raw_artifact
    assert "판교역" not in raw_artifact
