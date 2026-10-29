#!/usr/bin/env bash
# End to end on a local kind cluster: a throwaway Postgres, then install.sh dev (bundled Redis,
# the mock provider, generated Secrets), smoke.sh, upgrade.sh, smoke.sh again, uninstall.sh
# --purge, and the cluster deleted. Keyless: nothing reaches a real provider.
#
#   ./e2e.sh --image <proxy image already built> [--keep]
#
#   --image REF   the proxy image (or TOKENLEAN_IMAGE), e.g. one built from src/proxy:
#                 docker build -t tokenlean-lite-proxy:1.0.0-dev src/proxy
#   --keep        keep the cluster afterwards (delete it with KIND_CLUSTER=... ./e2e.sh --delete)
#   --delete      only delete the cluster
#
# Needs Docker only (Docker Desktop, or Docker Engine on Linux): kind, helm and kubectl run in
# pinned containers. KIND_CLUSTER names the cluster (default tokenlean-lite-e2e).
set -euo pipefail
# Git Bash (Windows): docker gets the host paths below as they are written.
export MSYS_NO_PATHCONV=1

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../../../.." && pwd)
CLUSTER=${KIND_CLUSTER:-tokenlean-lite-e2e}
IMAGE=${TOKENLEAN_IMAGE:-}
KEEP=0
DELETE_ONLY=0
NAMESPACE=tokenlean-lite

# Pins (released on or before 2026-10-15): kind v0.33.0 and the node image its release names,
# the Docker CLI that kind drives, and helm + kubectl.
KIND_URL=https://github.com/kubernetes-sigs/kind/releases/download/v0.33.0/kind-linux-amd64
KIND_SHA256=aee6151561422756b764a4ae28e7f44cda5af5a9eead3cc9985112b1de8d8e0d
NODE_IMAGE=kindest/node:v1.34.11@sha256:44e222ee2132dab25ff87301682f89eb82c7880ea3a1bf543bfe9708fd08d67d
DOCKER_CLI_IMAGE=docker:29.8.2-cli@sha256:35cfcf893c20f2931f4909151b5407aaaeab711277de7ce8b8f521f095d54c32
K8S_TOOLS_IMAGE=alpine/k8s:1.34.11@sha256:0382123ab8ae17d8da5d43cc299242933eb012cfbe56787b43731c942c3eed80

while [ $# -gt 0 ]; do
  case "$1" in
    --image) IMAGE=${2:?--image needs a value}; shift ;;
    --keep) KEEP=1 ;;
    --delete) DELETE_ONLY=1 ;;
    -h | --help) awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit 0 ;;
    *) echo "e2e: unknown option: $1" >&2; exit 1 ;;
  esac
  shift
done
case "$CLUSTER" in '' | *[!a-z0-9-]*) echo "e2e: KIND_CLUSTER may hold only a-z 0-9 -" >&2; exit 1 ;; esac

# A path as the Docker daemon sees it (Windows form under Git Bash).
host_path() { (cd "$1" && { pwd -W 2>/dev/null || pwd; }); }

WORK=$(mktemp -d 2>/dev/null || mktemp -d -t lite-e2e)
chmod 700 "$WORK"
mkdir -p "$WORK/state" "$WORK/bin"

kind() {
  docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v "$(host_path "$WORK/bin"):/bin-cache" \
    "$DOCKER_CLI_IMAGE" sh -ec '
      if [ ! -x /bin-cache/kind ]; then
        wget -q -O /bin-cache/kind.tmp "$0"
        echo "$1  /bin-cache/kind.tmp" | sha256sum -c - >/dev/null
        chmod +x /bin-cache/kind.tmp && mv /bin-cache/kind.tmp /bin-cache/kind
      fi
      shift; exec /bin-cache/kind "$@"' "$KIND_URL" "$KIND_SHA256" "$@"
}

# helm, kubectl and the scripts, on the cluster's network, with the repository read-only.
tools() {
  docker run --rm --network kind -v "$(host_path "$REPO"):/repo:ro" -v "$(host_path "$WORK"):/work" \
    -e KUBECONFIG=/work/kubeconfig -e TOKENLEAN_LITE_STATE_DIR=/work/state \
    -w /repo/deploy/lite/cloud "$K8S_TOOLS_IMAGE" "$@"
}

cleanup() {
  local code=$?
  if [ "$KEEP" = 0 ]; then
    echo "e2e: deleting cluster $CLUSTER"
    kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
    rm -rf "$WORK"
  else
    echo "e2e: kept cluster $CLUSTER; its kubeconfig and the smoke keys are in $WORK"
  fi
  exit "$code"
}
trap cleanup EXIT

if [ "$DELETE_ONLY" = 1 ]; then exit 0; fi
[ -n "$IMAGE" ] || { echo "e2e: --image (or TOKENLEAN_IMAGE) must name a proxy image you built" >&2; exit 1; }
docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "e2e: image $IMAGE not found locally" >&2; exit 1; }

echo "e2e: creating cluster $CLUSTER"
kind create cluster --name "$CLUSTER" --image "$NODE_IMAGE" --wait 180s
kind get kubeconfig --internal --name "$CLUSTER" >"$WORK/kubeconfig"
chmod 600 "$WORK/kubeconfig"
echo "e2e: loading $IMAGE into the cluster"
kind load docker-image "$IMAGE" --name "$CLUSTER"

echo "e2e: throwaway Postgres"
tools kubectl create namespace "$NAMESPACE"
od -An -tx1 -N24 /dev/urandom | tr -d ' \n' >"$WORK/pg-password"
tools kubectl -n "$NAMESPACE" create secret generic tokenlean-lite-postgres \
  --from-file=password=/work/pg-password
tools kubectl -n "$NAMESPACE" apply -f kind/dev-postgres.yaml
tools kubectl -n "$NAMESPACE" rollout status deployment/tokenlean-lite-dev-postgres --timeout 300s

echo "e2e: install"
tools bash ./install.sh dev --image "$IMAGE" --mock --create-secrets --values kind/values.yaml --timeout 10m
echo "e2e: smoke"
tools bash ./smoke.sh
echo "e2e: upgrade (the migration hook runs again, the proxy rolls)"
tools bash ./upgrade.sh dev --image "$IMAGE" --mock --values kind/values.yaml --timeout 10m
tools bash ./smoke.sh
echo "e2e: uninstall --purge"
tools bash ./uninstall.sh --purge
echo "e2e: all steps passed"
