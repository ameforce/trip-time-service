# TripTime Jenkins 멀티브랜치 파이프라인 수동 구성 런북

이 문서는 **ENM Jenkins에 네트워크로 직접 접속할 수 있는 환경**에서 수행하는 절차입니다. 비밀번호·토큰·키 본문은 기록하지 않습니다.

## 사전 조건

- Jenkins 관리 권한
- Git 저장소 URL 및 브랜치 접근 권한(Jenkins가 클론 가능)
- `Jenkinsfile` 저장소 루트 경로 사용

## 1. Jenkins 자격 증명 확인(이름만)

`Jenkinsfile`이 참조하는 ID:

| 용도 | 파라미터 / 기본값 | 필수 여부 |
|------|-------------------|-----------|
| enm-server SSH | `ENM_SSH_CREDENTIAL_ID` 기본 `enm-server-ssh-key` | 배포 단계 필수 |
| Docker Registry | `DOCKER_CREDENTIALS_ID` 기본 빈 문자열 | `DOCKER_REGISTRY`를 쓸 때만 필수 |

Jenkins에서 **Manage Jenkins → Credentials** 로 이동한 뒤:

- [ ] ID `enm-server-ssh-key` 존재(SSH Username with private key 권장)
- [ ] 레지스트리를 쓰는 경우: 파이프라인에 넣을 `DOCKER_CREDENTIALS_ID`와 동일 ID의 Username/Password 자격 증명 존재

## 2. 멀티브랜치 파이프라인 잡 생성/갱신

1. **New Item** → 이름 예: `trip-time-service`(또는 조직 폴더 아래 동일 유형)
2. 유형: **Multibranch Pipeline**
3. **Branch Sources** → **Add source** → **Git**
   - **Repository URL**: 이 저장소의 원격 URL
   - **Credentials**: Jenkins가 Git에 접근할 자격 증명(Deploy key / PAT 등)
4. **Behaviours**(필요 시):
   - 기본 브랜치 발견 + PR/브랜치 정책은 조직 표준에 맞게 설정
   - `moneyflow`와 동일한 “인덱싱 주기 / 웹훅”이 있으면 그대로 맞춤
5. **Build Configuration**:
   - **Mode**: by Jenkinsfile
   - **Script Path**: `Jenkinsfile`
6. 저장 후 **Scan Multibranch Pipeline Now** 로 인덱싱 확인

## 3. 트리거(브랜치 인덱싱 / 웹훅)

다음 중 조직 표준에 맞게 하나 이상 구성:

- [ ] **Periodically if not otherwise run** 또는 **Scan by webhook**(Multibranch Scanning Webhook 사용 시 Git 쪽 webhook URL 등록)
- [ ] Git 서버(예: GitHub/GitLab)에서 Jenkins로 push 이벤트 전달 시 브랜치 스캔이 도는지 확인

## 4. 브랜치 전략과 `Jenkinsfile`의 대응

`DEPLOY_TARGET=auto`(기본 동작)일 때:

- `main` → **prod** → 호스트/도메인: `triptime.enmsoftware.com`(파이프라인 내 정의)
- `main`이 아님 → **dev** → `dev.triptime.enmsoftware.com`
- **prod 배포는 `main`에서만 허용**(다른 브랜치에서 prod 선택 시 빌드 실패)

멀티브랜치 잡에서는 각 브랜치가 별도 빌드가 되므로, 위 규칙이 자동으로 적용됩니다.

## 5. 필수 파이프라인 파라미터 기본값(배포 성공 조건)

`Jenkinsfile`은 ENM dev/prod 공통 SSH 호스트 기본값을 포함합니다. 조직별 호스트가 다르면 잡/폴더
파라미터 기본값 또는 빌드 파라미터로 override합니다.

- [ ] `ENM_HOST`: enm-server SSH 호스트(기본 `enmsoftware.com`)
- [ ] `ENM_PORT`: SSH 포트(기본 `22`)
- [ ] `DEPLOY_TARGET`: `auto` 권장(브랜치별 prod/dev 자동)
- [ ] 레지스트리 미사용 시: `DOCKER_REGISTRY` 빈 값 유지 → 푸시 단계 스킵, 로컬 태그로 배포 스크립트 동작

파이프라인은 배포 직전에 `ssh-keyscan`으로 빌드 워크스페이스의 known_hosts 파일을 준비하고
`StrictHostKeyChecking=yes`로 `deploy/enm/deploy.sh`와 `rollback.sh`를 호출합니다.
(Optional) **Folder properties** 또는 **Parameterized Defaults** 플러그인 등으로 조직 표준과 맞게 기본 파라미터를 고정합니다.

## 6. 첫 성공 빌드 검증

- [ ] `main` 브랜치 빌드: 배포 후 `https://triptime.enmsoftware.com/healthz` 응답(파이프라인 Smoke 단계)
- [ ] 비-`main` 브랜치 빌드: `https://dev.triptime.enmsoftware.com/healthz`
- [ ] 실패 시 `post`의 rollback 스크립트 호출 여부를 콘솔 로그로 확인

## 7. 보안 체크리스트(저장소에 비밀 금지)

- [ ] 런북·README·이슈에 계정·토큰·개인키 본문을 넣지 않음
- [ ] Jenkins 자격 증명 ID는 **이름만** 문서화

---

**상태**: 이 런북은 네트워크 제한 환경에서 자동 검증 없이 작성되었습니다. Jenkins UI에서 credential ID 존재 여부를 반드시 대조하세요.
