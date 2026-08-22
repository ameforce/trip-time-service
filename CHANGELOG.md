# Changelog

형식: [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/) 를 따르며,
각 항목은 태그 간 실제 diff(`git diff v이전..v신규`) 검증을 근거로 한다.

## [v0.3.1] - 2026-08-22

### 수정 — 모바일 해상도 사용 불가 (RCA)
- **날짜/시간 패널 뷰포트 이탈 제거(근본 수정)**: ≤768px에서
  `.sidebar.open { transform: translateX(0) }`가 `position: fixed`인
  `#datetime-panel`의 containing block이 되어, 사이드바 내부 스크롤량만큼
  패널이 화면 밖으로 밀려나 연/월/일 입력과 달력 헤더에 도달할 수 없던 결함.
  패널을 `document.body`로 포탈 이동해 어떤 조상 transform과도 무관하게
  뷰포트 기준으로 배치되도록 했다. 외부 클릭 닫기 판정에 포탈된 패널 영역을
  포함했다.
- **저높이 뷰포트 패널 클리핑 제거**: 가로형 폰(높이 ≤~430px) 등에서 패널이
  뷰포트보다 커지는 경우 `max-height: 100dvh - 16px` + 내부 스크롤로 전체
  콘텐츠에 도달 가능하게 했다.
- **iOS Safari 동적 뷰포트 대응**: `.sidebar`/`.map-container`의
  `height: 100vh` 뒤에 `100dvh`를 덧써 주소창 수축/확장 시 하단 잘림을
  완화했다.

### 모바일 사용성 개선
- **드로어 배경 탭 닫기**: 사이드바 드로어 열림 시 반투명 스크림을 깔고,
  스크림 탭으로 닫을 수 있게 했다. 드로어가 닫히면 열려 있던 날짜/시간
  패널도 함께 닫힌다. `#mobile-toggle`에 `aria-expanded`를 연동했다.
- **회귀 테스트**: 사이드바 스크롤 후 패널이 뷰포트 안에 완전히 머무는지,
  스크림 탭으로 드로어가 닫히는지 검증하는 Playwright 테스트를 추가했다.

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
