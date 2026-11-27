#!/usr/bin/env bash
set -Eeuo pipefail

require_var() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "[deploy] missing required env: ${name}" >&2
    exit 1
  fi
}

require_var "DEPLOY_ENV"
require_var "IMAGE_REF"
require_var "ENM_HOST"
require_var "ENM_PORT"
require_var "ENM_USER"
require_var "ENM_SSH_KEY"

if [[ "${DEPLOY_ENV}" != "prod" && "${DEPLOY_ENV}" != "dev" ]]; then
  echo "[deploy] DEPLOY_ENV must be prod or dev" >&2
  exit 1
fi

CONTAINER_PREFIX="${CONTAINER_PREFIX:-triptime}"
CONTAINER_PORT="${CONTAINER_PORT:-8500}"
HOST_PORT_PROD="${HOST_PORT_PROD:-18500}"
HOST_PORT_DEV="${HOST_PORT_DEV:-18501}"
DOMAIN_PROD="${DOMAIN_PROD:-triptime.enmsoftware.com}"
DOMAIN_DEV="${DOMAIN_DEV:-dev.triptime.enmsoftware.com}"
REMOTE_APP_ROOT="${REMOTE_APP_ROOT:-/opt/${CONTAINER_PREFIX}}"
ENV_FILE_PROD="${ENV_FILE_PROD:-${REMOTE_APP_ROOT}/env/prod.env}"
ENV_FILE_DEV="${ENV_FILE_DEV:-${REMOTE_APP_ROOT}/env/dev.env}"
NETWORK_NAME="${NETWORK_NAME:-}"
USE_TRAEFIK_LABELS="${USE_TRAEFIK_LABELS:-false}"
TRAEFIK_ENTRYPOINTS="${TRAEFIK_ENTRYPOINTS:-websecure}"
TRAEFIK_TLS_CERTRESOLVER="${TRAEFIK_TLS_CERTRESOLVER:-}"
HEALTHCHECK_RETRIES="${HEALTHCHECK_RETRIES:-30}"
HEALTHCHECK_DELAY_SECONDS="${HEALTHCHECK_DELAY_SECONDS:-2}"
SSH_STRICT_HOST_KEY_CHECKING="${SSH_STRICT_HOST_KEY_CHECKING:-yes}"
SSH_KNOWN_HOSTS_FILE="${SSH_KNOWN_HOSTS_FILE:-${HOME}/.ssh/known_hosts}"
APP_VERSION="${APP_VERSION:-${IMAGE_REF##*:}}"
APP_REVISION="${APP_REVISION:-}"
DEPLOY_ATTEMPT_ID="${DEPLOY_ATTEMPT_ID:-}"
SOURCE_ARCHIVE_PATH="${SOURCE_ARCHIVE_PATH:-}"
TTS_PROVIDER="${TTS_PROVIDER:-naver_playwright}"
TTS_CHROME_NO_SANDBOX="${TTS_CHROME_NO_SANDBOX:-1}"
IMAGE_RETENTION_COUNT="${IMAGE_RETENTION_COUNT:-5}"

if [[ "${DEPLOY_ENV}" == "prod" ]]; then
  DEPLOY_DOMAIN="${DOMAIN_PROD}"
  HOST_PORT="${HOST_PORT_PROD}"
  ENV_FILE="${ENV_FILE_PROD}"
else
  DEPLOY_DOMAIN="${DOMAIN_DEV}"
  HOST_PORT="${HOST_PORT_DEV}"
  ENV_FILE="${ENV_FILE_DEV}"
fi

CONTAINER_NAME="${CONTAINER_PREFIX}-${DEPLOY_ENV}"
ROLLBACK_DIR="${REMOTE_APP_ROOT}/rollback/${DEPLOY_ENV}"

echo "[deploy] env=${DEPLOY_ENV} domain=${DEPLOY_DOMAIN} container=${CONTAINER_NAME}"
echo "[deploy] image=${IMAGE_REF}"

