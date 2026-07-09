# Phase 2 Playwright Adapter Boundary

## Purpose

- 네이버 browser automation의 adapter 경계를 고정하고, Playwright 런타임
  전환 이후의 현재 구조를 기록한다.
- Selenium 런타임 모듈(`naver_selenium.py`, `naver_browser_autocomplete.py`,
  `naver_geo.py`, `chrome_driver.py`)은 Task 6에서 삭제되었다.

## Migration Status (Task 6)

- 기본 provider: `naver_playwright` (`TTS_PROVIDER` 미설정 시)
- `naver_selenium`은 factory에서 거부되며 Playwright로 안내한다
- 프로덕션 `src/`에는 Selenium import가 없다
- live/E2E 하네스 기본값도 `naver_playwright`다

## Current Runtime Boundary

### 1. Coord-first direct URL path는 유지 대상

- [routes_trip.py](../src/trip_time_service/api/routes_trip.py) 의
  `_prepare_service_and_coords()`는 frontend `origin_coords` / `dest_coords`를
  `coords_map`으로 모아 provider에 주입한다.
- [geocode_services.py](../src/trip_time_service/api/geocode_services.py) 의
  `pre_geocode_for_provider()`는 duck-typed `set_coords()`가 있으면 provider
  내부 좌표 캐시를 채운다.
- [naver_playwright.py](../src/trip_time_service/providers/naver_playwright.py) 의
  `_build_directions_url()` / `_navigate_and_search()`가 이 좌표 캐시로
  `/p/directions/.../-/car` URL을 우선 사용한다.
- 이 경로는 autocomplete UI 구조와 거의 독립적이라서 안정 경계로 유지한다.

### 2. UI text-search fallback

- `_navigate_and_search_ac()`는 `https://map.naver.com/p/directions/-/-/-/car`
  로 들어간 뒤 `input.input_search` 2개를 찾는다.
- 출발/도착 선택은 `_select_place_ac()`에 묶여 있다.
- Playwright 경로는 보이는 DOM item을 클릭하고, 보이는 item이 없을 때만
  Enter fallback을 쓴다.

### 3. Departure time picker

- `_open_later_modal()`, `_ensure_picker_open()`, `_set_time_and_read()`,
  `_set_dropdown_value()`는 `button.later_departure_btn`,
  `button.later_departure_confirm_btn`, `button.dropdown_btn`,
  `[role='option']`, `button.calendar_day_btn` 등 명시적 selector를 쓴다.
- search path와 time-picker path는 `_search_adapter` /
  `_departure_picker` helper로 분리되어 있다.

### 4. Browser autocomplete worker

- [naver_playwright_autocomplete.py](../src/trip_time_service/api/naver_playwright_autocomplete.py)
  의 `NaverBrowserAutocompletePool`과 worker는 공유 Playwright browser
  runtime을 사용한다.
- query path는 `_query_locked()`에 모여 있고, `input.input_search`와
  `.scroll_box [role='option']` selector에 의존한다.
- 좌표 파싱은 [naver_playwright_geo.py](../src/trip_time_service/api/naver_playwright_geo.py)
  에서 공유한다.

### 5. Browser lifecycle

- Selenium `chrome_driver.py`는 삭제되었다.
- Playwright browser/context/page lifecycle은
  [playwright_runtime.py](../src/trip_time_service/playwright_runtime.py) 와
  provider/autocomplete/geo 모듈이 담당한다.

## Adapter Extraction Implication

### Preserve high-level provider behavior

- `TripTimeService`와 route API는 여전히 "provider에 route/time을 묻는다"는
  계약을 유지한다.
- Playwright provider는 기존 외부 계약(공개 API, 캐시, 좌표 duck typing,
  pool)을 유지한다.

### Seam shape

- `open_directions_with_coords(route)`:
  direct URL 접속 성공 여부만 책임진다.
- `search_route_by_text(origin, destination)`:
  directions page 진입, autocomplete DOM 확인, item 선택, search submit만
  책임진다.
- `open_departure_picker()` / `set_departure_datetime()` / `read_duration()`:
  time-picker 상태 머신만 책임진다.
- `close()`:
  browser/session/process cleanup만 책임진다.

- [test_naver_playwright_adapter_seams.py](../tests/test_naver_playwright_adapter_seams.py)
  가 seam 존재와 delegation을 회귀로 고정한다.

### Optional interface debt

- [base.py](../src/trip_time_service/providers/base.py) 의
  `TravelTimeProvider` protocol에는 `set_coords()`가 없다.
- 반면 [geocode_services.py](../src/trip_time_service/api/geocode_services.py)
  는 `hasattr(provider, "set_coords")`로 분기한다.
- `CoordinateAwareProvider`를 본계약으로 승격할지, duck typing을 유지할지는
  후속 결정 사항이다.

## E2E Harness Consequence

- [live_utils.ts](../tests/e2e/live_utils.ts) 의 `selectAutocompleteEntry()`는
  선택 payload에 대해 `coords_ready === true`, `lat != null`, `lon != null`를
  강제한다.
- 따라서 unresolved stable selection(`coords_ready=false`)은 현재 generic
  `runRouteScenario()` 경로로 검증할 수 없다.
- [road-address-route.spec.ts](../tests/e2e/road-address-route.spec.ts) 가
  별도 selection helper를 쓰는 이유도 이 제약 때문이다.
- live/E2E 기본 provider는 `naver_playwright`다.

## Recommendation For Next Patch

1. 사용자가 여전히 "로컬 UI에서만 실패"를 보면 cold autocomplete `ncaptcha`와
   provider 이슈를 분리 캡처한다.
2. `CoordinateAwareProvider`를 `TravelTimeProvider` 본계약으로 승격할지
   여부는 shared adapter / provider contract 범위를 본 뒤 결정한다.
3. Task 7: 로컬·라이브 검증과 feature 브랜치 dev 배포를 진행한다.
