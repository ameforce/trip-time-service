# trip-time-service agent instructions

## Branching and Git-flow rules

이 프로젝트의 브랜치/릴리즈 작업은 Fork 앱의 Git-flow 절차를 기준으로 수행한다.
버그 개선, 회귀 수정, 장애 대응, hotfix/fix 성격의 작업은 `main` 기준
`hotfix/vX.Y.Z` 브랜치에서 작업한다.

Hotfix 절차:

1. `main`에서 `hotfix/vX.Y.Z` 브랜치를 생성한다. 버전이 지정되지 않았으면
   현재 릴리즈의 patch 버전을 1 올린다.
2. hotfix 브랜치에 수정 커밋을 만든다.
3. `CHANGELOG.md`에 `vX.Y.Z` 항목을 추가한다(없으면 생성). 사용자 관점
   변화를 우선으로 기술하고, 커밋 메시지 나열이 아닌 개선 요약으로 쓴다.
4. hotfix를 닫을 때 `main`에 no-ff merge하고 기본 merge 메시지
   `Merge branch 'hotfix/vX.Y.Z'`를 유지한다.
5. `main` merge commit에 annotated tag `vX.Y.Z`를 생성한다.
6. tag `vX.Y.Z`를 `develop`에 no-ff merge하고 기본 merge 메시지
   `Merge tag 'vX.Y.Z' into develop`을 유지한다.
7. `main`, `develop`, tag를 push하고, GitHub Release에 CHANGELOG의 해당
   항목을 게시한다. 배포/검증을 완료한 뒤 hotfix 브랜치를 삭제한다.

새 기능은 `develop` 기준 `feature/<name>` 브랜치에서 작업한다. 기능 브랜치를
`develop`에 merge한 뒤, 그 `develop`을 기준으로 release 브랜치를 생성한다.
release 버전은 현재 릴리즈의 minor 버전을 1 올리고 patch 버전을 0으로 초기화한
`vMAJOR.MINOR.0` 형식이며, 브랜치 이름은 `release/vMAJOR.MINOR.0`이다.

Release 절차:

1. `develop`에서 `release/vMAJOR.MINOR.0` 브랜치를 생성한다.
2. `CHANGELOG.md`에 `vMAJOR.MINOR.0` 항목을 추가한다(없으면 생성). 이전 태그와의
   실제 diff를 근거로 사용자·운영 영향 중심으로 요약한다.
3. release 브랜치를 닫을 때 `main`에 no-ff merge하고 기본 merge 메시지
   `Merge branch 'release/vMAJOR.MINOR.0'`를 유지한다.
4. `main` merge commit에 annotated tag `vMAJOR.MINOR.0`을 생성한다.
5. tag `vMAJOR.MINOR.0`을 `develop`에 no-ff merge하고 기본 merge 메시지
   `Merge tag 'vMAJOR.MINOR.0' into develop`을 유지한다.
6. `main`, `develop`, tag를 push하고, GitHub Release에 CHANGELOG의 해당 항목을
   게시한다. 배포/검증을 완료한 뒤 release 브랜치를 삭제한다.

릴리즈 문서화 규칙:

- 모든 태그 릴리즈(hotfix/release)는 CHANGELOG.md 항목과 GitHub Release 게시를
  필수 산출물로 한다. 생략은 계약 위반이며, "배포 완료" 보고에 포함되지 않는다.
- 내용은 태그 간 diff(`git diff v이전..v신규`)로 검증한 사실만 기술한다.

fix/hotfix 또는 feature/release 성격의 작업에서 `main`과 `develop`에 직접 일반
커밋을 남기지 않는다. 이미 push된 Git-flow 그래프를 사용자 지시에 따라 고칠 때만
`--force-with-lease`로 갱신한다.

## Codex review gate for PR merges

feature/hotfix를 PR로 통합할 때 `@codex review` 결과는 단순 sleep/대기만으로
완료 판단하지 않는다. 아래 주기 폴링 게이트를 따른다.

1. PR에 `@codex review` 코멘트를 남긴 시각 `T0`와 대상 커밋 SHA를 기록한다.
2. 최소 30초 간격으로 PR review/inline/issue comment를 다시 조회한다.
   - `chatgpt-codex-connector` 등 Codex bot의 **새** review 또는 finding comment
   - 또는 Codex의 완료 신호(👍 반응, "no findings" 요약, follow-up verification)
3. `T0` 이전 결과만 보고 merge하지 않는다. 현재 HEAD 커밋 기준 결과가 올 때까지
   폴링한다. 장시간 미응답이면 계속 폴링하고, 완료 전에 merge하지 않는다.
4. finding 처리와 merge 판정, 자동 fix-재검증 반복 상한은 글로벌 agents.md
   (`~/.codex/AGENTS.md`)의 Review And Quality Gates 계약을 따른다.
   - 본문에서 자체 정의하던 "low 이상 전부 merge 보류" 기준과
     무제한 fix→push→재리뷰 반복 절차는 폐기한다(위 계약과 충돌).
5. 현재 HEAD에 대해 위 글로벌 계약상 차단 해당 finding이 없음을 폴링으로
   확인한 뒤에만 merge한다.

이 프로젝트에서 코드, 설정, 배포 스크립트, E2E/운영 정책을 변경한 작업은
로컬 검증만으로 완료 보고하지 않는다.

완료 보고 전에 반드시 다음을 수행하고 증거를 남긴다.

1. 변경을 커밋하고 원격 브랜치에 push한다.
2. non-main 브랜치는 dev 환경(`https://dev.triptime.enmsoftware.com`)에 배포한다.
   - 기본 경로는 Jenkins multibranch `trip-time-service`의 해당 브랜치 빌드다.
   - Jenkins가 사용할 수 없을 때만 `deploy/enm/deploy.sh` 직접 실행을 대체 경로로 사용한다.
3. 배포 후 dev에서 실제 서비스가 새 변경을 제공하는지 확인한다.
   - `/healthz`
   - `/api/config`의 version 또는 배포 식별자
   - 핵심 사용자 플로우의 실제 API/UI 성공
4. UI가 포함된 기능은 Playwright 등 브라우저 기반 검증으로 desktop 1종과 mobile viewport 1종을 확인하고,
   최소한 초기 화면, 핵심 상호작용 후, 최종 성공 화면 스크린샷 경로를 남긴다.
5. Naver live provider, captcha, panel parser, 외부 네트워크 문제로 핵심 플로우가 실패하면
   성공 완료로 보고하지 않는다. 실패 bucket과 artifact를 남기고 blocker로 보고하거나 계속 수정한다.
6. 최종 보고에는 배포 대상, 배포 빌드/커밋, dev URL, 실행한 검증 명령,
   실제 기능 성공 증거, 스크린샷 경로, 남은 리스크를 포함한다.
