#!/usr/bin/env bash
# Start the TokenLean LITE stack: proxy (LITE profile) + Postgres + Redis.
#
#   ./up.sh [--qdrant] [--langfuse] [--mock] [--no-build]
#
#   --qdrant    also start Qdrant (for optimisations you later turn on that need it)
#   --langfuse  also start Langfuse and send traces to it (UI on LITE_LANGFUSE_PORT)
#   --mock      also start a local OpenAI-compatible stand-in and route every model to it:
#               no provider key, no spend (what CI and smoke use)
#   --no-build  do not rebuild the proxy image
#
# First run: creates .env.lite from .env.lite.template and generates its secrets and proxy
# keys. Engine: CONTAINER_ENGINE=docker (default) or podman.
set -euo pipefail
# shellcheck source=_lib.sh
. "$(cd "$(dirname "$0")" && pwd)/_lib.sh"

QDRANT=0
LANGFUSE=0
MOCK=0
BUILD=1
while [ $# -gt 0 ]; do
  case "$1" in
    --qdrant) QDRANT=1 ;;
    --langfuse) LANGFUSE=1 ;;
    --mock) MOCK=1 ;;
    --no-build) BUILD=0 ;;
    -h | --help)
      sed -n '2,15p' "$0"
      exit 0
      ;;
    *) lite_die "unknown option: $1 (see --help)" ;;
  esac
  shift
done

lite_check_engine
command -v curl >/dev/null 2>&1 || lite_die "curl is required"

if [ ! -f "$LITE_ENV_FILE" ]; then
  cp "$LITE_DIR/.env.lite.template" "$LITE_ENV_FILE"
  echo "lite: created .env.lite (edit it to add provider keys)"
fi
chmod 600 "$LITE_ENV_FILE" 2>/dev/null || true

rand_hex() { od -An -tx1 -N"$1" /dev/urandom | tr -d ' \n'; }

# Set KEY in .env.lite only where it is empty or missing; never overwrite a value.
fill() {
  local key=$1 value=$2 tmp
  if grep -q "^$key=$" "$LITE_ENV_FILE"; then
    tmp="$LITE_ENV_FILE.tmp"
    awk -v k="$key" -v v="$value" '$0 == k "=" { print k "=" v; next } { print }' \
      "$LITE_ENV_FILE" >"$tmp"
    mv "$tmp" "$LITE_ENV_FILE"
  elif ! grep -q "^$key=" "$LITE_ENV_FILE"; then
    printf '%s=%s\n' "$key" "$value" >>"$LITE_ENV_FILE"
  fi
}

TENANT=$(sed -n 's/^LITE_TENANT_ID=//p' "$LITE_ENV_FILE" | tr -d '\r' | head -n 1)
TENANT=${TENANT:-LITE-LOC-01}
case "$TENANT" in *[!A-Za-z0-9._-]*) lite_die "LITE_TENANT_ID may hold only A-Z a-z 0-9 . _ -" ;; esac

fill TOKENLEAN_API_KEY "tok-$TENANT-$(rand_hex 24)"
fill TOKENLEAN_ADMIN_KEY "tok-$TENANT-admin-$(rand_hex 24)"
fill DB_PASSWORD "$(rand_hex 24)"
fill REDIS_PASSWORD "$(rand_hex 24)"
fill QDRANT_API_KEY "$(rand_hex 24)"
fill METRICS_SCRAPE_TOKEN "$(rand_hex 24)"
fill LANGFUSE_PUBLIC_KEY "pk-lf-$(rand_hex 16)"
fill LANGFUSE_SECRET_KEY "sk-lf-$(rand_hex 24)"
fill LANGFUSE_NEXTAUTH_SECRET "$(rand_hex 32)"
fill LANGFUSE_SALT "$(rand_hex 32)"
fill LANGFUSE_INIT_USER_PASSWORD "$(rand_hex 16)"
chmod 600 "$LITE_ENV_FILE" 2>/dev/null || true
lite_load_env

