# shellcheck shell=bash
# Shared by install.sh, upgrade.sh, uninstall.sh and smoke.sh. Bash 3.2+ (the macOS default),
# Linux, Git Bash. No jq, no GNU-only flags. helm (3.8+ or 4) and kubectl come from PATH, or
# from HELM / KUBECTL.

CLOUD_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CLOUD_CHART="$CLOUD_DIR/helm/tokenlean-lite"
# Owner-only files written by install --create-secrets / --mock and read by smoke: the dev
# bootstrap keys and the mode of each release (<namespace>.<release>.env). Git-ignored.
CLOUD_STATE_DIR="${TOKENLEAN_LITE_STATE_DIR:-$CLOUD_DIR/.state}"
CLOUD_HELM="${HELM:-helm}"
CLOUD_KUBECTL="${KUBECTL:-kubectl}"
CLOUD_NAMESPACE="${TOKENLEAN_LITE_NAMESPACE:-tokenlean-lite}"
CLOUD_RELEASE="${TOKENLEAN_LITE_RELEASE:-tokenlean-lite}"
CLOUD_CONTEXT=""
CLOUD_TIMEOUT=10m
CLOUD_ENV=""
CLOUD_IMAGE="${TOKENLEAN_IMAGE:-}"
CLOUD_VALUES=()
CLOUD_MOCK=0
CLOUD_CREATE_SECRETS=0
CLOUD_PURGE=0
CLOUD_URL=""
CLOUD_LOCAL_PORT=4180
CLOUD_SECRET_DIR=.          # where cloud_make_secret finds its files (set per run)
# Labels the Secrets that install --create-secrets made, so uninstall --purge finds only them.
CLOUD_CREATED_BY="app.kubernetes.io/created-by=tokenlean-lite-install"

cloud_die() {
  echo "tokenlean-lite: $*" >&2
  exit 1
}

# Print a script's header comment (the lines after the shebang, up to the first non-comment).
cloud_usage() {
  awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$1"
}

# cloud_parse_args "<options this script takes>" "$@": sets the CLOUD_* variables.
cloud_parse_args() {
  local accepts=" $1 " opt
  shift
  while [ $# -gt 0 ]; do
    opt=$1
    case "$opt" in
      -h | --help)
        cloud_usage "$0"
        exit 0
        ;;
      --*)
        case "$accepts" in *" $opt "*) ;; *) cloud_die "unknown option: $opt (see --help)" ;; esac
        ;;
      *)
        case "$accepts" in *" ENV "*) ;; *) cloud_die "unexpected argument: $opt (see --help)" ;; esac
        [ -z "$CLOUD_ENV" ] || cloud_die "one environment only (got $CLOUD_ENV and $opt)"
        CLOUD_ENV=$opt
        shift
        continue
        ;;
    esac
    case "$opt" in
      --mock) CLOUD_MOCK=1 ;;
      --create-secrets) CLOUD_CREATE_SECRETS=1 ;;
      --purge) CLOUD_PURGE=1 ;;
      *)
        [ $# -ge 2 ] || cloud_die "$opt needs a value"
        case "$opt" in
          --namespace) CLOUD_NAMESPACE=$2 ;;
          --release) CLOUD_RELEASE=$2 ;;
          --context) CLOUD_CONTEXT=$2 ;;
          --timeout) CLOUD_TIMEOUT=$2 ;;
          --image) CLOUD_IMAGE=$2 ;;
          --values) CLOUD_VALUES+=("$2") ;;
          --url) CLOUD_URL=$2 ;;
          --local-port) CLOUD_LOCAL_PORT=$2 ;;
        esac
        shift
        ;;
    esac
    shift
  done
  cloud_check_name namespace "$CLOUD_NAMESPACE" 63
  cloud_check_name release "$CLOUD_RELEASE" 53
  case "$CLOUD_LOCAL_PORT" in '' | *[!0-9]*) cloud_die "--local-port must be a number" ;; esac
}

# A Kubernetes name: lower-case letters, digits and '-', starting and ending with a letter or
# digit, at most $3 characters.
cloud_check_name() {
  case "$2" in
    '' | *[!a-z0-9-]* | -* | *-)
      cloud_die "the $1 '$2' may hold only a-z 0-9 - (starting and ending with a letter or digit)"
      ;;
  esac
  [ "${#2}" -le "$3" ] || cloud_die "the $1 '$2' is longer than $3 characters"
}

