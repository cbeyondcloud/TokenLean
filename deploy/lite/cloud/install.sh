#!/usr/bin/env bash
# Install TokenLean LITE on a Kubernetes cluster with the LITE Helm chart (an installed
# release is upgraded in place). Waits until the proxy has rolled out.
#
#   ./install.sh <env> --image <registry>/tokenlean-proxy:<release> [options]
#
#   <env>               dev | staging | prod | managed: the values file values/<env>.yaml
#   --image REF         the proxy image, an exact release (or TOKENLEAN_IMAGE)
#   --namespace NS      default tokenlean-lite (or TOKENLEAN_LITE_NAMESPACE)
#   --release NAME      default tokenlean-lite (or TOKENLEAN_LITE_RELEASE)
#   --context NAME      the kubeconfig context to use (default: the current one)
#   --values FILE       your own values over values/<env>.yaml (repeatable): hosts, Secret
#                       names, sizes. Never secret values: name Secrets instead.
#   --timeout DURATION  how long to wait for the rollout (default 10m)
#   --mock              dev only: answer every model with the bundled mock provider (no
#                       provider keys, no spend)
#   --create-secrets    dev only: create the Secrets values/dev.yaml names that are missing
#                       (Redis, metrics, bootstrap keys, LLM_KEY_* from your environment),
#                       not Postgres's. smoke.sh then finds the keys by itself.
#
# Needs helm 3.8+ (or 4) and kubectl (or HELM / KUBECTL), and the release's Secrets.
set -euo pipefail
# shellcheck source=_lib.sh
. "$(cd "$(dirname "$0")" && pwd)/_lib.sh"

cloud_parse_args "ENV --image --namespace --release --context --values --timeout --mock --create-secrets" "$@"
cloud_deploy install
