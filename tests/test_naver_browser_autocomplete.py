from __future__ import annotations

from trip_time_service.api import naver_playwright_autocomplete, routes_geo


class _FakeInput:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def click(self) -> None:
        self.events.append(("click", ""))

    def fill(self, value: str) -> None:
        self.events.append(("fill", value))

    def type(self, value: str) -> None:
        self.events.append(("type", value))


class _FakeAnchor:
    def __init__(self, href: str) -> None:
        self._href = href

    def get_attribute(self, name: str) -> str:
        return self._href if name == "href" else ""


class _FakeOption:
    def __init__(
        self,
        text: str,
        *,
        href: str = "",
        click_url: str = "",
        page: object | None = None,
    ) -> None:
        self._text = text
        self._href = href
        self._click_url = click_url
        self._page = page
        self.click_count = 0

    def inner_text(self) -> str:
        return self._text

    def get_attribute(self, name: str) -> str:
        return self._href if name == "href" else ""

    def query_selector_all(self, selector: str) -> list[_FakeAnchor]:
        if selector == "a[href]" and self._href:
            return [_FakeAnchor(self._href)]
        return []

    def click(self) -> None:
        self.click_count += 1
        if self._click_url and self._page is not None:
            self._page.url = self._click_url


class _FakePage:
    def __init__(self) -> None:
        self.input = _FakeInput()
        self.url = "https://map.naver.com/"
        self.option_calls = 0
        self.option_sequences = [
            [_FakeOption("검색어\n태안우체국")],
            [_FakeOption("검색어\n태안우체국")],
            [_FakeOption("검색어\n광교역")],
        ]
        self.evaluate_payload: dict | None = None
        self.evaluate_error: Exception | None = None

    def wait_for_selector(self, selector: str, *, timeout: float = 0) -> _FakeInput:
        assert selector == "input.input_search"
        return self.input

    def query_selector_all(self, selector: str) -> list[_FakeOption]:
        assert selector == naver_playwright_autocomplete._SUGGEST_OPTION_SELECTOR
        index = min(self.option_calls, len(self.option_sequences) - 1)
        self.option_calls += 1
        return self.option_sequences[index]

    def evaluate(self, *_args: object) -> dict:
        if self.evaluate_error is not None:
            raise self.evaluate_error
        if self.evaluate_payload is None:
            raise RuntimeError("evaluate payload not configured")
        return self.evaluate_payload


def _patch_wait_until_immediate(monkeypatch) -> None:
    def _immediate(predicate, *, timeout: float, poll: float = 0.05) -> bool:
        del timeout, poll
        try:
            return bool(predicate())
        except Exception:
            return False

    monkeypatch.setattr(
        naver_playwright_autocomplete,
        "_wait_until",
        _immediate,
    )


def _patch_wait_until_poll(monkeypatch) -> None:
    def _poll(predicate, *, timeout: float, poll: float = 0.05) -> bool:
        del poll
        attempts = max(1, int(timeout / 0.05) + 1)
        for _ in range(min(attempts, 8)):
            try:
                if predicate():
                    return True
            except Exception:
                pass
        try:
            return bool(predicate())
        except Exception:
            return False

    monkeypatch.setattr(
        naver_playwright_autocomplete,
        "_wait_until",
        _poll,
    )


def _patch_wait_until_never(monkeypatch) -> None:
    monkeypatch.setattr(
        naver_playwright_autocomplete,
        "_wait_until",
        lambda *_args, **_kwargs: False,
    )


def test_worker_waits_for_suggestions_matching_current_query(monkeypatch) -> None:
    _patch_wait_until_poll(monkeypatch)
    worker = naver_playwright_autocomplete._BrowserAutocompleteWorker(worker_index=1)
    page = _FakePage()

    results = worker._query_locked(
        page,
        "광교역",
        limit=5,
        wait_seconds=0.45,
    )

    assert len(results) == 1
    assert results[0]["display_name"] == "광교역"
    assert page.option_calls == 3


def test_worker_wait_budget_covers_live_naver_suggestion_latency(monkeypatch) -> None:
    def _latency_aware(predicate, *, timeout: float, poll: float = 0.05) -> bool:
        del poll
        if timeout < 5.0:
            return False
        try:
            return bool(predicate())
        except Exception:
            return False

    monkeypatch.setattr(
        naver_playwright_autocomplete,
        "_wait_until",
        _latency_aware,
    )
    worker = naver_playwright_autocomplete._BrowserAutocompleteWorker(worker_index=1)
    page = _FakePage()
    page.option_sequences = [
        [_FakeOption("장소\n경기 수원시 팔달구 경수대로680번길 40 센트럴하우스")]
    ]

    results = worker._query_locked(
        page,
        "경수대로680번길40",
        limit=5,
        wait_seconds=naver_playwright_autocomplete._SUGGEST_WAIT_SECONDS,
    )

    assert len(results) == 1
    assert "센트럴하우스" in str(results[0]["display_name"])


