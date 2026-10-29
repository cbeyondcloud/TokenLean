#!/usr/bin/env bash
# Upgrade an installed TokenLean LITE release: a new proxy image, changed values, or a newer
# chart. The migration hook runs first; the proxy rolls out one pod at a time.
#
#   ./upgrade.sh <env> --image <registry>/tokenlean-proxy:<release> [options]
#
#   <env>               dev | staging | prod | managed: the values file values/<env>.yaml
#   --image REF         the proxy image, an exact release (or TOKENLEAN_IMAGE)
#   --namespace NS      default tokenlean-lite (or TOKENLEAN_LITE_NAMESPACE)
#   --release NAME      default tokenlean-lite (or TOKENLEAN_LITE_RELEASE)
#   --context NAME      the kubeconfig context to use (default: the current one)
#   --values FILE       your own values over values/<env>.yaml (repeatable)
#   --timeout DURATION  how long to wait for the rollout (default 10m)
#   --mock              dev only: keep answering every model with the mock provider
#
# Stops when the release is not installed (use install.sh). A failed upgrade leaves the
# previous revision serving; `helm rollback <release> -n <namespace>` returns to it fully.
set -euo pipefail
# shellcheck source=_lib.sh
. "$(cd "$(dirname "$0")" && pwd)/_lib.sh"

cloud_parse_args "ENV --image --namespace --release --context --values --timeout --mock" "$@"
cloud_deploy upgrade
