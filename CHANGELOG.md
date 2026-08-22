# Changelog

형식: [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/) 를 따르며,
각 항목은 태그 간 실제 diff(`git diff v이전..v신규`) 검증을 근거로 한다.

## [v0.3.0] - 2026-08-22

### 수정 — 잘못된 정보 차단
- **OSRM 실패 시 가짜 값 제거**: 외부 경로 API(지오코딩/경로 조회) 실패 시
  "30분" 고정값을 반환해 캐시까지 저장하던 동작을 제거하고, 재시도 가능한
  `ProviderError`(503)로 승격했다.
- **OSRM 지오코딩 음성 캐시 제거**: 첫 실패가 캐시되어 프로세스 재시작까지
  재시도가 무의미했던 문제를 수정했다.

### 수정 — 프론트엔드 가용성
- **정적 JS MIME 고정**: OS MIME 설정 오염으로 `.js`가 `text/plain`으로
  서빙될 경우 nosniff 정책에 의해 웹 UI 전체가 실행 거부되던 환경적 결함을
  앱 시작 시 MIME 고정으로 차단했다.

### 변경 — 운영 기본값
- `TTS_RELOAD` 코드 기본값을 `false`로 변경(Dockerfile·배포 env는 기존부터
  false 명시로 실배포 동작 변화 없음). 개발 시에만 명시적으로 활성화.

### 추가 — 품질 자산
- 크로스 디바이스 E2E 매트릭스 신규(39 tests): Chromium/Firefox/WebKit ×
  iPhone SE~FHD 5종 해상도에서 렌더링·가로 오버플로·검색 플로우·모바일
  사이드바·날짜/시간 피커 회귀 검증.
- OSRM 에러 처리 회귀 테스트 5종 추가.

### 문서
- AGENTS.md 리뷰 게이트 중 글로벌 계약과 충돌하던 조항 폐기 및 위임으로 대체.
- README에 `TTS_RELOAD` 기본값 문서화.

## [v0.2.1] - 2026-07-09

- Playwright close hang이 shutdown을 막지 않도록 수정(hotfix).