cloud_check_env() {
  case "$CLOUD_ENV" in
    dev | staging | prod | managed) ;;
    '') cloud_die "name the environment: dev, staging, prod or managed (see --help)" ;;
    *) cloud_die "the environment must be dev, staging, prod or managed (got '$CLOUD_ENV')" ;;
  esac
  if [ "$CLOUD_MOCK" = 1 ] && [ "$CLOUD_ENV" != dev ]; then
    cloud_die "--mock is for dev only: it answers every model with the mock provider"
  fi
  if [ "$CLOUD_CREATE_SECRETS" = 1 ] && [ "$CLOUD_ENV" != dev ]; then
    cloud_die "--create-secrets is for dev only: create $CLOUD_ENV's Secrets from your secret store"
  fi
}

# Split CLOUD_IMAGE (registry[:port]/repository:tag) into CLOUD_IMAGE_REPO and CLOUD_IMAGE_TAG.
cloud_check_image() {
  [ -n "$CLOUD_IMAGE" ] || cloud_die "--image (or TOKENLEAN_IMAGE) must name the proxy image, e.g. registry.example.com/tokenlean-proxy:1.0.0"
  local last=${CLOUD_IMAGE##*/}
  case "$last" in
    *:*) ;;
    *) cloud_die "--image $CLOUD_IMAGE has no tag: name an exact release (repository:tag)" ;;
  esac
  CLOUD_IMAGE_TAG=${last##*:}
  CLOUD_IMAGE_REPO=${CLOUD_IMAGE%:*}
  case "$CLOUD_IMAGE_TAG" in
    '' | latest | set-me) cloud_die "--image $CLOUD_IMAGE: name an exact release, never latest" ;;
  esac
}

cloud_check_tools() {
  command -v "$CLOUD_HELM" >/dev/null 2>&1 || [ -x "$CLOUD_HELM" ] \
    || cloud_die "'$CLOUD_HELM' not found: install helm 3.8+ (or 4), or set HELM"
  command -v "$CLOUD_KUBECTL" >/dev/null 2>&1 || [ -x "$CLOUD_KUBECTL" ] \
    || cloud_die "'$CLOUD_KUBECTL' not found: install kubectl, or set KUBECTL"
}

cloud_helm() {
  if [ -n "$CLOUD_CONTEXT" ]; then
    "$CLOUD_HELM" "$@" --kube-context "$CLOUD_CONTEXT"
  else
    "$CLOUD_HELM" "$@"
  fi
}

cloud_kubectl() {
  if [ -n "$CLOUD_CONTEXT" ]; then
    "$CLOUD_KUBECTL" --context "$CLOUD_CONTEXT" -n "$CLOUD_NAMESPACE" "$@"
  else
    "$CLOUD_KUBECTL" -n "$CLOUD_NAMESPACE" "$@"
  fi
}

cloud_selector() {
  echo "app.kubernetes.io/instance=$CLOUD_RELEASE,app.kubernetes.io/component=$1"
}

cloud_state_file() {
  echo "$CLOUD_STATE_DIR/$CLOUD_NAMESPACE.$CLOUD_RELEASE.env"
}

cloud_state_get() {
  local file
  file=$(cloud_state_file)
  [ -f "$file" ] || return 0
  sed -n "s/^$1=//p" "$file" | tr -d '\r' | head -n 1
}

# Set KEY=value in the release's state file (created owner-only).
cloud_state_set() {
  local file tmp
  file=$(cloud_state_file)
  tmp="$file.tmp"
  (
    umask 077
    mkdir -p "$CLOUD_STATE_DIR"
    touch "$file"
    { grep -v "^$1=" "$file" || true; } >"$tmp"
    printf '%s=%s\n' "$1" "$2" >>"$tmp"
    mv "$tmp" "$file"
  )
  chmod 600 "$file" 2>/dev/null || true
}

cloud_context_name() {
  if [ -n "$CLOUD_CONTEXT" ]; then
    echo "$CLOUD_CONTEXT"
  else
    "$CLOUD_KUBECTL" config current-context 2>/dev/null || echo "(the current one)"
  fi
}

