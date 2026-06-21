from __future__ import annotations

from trip_time_service.api import naver_browser_autocomplete, routes_geo


class _FakeInput:
    def __init__(self) -> None:
        self.sent_keys: list[tuple[object, ...]] = []

    def click(self) -> None:
        return None

    def send_keys(self, *keys: object) -> None:
        self.sent_keys.append(keys)


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
        driver: object | None = None,
    ) -> None:
        self.text = text
        self._href = href
        self._click_url = click_url
        self._driver = driver
        self.click_count = 0

    def get_attribute(self, name: str) -> str:
        return self._href if name == "href" else ""

    def find_elements(self, *_args: object) -> list[_FakeAnchor]:
        return [_FakeAnchor(self._href)] if self._href else []

    def click(self) -> None:
        self.click_count += 1
        if self._click_url and self._driver is not None:
            self._driver.current_url = self._click_url


class _FakeDriver:
    def __init__(self) -> None:
        self.input = _FakeInput()
        self.current_url = "https://map.naver.com/"
        self.option_calls = 0
        self.option_sequences = [
            [_FakeOption("검색어\n태안우체국")],
            [_FakeOption("검색어\n태안우체국")],
            [_FakeOption("검색어\n광교역")],
        ]

    def find_element(self, *_args: object) -> _FakeInput:
        return self.input

    def find_elements(self, *_args: object) -> list[_FakeOption]:
        index = min(self.option_calls, len(self.option_sequences) - 1)
        self.option_calls += 1
        return self.option_sequences[index]


class _FakeWebDriverWait:
    def __init__(self, driver: _FakeDriver, _timeout: float) -> None:
        self.driver = driver

    def until(self, condition):
        for _ in range(4):
            result = condition(self.driver)
            if result:
                return result
        raise TimeoutError("condition was not satisfied")


class _NoOptionsWebDriverWait:
    def __init__(self, driver: _FakeDriver, timeout: float) -> None:
        self.driver = driver
        self.timeout = timeout

    def until(self, condition):
        result = condition(self.driver)
        if self.timeout == 2.5:
            return result
        raise TimeoutError("condition was not satisfied")


def test_worker_waits_for_suggestions_matching_current_query(monkeypatch) -> None:
    monkeypatch.setattr(
        naver_browser_autocomplete,
        "WebDriverWait",
        _FakeWebDriverWait,
    )
    worker = naver_browser_autocomplete._BrowserAutocompleteWorker(worker_index=1)
    driver = _FakeDriver()

    results = worker._query_locked(
        driver,
        "광교역",
        limit=5,
        wait_seconds=0.45,
    )

    assert len(results) == 1
    assert results[0]["display_name"] == "광교역"
    assert driver.option_calls == 3


