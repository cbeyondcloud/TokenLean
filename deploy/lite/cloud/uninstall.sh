#!/usr/bin/env bash
# Uninstall a TokenLean LITE release. The namespace, the Secrets and your Postgres data stay
# (usage rows, audit rows, proxy keys), so a later install picks up where this one stopped.
#
#   ./uninstall.sh [--namespace NS] [--release NAME] [--context NAME] [--purge]
#
#   --namespace NS   default tokenlean-lite (or TOKENLEAN_LITE_NAMESPACE)
#   --release NAME   default tokenlean-lite (or TOKENLEAN_LITE_RELEASE)
#   --context NAME   the kubeconfig context to use (default: the current one)
#   --purge          also delete the Secrets install --create-secrets made, the bundled
#                    Redis's volume and the keys file smoke reads. Postgres is never touched.
set -euo pipefail
# shellcheck source=_lib.sh
. "$(cd "$(dirname "$0")" && pwd)/_lib.sh"

cloud_parse_args "--namespace --release --context --purge" "$@"
cloud_check_tools
echo "tokenlean-lite: uninstall $CLOUD_RELEASE from namespace $CLOUD_NAMESPACE, context $(cloud_context_name)"
cloud_helm uninstall "$CLOUD_RELEASE" --namespace "$CLOUD_NAMESPACE" --wait \
  || cloud_die "helm uninstall failed"
if [ "$CLOUD_PURGE" = 1 ]; then
  cloud_kubectl delete secret -l "$CLOUD_CREATED_BY,app.kubernetes.io/instance=$CLOUD_RELEASE" \
    --ignore-not-found || true
  cloud_kubectl delete pvc -l "app.kubernetes.io/instance=$CLOUD_RELEASE" --ignore-not-found || true
  rm -f "$(cloud_state_file)"
  echo "tokenlean-lite: purged the release's generated Secrets, volumes and keys file"
fi
echo "tokenlean-lite: $CLOUD_RELEASE uninstalled"