SOURCE_ARCHIVE_REMOTE=""
if [[ -n "${SOURCE_ARCHIVE_PATH}" ]]; then
  if [[ ! -f "${SOURCE_ARCHIVE_PATH}" ]]; then
    echo "[deploy] SOURCE_ARCHIVE_PATH file not found: ${SOURCE_ARCHIVE_PATH}" >&2
    exit 1
  fi
  SOURCE_ARCHIVE_REMOTE="/tmp/${CONTAINER_PREFIX}-${DEPLOY_ENV}-bundle-$(date +%s).tgz"
  scp -i "${ENM_SSH_KEY}" \
    -P "${ENM_PORT}" \
    -o "StrictHostKeyChecking=${SSH_STRICT_HOST_KEY_CHECKING}" \
    -o "UserKnownHostsFile=${SSH_KNOWN_HOSTS_FILE}" \
    "${SOURCE_ARCHIVE_PATH}" \
    "${ENM_USER}@${ENM_HOST}:${SOURCE_ARCHIVE_REMOTE}" >/dev/null
fi

ssh -i "${ENM_SSH_KEY}" \
  -p "${ENM_PORT}" \
  -o "StrictHostKeyChecking=${SSH_STRICT_HOST_KEY_CHECKING}" \
  -o "UserKnownHostsFile=${SSH_KNOWN_HOSTS_FILE}" \
  "${ENM_USER}@${ENM_HOST}" "bash -s" <<REMOTE_SCRIPT
set -Eeuo pipefail

DEPLOY_ENV="$(printf '%q' "${DEPLOY_ENV}")"
IMAGE_REF="$(printf '%q' "${IMAGE_REF}")"
CONTAINER_PREFIX="$(printf '%q' "${CONTAINER_PREFIX}")"
CONTAINER_NAME="$(printf '%q' "${CONTAINER_NAME}")"
CONTAINER_PORT="$(printf '%q' "${CONTAINER_PORT}")"
HOST_PORT="$(printf '%q' "${HOST_PORT}")"
DEPLOY_DOMAIN="$(printf '%q' "${DEPLOY_DOMAIN}")"
ENV_FILE="$(printf '%q' "${ENV_FILE}")"
ROLLBACK_DIR="$(printf '%q' "${ROLLBACK_DIR}")"
NETWORK_NAME="$(printf '%q' "${NETWORK_NAME}")"
USE_TRAEFIK_LABELS="$(printf '%q' "${USE_TRAEFIK_LABELS}")"
TRAEFIK_ENTRYPOINTS="$(printf '%q' "${TRAEFIK_ENTRYPOINTS}")"
TRAEFIK_TLS_CERTRESOLVER="$(printf '%q' "${TRAEFIK_TLS_CERTRESOLVER}")"
HEALTHCHECK_RETRIES="$(printf '%q' "${HEALTHCHECK_RETRIES}")"
HEALTHCHECK_DELAY_SECONDS="$(printf '%q' "${HEALTHCHECK_DELAY_SECONDS}")"
APP_VERSION="$(printf '%q' "${APP_VERSION}")"
APP_REVISION="$(printf '%q' "${APP_REVISION}")"
DEPLOY_ATTEMPT_ID="$(printf '%q' "${DEPLOY_ATTEMPT_ID}")"
SOURCE_ARCHIVE_REMOTE="$(printf '%q' "${SOURCE_ARCHIVE_REMOTE}")"
REMOTE_APP_ROOT="$(printf '%q' "${REMOTE_APP_ROOT}")"
TTS_PROVIDER="$(printf '%q' "${TTS_PROVIDER}")"
TTS_CHROME_NO_SANDBOX="$(printf '%q' "${TTS_CHROME_NO_SANDBOX}")"
IMAGE_RETENTION_COUNT="$(printf '%q' "${IMAGE_RETENTION_COUNT}")"

normalize_optional() {
  local value="\$1"
  if [[ "\$value" == "''" || "\$value" == '""' ]]; then
    printf '%s' ""
    return
  fi
  printf '%s' "\$value"
}