# The proxy's key file: sha256(key) -> tenant. Only hashes; the keys stay in .env.lite.
if command -v sha256sum >/dev/null 2>&1; then
  sha() { printf '%s' "$1" | sha256sum | cut -d' ' -f1; }
else
  sha() { printf '%s' "$1" | shasum -a 256 | cut -d' ' -f1; }
fi
mkdir -p "$LITE_STATE_DIR"
chmod 755 "$LITE_STATE_DIR"   # the proxy runs as uid 1000, whoever owns this checkout
printf '{\n  "%s": {"tenant_id": "%s", "tier": "free"},\n  "%s": {"tenant_id": "%s", "tier": "free", "admin": true}\n}\n' \
  "$(sha "$TOKENLEAN_API_KEY")" "$TENANT" "$(sha "$TOKENLEAN_ADMIN_KEY")" "$TENANT" \
  >"$LITE_STATE_DIR/local-keys.json"
chmod 644 "$LITE_STATE_DIR/local-keys.json"   # read by the proxy's unprivileged user

# Overlays (paths inside the proxy container) and compose profiles for this run.
OVERLAYS="/app/lite/config/agentic.yaml"
PROFILES=()
if [ "$QDRANT" = 1 ]; then PROFILES+=(--profile qdrant); fi
if [ "$LANGFUSE" = 1 ]; then
  PROFILES+=(--profile langfuse)
  OVERLAYS="$OVERLAYS,/app/lite/config/langfuse.yaml"
fi
MODE=live
if [ "$MOCK" = 1 ]; then
  PROFILES+=(--profile mock)
  OVERLAYS="$OVERLAYS,/app/lite/mock/mock-provider.yaml"
  MODE=mock
fi
if [ -n "${LITE_EXTRA_OVERLAYS:-}" ]; then OVERLAYS="$OVERLAYS,$LITE_EXTRA_OVERLAYS"; fi
export LITE_CONFIG_OVERLAYS="$OVERLAYS"
printf 'LITE_MODE=%s\n' "$MODE" >"$LITE_STATE_DIR/stack.env"

UP_ARGS=(up -d --remove-orphans)
if [ -n "${TOKENLEAN_IMAGE:-}" ]; then
  if ! "$LITE_ENGINE" image inspect "$TOKENLEAN_IMAGE" >/dev/null 2>&1; then
    "$LITE_ENGINE" pull "$TOKENLEAN_IMAGE"
  fi
  UP_ARGS+=(--no-build)
elif [ "$BUILD" = 1 ]; then
  UP_ARGS+=(--build)
fi

echo "lite: starting ($MODE mode) with $LITE_ENGINE ..."
lite_compose ${PROFILES[@]+"${PROFILES[@]}"} "${UP_ARGS[@]}"

URL="$(lite_base_url)/health"
TIMEOUT=${LITE_HEALTH_TIMEOUT:-240}
START=$(date +%s)
printf 'lite: waiting for %s ' "$URL"
while :; do
  BODY=$(curl -fsS "$URL" 2>/dev/null || true)
  case "$BODY" in *'"status":"ok"'*) break ;; esac
  if [ $(($(date +%s) - START)) -ge "$TIMEOUT" ]; then
    echo
    lite_compose ${PROFILES[@]+"${PROFILES[@]}"} ps || true
    lite_compose logs --tail 60 proxy || true
    lite_die "the proxy did not report healthy within ${TIMEOUT}s (last: ${BODY:-no answer})"
  fi
  printf '.'
  sleep 3
done
echo " ok"
echo "lite: proxy  $(lite_base_url)  (OpenAI /v1/chat/completions, Anthropic /v1/messages, Gemini /v1beta)"
echo "lite: key    TOKENLEAN_API_KEY in $LITE_ENV_FILE"
if [ "$LANGFUSE" = 1 ]; then
  echo "lite: langfuse http://127.0.0.1:${LITE_LANGFUSE_PORT:-3110} (${LANGFUSE_INIT_USER_EMAIL:-admin@lite.local}, password in .env.lite)"
fi
echo "lite: next   ./smoke.sh"
