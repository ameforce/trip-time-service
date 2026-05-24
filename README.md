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

`TTS_PROVIDER`를 비우면 기본은 `naver_selenium`입니다(`src/trip_time_service/config.py`). 브라우저 없이 돌리려면 `mock`을 명시하세요.

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

- `naver_selenium`(미설정 시 기본): 네이버 지도 Selenium/Chromium 기반
- `mock`: 브라우저 없이 동작하는 개발 전용

```cmd
set TTS_PROVIDER=mock
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
- `TTS_RECOMMEND_MIN_SAMPLES` (기본 12, 도착 시각 기준 추천 시 최소 분석 샘플 수)
- `TTS_PORT_STRICT` (기본 `false`, `1`이면 지정 포트 점유 시 fallback 없이 시작 실패. E2E에서 harness/app 포트 불일치 방지용)
- `TTS_NAVER_MAP_CLIENT_ID` (선택, 네이버 클라우드 플랫폼 Maps API Client ID)
- `TTS_CORS_ALLOW_ORIGINS` (선택, CORS 허용 오리진 CSV. 예: `http://localhost:3000,https://example.com`)
- `TTS_AUTOCOMPLETE_BROWSER_ENABLE` (기본 `true`, 브라우저 자동완성 폴백 사용 여부)
- `TTS_AUTOCOMPLETE_BROWSER_HARD_CAP` (기본 6, 브라우저 워커 최대 상한)
- `TTS_AUTOCOMPLETE_BROWSER_MIN_WARM` (기본 1, idle 유지 워커 최소 수)
- `TTS_AUTOCOMPLETE_BROWSER_IDLE_TTL_SECONDS` (기본 180, 유휴 워커 세션 정리 기준)
- `TTS_AUTOCOMPLETE_BROWSER_WORKER_MEM_MB` (기본 550, 워커 1개당 메모리 예산 MB)
- `TTS_AUTOCOMPLETE_BROWSER_MEM_RESERVE_MB` (기본 1024, 시스템/앱 보호용 예약 메모리 MB)
- `TTS_AUTOCOMPLETE_BROWSER_SCALE_INTERVAL_SECONDS` (기본 10, 워커 리밸런싱 주기)

디버그 엔드포인트:
- `POST /api/debug/autocomplete/cache-clear` (autocomplete 캐시/브라우저 풀 초기화. 원격에서 쓰려면 `TTS_ENABLE_DEBUG_ROUTES=1`와 `TTS_DEBUG_TOKEN`/`X-TTS-Debug-Token`이 필요)
- `GET /api/debug/autocomplete/runtime` (브라우저 워커 수, 상한, busy miss 비율, `ncaptcha_backoff`, fixture/live mode, provider/autocomplete degraded counter. 원격에서 쓰려면 `TTS_ENABLE_DEBUG_ROUTES=1`와 debug token 필요)

## 테스트/린트(기본 검증)

```cmd
uv run --no-sync --extra dev pytest tests/test_time_utils.py tests/test_models.py tests/test_cache.py tests/test_versioning.py tests/test_main_port_fallback.py -q
uv run --no-sync --extra dev ruff check src tests
```

## E2E 테스트 (Playwright)

CI 기본 gate는 외부 Naver/브라우저 autocomplete/geocode/OSRM/route provider를 호출하지 않는 deterministic lane입니다.

```sh
npm run e2e:ci -- --reporter=list
npm run e2e:report
```

- 기본 산출물 경로: `.artifacts/live/`
- `e2e:ci`는 `.artifacts/e2e-ci/`를 쓰며 `TTS_PROVIDER=mock`, `TTS_E2E_FIXTURE_MODE=1`, `TTS_PORT_STRICT=1`로 서버를 자동 기동합니다. Live summary를 덮어쓰지 않습니다.
- fixture mode에서는 `/api/autocomplete`, `/api/geocode`, `/api/route`가 test fixture를 사용하고 debug runtime에 `external_provider_calls=0` 및 provider별 zero-call breakdown을 기록합니다.
- route smoke는 mock lane에서도 `.recommendation-card`를 요구합니다. provider-degraded branch로 green 처리하지 않습니다.
- `e2e:live`/`e2e:live:smoke`는 fixture를 끄고 `naver_selenium` provider로 실제 Naver drift를 검증합니다. Captcha/private UI drift/provider-degraded는 테스트 내부에서 skip/pass하지 않고 classified failure artifact로 남깁니다. Jenkins에서는 `LIVE_E2E_POLICY=off|advisory|blocking`으로만 blocking 여부를 제어합니다.
- live wrapper/teardown은 Playwright 종료 후 기본 archive-safe summary인 `.artifacts/live/e2e-live-summary.json`을 다시 씁니다. 실패한 live run도 `failed_count`와 bucket count를 남기며, 이 summary는 bucket/count/report id만 포함하고 raw DOM, full request body, origin/destination query, clicked label, selected value, env secret은 포함하지 않습니다.
- 각 smoke case는 `test-results` 아래 스크린샷 3장(초기/입력완료/최종)과 JSON report를 남기며, suite manifest는 `.artifacts/live/suite-artifacts.json`에 기록됩니다.
- 종료 후 temp `TTS_CHROME_USER_DATA_DIR` 기준 Chromium residual probe 결과는 `.artifacts/live/shutdown-leak-report.json`에 기록됩니다.
- prerequisite:
  - `e2e:ci`: Playwright Chromium 실행 가능
  - `e2e:live`: 외부 네트워크 접근 가능, Selenium Manager 또는 Chrome/Chromium 실행 가능