NETWORK_NAME="\$(normalize_optional "\${NETWORK_NAME}")"
TRAEFIK_TLS_CERTRESOLVER="\$(normalize_optional "\${TRAEFIK_TLS_CERTRESOLVER}")"
SOURCE_ARCHIVE_REMOTE="\$(normalize_optional "\${SOURCE_ARCHIVE_REMOTE}")"
APP_REVISION="\$(normalize_optional "\${APP_REVISION}")"
DEPLOY_ATTEMPT_ID="\$(normalize_optional "\${DEPLOY_ATTEMPT_ID}")"

if ! [[ "\${IMAGE_RETENTION_COUNT}" =~ ^[0-9]+$ ]]; then
  echo "[deploy] IMAGE_RETENTION_COUNT must be a non-negative integer" >&2
  exit 1
fi
if [[ -n "\${APP_REVISION}" ]] && ! [[ "\${APP_REVISION}" =~ ^([0-9a-f]{40}|[0-9a-f]{64})$ ]]; then
  echo "[deploy] APP_REVISION must be a full lowercase hexadecimal Git commit ID" >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "[deploy] docker command not found on remote host" >&2
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "[deploy] curl command not found on remote host (required for health check)" >&2
  exit 1
fi

if [[ ! -f "\${ENV_FILE}" ]]; then
  echo "[deploy] env file not found on remote host: \${ENV_FILE}" >&2
  exit 1
fi

mkdir -p "\${ROLLBACK_DIR}"

image_repository_for_ref() {
  local image_ref="\$1" image_name="\${1##*/}"
  if [[ "\${image_name}" != *:* ]]; then
    return 1
  fi
  printf '%s\n' "\${image_ref%:*}"
}

collect_protected_images() {
  printf '%s\n' "\${IMAGE_REF}"
  for marker in current-image.txt previous-image.txt; do
    [[ -f "\${ROLLBACK_DIR}/\${marker}" ]] && cat "\${ROLLBACK_DIR}/\${marker}" || true
  done
  docker ps --format '{{.Image}}' || true
}

cleanup_deploy_images() {
  local image_repository
  if ! image_repository="\$(image_repository_for_ref "\$1")"; then
    echo "[deploy] image retention skipped for untagged image ref=\$1"
    return
  fi

  local image_list
  if ! image_list="\$(docker image ls --format '{{.Repository}}:{{.Tag}}' "\${image_repository}")"; then
    echo "[deploy] image retention skipped because docker image ls failed"
    return
  fi

  local candidate_image protected_images retained_count=0
  protected_images="\$(collect_protected_images)"
  while IFS= read -r candidate_image; do
    [[ -z "\${candidate_image}" ]] && continue
    [[ "\${candidate_image}" == "<none>:<none>" ]] && continue
    [[ "\${candidate_image%:*}" == "\${image_repository}" ]] || continue
    grep -Fxq "\${candidate_image}" <<< "\${protected_images}" && continue
    if (( retained_count < IMAGE_RETENTION_COUNT )); then
      retained_count=\$((retained_count + 1))
      continue
    fi
    docker image rm "\${candidate_image}" >/dev/null 2>&1 \
      && echo "[deploy] removed old image tag=\${candidate_image}" \
      || echo "[deploy] skipped image removal tag=\${candidate_image}"
  done <<< "\${image_list}"
}

cleanup_deploy_build_dir() {
  local build_dir="\${REMOTE_APP_ROOT}/build/\${DEPLOY_ENV}"
  if [[ -d "\${build_dir}" ]] && ! rm -rf "\${build_dir}"; then
    echo "[deploy] skipped build directory cleanup=\${build_dir}"
  fi
}

previous_id=""
previous_image=""
previous_image_id=""
if previous="\$(docker container inspect --format '{{.Id}} {{.Config.Image}} {{.Image}}' "\${CONTAINER_NAME}" 2>/dev/null)"; then
  read -r previous_id previous_image previous_image_id <<< "\${previous}"