def test_worker_wait_budget_covers_live_naver_suggestion_latency(monkeypatch) -> None:
    class _LatencyAwareWebDriverWait:
        def __init__(self, driver: _FakeDriver, timeout: float) -> None:
            self.driver = driver
            self.timeout = timeout

        def until(self, condition):
            result = condition(self.driver)
            if self.timeout == 2.5:
                return result
            if self.timeout < 5.0:
                raise TimeoutError("live Naver suggestions were not ready yet")
            return result

    monkeypatch.setattr(
        naver_browser_autocomplete,
        "WebDriverWait",
        _LatencyAwareWebDriverWait,
    )
    worker = naver_browser_autocomplete._BrowserAutocompleteWorker(worker_index=1)
    driver = _FakeDriver()
    driver.option_sequences = [
        [_FakeOption("장소\n경기 수원시 팔달구 경수대로680번길 40 센트럴하우스")]
    ]

    results = worker._query_locked(
        driver,
        "경수대로680번길40",
        limit=5,
        wait_seconds=naver_browser_autocomplete._SUGGEST_WAIT_SECONDS,
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

    results = naver_browser_autocomplete._extract_suggestions_from_options(
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

    results = naver_browser_autocomplete._extract_suggestions_from_options(
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

    results = naver_browser_autocomplete._extract_suggestions_from_options(
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
    monkeypatch.setattr(
        naver_browser_autocomplete,
        "WebDriverWait",
        _FakeWebDriverWait,
    )
    fetch_calls = []

    def _fake_fetch(driver, query):
        fetch_calls.append((driver, query))
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
        naver_browser_autocomplete,
        "_fetch_instant_search_json",
        _fake_fetch,
        raising=False,
    )
    worker = naver_browser_autocomplete._BrowserAutocompleteWorker(worker_index=1)
    driver = _FakeDriver()
    option = _FakeOption(
        "장소\n주소\n경수대로680번길 40 센트럴하우스\n경기 수원시 장안구 우만동"
    )
    driver.option_sequences = [[option]]

    results = worker._query_locked(
        driver,
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
    assert fetch_calls == [(driver, "경수대로680번길40")]
    assert option.click_count == 0


def test_query_locked_matches_instant_search_place_by_canonical_text(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        naver_browser_autocomplete,
        "WebDriverWait",
        _FakeWebDriverWait,
    )
    fetch_calls = []

    def _fake_fetch(driver, query):
        fetch_calls.append((driver, query))
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
        naver_browser_autocomplete,
        "_fetch_instant_search_json",
        _fake_fetch,
        raising=False,
    )
    worker = naver_browser_autocomplete._BrowserAutocompleteWorker(worker_index=1)
    driver = _FakeDriver()
    option = _FakeOption(
        "장소\n출입구\n광교역(경기대)1번출구\n경기 수원시 영통구 이의동"
    )
    driver.option_sequences = [[option]]

    results = worker._query_locked(
        driver,
        "광교역",
        limit=5,
        wait_seconds=0.45,
    )

    assert len(results) == 1
    assert results[0]["display_name"] == "광교역(경기대)1번출구"
    assert results[0]["lat"] == "37.3014568"
    assert results[0]["lon"] == "127.0446723"
    assert fetch_calls == [(driver, "광교역")]
    assert option.click_count == 0


def test_query_locked_ignores_unrelated_instant_search_coordinates(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        naver_browser_autocomplete,
        "WebDriverWait",
        _FakeWebDriverWait,
    )
    monkeypatch.setattr(
        naver_browser_autocomplete,
        "_fetch_instant_search_json",
        lambda _driver, _query: {
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
    worker = naver_browser_autocomplete._BrowserAutocompleteWorker(worker_index=1)
    driver = _FakeDriver()
    option = _FakeOption(
        "장소\n출입구\n광교역(경기대)1번출구\n경기 수원시 영통구 이의동",
    )
    driver.option_sequences = [[option]]

    results = worker._query_locked(
        driver,
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
    monkeypatch.setattr(
        naver_browser_autocomplete,
        "WebDriverWait",
        _NoOptionsWebDriverWait,
    )
    fetch_calls = []

    def _fake_fetch(driver, query):
        fetch_calls.append((driver, query))
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
        naver_browser_autocomplete,
        "_fetch_instant_search_json",
        _fake_fetch,
        raising=False,
    )
    worker = naver_browser_autocomplete._BrowserAutocompleteWorker(worker_index=1)
    driver = _FakeDriver()
    driver.option_sequences = [[]]

    results = worker._query_locked(
        driver,
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
    assert fetch_calls == [(driver, "경수대로680번길40")]


def test_query_locked_synthesizes_place_when_dom_options_are_empty(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        naver_browser_autocomplete,
        "WebDriverWait",
        _NoOptionsWebDriverWait,
    )
    fetch_calls = []

    def _fake_fetch(driver, query):
        fetch_calls.append((driver, query))
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
        naver_browser_autocomplete,
        "_fetch_instant_search_json",
        _fake_fetch,
        raising=False,
    )
    worker = naver_browser_autocomplete._BrowserAutocompleteWorker(worker_index=1)
    driver = _FakeDriver()
    driver.option_sequences = [[]]

    results = worker._query_locked(
        driver,
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
    assert fetch_calls == [(driver, "광교역(경기대)1번출구")]


def test_query_locked_synthesis_ignores_unrelated_first_candidate(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        naver_browser_autocomplete,
        "WebDriverWait",
        _NoOptionsWebDriverWait,
    )
    monkeypatch.setattr(
        naver_browser_autocomplete,
        "_fetch_instant_search_json",
        lambda _driver, _query: {
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
    worker = naver_browser_autocomplete._BrowserAutocompleteWorker(worker_index=1)
    driver = _FakeDriver()
    driver.option_sequences = [[]]

    results = worker._query_locked(
        driver,
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
    monkeypatch.setattr(
        naver_browser_autocomplete,
        "WebDriverWait",
        _NoOptionsWebDriverWait,
    )
    monkeypatch.setattr(
        naver_browser_autocomplete,
        "_fetch_instant_search_json",
        lambda _driver, _query: {
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
    worker = naver_browser_autocomplete._BrowserAutocompleteWorker(worker_index=1)
    driver = _FakeDriver()
    driver.option_sequences = [[]]

    results = worker._query_locked(
        driver,
        "광교역(경기대)1번출구",
        limit=5,
        wait_seconds=0.45,
    )

    assert results == ()


def test_query_locked_synthesis_fails_soft_for_endpoint_failure_and_malformed(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        naver_browser_autocomplete,
        "WebDriverWait",
        _NoOptionsWebDriverWait,
    )
    payloads = iter([None, {"address": "not-list"}])
    fetch_calls = []

    def _fake_fetch(driver, query):
        fetch_calls.append((driver, query))
        return next(payloads)

    monkeypatch.setattr(
        naver_browser_autocomplete,
        "_fetch_instant_search_json",
        _fake_fetch,
        raising=False,
    )
    worker = naver_browser_autocomplete._BrowserAutocompleteWorker(worker_index=1)

    first_driver = _FakeDriver()
    first_driver.option_sequences = [[]]
    assert (
        worker._query_locked(
            first_driver,
            "경수대로680번길40",
            limit=5,
            wait_seconds=0.45,
        )
        == ()
    )

    second_driver = _FakeDriver()
    second_driver.option_sequences = [[]]
    assert (
        worker._query_locked(
            second_driver,
            "경수대로680번길40",
            limit=5,
            wait_seconds=0.45,
        )
        == ()
    )
    assert fetch_calls == [
        (first_driver, "경수대로680번길40"),
        (second_driver, "경수대로680번길40"),
    ]


def test_query_locked_uses_direct_instant_search_after_js_fetch_failure(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        naver_browser_autocomplete,
        "WebDriverWait",
        _NoOptionsWebDriverWait,
    )
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
        naver_browser_autocomplete,
        "_direct_fetch_instant_search_json",
        _fake_direct_fetch,
        raising=False,
    )
    worker = naver_browser_autocomplete._BrowserAutocompleteWorker(worker_index=1)
    driver = _FakeDriver()
    driver.option_sequences = [[]]

    results = worker._query_locked(
        driver,
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
    monkeypatch.setattr(
        naver_browser_autocomplete,
        "WebDriverWait",
        _NoOptionsWebDriverWait,
    )
    payloads = iter([None, {"captcha": "ncaptcha"}, "malformed", TimeoutError()])
    direct_calls = []

    def _fake_direct_fetch(query):
        direct_calls.append(query)
        payload = next(payloads)
        if isinstance(payload, Exception):
            raise payload
        return payload

    monkeypatch.setattr(
        naver_browser_autocomplete,
        "_direct_fetch_instant_search_json",
        _fake_direct_fetch,
        raising=False,
    )
    worker = naver_browser_autocomplete._BrowserAutocompleteWorker(worker_index=1)

    for _ in range(4):
        driver = _FakeDriver()
        driver.option_sequences = [[]]
        assert (
            worker._query_locked(
                driver,
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
        naver_browser_autocomplete,
        "_direct_fetch_instant_search_json",
        _fake_direct_fetch,
        raising=False,
    )

    class _JsFailureDriver(_FakeDriver):
        def execute_async_script(self, *_args: object) -> dict:
            return {"ok": False, "status": 500, "text": "error"}

    class _JsSuccessDriver(_FakeDriver):
        def execute_async_script(self, *_args: object) -> dict:
            return {
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

    assert naver_browser_autocomplete._fetch_instant_search_json(
        _JsFailureDriver(),
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

    assert naver_browser_autocomplete._fetch_instant_search_json(
        _JsSuccessDriver(),
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
    monkeypatch.setattr(
        naver_browser_autocomplete,
        "WebDriverWait",
        _NoOptionsWebDriverWait,
    )
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
        naver_browser_autocomplete,
        "_direct_fetch_instant_search_json",
        _fake_direct_fetch,
        raising=False,
    )
    worker = naver_browser_autocomplete._BrowserAutocompleteWorker(worker_index=1)
    driver = _FakeDriver()
    driver.option_sequences = [[]]

    results = worker._query_locked(
        driver,
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
        naver_browser_autocomplete,
        "_direct_fetch_instant_search_json",
        _fake_direct_fetch,
        raising=False,
    )

    results = naver_browser_autocomplete.fetch_autocomplete_from_instant_search(
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
    monkeypatch.setattr(
        naver_browser_autocomplete,
        "WebDriverWait",
        _FakeWebDriverWait,
    )
    monkeypatch.setattr(
        naver_browser_autocomplete,
        "_fetch_instant_search_json",
        lambda _driver, _query: {
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
    worker = naver_browser_autocomplete._BrowserAutocompleteWorker(worker_index=1)
    driver = _FakeDriver()
    option = _FakeOption(
        "장소\n출입구\n광교역(경기대)1번출구\n경기 수원시 영통구 이의동"
    )
    driver.option_sequences = [[option]]

    results = worker._query_locked(
        driver,
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
    monkeypatch.setattr(
        naver_browser_autocomplete,
        "WebDriverWait",
        _FakeWebDriverWait,
    )

    def _fake_fetch(_driver, _query):
        raise RuntimeError("429 ncaptcha")

    monkeypatch.setattr(
        naver_browser_autocomplete,
        "_fetch_instant_search_json",
        _fake_fetch,
        raising=False,
    )
    worker = naver_browser_autocomplete._BrowserAutocompleteWorker(worker_index=1)
    driver = _FakeDriver()
    option = _FakeOption(
        "장소\n출입구\n광교역(경기대)1번출구\n경기 수원시 영통구 이의동",
        click_url=(
            "https://map.naver.com/p/place/"
            "14142473.386372,4481285.70836642,광교역"
        ),
        driver=driver,
    )
    driver.option_sequences = [[option]]

    results = worker._query_locked(
        driver,
        "광교역",
        limit=5,
        wait_seconds=0.45,
    )

    assert len(results) == 1
    assert results[0]["lat"] == ""
    assert results[0]["lon"] == ""
    assert option.click_count == 0