def test_extract_suggestions_preserves_coords_from_descendant_anchor_href() -> None:
    option = _FakeOption(
        "장소\n출입구\n광교역(경기대)1번출구\n경기 수원시 영통구 이의동",
        href=(
            "https://map.naver.com/p/place/"
            "14142473.386372,4481285.70836642,광교역(경기대)1번출구"
        ),
    )

    results = naver_playwright_autocomplete._extract_suggestions_from_options(
        "광교역",
        [option],
        limit=5,
    )

    assert len(results) == 1
    result = results[0]
    assert result["display_name"] == "광교역(경기대)1번출구"
    assert result["address"] == "경기 수원시 영통구 이의동"
    assert result["type"] == "출입구"
    assert result["source"] == "naver_browser_suggest"
    assert result["lat"] != ""
    assert result["lon"] != ""
    assert 33 <= float(result["lat"]) <= 43
    assert 124 <= float(result["lon"]) <= 132


def test_extract_suggestions_ignores_malformed_coordinate_href() -> None:
    option = _FakeOption(
        "장소\n출입구\n광교역(경기대)1번출구\n경기 수원시 영통구 이의동",
        href="https://map.naver.com/p/place/not-coordinates,광교역(경기대)1번출구",
    )

    results = naver_playwright_autocomplete._extract_suggestions_from_options(
        "광교역",
        [option],
        limit=5,
    )

    assert len(results) == 1
    assert results[0]["lat"] == ""
    assert results[0]["lon"] == ""


def test_extract_suggestions_keeps_live_shaped_hrefless_options_unresolved(
) -> None:
    option = _FakeOption(
        "장소\n출입구\n광교역(경기대)1번출구\n경기 수원시 영통구 이의동"
    )

    results = naver_playwright_autocomplete._extract_suggestions_from_options(
        "광교역",
        [option],
        limit=5,
    )

    assert len(results) == 1
    assert results[0]["display_name"] == "광교역(경기대)1번출구"
    assert results[0]["lat"] == ""
    assert results[0]["lon"] == ""


def test_query_locked_enriches_live_hrefless_address_from_instant_search(
    monkeypatch,
) -> None:
    _patch_wait_until_immediate(monkeypatch)
    fetch_calls = []

    def _fake_fetch(page, query):
        fetch_calls.append((page, query))
        return {
            "address": [
                {
                    "name": "경수대로680번길 40",
                    "roadAddress": "경기 수원시 장안구 경수대로680번길 40",
                    "address": "경기 수원시 장안구 우만동",
                    "x": "127.029391112",
                    "y": "37.286775649",
                }
            ],
            "searchCoord": {"x": "127.12057619212703", "y": "37.40607799999982"},
        }

    monkeypatch.setattr(
        naver_playwright_autocomplete,
        "_fetch_instant_search_json",
        _fake_fetch,
        raising=False,
    )
    worker = naver_playwright_autocomplete._BrowserAutocompleteWorker(worker_index=1)
    page = _FakePage()
    option = _FakeOption(
        "장소\n주소\n경수대로680번길 40 센트럴하우스\n경기 수원시 장안구 우만동"
    )
    page.option_sequences = [[option]]

    results = worker._query_locked(
        page,
        "경수대로680번길40",
        limit=5,
        wait_seconds=0.45,
    )

    assert len(results) == 1
    assert results[0]["lat"] == "37.286775649"
    assert results[0]["lon"] == "127.029391112"
    serialized = routes_geo._serialize_autocomplete_items(results)
    assert serialized[0]["coords_ready"] is True
    assert serialized[0]["lat"] == 37.286775649
    assert serialized[0]["lon"] == 127.029391112
    assert fetch_calls == [(page, "경수대로680번길40")]
    assert option.click_count == 0