fi
if [[ -n "\${APP_REVISION}" && "\${previous_image}" == "\${IMAGE_REF}" ]]; then
  if ! tagged_id="\$(docker image inspect --format '{{.Id}}' "\${IMAGE_REF}")" \
    || [[ "\${tagged_id}" != "\${previous_image_id}" ]]; then
    echo "[deploy] active image and deploy tag identify different bytes; deployment skipped" >&2
    exit 1
  fi
fi

if ! docker image inspect "\${IMAGE_REF}" >/dev/null 2>&1; then
  if ! docker pull "\${IMAGE_REF}" >/dev/null 2>&1; then
    if [[ -z "\${SOURCE_ARCHIVE_REMOTE}" || ! -f "\${SOURCE_ARCHIVE_REMOTE}" ]]; then
      echo "[deploy] image pull failed and source bundle is missing: \${IMAGE_REF}" >&2
      exit 1
    fi
    build_dir="\${REMOTE_APP_ROOT}/build/\${DEPLOY_ENV}"
    rm -rf "\${build_dir}"
    mkdir -p "\${build_dir}"
    tar -xzf "\${SOURCE_ARCHIVE_REMOTE}" -C "\${build_dir}"
    docker build \
      --build-arg APP_VERSION="\${APP_VERSION}" \
      --build-arg APP_REVISION="\${APP_REVISION}" \
      --tag "\${IMAGE_REF}" \
      "\${build_dir}" >/dev/null
    rm -f "\${SOURCE_ARCHIVE_REMOTE}"
  fi
fi
verified_image_id=""
if [[ -n "\${APP_REVISION}" ]]; then
  image_identity="\$(docker image inspect --format '{{.Id}}|{{index .Config.Labels "org.opencontainers.image.revision"}}' "\${IMAGE_REF}")"
  IFS='|' read -r verified_image_id image_revision <<< "\${image_identity}"
  if [[ "\${image_revision}" != "\${APP_REVISION}" ]]; then
    echo "[deploy] image revision does not match checked-out commit \${APP_REVISION}; deployment skipped" >&2
    exit 1
  fi
fi

if [[ -n "\${SOURCE_ARCHIVE_REMOTE}" && -f "\${SOURCE_ARCHIVE_REMOTE}" ]]; then
  rm -f "\${SOURCE_ARCHIVE_REMOTE}"
fi

run_container() {
  local image_ref="\$1"
  local override_provider="\${2:-}"
  local -a args
  args=(
    --name "\${CONTAINER_NAME}"
    --restart unless-stopped
    --env-file "\${ENV_FILE}"
    --env "TTS_CHROME_NO_SANDBOX=\${TTS_CHROME_NO_SANDBOX}"
    --publish "127.0.0.1:\${HOST_PORT}:\${CONTAINER_PORT}"
    --label "service=\${CONTAINER_PREFIX}"
    --label "deploy_env=\${DEPLOY_ENV}"
  )

  # New deploys may override a stale remote env (e.g. naver_selenium).
  # Auto-rollback to previous_image must NOT force naver_playwright — older
  # images may only accept naver / naver_selenium from the env file.
  if [[ -n "\${override_provider}" ]]; then
    # Keep the new release version out of containers restored during rollback.
    args+=(--env "TTS_VERSION=\${APP_VERSION}")
    args+=(--env "TTS_PROVIDER=\${override_provider}")
    if [[ -n "\${DEPLOY_ATTEMPT_ID}" ]]; then
      args+=(
        --label "triptime_deploy_attempt=\${DEPLOY_ATTEMPT_ID}"
        --label "triptime_previous_image=\${previous_image}"
      )
    fi
  fi

  if [[ -n "\${NETWORK_NAME}" ]]; then
    args+=(--network "\${NETWORK_NAME}")
  fi

  if [[ "\${USE_TRAEFIK_LABELS}" == "true" ]]; then
    local router_name
    router_name="\${CONTAINER_NAME}"
    args+=(
      --label "traefik.enable=true"
      --label "traefik.http.routers.\${router_name}.rule=Host(\`\${DEPLOY_DOMAIN}\`)"
      --label "traefik.http.routers.\${router_name}.entrypoints=\${TRAEFIK_ENTRYPOINTS}"
      --label "traefik.http.services.\${router_name}.loadbalancer.server.port=\${CONTAINER_PORT}"
    )
    if [[ -n "\${TRAEFIK_TLS_CERTRESOLVER}" ]]; then
      args+=(
        --label "traefik.http.routers.\${router_name}.tls=true"
        --label "traefik.http.routers.\${router_name}.tls.certresolver=\${TRAEFIK_TLS_CERTRESOLVER}"
      )
    fi
  fi

  # Keep the ID even if starting a successfully created container fails.
  container_id=""
  container_id="\$(docker create "\${args[@]}" "\${image_ref}")" || return
  if [[ -n "\${override_provider}" && -n "\${verified_image_id}" ]]; then
    local created_image_id
    if ! created_image_id="\$(docker container inspect --format '{{.Image}}' "\${container_id}")" \
      || [[ "\${created_image_id}" != "\${verified_image_id}" ]]; then
      echo "[deploy] candidate was created from a different image ID" >&2
      return 1
    fi
  fi
  docker start "\${container_id}" >/dev/null
}

