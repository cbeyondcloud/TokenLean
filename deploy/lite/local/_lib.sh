# shellcheck shell=bash
# Shared by up.sh, down.sh and smoke.sh. Bash 3.2+ (the macOS default), Linux, Git Bash.
# No jq, no GNU-only flags.

LITE_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
LITE_ENV_FILE="$LITE_DIR/.env.lite"
LITE_STATE_DIR="$LITE_DIR/.state"
LITE_COMPOSE_FILE="$LITE_DIR/docker-compose.lite.yml"
# docker (default) or podman. Both are driven as `<engine> compose ...`.
LITE_ENGINE="${CONTAINER_ENGINE:-docker}"
LITE_ALL_PROFILES=(--profile qdrant --profile langfuse --profile mock)

lite_die() {
  echo "lite: $*" >&2
  exit 1
}

lite_check_engine() {
  command -v "$LITE_ENGINE" >/dev/null 2>&1 \
    || lite_die "'$LITE_ENGINE' not found (set CONTAINER_ENGINE=docker or podman)"
  "$LITE_ENGINE" compose version >/dev/null 2>&1 \
    || lite_die "'$LITE_ENGINE compose' is not available (Docker Compose v2, or podman with a compose provider)"
}

# Export each KEY=value of .env.lite. Values are taken literally (never evaluated by the
# shell). A variable already set to a non-empty value in the environment wins, as it does
# for compose itself.
lite_load_env() {
  [ -f "$LITE_ENV_FILE" ] || lite_die ".env.lite not found - run ./up.sh first"
  local line key value
  while IFS= read -r line || [ -n "$line" ]; do
    line=${line%$'\r'}
    case "$line" in '' | '#'*) continue ;; esac
    key=${line%%=*}
    value=${line#*=}
    case "$key" in '' | *[!A-Za-z0-9_]*) continue ;; esac
    if [ -z "${!key:-}" ]; then
      export "$key=$value"
    fi
  done <"$LITE_ENV_FILE"
}

lite_compose() {
  "$LITE_ENGINE" compose -f "$LITE_COMPOSE_FILE" --env-file "$LITE_ENV_FILE" "$@"
}

lite_base_url() {
  echo "http://127.0.0.1:${LITE_PROXY_PORT:-4100}"
}
