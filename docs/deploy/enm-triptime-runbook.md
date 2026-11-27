# TripTime ENM 배포/롤백 운영 런북

## 1) 목적

TripTime를 Jenkins 기반으로 빌드하고 `enm-server`에 Docker 컨테이너로 배포하기 위한 운영 절차를 정의한다.

## 2) 브랜치/도메인 매핑

- `main` 브랜치 빌드: `https://triptime.enmsoftware.com`
- non-main 브랜치 빌드: `https://dev.triptime.enmsoftware.com`

`Jenkinsfile`에서 `DEPLOY_TARGET=auto`일 때 위 매핑이 자동 적용된다.

## 3) 필수 자격 증명/설정

### Jenkins Credentials

- `ENM_SSH_CREDENTIAL_ID` (기본: `enm-server-ssh-key`)
  - 타입: SSH Username with private key
- `DOCKER_CREDENTIALS_ID`
  - `DOCKER_REGISTRY`를 사용할 때만 필요

### Jenkins 파라미터 핵심값

- `ENM_HOST` (기본: `enmsoftware.com`, 조직별 호스트가 다르면 override)
- `ENM_PORT` (기본 `22`)
- `DEPLOY_TARGET` (기본 `auto`)
- `IMAGE_REPOSITORY` (기본 `trip-time-service`)
- `IMAGE_RETENTION_COUNT` (기본 `5`)
- `REMOTE_APP_ROOT` (기본 `/opt/triptime`)

## 4) 이미지/버전 규칙

- 런타임 표시 버전: `vMAJOR.MINOR.PATCH.COMMIT`
  - 우선순위:
    1. `TTS_VERSION` 환경변수
    2. git tag+commit 계산
    3. 패키지 버전 fallback (`v0.1.0.0` 형태)
- `main` 배포 이미지 태그: `APP_VERSION`
- non-main 배포 이미지 태그: `APP_VERSION-<branch>-<short_sha>`
- 배포 성공 후 `deploy/enm/deploy.sh`는 같은 이미지 저장소의 오래된 태그만 정리한다.
  `current-image.txt`, `previous-image.txt`, 실행 중인 컨테이너가 참조하는 이미지,
  그리고 최신 `IMAGE_RETENTION_COUNT`개 태그는 보존한다.
- `docker image prune`, `docker system prune` 같은 broad cleanup은 사용하지 않는다.

## 5) 기본 배포 절차

1. Jenkins 멀티브랜치 잡에서 대상 브랜치 빌드 시작
2. 파이프라인 단계
   - `Lint & Test`
   - `Resolve Version & Deploy Target`
   - `Build Docker Image`
   - `Push Docker Image` (레지스트리 사용 시)
   - `Deploy To ENM`
     - 빌드 워크스페이스에 ENM SSH known_hosts 파일을 생성한 뒤 `StrictHostKeyChecking=yes`로 배포
   - `Smoke Verify`
     - 공개 `/api/config`의 버전과 제공자를 확인한 뒤 자동완성과 출발 시각·도착 시각 기준 경로 API의 최종 응답을 검증
     - 단계별 결과와 실패 유형은 Jenkins의 `.ci-artifacts/release-smoke-<build-number>.json`에 보관
3. 배포 스크립트
   - `deploy/enm/deploy.sh`
   - 새 이미지를 확보한 뒤 기존 이미지 정보를 저장하고 컨테이너 교체
   - Jenkins 배포에서는 이미지의 OCI 리비전 레이블과 체크아웃 커밋 SHA가 일치할 때만 교체
   - 새 컨테이너 기동 또는 내부 `/healthz` 검증에 실패하면 스크립트가 자동 롤백 시도
   - `/healthz` 체크 성공 후 원격 빌드 디렉터리와 오래된 같은 저장소 이미지 태그 정리
   - Jenkins가 공개 API 검증을 완료하기 전에 빌드가 실패하거나 중단되면 해당 빌드에서 생성한 컨테이너만 이전 이미지로 복구 시도
   - 배포나 롤백 중 컨테이너 제거 이후 복구가 중단되면 같은 배포 시도의 기록에 저장한 이전 이미지 ID로 재시도
   - 이전 이미지에 저장된 유효한 버전을 복구 컨테이너에 전달해 env 파일의 기본 버전을 덮어씀
   - 배포와 롤백은 환경별 `flock`을 공유하며 최대 120초 동안 잠금 획득을 대기

## 6) 롤백 절차

자동 롤백이 실패하거나 수동 롤백이 필요하면 Jenkins 또는 운영 터미널에서 실행:

```bash
export DEPLOY_ENV=prod   # 또는 dev
export ENM_HOST=<enm_host>
export ENM_PORT=22
export ENM_USER=<ssh_user>
export ENM_SSH_KEY=<ssh_key_path>
bash deploy/enm/rollback.sh
```

특정 이미지로 강제 롤백:

```bash
export ROLLBACK_IMAGE_REF=<registry/repo:tag>
bash deploy/enm/rollback.sh
```

## 7) 검증 체크리스트

### 서버/엔드포인트

- [ ] `https://triptime.enmsoftware.com/healthz` (main)
- [ ] `https://dev.triptime.enmsoftware.com/healthz` (non-main)
- [ ] 공개 API 검증 아티팩트에서 배포 이미지 태그와 실제 버전 일치 확인
- [ ] 같은 아티팩트에서 실시간 자동완성과 출발 시각·도착 시각 기준 검색의 최종 응답 성공 확인

직접 배포할 때는 배포 스크립트가 끝난 뒤 `--expected-version`에 방금 배포한
이미지 태그를 지정해 같은 검증을 실행한다.

```bash
uv run --no-sync python deploy/enm/smoke.py \
  --base-url "https://${DEPLOY_DOMAIN}" \
  --expected-version "${APP_VERSION}" \
  --artifact .ci-artifacts/release-smoke.json
```

### UI/버전

- [ ] 페이지 우측 하단 버전 배지가 표시되는지 확인
- [ ] `/api/config` 응답에 `version` 필드가 포함되는지 확인
- [ ] Playwright 회귀 테스트 통과 (`arrival-mode`, `departure-mode`)

## 8) 장애 대응 요약

1. `Smoke Verify` 실패 시 Jenkins 콘솔에서 `deploy.sh`/`rollback.sh` 로그 우선 확인
2. `ENM_HOST`, credential ID, 네트워크 접근(VPN/방화벽) 확인
3. 이미지 pull 실패 시 레지스트리 권한/토큰 만료 확인
4. 컨테이너 기동 후 실패 시
   - 원격 env 파일(`prod.env`/`dev.env`) 존재 여부
   - 포트 충돌(`HOST_PORT_PROD`/`HOST_PORT_DEV`)
   - `/healthz` 응답 여부

## 9) 참조 문서

- `docs/deploy/enm-moneyflow-baseline.md`
- `docs/deploy/jenkins-triptime-runbook.md`
- `Jenkinsfile`
- `deploy/enm/deploy.sh`
- `deploy/enm/rollback.sh`
