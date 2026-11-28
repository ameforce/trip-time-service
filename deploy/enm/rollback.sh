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
if ! command -v flock >/dev/null 2>&1; then
  echo "[rollback] flock is required for coordinated deployment recovery" >&2
  exit 1
fi
mkdir -p "\${ROLLBACK_DIR}"
handoff_file="\${ROLLBACK_DIR}/deploy-handoff.txt"
exec 9>>"\${ROLLBACK_DIR}/deploy.lock"
if ! flock -w 120 9; then
  echo "[rollback] timed out acquiring the environment deployment lock" >&2
  exit 1
fi

check_health() {
  curl --fail --silent --show-error --max-time 5 "http://127.0.0.1:\${HOST_PORT}/healthz" >/dev/null
}

load_handoff() {
  local -a record=()
  [[ -f "\${handoff_file}" ]] || return 1
  mapfile -t -n 6 record < "\${handoff_file}" || return 1
  [[ "\${#record[@]}" == 5 ]] || return 1
  if [[ "\${record[0]}" != "\${ROLLBACK_EXPECTED_ATTEMPT_ID}" \
    || "\${record[1]}" != "\${ROLLBACK_EXPECTED_IMAGE_REF}" \
    || ! "\${record[2]}" =~ ^[0-9a-f]{64}$ \
    || ! "\${record[3]}" =~ ^[-0-9A-Za-z._:/@]+$ \
    || ! "\${record[4]}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    return 1
  fi
  handoff_previous_ref="\${record[3]}"
  handoff_previous_id="\${record[4]}"
  handoff_replaced_id="\${record[2]}"
}

read_image_version() {
  local version
  version="\$(docker image inspect --format '{{range .Config.Env}}{{if eq (printf "%.12s" .) "TTS_VERSION="}}{{println .}}{{end}}{{end}}' "\$1")" || return 1
  [[ "\${version}" == TTS_VERSION=* ]] || return 0
  version="\${version#TTS_VERSION=}"
  if [[ "\${#version}" -le 128 && "\${version}" != "v0.0.0.0" \
    && "\${version}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(\.[0-9]+)?(-[A-Za-z0-9._-]+)?$ ]]; then
    printf '%s' "\${version}"
  fi
}

container_label() {
  case "\$1" in
    '<no value>'|'<nil>') ;;
    *) printf '%s' "\$1" ;;
  esac
}

current_id=""
current_image=""
current_attempt=""
current_previous=""
current_previous_id=""
current_image_id=""
current_restore_attempt=""
current_restore_candidate=""
current_restored_ref=""
current_running=""
container_format='{{println .Id}}{{println .Config.Image}}{{with .Config.Labels}}{{with index . "triptime_deploy_attempt"}}{{.}}{{end}}{{end}}{{println}}{{with .Config.Labels}}{{with index . "triptime_previous_image"}}{{.}}{{end}}{{end}}{{println}}{{with .Config.Labels}}{{with index . "triptime_previous_image_id"}}{{.}}{{end}}{{end}}{{println}}{{println .Image}}{{with .Config.Labels}}{{with index . "triptime_restore_attempt"}}{{.}}{{end}}{{end}}{{println}}{{with .Config.Labels}}{{with index . "triptime_restore_candidate"}}{{.}}{{end}}{{end}}{{println}}{{with .Config.Labels}}{{with index . "triptime_restored_image_ref"}}{{.}}{{end}}{{end}}{{println}}{{.State.Running}}'
if current="\$(docker container inspect --format "\${container_format}" "\${CONTAINER_NAME}" 2>/dev/null)"; then
  mapfile -t fields <<< "\${current}"
  current_id="\${fields[0]}"
  current_image="\${fields[1]:-}"
  current_attempt="\$(container_label "\${fields[2]:-}")"
  current_previous="\$(container_label "\${fields[3]:-}")"
  current_previous_id="\$(container_label "\${fields[4]:-}")"
  current_image_id="\${fields[5]:-}"
  current_restore_attempt="\$(container_label "\${fields[6]:-}")"
  current_restore_candidate="\$(container_label "\${fields[7]:-}")"
  current_restored_ref="\$(container_label "\${fields[8]:-}")"
  current_running="\${fields[9]:-}"
elif ! active_id="\$(docker container ls --all --quiet --filter "name=^/\${CONTAINER_NAME}$")" \
  || [[ -n "\${active_id}" ]]; then
  echo "[rollback] cannot determine active container state" >&2
  exit 1
fi

target_image_id=""
resume_restore_id=""
if [[ -n "\${ROLLBACK_EXPECTED_ATTEMPT_ID}" ]]; then
  handoff_previous_ref=""
  handoff_previous_id=""
  handoff_replaced_id=""
  if [[ -n "\${ROLLBACK_EXPECTED_IMAGE_REF}" ]] && load_handoff; then
    echo "[rollback] found a recovery record for this deployment attempt"
  fi
  if [[ -z "\${current_id}" && -n "\${handoff_previous_id}" ]]; then
    target_image="\${handoff_previous_ref}"
    target_image_id="\${handoff_previous_id}"
    echo "[rollback] recovering the previous image after an interrupted switch"
  elif [[ -n "\${current_id}" && -n "\${handoff_previous_id}" \
    && "\${current_restore_attempt}" == "\${ROLLBACK_EXPECTED_ATTEMPT_ID}" ]]; then
    if [[ "\${current_restore_candidate}" != "\${ROLLBACK_EXPECTED_IMAGE_REF}" \
      || "\${current_restored_ref}" != "\${handoff_previous_ref}" \
      || "\${current_image_id}" != "\${handoff_previous_id}" \
      || ("\${current_running}" != "true" && "\${current_running}" != "false") ]]; then
      echo "[rollback] restored container identity does not match the recovery record" >&2
      exit 1
    fi
    target_image="\${handoff_previous_ref}"
    target_image_id="\${handoff_previous_id}"
    resume_restore_id="\${current_id}"
  elif [[ -z "\${current_id}" || "\${current_attempt}" != "\${ROLLBACK_EXPECTED_ATTEMPT_ID}" \
    || "\${current_image}" != "\${ROLLBACK_EXPECTED_IMAGE_REF}" ]]; then
    if [[ -n "\${handoff_replaced_id}" && "\${current_id}" == "\${handoff_replaced_id}" ]]; then
      rm -f -- "\${handoff_file}"
    fi
    echo "[rollback] no active candidate from this deployment attempt"
    exit 3
  else
    target_image="\${current_previous}"
    target_image_id="\${current_previous_id}"
    if [[ -z "\${target_image}" && -n "\${target_image_id}" ]]; then
      if [[ "\${target_image_id}" == "\${handoff_previous_id}" ]]; then
        target_image="\${handoff_previous_ref}"
      else
        target_image="\${target_image_id}"
      fi
    fi
    if [[ "\${target_image}" == "\${handoff_previous_ref}" ]]; then
      if [[ -n "\${target_image_id}" && "\${target_image_id}" != "\${handoff_previous_id}" ]]; then
        echo "[rollback] recorded previous image IDs disagree" >&2
        exit 1
      fi
      target_image_id="\${handoff_previous_id}"
    fi
  fi
else
  target_image="\${ROLLBACK_IMAGE_REF}"
  if [[ -z "\${target_image}" && -f "\${ROLLBACK_DIR}/previous-image.txt" ]]; then
    target_image="\$(cat "\${ROLLBACK_DIR}/previous-image.txt")"
    target_image="\$(container_label "\${target_image}")"
  fi
  if [[ -n "\${ROLLBACK_EXPECTED_IMAGE_REF}" && "\${current_image}" != "\${ROLLBACK_EXPECTED_IMAGE_REF}" ]]; then
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
if [[ -z "\${target_image_id}" ]]; then
  if ! docker image inspect "\${target_image}" >/dev/null 2>&1; then
    docker pull "\${target_image}" >/dev/null
  fi
  target_image_id="\$(docker image inspect --format '{{.Id}}' "\${target_image}")"
fi
if [[ ! "\${target_image_id}" =~ ^sha256:[0-9a-f]{64}$ ]] \
  || ! restore_version="\$(read_image_version "\${target_image_id}")"; then
  echo "[rollback] the previous image ID is invalid or unavailable" >&2
  exit 1
fi
if [[ -n "\${current_id}" && -z "\${resume_restore_id}" ]]; then
  if [[ -n "\${ROLLBACK_EXPECTED_ATTEMPT_ID}" ]]; then
    if [[ ! "\${current_id}" =~ ^[0-9a-f]{64}$ \
      || ! "\${target_image}" =~ ^[-0-9A-Za-z._:/@]+$ ]]; then
      echo "[rollback] cannot record the active container or previous image" >&2
      exit 1
    fi
    # A retry must know what to restore even if this run stops after removal.
    handoff_tmp="\$(mktemp "\${ROLLBACK_DIR}/.deploy-handoff.XXXXXX")"
    if ! printf '%s\n' "\${ROLLBACK_EXPECTED_ATTEMPT_ID}" "\${ROLLBACK_EXPECTED_IMAGE_REF}" \
      "\${current_id}" "\${target_image}" "\${target_image_id}" > "\${handoff_tmp}" \
      || ! mv -fT -- "\${handoff_tmp}" "\${handoff_file}"; then
      rm -f -- "\${handoff_tmp}"
      echo "[rollback] could not record the rollback handoff" >&2
      exit 1
    fi
  fi
  docker rm -f "\${current_id}" >/dev/null
fi

create_restored_container() {
  local image_ref="\$1"
  local -a args
  args=(
    --name "\${CONTAINER_NAME}"
    --restart unless-stopped
    --env-file "\${ENV_FILE}"
    --mount "type=volume,source=\${CONTAINER_NAME}-accounts,target=/var/lib/triptime"
    --env "TTS_ACCOUNT_DB_PATH=/var/lib/triptime/accounts.sqlite3"
    --env "TTS_ACCOUNT_ALLOWED_ORIGIN=https://\${DEPLOY_DOMAIN}"
    --env "TTS_CHROME_NO_SANDBOX=\${TTS_CHROME_NO_SANDBOX}"
    --publish "127.0.0.1:\${HOST_PORT}:\${CONTAINER_PORT}"
    --label "service=\${CONTAINER_PREFIX}"
    --label "deploy_env=\${DEPLOY_ENV}"
  )
  args+=(--label "triptime_restored_image_ref=\${target_image}")
  if [[ -n "\${ROLLBACK_EXPECTED_ATTEMPT_ID}" ]]; then
    args+=(
      --label "triptime_restore_attempt=\${ROLLBACK_EXPECTED_ATTEMPT_ID}"
      --label "triptime_restore_candidate=\${ROLLBACK_EXPECTED_IMAGE_REF}"
    )
  fi
  if [[ -n "\${restore_version}" ]]; then
    args+=(--env "TTS_VERSION=\${restore_version}")
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

  docker create "\${args[@]}" "\${image_ref}"
}

restored_at_expected_state() {
  local expected_running="\$1" state
  state="\$(docker container inspect --format '{{.Id}}|{{.Image}}|{{.State.Running}}' "\${CONTAINER_NAME}")" \
    && [[ "\${state}" == "\${restored_id}|\${target_image_id}|\${expected_running}" ]]
}

restored_healthy() {
  check_health && restored_at_expected_state true
}

finish_restore() {
  printf '%s\n' "\${target_image}" > "\${ROLLBACK_DIR}/current-image.txt"
  rm -f -- "\${handoff_file}"
  echo "[rollback] health check passed"
}

if [[ -n "\${resume_restore_id}" ]]; then
  restored_id="\${resume_restore_id}"
  if [[ "\${current_running}" == "true" ]] && restored_healthy; then
    finish_restore
    exit 0
  fi
  if ! restored_at_expected_state "\${current_running}"; then
    echo "[rollback] the restored container state changed before restart" >&2
    exit 1
  fi
  if [[ "\${current_running}" == "true" ]]; then
    echo "[rollback] restarting the previous image owned by this attempt"
    docker restart "\${restored_id}" >/dev/null
  else
    echo "[rollback] starting the previous image already created by this attempt"
    docker start "\${restored_id}" >/dev/null
  fi
else
  restored_id="\$(create_restored_container "\${target_image_id}")"
  docker start "\${restored_id}" >/dev/null
fi

for ((i=1; i<=HEALTHCHECK_RETRIES; i++)); do
  if restored_healthy; then
    finish_restore
    exit 0
  fi
  sleep "\${HEALTHCHECK_DELAY_SECONDS}"
done

echo "[rollback] health check failed after rollback deploy" >&2
exit 1
REMOTE_SCRIPT
