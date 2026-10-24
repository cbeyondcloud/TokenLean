#!/usr/bin/env bash
# Stop the TokenLean LITE stack (every optional service too). Data volumes are kept.
#
#   ./down.sh [--purge]
#
#   --purge  also delete the volumes (Postgres, Redis, Qdrant, Prometheus, Grafana data;
#            the Postgres data holds the proxy keys issued through /admin/keys). .env.lite is kept, so
#            the next ./up.sh reuses the same keys and passwords on fresh databases.
#
# Engine: CONTAINER_ENGINE=docker (default) or podman.
set -euo pipefail
# shellcheck source=_lib.sh
. "$(cd "$(dirname "$0")" && pwd)/_lib.sh"

PURGE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --purge) PURGE=1 ;;
    -h | --help)
      lite_usage "$0"
      exit 0
      ;;
    *) lite_die "unknown option: $1 (see --help)" ;;
  esac
  shift
done

lite_check_engine
if [ -f "$LITE_ENV_FILE" ]; then
  lite_load_env
else
  # compose checks these even to stop; any value will do when there is no .env.lite
  export DB_PASSWORD=unused REDIS_PASSWORD=unused
  LITE_ENV_FILE="$LITE_DIR/.env.lite.template"
fi
# An .env.lite from before the grafana profile has no Grafana password; stopping needs none.
export GRAFANA_ADMIN_PASSWORD="${GRAFANA_ADMIN_PASSWORD:-unused}"

ARGS=(down --remove-orphans)
if [ "$PURGE" = 1 ]; then ARGS+=(--volumes); fi
lite_compose "${LITE_ALL_PROFILES[@]}" "${ARGS[@]}"
echo "lite: stopped$([ "$PURGE" = 1 ] && echo ' and volumes removed')"