def test_query_locked_matches_instant_search_place_by_canonical_text(
    monkeypatch,
) -> None:
    _patch_wait_until_immediate(monkeypatch)
    fetch_calls = []

    def _fake_fetch(page, query):
        fetch_calls.append((page, query))
        return {
            "place": [
                {
                    "name": "광교역",
                    "roadAddress": "경기 수원시 영통구 이의동",
                    "x": "127.044227",
                    "y": "37.3021009",
                }
            ],
            "all": [
                {
                    "place": {
                        "name": "광교역(경기대)1번출구",
                        "address": "경기 수원시 영통구 이의동",
                        "x": "127.0446723",
                        "y": "37.3014568",
                    }
                }
            ],
        }

    monkeypatch.setattr(
        naver_playwright_autocomplete,
        "_fetch_instant_search_json",
        _fake_fetch,
        raising=False,
    )
    worker = naver_playwright_autocomplete._BrowserAutocompleteWorker(worker_index=1)
    page = _FakePage()
    option = _FakeOption(
        "장소\n출입구\n광교역(경기대)1번출구\n경기 수원시 영통구 이의동"
    )
    page.option_sequences = [[option]]

    results = worker._query_locked(
        page,
        "광교역",
        limit=5,
        wait_seconds=0.45,
    )

    assert len(results) == 1
    assert results[0]["display_name"] == "광교역(경기대)1번출구"
    assert results[0]["lat"] == "37.3014568"
    assert results[0]["lon"] == "127.0446723"
    assert fetch_calls == [(page, "광교역")]
    assert option.click_count == 0


def test_query_locked_ignores_unrelated_instant_search_coordinates(
    monkeypatch,
) -> None:
    _patch_wait_until_immediate(monkeypatch)
    monkeypatch.setattr(
        naver_playwright_autocomplete,
        "_fetch_instant_search_json",
        lambda _page, _query: {
            "address": [
                {
                    "name": "서울역",
                    "address": "서울 중구 한강대로 405",
                    "x": "126.9707",
                    "y": "37.5547",
                }
            ]
        },
        raising=False,
    )
    worker = naver_playwright_autocomplete._BrowserAutocompleteWorker(worker_index=1)
    page = _FakePage()
    option = _FakeOption(
        "장소\n출입구\n광교역(경기대)1번출구\n경기 수원시 영통구 이의동",
    )
    page.option_sequences = [[option]]

    results = worker._query_locked(
        page,
        "광교역",
        limit=5,
        wait_seconds=0.45,
    )

    assert len(results) == 1
    assert results[0]["lat"] == ""
    assert results[0]["lon"] == ""
    assert option.click_count == 0


def test_query_locked_synthesizes_address_when_dom_options_are_empty(
    monkeypatch,
) -> None:
    _patch_wait_until_never(monkeypatch)
    fetch_calls = []

    def _fake_fetch(page, query):
        fetch_calls.append((page, query))
        return {
            "address": [
                {
                    "name": "경수대로680번길 40",
                    "address": "경기 수원시 팔달구 경수대로680번길 40 센트럴하우스",
                    "x": "127.029391112",
                    "y": "37.286775649",
                }
            ],
            "searchCoord": {"x": "127.12057619212703", "y": "37.40607799999982"},
        }

    monkeypatch.setattr(
        naver_playwright_autocomplete,
        "_fetch_instant_search_json",
        _fake_fetch,
        raising=False,
    )
    worker = naver_playwright_autocomplete._BrowserAutocompleteWorker(worker_index=1)
    page = _FakePage()
    page.option_sequences = [[]]

    results = worker._query_locked(
        page,
        "경수대로680번길40",
        limit=5,
        wait_seconds=0.45,
    )

    assert len(results) == 1
    assert results[0]["display_name"] == "경수대로680번길 40"
    assert results[0]["address"] == "경기 수원시 팔달구 경수대로680번길 40 센트럴하우스"
    assert results[0]["type"] == "주소"
    assert results[0]["lat"] == "37.286775649"
    assert results[0]["lon"] == "127.029391112"
    assert results[0]["source"] == "naver_browser_suggest"
    serialized = routes_geo._serialize_autocomplete_items(results)
    assert serialized[0]["coords_ready"] is True
    assert serialized[0]["selection_kind"] == "address"
    assert fetch_calls == [(page, "경수대로680번길40")]