# What a failed rollout looks like: the release's pods, recent events, the proxy's last logs.
cloud_diagnose() {
  echo "tokenlean-lite: pods of release $CLOUD_RELEASE:" >&2
  cloud_kubectl get pods -l "app.kubernetes.io/instance=$CLOUD_RELEASE" >&2 || true
  cloud_kubectl get events --sort-by=.lastTimestamp 2>/dev/null | tail -n 15 >&2 || true
  cloud_kubectl logs -l "$(cloud_selector proxy)" --tail 40 >&2 || true
  cloud_kubectl logs -l "$(cloud_selector migrations)" --tail 40 >&2 || true
}

cloud_rand_hex() { od -An -tx1 -N"$1" /dev/urandom | tr -d ' \n'; }

if command -v sha256sum >/dev/null 2>&1; then
  cloud_sha256() { printf '%s' "$1" | sha256sum | cut -d' ' -f1; }
else
  cloud_sha256() { printf '%s' "$1" | shasum -a 256 | cut -d' ' -f1; }
fi

# cloud_make_secret NAME [key=file ...]: create the Secret from files in CLOUD_SECRET_DIR (never
# from values on the command line) and label it, unless it exists. Returns 1 when it already
# existed. kubectl runs in that folder and gets relative names: Git Bash rewrites no path after
# `--from-file=key=`, so an absolute /tmp path would not reach a Windows kubectl.exe.
cloud_make_secret() {
  local name=$1 arg
  shift
  if cloud_kubectl get secret "$name" >/dev/null 2>&1; then
    echo "tokenlean-lite: kept the existing Secret $name"
    return 1
  fi
  local args=(create secret generic "$name")
  for arg in "$@"; do args+=("--from-file=$arg"); done
  if ! (cd "$CLOUD_SECRET_DIR" && cloud_kubectl "${args[@]}" >/dev/null); then
    cloud_die "could not create the Secret $name"
  fi
  cloud_kubectl label secret "$name" "$CLOUD_CREATED_BY" \
    "app.kubernetes.io/instance=$CLOUD_RELEASE" >/dev/null || true
  echo "tokenlean-lite: created the Secret $name"
}

# Dev only: the Secrets values/dev.yaml names, except Postgres's (it belongs to your dev
# Postgres): random passwords, bootstrap proxy keys (hashes in the Secret, the keys in the
# owner-only state file for smoke), and the provider keys set as LLM_KEY_* in the environment.
cloud_create_dev_secrets() {
  local tenant=${LITE_TENANT_ID:-LITE-DEV-01} tmp api admin var
  case "$tenant" in *[!A-Za-z0-9._-]* | '') cloud_die "LITE_TENANT_ID may hold only A-Z a-z 0-9 . _ -" ;; esac
  if ! cloud_kubectl get namespace "$CLOUD_NAMESPACE" >/dev/null 2>&1; then
    if [ -n "$CLOUD_CONTEXT" ]; then
      "$CLOUD_KUBECTL" --context "$CLOUD_CONTEXT" create namespace "$CLOUD_NAMESPACE" >/dev/null
    else
      "$CLOUD_KUBECTL" create namespace "$CLOUD_NAMESPACE" >/dev/null
    fi
  fi
  tmp=$(umask 077 && mktemp -d 2>/dev/null || mktemp -d -t tokenlean-lite)
  chmod 700 "$tmp"
  CLOUD_SECRET_DIR=$tmp
  api="tok-$tenant-$(cloud_rand_hex 24)"
  admin="tok-$tenant-admin-$(cloud_rand_hex 24)"
  printf '{\n  "%s": {"tenant_id": "%s", "tier": "free"},\n  "%s": {"tenant_id": "%s", "tier": "free", "admin": true}\n}\n' \
    "$(cloud_sha256 "$api")" "$tenant" "$(cloud_sha256 "$admin")" "$tenant" >"$tmp/local-keys.json"
  if cloud_make_secret tokenlean-lite-bootstrap-keys "local-keys.json=local-keys.json"; then
    cloud_state_set TOKENLEAN_API_KEY "$api"
    cloud_state_set TOKENLEAN_ADMIN_KEY "$admin"
    cloud_state_set LITE_TENANT_ID "$tenant"
    echo "tokenlean-lite: the bootstrap keys are in $(cloud_state_file) (owner-only)"
  elif [ -z "$(cloud_state_get TOKENLEAN_API_KEY)" ]; then
    echo "tokenlean-lite: note: no keys on file for this release; smoke needs TOKENLEAN_API_KEY and TOKENLEAN_ADMIN_KEY" >&2
  fi
  cloud_rand_hex 24 >"$tmp/redis-password"
  cloud_make_secret tokenlean-lite-redis "password=redis-password" || true
  cloud_rand_hex 24 >"$tmp/scrape-token"
  cloud_make_secret tokenlean-lite-metrics "scrape-token=scrape-token" || true
  local provider=()
  for var in $(env | sed -n 's/^\(LLM_KEY_[A-Z0-9_]*\)=.\{1,\}$/\1/p'); do
    printf '%s' "${!var}" >"$tmp/$var"
    provider+=("$var=$var")
  done
  cloud_make_secret tokenlean-lite-provider-keys ${provider[@]+"${provider[@]}"} || true
  rm -rf "$tmp"
}

