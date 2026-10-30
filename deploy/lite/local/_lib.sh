# shellcheck shell=bash
# Shared by up.sh, down.sh and smoke.sh. Bash 3.2+ (the macOS default), Linux, Git Bash.
# No jq, no GNU-only flags.

LITE_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# LITE_ENV_FILE and LITE_BASE_URL (environment only) point smoke at another stack: the cloud
# smoke (deploy/lite/cloud/smoke.sh) runs these same checks against a cluster that way.
LITE_ENV_FILE="${LITE_ENV_FILE:-$LITE_DIR/.env.lite}"
LITE_STATE_DIR="$LITE_DIR/.state"
LITE_COMPOSE_FILE="$LITE_DIR/docker-compose.lite.yml"
# Every compose call names this project explicitly (-p), so nothing else can redirect it.
# LITE_PROJECT (environment only, never .env.lite) runs a second LITE stack next to the
# first, e.g. a review checkout or a CI job; give it its own LITE_*_PORT values too.
LITE_PROJECT="${LITE_PROJECT:-tokenlean-lite}"
case "$LITE_PROJECT" in
  '' | *[!a-z0-9_-]* | [!a-z0-9]*)
    echo "lite: LITE_PROJECT may hold only a-z 0-9 _ - (starting with a letter or digit)" >&2
    exit 1
    ;;
esac
# docker (default) or podman. Both are driven as `<engine> compose ...`.
LITE_ENGINE="${CONTAINER_ENGINE:-docker}"
LITE_ALL_PROFILES=(--profile qdrant --profile langfuse --profile mock --profile grafana)

# Compose settings exported for another stack must never steer these scripts: the full
# TokenLean .env sets COMPOSE_PROJECT_NAME=token-opt, and with it `down --purge` would
# remove that stack's containers and volumes.
unset COMPOSE_PROJECT_NAME COMPOSE_FILE COMPOSE_PROFILES

# Git Bash (MSYS2) rewrites an exported variable that looks like a path when it starts a
# Windows program: /app/lite/config/agentic.yaml would reach docker.exe as
# C:/Program Files/Git/app/lite/config/agentic.yaml. The overlays are paths inside the
# container, so that variable is excluded. Ignored everywhere else.
export MSYS2_ENV_CONV_EXCL="LITE_CONFIG_OVERLAYS${MSYS2_ENV_CONV_EXCL:+;$MSYS2_ENV_CONV_EXCL}"

lite_die() {
  echo "lite: $*" >&2
  exit 1
}

# Print a script's header comment (the lines after the shebang, up to the first non-comment).
lite_usage() {
  awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$1"
}

lite_check_engine() {
  command -v "$LITE_ENGINE" >/dev/null 2>&1 \
    || lite_die "'$LITE_ENGINE' not found (set CONTAINER_ENGINE=docker or podman)"
  "$LITE_ENGINE" compose version >/dev/null 2>&1 \
    || lite_die "'$LITE_ENGINE compose' is not available (Docker Compose v2, or podman with a compose provider)"
}

# Export each KEY=value of .env.lite. Values are taken literally (never evaluated by the
# shell). A variable already set to a non-empty value in the environment wins, as it does
# for compose itself. COMPOSE_* keys are ignored (see above).
lite_load_env() {
  [ -f "$LITE_ENV_FILE" ] || lite_die ".env.lite not found - run ./up.sh first"
  local line key value
  while IFS= read -r line || [ -n "$line" ]; do
    line=${line%$'\r'}
    case "$line" in '' | '#'*) continue ;; esac
    key=${line%%=*}
    value=${line#*=}
    case "$key" in '' | *[!A-Za-z0-9_]* | COMPOSE_*) continue ;; esac
    if [ -z "${!key:-}" ]; then
      export "$key=$value"
    fi
  done <"$LITE_ENV_FILE"
}

# .env.lite edited in a Windows editor gets CRLF line ends; drop the CRs so no value keeps
# one (compose, bash and the key file would all see it).
lite_normalize_env_file() {
  local tmp="$LITE_ENV_FILE.tmp"
  tr -d '\r' <"$LITE_ENV_FILE" >"$tmp"
  mv "$tmp" "$LITE_ENV_FILE"
}

# Set KEY in .env.lite only where it is empty or missing; never overwrite a value.
lite_fill() {
  local key=$1 value=$2 tmp="$LITE_ENV_FILE.tmp"
  if grep -q "^$key=$" "$LITE_ENV_FILE"; then
    awk -v k="$key" -v v="$value" '$0 == k "=" { print k "=" v; next } { print }' \
      "$LITE_ENV_FILE" >"$tmp"
    mv "$tmp" "$LITE_ENV_FILE"
  elif ! grep -q "^$key=" "$LITE_ENV_FILE"; then
    printf '%s=%s\n' "$key" "$value" >>"$LITE_ENV_FILE"
  fi
}

lite_compose() {
  "$LITE_ENGINE" compose -p "$LITE_PROJECT" -f "$LITE_COMPOSE_FILE" --env-file "$LITE_ENV_FILE" "$@"
}

lite_base_url() {
  if [ -n "${LITE_BASE_URL:-}" ]; then
    echo "${LITE_BASE_URL%/}"
  else
    echo "http://127.0.0.1:${LITE_PROXY_PORT:-4100}"
  fi
}