def test_query_locked_synthesizes_place_when_dom_options_are_empty(
    monkeypatch,
) -> None:
    _patch_wait_until_never(monkeypatch)
    fetch_calls = []

    def _fake_fetch(page, query):
        fetch_calls.append((page, query))
        return {
            "place": [
                {
                    "name": "광교역(경기대)1번출구",
                    "address": "경기 수원시 영통구 이의동",
                    "x": "127.0446723",
                    "y": "37.3014568",
                }
            ]
        }

    monkeypatch.setattr(
        naver_playwright_autocomplete,
        "_fetch_instant_search_json",
        _fake_fetch,
        raising=False,
    )
    worker = naver_playwright_autocomplete._BrowserAutocompleteWorker(worker_index=1)
    page = _FakePage()
    page.option_sequences = [[]]

    results = worker._query_locked(
        page,
        "광교역(경기대)1번출구",
        limit=5,
        wait_seconds=0.45,
    )

    assert len(results) == 1
    assert results[0]["display_name"] == "광교역(경기대)1번출구"
    assert results[0]["type"] == "장소"
    assert results[0]["lat"] == "37.3014568"
    assert results[0]["lon"] == "127.0446723"
    serialized = routes_geo._serialize_autocomplete_items(results)
    assert serialized[0]["coords_ready"] is True
    assert fetch_calls == [(page, "광교역(경기대)1번출구")]


def test_query_locked_synthesis_ignores_unrelated_first_candidate(
    monkeypatch,
) -> None:
    _patch_wait_until_never(monkeypatch)
    monkeypatch.setattr(
        naver_playwright_autocomplete,
        "_fetch_instant_search_json",
        lambda _page, _query: {
            "place": [
                {
                    "name": "서울역",
                    "address": "서울 중구 한강대로 405",
                    "x": "126.9707",
                    "y": "37.5547",
                },
                {
                    "name": "광교역(경기대)1번출구",
                    "address": "경기 수원시 영통구 이의동",
                    "x": "127.0446723",
                    "y": "37.3014568",
                },
            ]
        },
        raising=False,
    )
    worker = naver_playwright_autocomplete._BrowserAutocompleteWorker(worker_index=1)
    page = _FakePage()
    page.option_sequences = [[]]

    results = worker._query_locked(
        page,
        "광교역(경기대)1번출구",
        limit=5,
        wait_seconds=0.45,
    )

    assert len(results) == 1
    assert results[0]["display_name"] == "광교역(경기대)1번출구"
    assert results[0]["lat"] == "37.3014568"
    assert results[0]["lon"] == "127.0446723"


def test_query_locked_synthesis_ignores_searchcoord_recommendations_and_ads(
    monkeypatch,
) -> None:
    _patch_wait_until_never(monkeypatch)
    monkeypatch.setattr(
        naver_playwright_autocomplete,
        "_fetch_instant_search_json",
        lambda _page, _query: {
            "searchCoord": {"x": "127.12057619212703", "y": "37.40607799999982"},
            "recommendations": [
                {
                    "name": "광교역(경기대)1번출구",
                    "x": "127.0446723",
                    "y": "37.3014568",
                }
            ],
            "ads": [
                {
                    "name": "광교역(경기대)1번출구",
                    "x": "127.0446723",
                    "y": "37.3014568",
                }
            ],
        },
        raising=False,
    )
    worker = naver_playwright_autocomplete._BrowserAutocompleteWorker(worker_index=1)
    page = _FakePage()
    page.option_sequences = [[]]

    results = worker._query_locked(
        page,
        "광교역(경기대)1번출구",
        limit=5,
        wait_seconds=0.45,
    )

    assert results == ()


def test_query_locked_synthesis_fails_soft_for_endpoint_failure_and_malformed(
    monkeypatch,
) -> None:
    _patch_wait_until_never(monkeypatch)
    payloads = iter([None, {"address": "not-list"}])
    fetch_calls = []

    def _fake_fetch(page, query):
        fetch_calls.append((page, query))
        return next(payloads)

    monkeypatch.setattr(
        naver_playwright_autocomplete,
        "_fetch_instant_search_json",
        _fake_fetch,
        raising=False,
    )
    worker = naver_playwright_autocomplete._BrowserAutocompleteWorker(worker_index=1)

    first_page = _FakePage()
    first_page.option_sequences = [[]]
    assert (
        worker._query_locked(
            first_page,
            "경수대로680번길40",
            limit=5,
            wait_seconds=0.45,
        )
        == ()
    )

    second_page = _FakePage()
    second_page.option_sequences = [[]]
    assert (
        worker._query_locked(
            second_page,
            "경수대로680번길40",
            limit=5,
            wait_seconds=0.45,
        )
        == ()
    )
    assert fetch_calls == [
        (first_page, "경수대로680번길40"),
        (second_page, "경수대로680번길40"),
    ]


