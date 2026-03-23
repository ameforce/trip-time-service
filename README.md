# trip-time-service

네이버 지도(차량)에서 **출발 시각에 따라 달라지는 예상 소요시간**을 수집해,

- **출발 시각 입력 → 도착 시각 계산 + 출발 시각 분석(추천 포함)**
- **도착 시각 입력 → “덜 막히는(쾌적한)” 출발 시각 추천**

을 제공하는 Python 서비스입니다. 초기 구현은 Selenium/Chromium 자동화 기반(Provider)으로 진행하며,
향후 웹 페이지 제공을 위해 FastAPI HTTP API를 중심에 둡니다.

## 빠른 시작 (uv)

```cmd
uv sync
```

개발 의존성 포함 설치:

```cmd
uv sync --extra dev
```

## 실행

기본 provider는 `mock`입니다(브라우저 없이 동작).

```cmd
uv run trip-time-service
```

기본 포트는 **8500**입니다. 다른 포트/호스트로 실행:

```cmd
set TTS_HOST=0.0.0.0
set TTS_PORT=9000
uv run trip-time-service
```

## API 예시

### 1) 출발 시각 입력 → 도착 시각

```cmd
curl -X POST http://127.0.0.1:8500/v1/trip/arrival-time -H "Content-Type: application/json" -d "{\"origin\":\"강남역\",\"destination\":\"판교역\",\"departure_time\":\"2026-01-24T08:10:00+09:00\"}"
```

### 2) 도착 시각 입력 → 추천 출발 시각

```cmd
curl -X POST http://127.0.0.1:8500/v1/trip/recommended-departure-time -H "Content-Type: application/json" -d "{\"origin\":\"강남역\",\"destination\":\"판교역\",\"desired_arrival_time\":\"2026-01-24T09:00:00+09:00\"}"
```

## Provider 선택

환경변수 `TTS_PROVIDER`로 provider를 바꿀 수 있습니다.

- `mock`(기본): 브라우저 없이 동작하는 개발/테스트용
- `naver_selenium`: 네이버 지도 Selenium/Chromium 기반(현재 selector/flow는 TODO 상태)

```cmd
set TTS_PROVIDER=naver_selenium
uv run trip-time-service
```

> 참고: Selenium 4.x는 Selenium Manager를 통해 ChromeDriver를 자동으로 준비할 수 있습니다(최초 실행 시 네트워크 필요).

## 웹 UI

서버 실행 후 브라우저에서 `http://127.0.0.1:8500` 에 접속하면 웹 UI를 사용할 수 있습니다.

- 출발지/도착지 텍스트 입력
- **출발 시각 기준**: 입력한 출발 시각으로 단일 조회를 먼저 표시한 뒤, 추천 출발 분석 카드로 갱신
- **도착 시각 기준**: 희망 도착 시각 기준 추천 출발 시각 계산 (현재 시각 출발 단일 조회는 참고용 카드로 별도 표시)
- 네이버 맵 연동(선택): `TTS_NAVER_MAP_CLIENT_ID`를 설정하면 지도에 출발지/도착지 마커 표시

## 주요 설정(환경변수)

- `TTS_TIMEZONE` (기본 `Asia/Seoul`)
- `TTS_HEADLESS` (기본 `true`)
- `TTS_CACHE_TTL_SECONDS` (기본 600)
- `TTS_STEP_MINUTES` (기본 10)
- `TTS_LOOKBACK_HOURS` (기본 3)
- `TTS_MAX_QUERIES` (기본 120)
- `TTS_NAVER_MAP_CLIENT_ID` (선택, 네이버 클라우드 플랫폼 Maps API Client ID)
- `TTS_CORS_ALLOW_ORIGINS` (선택, CORS 허용 오리진 CSV. 예: `http://localhost:3000,https://example.com`)

## 테스트/린트(PEP8)

```cmd
uv run --extra dev pytest
uv run --extra dev ruff check .
uv run --extra dev ruff format .
```

## E2E 테스트 (Playwright)

```cmd
cmd /c npx playwright test
cmd /c npx playwright test --headed
cmd /c npx playwright show-report .artifacts/e2e/playwright-report
```

- 기본 산출물 경로: `.artifacts/e2e/`
- 테스트는 서버를 자동 기동(`uv run trip-time-service`)하며 `mock` provider를 사용합니다.

## 리뷰게이트 (Cursor 우선)

```cmd
cmd /c .cursor\review\run-review-gate.cmd cursor-first
```

- 완료 후 결과 확인:

```cmd
cmd /c type .cursor\review\out\review-result.txt
```

## 주의사항

- 네이버 지도 UI는 수시로 변경될 수 있어 자동화가 쉽게 깨질 수 있습니다.
- 과도한 요청은 차단/제한의 원인이 될 수 있으므로 캐시/쿼리 상한을 유지하세요.
- 서비스 제공 전, 데이터 이용 정책(약관/허용 범위)을 반드시 검토하세요.
