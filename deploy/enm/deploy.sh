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
SOURCE_ARCHIVE_PATH="${SOURCE_ARCHIVE_PATH:-}"
TTS_CHROME_NO_SANDBOX="${TTS_CHROME_NO_SANDBOX:-1}"

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
SOURCE_ARCHIVE_REMOTE="$(printf '%q' "${SOURCE_ARCHIVE_REMOTE}")"
REMOTE_APP_ROOT="$(printf '%q' "${REMOTE_APP_ROOT}")"
TTS_CHROME_NO_SANDBOX="$(printf '%q' "${TTS_CHROME_NO_SANDBOX}")"

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

previous_image=""
if docker container inspect "\${CONTAINER_NAME}" >/dev/null 2>&1; then
  previous_image="\$(docker inspect --format '{{.Config.Image}}' "\${CONTAINER_NAME}" || true)"
  if [[ -n "\${previous_image}" ]]; then
    printf '%s\n' "\${previous_image}" > "\${ROLLBACK_DIR}/previous-image.txt"
  fi
  docker rm -f "\${CONTAINER_NAME}" >/dev/null
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
      --tag "\${IMAGE_REF}" \
      "\${build_dir}" >/dev/null
    rm -f "\${SOURCE_ARCHIVE_REMOTE}"
  fi
fi

if [[ -n "\${SOURCE_ARCHIVE_REMOTE}" && -f "\${SOURCE_ARCHIVE_REMOTE}" ]]; then
  rm -f "\${SOURCE_ARCHIVE_REMOTE}"
fi

run_container() {
  local image_ref="\$1"
  local -a args
  args=(
    --detach
    --name "\${CONTAINER_NAME}"
    --restart unless-stopped
    --env-file "\${ENV_FILE}"
    # Runtime version must come from the deployed image/tag, not a stale env file.
    --env "TTS_VERSION=\${APP_VERSION}"
    --env "TTS_CHROME_NO_SANDBOX=\${TTS_CHROME_NO_SANDBOX}"
    --publish "127.0.0.1:\${HOST_PORT}:\${CONTAINER_PORT}"
    --label "service=\${CONTAINER_PREFIX}"
    --label "deploy_env=\${DEPLOY_ENV}"
  )

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

  docker run "\${args[@]}" "\${image_ref}" >/dev/null
}

check_health() {
  curl --fail --silent --show-error "http://127.0.0.1:\${HOST_PORT}/healthz" >/dev/null
}

run_container "\${IMAGE_REF}"

for ((i=1; i<=HEALTHCHECK_RETRIES; i++)); do
  if check_health; then
    printf '%s\n' "\${IMAGE_REF}" > "\${ROLLBACK_DIR}/current-image.txt"
    date -u +"%Y-%m-%dT%H:%M:%SZ" > "\${ROLLBACK_DIR}/last-deployed-at.txt"
    echo "[deploy] health check passed (\${i}/\${HEALTHCHECK_RETRIES})"
    exit 0
  fi
  sleep "\${HEALTHCHECK_DELAY_SECONDS}"
done

echo "[deploy] health check failed for image=\${IMAGE_REF}" >&2

if [[ -n "\${previous_image}" ]]; then
  echo "[deploy] auto rollback to previous image=\${previous_image}"
  docker rm -f "\${CONTAINER_NAME}" >/dev/null 2>&1 || true
  if ! docker image inspect "\${previous_image}" >/dev/null 2>&1; then
    docker pull "\${previous_image}" >/dev/null 2>&1 || true
  fi
  run_container "\${previous_image}"
  for ((i=1; i<=HEALTHCHECK_RETRIES; i++)); do
    if check_health; then
      printf '%s\n' "\${previous_image}" > "\${ROLLBACK_DIR}/current-image.txt"
      echo "[deploy] rollback health check passed"
      exit 1
    fi
    sleep "\${HEALTHCHECK_DELAY_SECONDS}"
  done
fi

echo "[deploy] rollback failed or previous image unavailable" >&2
exit 1
REMOTE_SCRIPT
