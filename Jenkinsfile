pipeline {
  agent any

  options {
    disableConcurrentBuilds()
    buildDiscarder(logRotator(numToKeepStr: "30"))
    timeout(time: 60, unit: "MINUTES")
  }

  parameters {
    choice(
      name: "DEPLOY_TARGET",
      choices: ["auto", "prod", "dev"],
      description: "auto는 main=prod, 그 외=dev로 자동 매핑합니다."
    )
    string(name: "ENM_HOST", defaultValue: "enmsoftware.com", description: "enm-server SSH host")
    string(name: "ENM_PORT", defaultValue: "22", description: "enm-server SSH port")
    string(name: "ENM_SSH_CREDENTIAL_ID", defaultValue: "enm-server-ssh-key", description: "Jenkins SSH key credential ID")
    string(name: "DOCKER_REGISTRY", defaultValue: "", description: "예: ghcr.io/my-org (비우면 로컬 이미지 사용)")
    string(
      name: "DOCKER_CREDENTIALS_ID",
      defaultValue: "",
      description: "Docker registry username/password credential ID"
    )
    string(name: "IMAGE_REPOSITORY", defaultValue: "trip-time-service", description: "이미지 저장소 이름")
    string(name: "IMAGE_RETENTION_COUNT", defaultValue: "5", description: "배포 성공 후 같은 이미지 저장소에서 보존할 최신 태그 수")
    string(name: "REMOTE_APP_ROOT", defaultValue: "/opt/triptime", description: "원격 배포 루트 경로")
    string(name: "NETWORK_NAME", defaultValue: "", description: "컨테이너 연결 Docker network (선택)")
    string(name: "HOST_PORT_PROD", defaultValue: "18500", description: "prod host port -> container 8500")
    string(name: "HOST_PORT_DEV", defaultValue: "18501", description: "dev host port -> container 8500")
    booleanParam(name: "USE_TRAEFIK_LABELS", defaultValue: false, description: "Traefik 라벨 자동 부착 여부")
    choice(
      name: "LIVE_E2E_POLICY",
      choices: ["off", "advisory", "blocking"],
      description: "실제 Naver provider live E2E 정책: off=skip, advisory=UNSTABLE, blocking=fail build"
    )
    string(name: "TRAEFIK_ENTRYPOINTS", defaultValue: "websecure", description: "Traefik entrypoints")
    string(name: "TRAEFIK_TLS_CERTRESOLVER", defaultValue: "", description: "Traefik certresolver (선택)")
  }

  environment {
    DOMAIN_PROD = "triptime.enmsoftware.com"
    DOMAIN_DEV = "dev.triptime.enmsoftware.com"
    CONTAINER_PREFIX = "triptime"
    CONTAINER_PORT = "8500"
  }

  stages {
    stage("Checkout") {
      steps {
        script {
          env.DEPLOY_ATTEMPTED = "false"
        }
        checkout scm
        sh "git fetch --tags --force"
      }
    }

    stage("Setup Python Toolchain") {
      steps {
        sh '''
          set -eu
          export PATH="$HOME/.local/bin:$PATH"
          if command -v uv >/dev/null 2>&1; then
            uv --version
          elif python3 -m pip --version >/dev/null 2>&1; then
            python3 -m pip install --user --upgrade pip uv
          else
            curl -LsSf https://astral.sh/uv/install.sh -o /tmp/uv-install.sh
            sh /tmp/uv-install.sh
          fi
          uv --version
        '''
      }
    }

    stage("Lint & Test") {
      steps {
        sh '''
          export PATH="$HOME/.local/bin:$PATH"
          uv sync --frozen --extra dev
          git diff --check
          uv run --no-sync --extra dev ruff check .
          uv run --no-sync --extra dev pytest
          npm ci
          npm run e2e:ci -- --reporter=list
        '''
      }
    }

    stage("Live E2E") {
      when {
        expression { return params.LIVE_E2E_POLICY != "off" }
      }
      steps {
        script {
          try {
            if (params.LIVE_E2E_POLICY == "advisory") {
              catchError(buildResult: "SUCCESS", stageResult: "UNSTABLE") {
                sh '''
                  set +e
                  export PATH="$HOME/.local/bin:$PATH"
                  export TTS_CHROME_NO_SANDBOX=1
                  LIVE_E2E_POLICY=advisory npm run e2e:live:smoke -- --reporter=list
                  SMOKE_STATUS=$?
                  LIVE_E2E_POLICY=advisory npm run e2e:live:diagnose -- --reporter=list
                  DIAGNOSE_STATUS=$?
                  if [ "$SMOKE_STATUS" -ne 0 ]; then
                    exit "$SMOKE_STATUS"
                  fi
                  exit "$DIAGNOSE_STATUS"
                '''
              }
            } else {
              sh '''
                export PATH="$HOME/.local/bin:$PATH"
                export TTS_CHROME_NO_SANDBOX=1
                LIVE_E2E_POLICY=blocking npm run e2e:live:smoke -- --reporter=list
              '''
            }
          } finally {
            archiveArtifacts(
              artifacts: ".artifacts/live/e2e-runtime.json,.artifacts/live/e2e-live-summary.json",
              allowEmptyArchive: true
            )
          }
        }
      }
    }

    stage("Resolve Version & Deploy Target") {
      steps {
        script {
          env.APP_VERSION = sh(
            script: 'export PATH="$HOME/.local/bin:$PATH"; uv run --no-sync python scripts/version/resolve_version.py',
            returnStdout: true
          ).trim()
          env.SHORT_SHA = sh(
            script: "git rev-parse --short=8 HEAD",
            returnStdout: true
          ).trim()
          env.BRANCH_SLUG = env.BRANCH_NAME
            .toLowerCase()
            .replaceAll("[^a-z0-9._-]", "-")

          String deployTarget = params.DEPLOY_TARGET == "auto"
            ? (env.BRANCH_NAME == "main" ? "prod" : "dev")
            : params.DEPLOY_TARGET

          if (deployTarget == "prod" && env.BRANCH_NAME != "main") {
            error("prod 배포는 main 브랜치에서만 허용됩니다. branch=${env.BRANCH_NAME}")
          }

          env.EFFECTIVE_DEPLOY_ENV = deployTarget
          env.TARGET_DOMAIN = deployTarget == "prod" ? env.DOMAIN_PROD : env.DOMAIN_DEV
          env.IMAGE_TAG = deployTarget == "prod"
            ? env.APP_VERSION
            : "${env.APP_VERSION}-${env.BRANCH_SLUG}-${env.SHORT_SHA}"
          env.DEPLOY_APP_VERSION = env.IMAGE_TAG

          String localImageRef = "${params.IMAGE_REPOSITORY}:${env.IMAGE_TAG}"
          env.REMOTE_IMAGE_REF = params.DOCKER_REGISTRY?.trim()
            ? "${params.DOCKER_REGISTRY}/${params.IMAGE_REPOSITORY}:${env.IMAGE_TAG}"
            : localImageRef

          env.EFFECTIVE_ENM_HOST = params.ENM_HOST?.trim()
            ?: env.ENM_HOST?.trim()
            ?: "enmsoftware.com"
          env.EFFECTIVE_ENM_PORT = params.ENM_PORT?.trim()
            ?: env.ENM_PORT?.trim()
            ?: "22"
          env.DEPLOY_KNOWN_HOSTS = "${env.WORKSPACE}/.ci-artifacts/enm-known-hosts"

          echo "APP_VERSION=${env.APP_VERSION}"
          echo "DEPLOY_APP_VERSION=${env.DEPLOY_APP_VERSION}"
          echo "DEPLOY_ENV=${env.EFFECTIVE_DEPLOY_ENV}"
          echo "IMAGE_REF=${env.REMOTE_IMAGE_REF}"
          echo "TARGET_DOMAIN=${env.TARGET_DOMAIN}"
          echo "ENM_HOST configured=${env.EFFECTIVE_ENM_HOST ? 'yes' : 'no'}"
        }
      }
    }

    stage("Package Deploy Bundle") {
      steps {
        sh '''
          mkdir -p .ci-artifacts
          BUNDLE_FILE=".ci-artifacts/triptime-${APP_VERSION}-${SHORT_SHA}.tgz"
          git archive --format=tgz -o "$BUNDLE_FILE" HEAD
          echo "$BUNDLE_FILE" > .ci-artifacts/current-bundle.path
        '''
        script {
          env.DEPLOY_BUNDLE_PATH = sh(
            script: "cat .ci-artifacts/current-bundle.path",
            returnStdout: true
          ).trim()
          echo "DEPLOY_BUNDLE_PATH=${env.DEPLOY_BUNDLE_PATH}"
        }
      }
    }

    stage("Deploy To ENM") {
      when {
        not { changeRequest() }
      }
      steps {
        script {
          if (!env.EFFECTIVE_ENM_HOST?.trim()) {
            error("ENM_HOST 값이 필요합니다.")
          }
          if (!env.EFFECTIVE_ENM_PORT?.trim()) {
            error("ENM_PORT 값이 필요합니다.")
          }
          env.DEPLOY_ATTEMPTED = "true"
        }
        sh '''
          set -eu
          mkdir -p .ci-artifacts
          ssh-keyscan -p "$EFFECTIVE_ENM_PORT" "$EFFECTIVE_ENM_HOST" > "$DEPLOY_KNOWN_HOSTS"
          test -s "$DEPLOY_KNOWN_HOSTS"
        '''
        withCredentials([
          sshUserPrivateKey(
            credentialsId: "${params.ENM_SSH_CREDENTIAL_ID?.trim() ?: 'enm-server-ssh-key'}",
            keyFileVariable: "ENM_SSH_KEY",
            usernameVariable: "ENM_USER"
          )
        ]) {
          withEnv([
            "DEPLOY_ENV=${env.EFFECTIVE_DEPLOY_ENV}",
            "IMAGE_REF=${env.REMOTE_IMAGE_REF}",
            "IMAGE_RETENTION_COUNT=${params.IMAGE_RETENTION_COUNT}",
            "APP_VERSION=${env.DEPLOY_APP_VERSION}",
            "SOURCE_ARCHIVE_PATH=${env.DEPLOY_BUNDLE_PATH}",
            "ENM_HOST=${env.EFFECTIVE_ENM_HOST}",
            "ENM_PORT=${env.EFFECTIVE_ENM_PORT}",
            "SSH_STRICT_HOST_KEY_CHECKING=yes",
            "SSH_KNOWN_HOSTS_FILE=${env.DEPLOY_KNOWN_HOSTS}",
            "REMOTE_APP_ROOT=${params.REMOTE_APP_ROOT}",
            "CONTAINER_PREFIX=${env.CONTAINER_PREFIX}",
            "CONTAINER_PORT=${env.CONTAINER_PORT}",
            "HOST_PORT_PROD=${params.HOST_PORT_PROD}",
            "HOST_PORT_DEV=${params.HOST_PORT_DEV}",
            "DOMAIN_PROD=${env.DOMAIN_PROD}",
            "DOMAIN_DEV=${env.DOMAIN_DEV}",
            "NETWORK_NAME=${params.NETWORK_NAME}",
            "USE_TRAEFIK_LABELS=${params.USE_TRAEFIK_LABELS}",
            "TRAEFIK_ENTRYPOINTS=${params.TRAEFIK_ENTRYPOINTS}",
            "TRAEFIK_TLS_CERTRESOLVER=${params.TRAEFIK_TLS_CERTRESOLVER}"
          ]) {
            sh "bash deploy/enm/deploy.sh"
          }
        }
      }
    }

    stage("Smoke Verify") {
      when {
        allOf {
          not { changeRequest() }
          expression { return env.DEPLOY_ATTEMPTED == "true" }
        }
      }
      steps {
        sh '''
          curl --fail --silent --show-error "https://$TARGET_DOMAIN/healthz" >/dev/null
          echo "Smoke check passed: https://$TARGET_DOMAIN/healthz"
        '''
      }
    }
  }

  post {
    failure {
      script {
        if (env.DEPLOY_ATTEMPTED != "true") {
          echo "Deploy not attempted. Rollback skipped."
          return
        }

        if (!env.EFFECTIVE_ENM_HOST?.trim()) {
          echo "ENM_HOST is empty. Rollback skipped."
          return
        }

        echo "Deployment failed. Attempting rollback..."
      }

      withCredentials([
        sshUserPrivateKey(
          credentialsId: "${params.ENM_SSH_CREDENTIAL_ID?.trim() ?: 'enm-server-ssh-key'}",
          keyFileVariable: "ENM_SSH_KEY",
          usernameVariable: "ENM_USER"
        )
      ]) {
        withEnv([
          "DEPLOY_ENV=${env.EFFECTIVE_DEPLOY_ENV}",
          "IMAGE_REF=${env.REMOTE_IMAGE_REF}",
          "ENM_HOST=${env.EFFECTIVE_ENM_HOST}",
          "ENM_PORT=${env.EFFECTIVE_ENM_PORT}",
          "SSH_STRICT_HOST_KEY_CHECKING=yes",
          "SSH_KNOWN_HOSTS_FILE=${env.DEPLOY_KNOWN_HOSTS}",
          "REMOTE_APP_ROOT=${params.REMOTE_APP_ROOT}",
          "CONTAINER_PREFIX=${env.CONTAINER_PREFIX}",
          "CONTAINER_PORT=${env.CONTAINER_PORT}",
          "HOST_PORT_PROD=${params.HOST_PORT_PROD}",
          "HOST_PORT_DEV=${params.HOST_PORT_DEV}",
          "DOMAIN_PROD=${env.DOMAIN_PROD}",
          "DOMAIN_DEV=${env.DOMAIN_DEV}",
          "NETWORK_NAME=${params.NETWORK_NAME}",
          "USE_TRAEFIK_LABELS=${params.USE_TRAEFIK_LABELS}",
          "TRAEFIK_ENTRYPOINTS=${params.TRAEFIK_ENTRYPOINTS}",
          "TRAEFIK_TLS_CERTRESOLVER=${params.TRAEFIK_TLS_CERTRESOLVER}"
        ]) {
          sh "bash deploy/enm/rollback.sh || true"
        }
      }
    }
  }
}