def test_query_locked_uses_direct_instant_search_after_js_fetch_failure(
    monkeypatch,
) -> None:
    _patch_wait_until_never(monkeypatch)
    direct_calls = []

    def _fake_direct_fetch(query):
        direct_calls.append(query)
        return {
            "address": [
                {
                    "name": "경수대로680번길 40",
                    "roadAddress": "경기 수원시 팔달구 경수대로680번길 40",
                    "x": "127.029391112",
                    "y": "37.286775649",
                }
            ],
        }

    monkeypatch.setattr(
        naver_playwright_autocomplete,
        "_direct_fetch_instant_search_json",
        _fake_direct_fetch,
        raising=False,
    )
    worker = naver_playwright_autocomplete._BrowserAutocompleteWorker(worker_index=1)
    page = _FakePage()
    page.option_sequences = [[]]

    results = worker._query_locked(
        page,
        "경수대로680번길40",
        limit=5,
        wait_seconds=0.45,
    )

    assert len(results) == 1
    assert results[0]["display_name"] == "경수대로680번길 40"
    assert results[0]["lat"] == "37.286775649"
    assert results[0]["lon"] == "127.029391112"
    serialized = routes_geo._serialize_autocomplete_items(results)
    assert serialized[0]["coords_ready"] is True
    assert direct_calls == ["경수대로680번길40"]


def test_query_locked_direct_instant_search_fails_soft_for_blocked_or_bad_payloads(
    monkeypatch,
) -> None:
    _patch_wait_until_never(monkeypatch)
    payloads = iter([None, {"captcha": "ncaptcha"}, "malformed", TimeoutError()])
    direct_calls = []

    def _fake_direct_fetch(query):
        direct_calls.append(query)
        payload = next(payloads)
        if isinstance(payload, Exception):
            raise payload
        return payload

    monkeypatch.setattr(
        naver_playwright_autocomplete,
        "_direct_fetch_instant_search_json",
        _fake_direct_fetch,
        raising=False,
    )
    worker = naver_playwright_autocomplete._BrowserAutocompleteWorker(worker_index=1)

    for _ in range(4):
        page = _FakePage()
        page.option_sequences = [[]]
        assert (
            worker._query_locked(
                page,
                "경수대로680번길40",
                limit=5,
                wait_seconds=0.45,
            )
            == ()
        )

    assert direct_calls == ["경수대로680번길40"] * 4


def test_direct_instant_search_runs_once_after_js_failure_and_not_after_success(
    monkeypatch,
) -> None:
    direct_calls = []

    def _fake_direct_fetch(query):
        direct_calls.append(query)
        return {
            "address": [
                {
                    "name": "경수대로680번길 40",
                    "x": "127.029391112",
                    "y": "37.286775649",
                }
            ],
        }

    monkeypatch.setattr(
        naver_playwright_autocomplete,
        "_direct_fetch_instant_search_json",
        _fake_direct_fetch,
        raising=False,
    )

    failure_page = _FakePage()
    failure_page.evaluate_payload = {"ok": False, "status": 500, "text": "error"}
    assert naver_playwright_autocomplete._fetch_instant_search_json(
        failure_page,
        "경수대로680번길40",
    ) == {
        "address": [
            {
                "name": "경수대로680번길 40",
                "x": "127.029391112",
                "y": "37.286775649",
            }
        ],
    }
    assert direct_calls == ["경수대로680번길40"]

    success_page = _FakePage()
    success_page.evaluate_payload = {
        "ok": True,
        "status": 200,
        "json": {
            "address": [
                {
                    "name": "경수대로680번길 40",
                    "x": "127.029391112",
                    "y": "37.286775649",
                }
            ]
        },
    }
    assert naver_playwright_autocomplete._fetch_instant_search_json(
        success_page,
        "경수대로680번길40",
    ) == {
        "address": [
            {
                "name": "경수대로680번길 40",
                "x": "127.029391112",
                "y": "37.286775649",
            }
        ]
    }
    assert direct_calls == ["경수대로680번길40"]


