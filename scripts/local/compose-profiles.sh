#!/usr/bin/env bash
# Sourced (never run) by the local deploys — deploy-local.sh, start-local.sh, build-local.sh, the
# precheck and the post-deploy check; the commercial local deploy and the pitch harness too — to
# choose docker-compose.yml's optional profiles:
#   observability   Prometheus, Grafana, Jaeger
#   tracing         Langfuse, which runs only when the proxy sends it traces:
#                   groups.G18_observability.langfuse_enabled, off in the OSS template
#
# Profiles are merged into COMPOSE_PROFILES, never overwritten: a value the caller or .env set is
# kept, a name is listed once, `*` (every profile) is never glob-expanded, and an empty value is
# never exported (an exported empty COMPOSE_PROFILES would hide the one in .env).

_compose_warn() {
  if declare -F warn >/dev/null 2>&1; then warn "$*"; else echo "[WARN]  $*" >&2; fi
}

# compose_profiles_add PROFILE... — merge PROFILEs into COMPOSE_PROFILES and export it.
compose_profiles_add() {
  local -a have=() raw=()
  local p q dup
  IFS=, read -r -a raw <<< "${COMPOSE_PROFILES:-}"
  for p in ${raw[@]+"${raw[@]}"} "$@"; do
    p="${p//[[:space:]]/}"
    [[ -n "$p" ]] || continue
    dup=false
    for q in ${have[@]+"${have[@]}"}; do
      [[ "$q" == "$p" ]] && { dup=true; break; }
    done
    [[ "$dup" == true ]] || have+=("$p")
  done
  (( ${#have[@]} )) || return 0
  local IFS=,
  export COMPOSE_PROFILES="${have[*]}"
}

# compose_profiles_has PROFILE — true when COMPOSE_PROFILES lists PROFILE, or `*`.
compose_profiles_has() {
  local -a raw=()
  local p
  IFS=, read -r -a raw <<< "${COMPOSE_PROFILES:-}"
  for p in ${raw[@]+"${raw[@]}"}; do
    p="${p//[[:space:]]/}"
    [[ "$p" == "$1" || "$p" == "*" ]] && return 0
  done
  return 1
}

# Prints true, false or unknown: whether the config turns Langfuse tracing on, read as the proxy
# reads it (config.yaml, then params_dir's *.yaml/*.yml in name order, `${VAR}` placeholders
# expanded, then each tenant's operator overlay). Only the answer is printed, never a value.
_compose_read_tracing() {   # $1 = interpreter, $2 = repo root
  "$1" - "$2" 2>/dev/null <<'PY'
import os, re, sys
try:
    import yaml
except Exception:
    print("unknown"); sys.exit(0)
root = sys.argv[1]
placeholder = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

def load(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

def expand(value):
    if not isinstance(value, str):
        return value
    return placeholder.sub(lambda m: os.environ.get(
        m.group(1), m.group(2) if m.group(2) is not None else ""), value)

try:
    cfg = load(os.path.join(root, "config", "config.yaml"))
    if not isinstance(cfg, dict):
        raise ValueError("the config is not a mapping")
except Exception:
    print("unknown"); sys.exit(0)
params = cfg.get("params_dir") or ""
if params and not os.path.isabs(params):
    params = os.path.join(root, params)
if params and os.path.isdir(params):
    for name in sorted(os.listdir(params)):
        if not name.endswith((".yaml", ".yml")):
            continue
        try:
            data = load(os.path.join(params, name))
            items = list(data.items())
        except Exception:
            continue                              # the proxy skips a file it cannot load
        for key, value in items:
            if key == "groups" and isinstance(value, dict):
                if not isinstance(cfg.get("groups"), dict):
                    cfg["groups"] = {}
                for group, settings in value.items():
                    if settings is None:
                        continue
                    if isinstance(settings, dict) and isinstance(cfg["groups"].get(group), dict):
                        cfg["groups"][group].update(settings)
                    else:
                        cfg["groups"][group] = settings
            elif isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value

def tracing_on(groups):
    group = groups.get("G18_observability") if isinstance(groups, dict) else None
    return isinstance(group, dict) and bool(expand(group.get("langfuse_enabled", False)))

tenants = cfg.get("tenants") if isinstance(cfg.get("tenants"), dict) else {}
wanted = tracing_on(cfg.get("groups")) or any(
    tracing_on(t.get("groups")) for t in tenants.values() if isinstance(t, dict))
print("true" if wanted else "false")
PY
}

# compose_tracing_wanted ROOT — true when ROOT/config/config.yaml turns Langfuse tracing on (in
# the base config, a params file, or any tenant's overlay). No config: false. A config this
# cannot read (no Python with PyYAML, YAML that will not parse): false, with a warning naming
# --with-tracing. A config the proxy loads from GCS is not seen here.
compose_tracing_wanted() {
  local root="${1:?compose_tracing_wanted needs the repo root}" py answer
  [[ -f "$root/config/config.yaml" ]] || return 1
  for py in python3 python; do
    command -v "$py" >/dev/null 2>&1 || continue
    answer="$(_compose_read_tracing "$py" "$root")" || answer=""
    answer="${answer//$'\r'/}"
    case "$answer" in
      true)  return 0 ;;
      false) return 1 ;;
    esac
  done
  _compose_warn "Could not read config/config.yaml to tell whether Langfuse tracing is on (this needs Python with PyYAML), so Langfuse is not started. Pass --with-tracing to start it."
  return 1
}

# compose_profiles_apply ROOT WITH_GRAFANA WITH_TRACING — what the local deploys start:
# `observability` with --with-grafana; `tracing` with --with-tracing or when the config turns
# tracing on.
compose_profiles_apply() {
  local root="${1:?compose_profiles_apply needs the repo root}" grafana="${2:-false}" tracing="${3:-false}"
  if [[ "$grafana" == true ]]; then
    compose_profiles_add observability
  fi
  if [[ "$tracing" == true ]] || compose_tracing_wanted "$root"; then
    compose_profiles_add tracing
  fi
  if compose_profiles_has tracing && [[ -z "${LANGFUSE_PUBLIC_KEY:-}" || -z "${LANGFUSE_SECRET_KEY:-}" ]]; then
    _compose_warn "Langfuse will run, but the proxy sends it nothing until LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are set in .env."
  fi
  return 0
}

# compose_note_idle_langfuse — after `up`: a stack started before Langfuse became the `tracing`
# profile keeps its container running until it is stopped. Say so when tracing is off.
compose_note_idle_langfuse() {
  compose_profiles_has tracing && return 0
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx 'token-opt-langfuse'; then
    _compose_warn "token-opt-langfuse is still running although tracing is off (it started before Langfuse became the 'tracing' profile). Stop it with: docker stop token-opt-langfuse (its data stays in Postgres)."
  fi
  return 0
}
