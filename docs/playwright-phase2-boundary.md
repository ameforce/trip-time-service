# Phase 2 Playwright Adapter Boundary

## Purpose

- `Selenium` 기반 네이버 browser automation을 `Playwright` adapter로 바꾸기 전에, 지금 코드가 어떤 경계로 묶여 있는지 먼저 고정한다.
- 이번 문서는 구조 메모다. 아직 runtime behavior는 바꾸지 않는다.

## Current Runtime Boundary

### 1. Coord-first direct URL path는 유지 대상

- [routes_trip.py](/C:/Workspace/Daeng/Git/trip-time-service/src/trip_time_service/api/routes_trip.py#L35) 의 `_prepare_service_and_coords()`는 frontend `origin_coords` / `dest_coords`를 `coords_map`으로 모아 provider에 주입한다.
- [geocode_services.py](/C:/Workspace/Daeng/Git/trip-time-service/src/trip_time_service/api/geocode_services.py#L1164) 의 `pre_geocode_for_provider()`는 duck-typed `set_coords()`가 있으면 provider 내부 좌표 캐시를 채운다.
- [naver_selenium.py](/C:/Workspace/Daeng/Git/trip-time-service/src/trip_time_service/providers/naver_selenium.py#L313) 의 `_build_directions_url()`은 이 좌표 캐시를 이용해 `/p/directions/.../-/car` URL을 만들고, [naver_selenium.py](/C:/Workspace/Daeng/Git/trip-time-service/src/trip_time_service/providers/naver_selenium.py#L337) 의 `_navigate_and_search()`가 우선 이 경로를 탄다.
- 이 경로는 autocomplete UI 구조와 거의 독립적이라서, `Playwright` 전환 시에도 먼저 보존해야 하는 안정 경계다.

### 2. UI text-search fallback가 현재 가장 취약한 구간이다

- [naver_selenium.py](/C:/Workspace/Daeng/Git/trip-time-service/src/trip_time_service/providers/naver_selenium.py#L359) 의 `_navigate_and_search_ac()`는 `https://map.naver.com/p/directions/-/-/-/car`로 들어간 뒤 `input.input_search` 2개를 다시 찾는다.
- 출발/도착 선택은 [naver_selenium.py](/C:/Workspace/Daeng/Git/trip-time-service/src/trip_time_service/providers/naver_selenium.py#L398) 의 `_select_place_ac()`에 묶여 있다.
- 이 함수는 `.list_place li.item_place` visible item 존재만 확인한 뒤 실제 item click이 아니라 `ARROW_DOWN`, `ARROW_DOWN`, `ENTER`를 고정 순서로 보낸다.
- 결과가 없으면 다시 `ENTER`로 text search fallback을 태운다. 즉, "어떤 DOM item을 선택했는지"가 adapter 경계 밖으로 드러나지 않는다.
- `Playwright` 전환의 핵심은 webdriver 교체 자체보다, 이 blind key-navigation을 "선택된 item 확인 가능"한 DOM-driven flow로 바꾸는 것이다.

### 3. Departure time picker는 search path보다 치환 난도가 낮다

- [naver_selenium.py](/C:/Workspace/Daeng/Git/trip-time-service/src/trip_time_service/providers/naver_selenium.py#L516) 의 `_open_later_modal()`
- [naver_selenium.py](/C:/Workspace/Daeng/Git/trip-time-service/src/trip_time_service/providers/naver_selenium.py#L561) 의 `_ensure_picker_open()`
- [naver_selenium.py](/C:/Workspace/Daeng/Git/trip-time-service/src/trip_time_service/providers/naver_selenium.py#L607) 의 `_set_time_and_read()`
- [naver_selenium.py](/C:/Workspace/Daeng/Git/trip-time-service/src/trip_time_service/providers/naver_selenium.py#L939) 의 `_set_dropdown_value()`

이 구간은 `button.later_departure_btn`, `button.later_departure_confirm_btn`, `button.dropdown_btn`, `[role='option']`, `button.calendar_day_btn`처럼 상대적으로 명시적인 selector를 쓴다. `Playwright` adapter 1차 목표는 search path와 time-picker path를 분리해, 후자는 거의 동일한 상태 머신으로 옮길 수 있게 만드는 것이다.

### 4. Browser autocomplete worker가 bootstrap 로직을 중복한다

- [naver_browser_autocomplete.py](/C:/Workspace/Daeng/Git/trip-time-service/src/trip_time_service/api/naver_browser_autocomplete.py#L548) 의 `NaverBrowserAutocompletePool`과 worker는 별도 Chrome lifecycle을 가진다.
- query path는 [naver_browser_autocomplete.py](/C:/Workspace/Daeng/Git/trip-time-service/src/trip_time_service/api/naver_browser_autocomplete.py#L456) 의 `_query_locked()` 하나에 실질적으로 모여 있다.
- 여기서도 `input.input_search`와 `.scroll_box [role='option']` selector에 의존하고, 결과는 [naver_browser_autocomplete.py](/C:/Workspace/Daeng/Git/trip-time-service/src/trip_time_service/api/naver_browser_autocomplete.py#L486) 의 `_extract_suggestions_from_options()`가 text만 파싱한다.
- 즉 provider search fallback과 browser autocomplete pool은 "Naver map bootstrap + input search + suggestion parsing" 계약을 중복 구현하고 있다.

### 5. Driver lifecycle 공통화는 작은 모듈부터 시작했다

- [chrome_driver.py](/C:/Workspace/Daeng/Git/trip-time-service/src/trip_time_service/chrome_driver.py)가 추가됐다.
- 현재 공통화된 범위는 `build_chrome_options()`, `force_kill_webdriver_process()`, `close_webdriver_with_timeout()` 세 가지다.
- [naver_browser_autocomplete.py](/C:/Workspace/Daeng/Git/trip-time-service/src/trip_time_service/api/naver_browser_autocomplete.py) 와 [naver_selenium.py](/C:/Workspace/Daeng/Git/trip-time-service/src/trip_time_service/providers/naver_selenium.py) 는 이 모듈을 사용하지만, profile-dir scavenging과 worker별 `user-data-dir` suffix 생성은 caller에 남겨 두었다.
- 즉 전면 session adapter 이전에 lifecycle 중복부터 먼저 줄이는 전략으로 갔다.

## Adapter Extraction Implication

### Preserve high-level provider behavior

- `TripTimeService`와 route API는 여전히 "provider에 route/time을 묻는다"는 현재 계약을 유지하는 편이 낮은 위험이다.
- Phase 2는 provider 외부 contract를 바꾸기보다, `naver_selenium` 내부를 browser adapter 경계로 분리하는 작업이 우선이다.

### First seam extracted

- [naver_selenium.py](/C:/Workspace/Daeng/Git/trip-time-service/src/trip_time_service/providers/naver_selenium.py)에는 이제 private helper `_search_adapter`, `_departure_picker`가 있다.
- `_search_adapter`는 direct URL path / autocomplete text-search fallback / place selection을 맡고, `_departure_picker`는 modal open / calendar / dropdown / duration read를 맡는다.
- provider의 기존 private method 이름(`_navigate_and_search()`, `_set_time_and_read()` 등)은 wrapper로 남아 있지만, [naver_selenium.py](/C:/Workspace/Daeng/Git/trip-time-service/src/trip_time_service/providers/naver_selenium.py)의 `_query_locked()`는 helper delegation을 직접 사용한다.
- [test_naver_selenium_adapter_seams.py](/C:/Workspace/Daeng/Git/trip-time-service/tests/test_naver_selenium_adapter_seams.py)가 이 seam 존재와 delegation을 회귀로 고정한다.

### First seam shape

- `open_directions_with_coords(route)`:
  direct URL 접속 성공 여부만 책임진다.
- `search_route_by_text(origin, destination)`:
  directions page 진입, autocomplete DOM 확인, item 선택, search submit만 책임진다.
- `open_departure_picker()` / `set_departure_datetime()` / `read_duration()`:
  time-picker 상태 머신만 책임진다.
- `close()`:
  browser/session/process cleanup만 책임진다.

### Optional interface debt

- [base.py](/C:/Workspace/Daeng/Git/trip-time-service/src/trip_time_service/providers/base.py#L22) 의 `TravelTimeProvider` protocol에는 `set_coords()`가 없다.
- 반면 [geocode_services.py](/C:/Workspace/Daeng/Git/trip-time-service/src/trip_time_service/api/geocode_services.py#L1169) 는 `hasattr(provider, "set_coords")`로 분기한다.
- `Playwright` adapter 착수 전에 `CoordinateAwareProvider` 같은 optional protocol을 둘지, 현재 duck typing을 유지할지 먼저 결정해야 한다.

## E2E Harness Consequence

- [live_utils.ts](/C:/Workspace/Daeng/Git/trip-time-service/tests/e2e/live_utils.ts#L196) 의 `selectAutocompleteEntry()`는 선택 payload에 대해 `coords_ready === true`, `lat != null`, `lon != null`를 강제한다.
- 따라서 unresolved stable selection(`coords_ready=false`)은 현재 generic `runRouteScenario()` 경로로 검증할 수 없다.
- [road-address-route.spec.ts](/C:/Workspace/Daeng/Git/trip-time-service/tests/e2e/road-address-route.spec.ts#L27) 가 별도 selection helper를 쓰는 이유도 이 제약 때문이다.
- 현재 [road-address-route.spec.ts](/C:/Workspace/Daeng/Git/trip-time-service/tests/e2e/road-address-route.spec.ts)는 `경수대로680번길40 -> 잠실역`, `경수대로680번길40 -> 네이버 1784`, `잠실역 -> 경수대로680번길40` 세 케이스를 dedicated corpus로 고정한다.
- 결론:
  unresolved road-address regression은 당분간 `routes-extended.json`이 아니라 dedicated spec 패턴으로 고정해야 한다.

## Candidate Matrix

### `routes-extended.json`에 넣을 generic coords-ready 후보

- `세종대로 110 -> 서울대병원`
  `autocomplete-extended.json`에 이미 있는 coords-ready road-address + large POI 조합이다.
- `성균관대학교 자연과학캠퍼스 -> 판교역`
  긴 POI token + station 조합으로 DOM 선택 안정성을 본다.
- `서울대병원 -> 한강대로 405`
  verbose POI -> coords-ready road-address 역방향 조합이다.

### dedicated `road-address-route.spec.ts` 패턴으로 고정할 unresolved 후보

- `경수대로680번길40 -> 잠실역`
  현재 baseline.
- `경수대로680번길40 -> 네이버 1784`
  unresolved road-address origin -> office POI.
- `잠실역 -> 경수대로680번길40`
  unresolved road-address destination 역방향.

## Recommendation For Next Patch

1. 사용자가 여전히 "로컬 UI에서만 실패"를 보면 cold autocomplete `ncaptcha`와 provider 이슈를 분리 캡처한다.
2. `chrome_driver.py`에 남겨 둔 caller-specific 범위(profile scavenging, worker suffix)를 더 공통화할지 결정한다.
3. `CoordinateAwareProvider`를 `TravelTimeProvider` 본계약으로 승격할지 여부는 shared adapter / provider contract 범위를 본 뒤 결정한다.
