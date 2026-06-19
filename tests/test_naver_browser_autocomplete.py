from __future__ import annotations

from trip_time_service.api import naver_browser_autocomplete


class _FakeInput:
    def __init__(self) -> None:
        self.sent_keys: list[tuple[object, ...]] = []

    def click(self) -> None:
        return None

    def send_keys(self, *keys: object) -> None:
        self.sent_keys.append(keys)


class _FakeOption:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeDriver:
    def __init__(self) -> None:
        self.input = _FakeInput()
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
