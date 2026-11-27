#!/usr/bin/env bash
set -Eeuo pipefail

require_var() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "[rollback] missing required env: ${name}" >&2
    exit 1
  fi
}

require_var "DEPLOY_ENV"
require_var "ENM_HOST"
require_var "ENM_PORT"
require_var "ENM_USER"
require_var "ENM_SSH_KEY"

if [[ "${DEPLOY_ENV}" != "prod" && "${DEPLOY_ENV}" != "dev" ]]; then
  echo "[rollback] DEPLOY_ENV must be prod or dev" >&2
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
ROLLBACK_IMAGE_REF="${ROLLBACK_IMAGE_REF:-}"
ROLLBACK_EXPECTED_IMAGE_REF="${ROLLBACK_EXPECTED_IMAGE_REF:-}"
ROLLBACK_EXPECTED_ATTEMPT_ID="${ROLLBACK_EXPECTED_ATTEMPT_ID:-}"
SSH_STRICT_HOST_KEY_CHECKING="${SSH_STRICT_HOST_KEY_CHECKING:-yes}"
SSH_KNOWN_HOSTS_FILE="${SSH_KNOWN_HOSTS_FILE:-${HOME}/.ssh/known_hosts}"
# Do not force TTS_PROVIDER on rollback: previous images may only accept
# naver / naver_selenium. Let the image + env file choose the provider.
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

echo "[rollback] env=${DEPLOY_ENV} domain=${DEPLOY_DOMAIN} container=${CONTAINER_NAME}"

ssh -i "${ENM_SSH_KEY}" \
  -p "${ENM_PORT}" \
  -o "StrictHostKeyChecking=${SSH_STRICT_HOST_KEY_CHECKING}" \
  -o "UserKnownHostsFile=${SSH_KNOWN_HOSTS_FILE}" \
  "${ENM_USER}@${ENM_HOST}" "bash -s" <<REMOTE_SCRIPT
set -Eeuo pipefail

DEPLOY_ENV="$(printf '%q' "${DEPLOY_ENV}")"
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
ROLLBACK_IMAGE_REF="$(printf '%q' "${ROLLBACK_IMAGE_REF}")"
ROLLBACK_EXPECTED_IMAGE_REF="$(printf '%q' "${ROLLBACK_EXPECTED_IMAGE_REF}")"
ROLLBACK_EXPECTED_ATTEMPT_ID="$(printf '%q' "${ROLLBACK_EXPECTED_ATTEMPT_ID}")"
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
ROLLBACK_IMAGE_REF="\$(normalize_optional "\${ROLLBACK_IMAGE_REF}")"
ROLLBACK_EXPECTED_IMAGE_REF="\$(normalize_optional "\${ROLLBACK_EXPECTED_IMAGE_REF}")"
ROLLBACK_EXPECTED_ATTEMPT_ID="\$(normalize_optional "\${ROLLBACK_EXPECTED_ATTEMPT_ID}")"

if ! command -v docker >/dev/null 2>&1; then
  echo "[rollback] docker command not found on remote host" >&2
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "[rollback] curl command not found on remote host (required for health check)" >&2
  exit 1
fi

check_health() {
  curl --fail --silent --show-error --max-time 5 "http://127.0.0.1:\${HOST_PORT}/healthz" >/dev/null
}

current_id=""
current_image=""
current_attempt=""
current_previous=""
container_format='{{println .Id}}{{println .Config.Image}}{{println (index .Config.Labels "triptime_deploy_attempt")}}{{index .Config.Labels "triptime_previous_image"}}'
if current="\$(docker container inspect --format "\${container_format}" "\${CONTAINER_NAME}" 2>/dev/null)"; then
  mapfile -t fields <<< "\${current}"
  current_id="\${fields[0]}"
  current_image="\${fields[1]:-}"
  current_attempt="\${fields[2]:-}"
  current_previous="\${fields[3]:-}"
elif ! active_id="\$(docker container ls --all --quiet --filter "name=^/\${CONTAINER_NAME}$")" \
  || [[ -n "\${active_id}" ]]; then
  echo "[rollback] cannot determine active container state" >&2
  exit 1
fi

if [[ -n "\${ROLLBACK_EXPECTED_ATTEMPT_ID}" ]]; then
  if [[ -z "\${current_id}" || "\${current_attempt}" != "\${ROLLBACK_EXPECTED_ATTEMPT_ID}" \
    || "\${current_image}" != "\${ROLLBACK_EXPECTED_IMAGE_REF}" ]]; then
    echo "[rollback] no active candidate from this deployment attempt"
    exit 3
  fi
  target_image="\${current_previous}"
else
  target_image="\${ROLLBACK_IMAGE_REF}"
  if [[ -z "\${target_image}" && -f "\${ROLLBACK_DIR}/previous-image.txt" ]]; then
    target_image="\$(cat "\${ROLLBACK_DIR}/previous-image.txt")"
  fi
fi

if [[ -n "\${ROLLBACK_EXPECTED_IMAGE_REF}" ]]; then
  if [[ "\${current_image}" != "\${ROLLBACK_EXPECTED_IMAGE_REF}" ]]; then
    if [[ -n "\${target_image}" && "\${current_image}" == "\${target_image}" ]] && check_health; then
      echo "[rollback] previous image is already running and healthy"
      exit 0
    fi
    echo "[rollback] active container no longer runs the failed deployment image" >&2
    exit 1
  fi
fi

if [[ -z "\${target_image}" ]]; then
  echo "[rollback] target rollback image not found" >&2
  exit 1
fi
if [[ ! -f "\${ENV_FILE}" ]]; then
  echo "[rollback] env file not found on remote host: \${ENV_FILE}" >&2
  exit 1
fi
if ! docker image inspect "\${target_image}" >/dev/null 2>&1; then
  docker pull "\${target_image}" >/dev/null
fi
if [[ -n "\${current_id}" ]]; then
  docker rm -f "\${current_id}" >/dev/null
fi

run_container() {
  local image_ref="\$1"
  local -a args
  args=(
    --detach
    --name "\${CONTAINER_NAME}"
    --restart unless-stopped
    --env-file "\${ENV_FILE}"
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

run_container "\${target_image}"

for ((i=1; i<=HEALTHCHECK_RETRIES; i++)); do
  if check_health; then
    printf '%s\n' "\${target_image}" > "\${ROLLBACK_DIR}/current-image.txt"
    echo "[rollback] health check passed"
    exit 0
  fi
  sleep "\${HEALTHCHECK_DELAY_SECONDS}"
done

echo "[rollback] health check failed after rollback deploy" >&2
exit 1
REMOTE_SCRIPT