# install / upgrade: helm upgrade [--install] with values/<env>.yaml, then wait for the rollout.
cloud_deploy() {
  local mode=$1 values deployment v
  cloud_check_env
  cloud_check_image
  cloud_check_tools
  values="$CLOUD_DIR/values/$CLOUD_ENV.yaml"
  [ -f "$values" ] || cloud_die "$values not found"
  for v in ${CLOUD_VALUES[@]+"${CLOUD_VALUES[@]}"}; do
    [ -f "$v" ] || cloud_die "--values $v: file not found"
  done
  if [ "$mode" = upgrade ] && ! cloud_helm status "$CLOUD_RELEASE" --namespace "$CLOUD_NAMESPACE" >/dev/null 2>&1; then
    cloud_die "no release $CLOUD_RELEASE in namespace $CLOUD_NAMESPACE: run install.sh first"
  fi
  echo "tokenlean-lite: $mode $CLOUD_RELEASE ($CLOUD_ENV, $CLOUD_IMAGE) in namespace $CLOUD_NAMESPACE, context $(cloud_context_name)"
  if [ "$CLOUD_CREATE_SECRETS" = 1 ]; then cloud_create_dev_secrets; fi

  local args=(upgrade)
  if [ "$mode" = install ]; then args+=(--install); fi
  args+=("$CLOUD_RELEASE" "$CLOUD_CHART" --namespace "$CLOUD_NAMESPACE")
  if [ "$mode" = install ]; then args+=(--create-namespace); fi
  args+=(-f "$values")
  for v in ${CLOUD_VALUES[@]+"${CLOUD_VALUES[@]}"}; do args+=(-f "$v"); done
  args+=(--set "image.repository=$CLOUD_IMAGE_REPO" --set "image.tag=$CLOUD_IMAGE_TAG")
  if [ "$CLOUD_MOCK" = 1 ]; then args+=(--set mockProvider.enabled=true); fi
  args+=(--wait --timeout "$CLOUD_TIMEOUT")
  if ! cloud_helm "${args[@]}"; then
    cloud_diagnose
    cloud_die "helm $mode failed (the release keeps its previous revision; helm history $CLOUD_RELEASE -n $CLOUD_NAMESPACE)"
  fi

  deployment=$(cloud_kubectl get deployment -l "$(cloud_selector proxy)" -o name | head -n 1)
  [ -n "$deployment" ] || cloud_die "no proxy Deployment found for release $CLOUD_RELEASE"
  if ! cloud_kubectl rollout status "$deployment" --timeout "$CLOUD_TIMEOUT"; then
    cloud_diagnose
    cloud_die "the proxy did not roll out within $CLOUD_TIMEOUT"
  fi
  if [ "$CLOUD_MOCK" = 1 ]; then cloud_state_set LITE_MODE mock; else cloud_state_set LITE_MODE live; fi
  echo "tokenlean-lite: $CLOUD_RELEASE is up in namespace $CLOUD_NAMESPACE"
  echo "tokenlean-lite: next   ./smoke.sh --namespace $CLOUD_NAMESPACE --release $CLOUD_RELEASE"
}
