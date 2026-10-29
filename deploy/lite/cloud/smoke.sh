#!/usr/bin/env bash
# Smoke-test a TokenLean LITE release on a cluster: the same checks as deploy/lite/local/smoke.sh
# (health, chat, stream, messages, cost headers, usage rows, a team-bound key with its cap and
# its revocation), run against the ingress URL or a port-forward. Exit 0 when all pass.
#
#   ./smoke.sh [--url URL] [--namespace NS] [--release NAME] [--context NAME] [--local-port N]
#
#   --url URL        the release's address (its ingress, https://...). Without it, the proxy
#                    Service is port-forwarded to 127.0.0.1:<local-port> (default 4180).
#   --namespace NS   default tokenlean-lite (or TOKENLEAN_LITE_NAMESPACE)
#   --release NAME   default tokenlean-lite (or TOKENLEAN_LITE_RELEASE)
#   --context NAME   the kubeconfig context to use (default: the current one)
#
# Keys: TOKENLEAN_API_KEY and TOKENLEAN_ADMIN_KEY (and LITE_TENANT_ID, their tenant) from the
# environment, else the ones install --create-secrets wrote. Model: SMOKE_MODEL, else the mock
# when the release was installed with --mock. A private CA: CURL_CA_BUNDLE=<pem file>.
set -euo pipefail
# shellcheck source=_lib.sh
. "$(cd "$(dirname "$0")" && pwd)/_lib.sh"

cloud_parse_args "--url --namespace --release --context --local-port" "$@"
command -v curl >/dev/null 2>&1 || cloud_die "curl is required"

API_KEY=${TOKENLEAN_API_KEY:-$(cloud_state_get TOKENLEAN_API_KEY)}
ADMIN_KEY=${TOKENLEAN_ADMIN_KEY:-$(cloud_state_get TOKENLEAN_ADMIN_KEY)}
TENANT=${LITE_TENANT_ID:-$(cloud_state_get LITE_TENANT_ID)}
[ -n "$API_KEY" ] || cloud_die "TOKENLEAN_API_KEY is not set: export it and TOKENLEAN_ADMIN_KEY, or install a dev release with --create-secrets"
[ -n "$ADMIN_KEY" ] || cloud_die "TOKENLEAN_ADMIN_KEY is not set (the team-key checks use /admin/keys)"
MODEL=${SMOKE_MODEL:-}
if [ -z "$MODEL" ]; then
  if [ "$(cloud_state_get LITE_MODE)" = mock ]; then MODEL=lite-mock-1
  else cloud_die "set SMOKE_MODEL to a model the release answers (or install a dev release with --mock)"
  fi
fi

TMP=$(umask 077 && mktemp -d 2>/dev/null || mktemp -d -t lite-cloud-smoke)
PF_PID=""
cleanup() {
  if [ -n "$PF_PID" ]; then kill "$PF_PID" 2>/dev/null || true; fi
  rm -rf "$TMP"
}
trap cleanup EXIT

URL=$CLOUD_URL
if [ -z "$URL" ]; then
  cloud_check_tools
  SVC=$(cloud_kubectl get service -l "$(cloud_selector proxy)" -o name | head -n 1)
  [ -n "$SVC" ] || cloud_die "no proxy Service for release $CLOUD_RELEASE in namespace $CLOUD_NAMESPACE"
  # kubectl itself goes to the background, not cloud_kubectl (a function): $! would then be a
  # subshell, and the cleanup would leave the real port-forward holding the port.
  PF=("$CLOUD_KUBECTL")
  if [ -n "$CLOUD_CONTEXT" ]; then PF+=(--context "$CLOUD_CONTEXT"); fi
  "${PF[@]}" -n "$CLOUD_NAMESPACE" port-forward "$SVC" "$CLOUD_LOCAL_PORT:http" \
    >"$TMP/port-forward.log" 2>&1 &
  PF_PID=$!
  URL="http://127.0.0.1:$CLOUD_LOCAL_PORT"
  for _ in $(seq 1 30); do
    if curl -fsS "$URL/health" >/dev/null 2>&1; then break; fi
    kill -0 "$PF_PID" 2>/dev/null || cloud_die "port-forward stopped: $(cat "$TMP/port-forward.log")"
    sleep 1
  done
fi

# The keys reach the local smoke in an owner-only env file, never on a command line.
printf 'TOKENLEAN_API_KEY=%s\nTOKENLEAN_ADMIN_KEY=%s\nLITE_TENANT_ID=%s\n' \
  "$API_KEY" "$ADMIN_KEY" "${TENANT:-LITE-DEV-01}" >"$TMP/smoke.env"
echo "tokenlean-lite smoke: release $CLOUD_RELEASE in namespace $CLOUD_NAMESPACE at $URL"
set +e
env -u TOKENLEAN_API_KEY -u TOKENLEAN_ADMIN_KEY -u LITE_TENANT_ID \
  LITE_ENV_FILE="$TMP/smoke.env" LITE_BASE_URL="$URL" SMOKE_MODEL="$MODEL" \
  bash "$CLOUD_DIR/../local/smoke.sh"
CODE=$?
set -e
exit "$CODE"