check_health() {
  curl --fail --silent --show-error --max-time 5 "http://127.0.0.1:\${HOST_PORT}/healthz" >/dev/null
}

if [[ -n "\${previous_id}" ]]; then
  if [[ "\${previous_image}" == "\${IMAGE_REF}" ]]; then
    # A rerun for the same image must preserve the preceding rollback target.
    previous_image=""
    if [[ -f "\${ROLLBACK_DIR}/previous-image.txt" ]]; then
      previous_image="\$(cat "\${ROLLBACK_DIR}/previous-image.txt")"
    fi
  fi
  if [[ "\${previous_image}" == "\${IMAGE_REF}" ]]; then
    previous_image=""
  fi
  printf '%s\n' "\${previous_image}" > "\${ROLLBACK_DIR}/previous-image.txt"
  docker rm -f "\${previous_id}" >/dev/null
else
  printf '\n' > "\${ROLLBACK_DIR}/previous-image.txt"
fi

wait_for_health() {
  for ((i=1; i<=HEALTHCHECK_RETRIES; i++)); do
    if check_health; then
      return 0
    fi
    sleep "\${HEALTHCHECK_DELAY_SECONDS}"
  done
  return 1
}

if run_container "\${IMAGE_REF}" "\${TTS_PROVIDER}" && wait_for_health \
  && printf '%s\n' "\${IMAGE_REF}" > "\${ROLLBACK_DIR}/current-image.txt" \
  && date -u +"%Y-%m-%dT%H:%M:%SZ" > "\${ROLLBACK_DIR}/last-deployed-at.txt"; then
  echo "[deploy] health check passed"
  cleanup_deploy_build_dir
  cleanup_deploy_images "\${IMAGE_REF}"
  exit 0
fi

echo "[deploy] container startup, health check or deployment state update failed for image=\${IMAGE_REF}" >&2

if [[ -n "\${previous_image}" ]]; then
  echo "[deploy] auto rollback to previous image=\${previous_image}"
  if [[ -n "\${container_id}" ]] && ! docker rm -f "\${container_id}" >/dev/null; then
    echo "[deploy] could not remove failed candidate container" >&2
    exit 1
  fi
  if ! docker image inspect "\${previous_image}" >/dev/null 2>&1; then
    docker pull "\${previous_image}" >/dev/null 2>&1 || true
  fi
  # Omit provider override so previous image uses env-file / image defaults.
  if run_container "\${previous_image}" && wait_for_health \
    && printf '%s\n' "\${previous_image}" > "\${ROLLBACK_DIR}/current-image.txt"; then
    echo "[deploy] rollback health check passed"
    exit 1
  fi
fi

echo "[deploy] rollback failed or previous image unavailable" >&2
exit 1
REMOTE_SCRIPT
