# trip-time-service agent instructions

## Completion gate for code/config changes

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