def test_direct_instant_search_synthesis_ignores_center_ads_and_recommendations(
    monkeypatch,
) -> None:
    _patch_wait_until_never(monkeypatch)
    direct_calls = []

    def _fake_direct_fetch(query):
        direct_calls.append(query)
        return {
            "searchCoord": {"x": "127.12057619212703", "y": "37.40607799999982"},
            "request": {"coords": "37.40607799999982,127.12057619212703"},
            "ads": [
                {
                    "name": "경수대로680번길 40",
                    "x": "127.111111",
                    "y": "37.111111",
                }
            ],
            "recommendations": [
                {
                    "name": "경수대로680번길 40",
                    "x": "127.222222",
                    "y": "37.222222",
                }
            ],
            "all": [
                {
                    "address": [
                        {
                            "name": "경수대로680번길 40",
                            "x": "127.029391112",
                            "y": "37.286775649",
                        }
                    ]
                }
            ],
        }

    monkeypatch.setattr(
        naver_playwright_autocomplete,
        "_direct_fetch_instant_search_json",
        _fake_direct_fetch,
        raising=False,
    )
    worker = naver_playwright_autocomplete._BrowserAutocompleteWorker(worker_index=1)
    page = _FakePage()
    page.option_sequences = [[]]

    results = worker._query_locked(
        page,
        "경수대로680번길40",
        limit=5,
        wait_seconds=0.45,
    )

    assert len(results) == 1
    assert results[0]["lat"] == "37.286775649"
    assert results[0]["lon"] == "127.029391112"
    assert direct_calls == ["경수대로680번길40"]


def test_fetch_autocomplete_from_instant_search_uses_direct_fetch_and_synthesis(
    monkeypatch,
) -> None:
    direct_calls: list[str] = []

    def _fake_direct_fetch(query: str):
        direct_calls.append(query)
        return {
            "address": [
                {
                    "name": "경수대로680번길 40",
                    "roadAddress": "경기 수원시 팔달구 경수대로680번길 40",
                    "x": "127.029391112",
                    "y": "37.286775649",
                }
            ],
            "searchCoord": {"x": "127.12057619212703", "y": "37.40607799999982"},
            "recommendations": [
                {
                    "name": "경수대로680번길 40",
                    "x": "127.222222",
                    "y": "37.222222",
                }
            ],
        }

    monkeypatch.setattr(
        naver_playwright_autocomplete,
        "_direct_fetch_instant_search_json",
        _fake_direct_fetch,
        raising=False,
    )

    results = naver_playwright_autocomplete.fetch_autocomplete_from_instant_search(
        "경수대로680번길40",
        limit=5,
    )

    assert direct_calls == ["경수대로680번길40"]
    assert len(results) == 1
    assert results[0]["lat"] == "37.286775649"
    assert results[0]["lon"] == "127.029391112"
    assert results[0]["source"] == "naver_browser_suggest"


def test_query_locked_never_uses_searchcoord_or_request_center(
    monkeypatch,
) -> None:
    _patch_wait_until_immediate(monkeypatch)
    monkeypatch.setattr(
        naver_playwright_autocomplete,
        "_fetch_instant_search_json",
        lambda _page, _query: {
            "searchCoord": {"x": "127.12057619212703", "y": "37.40607799999982"},
            "place": [
                {
                    "name": "다른 장소",
                    "x": "127.12057619212703",
                    "y": "37.40607799999982",
                }
            ],
        },
        raising=False,
    )
    worker = naver_playwright_autocomplete._BrowserAutocompleteWorker(worker_index=1)
    page = _FakePage()
    option = _FakeOption(
        "장소\n출입구\n광교역(경기대)1번출구\n경기 수원시 영통구 이의동"
    )
    page.option_sequences = [[option]]

    results = worker._query_locked(
        page,
        "광교역",
        limit=5,
        wait_seconds=0.45,
    )

    assert len(results) == 1
    assert results[0]["lat"] == ""
    assert results[0]["lon"] == ""
    assert option.click_count == 0


def test_query_locked_instant_search_failure_fails_soft_without_click(
    monkeypatch,
) -> None:
    _patch_wait_until_immediate(monkeypatch)

    def _fake_fetch(_page, _query):
        raise RuntimeError("429 ncaptcha")

    monkeypatch.setattr(
        naver_playwright_autocomplete,
        "_fetch_instant_search_json",
        _fake_fetch,
        raising=False,
    )
    worker = naver_playwright_autocomplete._BrowserAutocompleteWorker(worker_index=1)
    page = _FakePage()
    option = _FakeOption(
        "장소\n출입구\n광교역(경기대)1번출구\n경기 수원시 영통구 이의동",
        click_url=(
            "https://map.naver.com/p/place/"
            "14142473.386372,4481285.70836642,광교역"
        ),
        page=page,
    )
    page.option_sequences = [[option]]

    results = worker._query_locked(
        page,
        "광교역",
        limit=5,
        wait_seconds=0.45,
    )

    assert len(results) == 1
    assert results[0]["lat"] == ""
    assert results[0]["lon"] == ""
    assert option.click_count == 0