- live lane:

```sh
npm run e2e:live -- --reporter=list
npm run e2e:live:smoke -- --reporter=list
npm run e2e:live:diagnose -- --reporter=list
npm run e2e:live -- --headed --reporter=list
```

- extended live suite는 명시적 opt-in일 때만 실행:

```sh
npm run e2e:live:extended -- --reporter=list
```

Live failure buckets:

- `ncaptcha_backoff`: Naver captcha/private UI backoff로 provider 접근이 제한됨
- `panel_parse_timeout`: provider panel DOM/text 구조 drift 또는 로딩 지연
- `provider_retry_exhausted`: provider retry 한도를 초과한 route/autocomplete 실패
- `coords_unresolved`: route-critical autocomplete selection 좌표 미해결
- `stream_stall_timeout`: 추천 스트림 worker idle timeout
- `environment_unavailable`: 브라우저/network/provider runtime 기동 실패 또는 report 미생성
- `unknown`: 위 bucket으로 분류되지 않는 live 실패

## 자동완성 성능 벤치 / live corpus report

```cmd
uv run trip-time-service
uv run --no-sync python scripts/benchmark_autocomplete_latency.py --base-url http://127.0.0.1:8500 --dataset tests/live/data/autocomplete-blocking.json --cold-rounds 1 --hot-rounds 2
uv run --no-sync python scripts/benchmark_autocomplete_latency.py --base-url http://127.0.0.1:8500 --dataset tests/live/data/autocomplete-extended.json --cold-rounds 1 --hot-rounds 2 --report .artifacts/live/autocomplete-extended.json
```

- report에는 per-query latency, empty retry 결과, source 분포, top-hit token match가 기록됩니다.

## 수동 UI 검증 (live-only)

```cmd
uv run --no-sync python scripts/verify_ui_departure_search.py
uv run --no-sync python scripts/verify_ui_departure_search.py --headed --case-index 1
```

- 기본 dataset: `tests/live/data/routes-blocking.json`의 departure scenario
- 산출물: `.artifacts/live/manual-ui/` 아래 스크린샷 4장과 `verify-ui-departure-search.json`

## 버전 표기 규칙

- 런타임 표기 버전은 `vMAJOR.MINOR.PATCH.COMMIT` 형식을 사용합니다.
- 계산 우선순위:
  1. `TTS_VERSION` 환경변수
  2. Git tag(`vMAJOR.MINOR.PATCH`) + tag 이후 commit 수
  3. fallback: 패키지 버전 기반(`v0.1.0.0` 형태)
- 버전 계산 확인:

```cmd
uv run --no-sync python scripts/version/resolve_version.py
```

## Docker 이미지 빌드

```cmd
cmd /c docker build --build-arg APP_VERSION=v0.1.0.0 -t trip-time-service:local .
cmd /c docker run --rm -p 8500:8500 trip-time-service:local
```

## Jenkins + ENM 배포 개요

- 파이프라인 정의: `Jenkinsfile`
- 브랜치 매핑:
  - `main` -> `https://triptime.enmsoftware.com`
  - non-main -> `https://dev.triptime.enmsoftware.com`
- 원격 배포/롤백 스크립트:
  - `deploy/enm/deploy.sh`
  - `deploy/enm/rollback.sh`
- 운영 문서:
  - `docs/deploy/enm-moneyflow-baseline.md`
  - `docs/deploy/jenkins-triptime-runbook.md`
  - `docs/deploy/enm-triptime-runbook.md`

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
