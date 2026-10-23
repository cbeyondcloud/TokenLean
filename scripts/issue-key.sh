#!/usr/bin/env bash
# =============================================================================
# issue-key.sh — Issue, revoke, rotate and list proxy API keys
# =============================================================================
# Usage:
#   ./scripts/issue-key.sh issue  --tenant <id> [--tier free|enterprise] [--admin] [--gateway]
#                                 [--team NAME] [--project ID]
#   ./scripts/issue-key.sh revoke --tenant <id>  [--project ID]
#   ./scripts/issue-key.sh list                  [--project ID]
#   (issue/revoke also take --backend blob and --region R — see "Key store" below)
# Through the proxy's admin API (/admin/keys), whatever key store the proxy uses:
#   ./scripts/issue-key.sh issue  --api URL --tenant <id> [--team NAME] [--bind-user USER]
#                                 [--tier free|enterprise] [--label TEXT] [--admin] [--gateway]
#   ./scripts/issue-key.sh list   --api URL [--tenant <id>] [--team NAME] [--bind-user USER]
#   ./scripts/issue-key.sh revoke --api URL --key-id ID
#   ./scripts/issue-key.sh rotate --api URL --key-id ID
#
#   --admin    key may impersonate another tenant via X-Tenant-ID (operator/benchmark).
#   --gateway  key is a customer API gateway: its X-Team header is trusted (selects the
#              rate-limit bucket + team metric label). Every other key's X-Team is ignored.
#   --team     bind the key to a team of the tenant (1-64 of A-Z a-z 0-9 . _ -, not
#              "default"): its requests carry that team (usage_events.team, team spend cap,
#              team config overlay), whatever X-Team says.
#
# Admin API mode (--api URL, or TOKENLEAN_ADMIN_URL): the script calls the proxy's
# /admin/keys instead of writing Secret Manager, so it works with any key store
# (key_store.backend: blob | postgres) and the proxy audits every change. The admin proxy
# key is read from TOKENLEAN_ADMIN_KEY only (never a flag: a command line is visible to
# other users of the host) and handed to curl in a private header file. A team-bound admin
# key manages only its own team's keys. Keys are named by their key id (list shows it); the
# raw key is printed once, on issue and rotate. gcloud is not used in this mode.
#   --bind-user  bind the key to a user (1-128 of A-Z a-z 0-9 . _ @ + -): its requests are
#                that user's, whatever X-User-ID says. (--user stays an alias of --tenant.)
#   --label      a note that list shows (1-128 printable characters).
#   --key-id     the key to revoke or rotate (16-64 lowercase hex characters).
#   --insecure   allow a plain-http URL to another host (https, or http to this machine,
#                otherwise). Set the key without echoing it or keeping it in shell history:
#                  read -rs TOKENLEAN_ADMIN_KEY && export TOKENLEAN_ADMIN_KEY
#
# Key store: without --api this script writes ONLY the Secret Manager blob, and issue/revoke
# then need an explicit --backend blob: a proxy whose config sets key_store.backend: postgres
# reads Postgres, which this script cannot detect (a blob write there would change nothing). issue/revoke first read the
# deployed Cloud Run service (PROXY_SERVICE_NAME, default token-proxy; --region or
# GCP_REGION, default asia-south1) and refuse when it validates keys against Postgres
# (PROXY_KEYS_BACKEND=postgres, the commercial deploy) — use the admin console there.
# If the service cannot be read, pass --backend blob to confirm the blob store.
#
# The proxy key secret in Secret Manager is a JSON object mapping the SHA-256 of
# each raw key to its tenant metadata (new format):
#   { "<sha256_of_key>": {"tenant_id": "...", "tier": "...", "admin"?: true, "gateway"?: true,
#                          "team"?: "..."}, ... }
# Legacy string entries ({ "<hash>": "<user_id>" }) are still accepted by the
# proxy but resolve to the "default" tenant — always issue new-format keys.
#
# Developers receive ONLY the raw key — never the LLM provider keys.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

COMMAND=""
USER_ID=""
TENANT_ID=""
TIER="free"
ADMIN="false"
GATEWAY="false"
TEAM=""
BIND_USER=""
LABEL=""
KEY_ID=""
API_URL=""
INSECURE="false"
PROJECT_ID=""
SECRET_NAME="${PROXY_KEYS_SECRET_NAME:-token-proxy-api-keys}"
BACKEND=""
REGION_ARG=""

# ─── Load .env if present ─────────────────────────────────────────────────────
ENV_FILE="${REPO_ROOT}/.env"
if [[ -f "$ENV_FILE" ]]; then
  set -a; source "$ENV_FILE"; set +a
fi

# ─── Argument parsing ─────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    issue|revoke|list|rotate) COMMAND="$1"; shift ;;
    --user)    USER_ID="$2";    shift 2 ;;
    --tenant)  TENANT_ID="$2";  shift 2 ;;
    --tier)    TIER="$2";       shift 2 ;;
    --admin)   ADMIN="true";    shift ;;
    --gateway) GATEWAY="true";  shift ;;
    --team)    TEAM="$2";       shift 2 ;;
    --bind-user) BIND_USER="$2"; shift 2 ;;
    --label)   LABEL="$2";      shift 2 ;;
    --key-id)  KEY_ID="$2";     shift 2 ;;
    --api)     API_URL="$2";    shift 2 ;;
    --insecure) INSECURE="true"; shift ;;
    --project) PROJECT_ID="$2"; shift 2 ;;
    --secret)  SECRET_NAME="$2"; shift 2 ;;
    --backend) BACKEND="$2";    shift 2 ;;
    --region)  REGION_ARG="$2"; shift 2 ;;
    --help)
      sed -n '/^# Usage:/,/^# ===/p' "$0" | head -16
      exit 0 ;;
    *) error "Unknown option: $1" ;;
  esac
done

[[ -z "$COMMAND" ]] && error "Command required: issue | revoke | rotate | list"
API_URL="${API_URL:-${TOKENLEAN_ADMIN_URL:-}}"

# ─── Admin API mode (--api URL) ───────────────────────────────────────────────
die_api() { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

API_CODE=""
API_BODY=""
NL=$'\n'
HDR_FILE=""
# The header file holding the admin key is removed however the script ends.
trap 'rm -f "${HDR_FILE:-}"' EXIT
trap 'rm -f "${HDR_FILE:-}"; exit 130' INT
trap 'rm -f "${HDR_FILE:-}"; exit 143' TERM
api_request() {
  # api_request METHOD PATH [JSON]: sets API_CODE and API_BODY. The admin key goes to curl
  # in a mode-600 header file, removed at once, so it never appears on a command line.
  local method="$1" path="$2" body="${3:-}" hdr out
  hdr="$(mktemp)"
  HDR_FILE="$hdr"
  chmod 600 "$hdr"
  printf 'Authorization: Bearer %s\n' "$TOKENLEAN_ADMIN_KEY" > "$hdr"
  local args=(-sS -X "$method" -H "@${hdr}" -H "Content-Type: application/json"
              -w "${NL}%{http_code}")
  if [[ -n "$body" ]]; then
    args+=(--data-binary "$body")
  fi
  if ! out=$(curl "${args[@]}" "${API_URL%/}${path}"); then
    rm -f "$hdr"
    die_api "Could not reach the admin API at ${API_URL}"
  fi
  rm -f "$hdr"
  API_CODE="${out##*"$NL"}"
  API_BODY="${out%"$NL"*}"
}

api_ok_or_die() {
  if [[ ! "$API_CODE" =~ ^2 ]]; then
    local detail
    detail=$(printf '%s' "$API_BODY" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin).get('detail')
except Exception:
    d = None
print(json.dumps(d) if isinstance(d, (dict, list)) else (d or ''))" 2>/dev/null || true)
    die_api "The admin API answered HTTP ${API_CODE}: ${detail:-$API_BODY}"
  fi
}

api_print_key() {
  # The raw key, exactly once, with its id and binding.
  printf '%s' "$API_BODY" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print('  Key id:  ' + str(d.get('key_id')))
if d.get('replaced_key_id'):
    print('  Replaces key id ' + str(d['replaced_key_id']) + ', now revoked')
print('  Binding: ' + ', '.join(f'{k}={d[k]}' for k in ('tenant_id', 'team', 'user', 'tier')
                                 if d.get(k)))
print('  Key:     ' + str(d.get('key')))
print('  Usage:   Authorization: Bearer ' + str(d.get('key')))"
  warn "Store this key securely — it cannot be retrieved again."
}

api_issue() {
  local tenant="${TENANT_ID:-$USER_ID}" body
  [[ -z "$tenant" ]] && die_api "--tenant is required for issue"
  case "$TIER" in
    free|enterprise) ;;
    *) die_api "--tier must be free|enterprise (got '${TIER}')" ;;
  esac
  if [[ -n "$TEAM" ]] && [[ "$TEAM" == "default" || ! "$TEAM" =~ ^[A-Za-z0-9._-]{1,64}$ ]]; then
    die_api "--team must be 1-64 characters of A-Z a-z 0-9 . _ - and not 'default'"
  fi
  if [[ -n "$BIND_USER" && ! "$BIND_USER" =~ ^[A-Za-z0-9._@+-]{1,128}$ ]]; then
    die_api "--bind-user must be 1-128 characters of A-Z a-z 0-9 . _ @ + -"
  fi
  body=$(TENANT="$tenant" TIER="$TIER" TEAM="$TEAM" BIND_USER="$BIND_USER" LABEL="$LABEL" \
         ADMIN="$ADMIN" GATEWAY="$GATEWAY" python3 -c "
import json, os
b = {'tenant_id': os.environ['TENANT'], 'tier': os.environ['TIER']}
for field, var in (('team', 'TEAM'), ('user', 'BIND_USER'), ('label', 'LABEL')):
    if os.environ.get(var):
        b[field] = os.environ[var]
for flag in ('admin', 'gateway'):
    if os.environ.get(flag.upper()) == 'true':
        b[flag] = True
print(json.dumps(b))")
  api_request POST /admin/keys "$body"
  api_ok_or_die
  success "Key issued through ${API_URL}"
  api_print_key
}

api_list() {
  local query
  query=$(TENANT="${TENANT_ID:-$USER_ID}" TEAM="$TEAM" BIND_USER="$BIND_USER" python3 -c "
import os, urllib.parse
q = {k: os.environ[v] for k, v in (('tenant_id', 'TENANT'), ('team', 'TEAM'),
                                    ('user', 'BIND_USER')) if os.environ.get(v)}
print(('?' + urllib.parse.urlencode(q, safe='@')) if q else '')")
  api_request GET "/admin/keys${query}"
  api_ok_or_die
  printf '%s' "$API_BODY" | python3 -c "
import json, sys
keys = json.load(sys.stdin).get('keys') or []
if not keys:
    print('  (no keys in your scope)')
for k in keys:
    flags = ' '.join(f for f in ('admin', 'gateway', 'suspended') if k.get(f))
    cols = [k.get('key_id')] + [f'{f}={k.get(f) or \"-\"}' for f in
                                ('tenant_id', 'team', 'user', 'tier', 'label', 'created_at')]
    print('  ' + '  '.join(str(c) for c in cols) + (('  ' + flags) if flags else ''))
print(f'Total: {len(keys)} key(s)')"
}

api_one_key() {
  if [[ -z "$KEY_ID" ]]; then
    die_api "--key-id is required for ${COMMAND} in admin API mode (list shows each key's id)"
  fi
  [[ "$KEY_ID" =~ ^[0-9a-f]{16,64}$ ]] || die_api "--key-id must be 16-64 lowercase hex characters"
  if [[ "$COMMAND" == "revoke" ]]; then
    api_request DELETE "/admin/keys/${KEY_ID}"
    api_ok_or_die
    success "Revoked key ${KEY_ID}"
  else
    api_request POST "/admin/keys/${KEY_ID}/rotate"
    api_ok_or_die
    success "Rotated key ${KEY_ID}"
    api_print_key
  fi
}

api_url_is_safe() {
  # https anywhere; plain http only to this machine (the admin key would cross the network
  # in clear text), unless --insecure says so.
  case "$API_URL" in
    https://*) return 0 ;;
    http://localhost|http://localhost[:/]*|http://127.*|http://\[::1\]*) return 0 ;;
  esac
  return 1
}

if [[ -n "$API_URL" ]]; then
  # Checked before anything is sent; the key itself is never echoed.
  if ! api_url_is_safe; then
    if [[ "$INSECURE" != "true" ]]; then
      die_api "${API_URL} is not https: the admin key would cross the network in clear text. \
Use https, or pass --insecure if this link is private."
    fi
    warn "--insecure: sending the admin key over plain http to ${API_URL}"
  fi
  case "$COMMAND" in
    issue)         [[ -n "${TENANT_ID:-$USER_ID}" ]] || die_api "--tenant is required for issue" ;;
    revoke|rotate) [[ -n "$KEY_ID" ]] || die_api "--key-id is required for ${COMMAND} in admin \
API mode (list shows each key's id)" ;;
  esac
  [[ -n "${TOKENLEAN_ADMIN_KEY:-}" ]] || die_api "TOKENLEAN_ADMIN_KEY must hold an admin proxy \
key (it is never taken on the command line)."
  case "$COMMAND" in
    issue)         api_issue ;;
    list)          api_list ;;
    revoke|rotate) api_one_key ;;
  esac
  exit 0
fi
if [[ "$COMMAND" == "rotate" ]]; then
  error "rotate works through the admin API only: pass --api URL (or set TOKENLEAN_ADMIN_URL) \
and TOKENLEAN_ADMIN_KEY"
fi
case "$BACKEND" in
  ""|blob|postgres) ;;
  *) error "--backend must be blob|postgres (got '${BACKEND}')" ;;
esac
SERVICE_NAME="${PROXY_SERVICE_NAME:-token-proxy}"
REGION="${REGION_ARG:-${GCP_REGION:-asia-south1}}"

# ─── Resolve project ──────────────────────────────────────────────────────────
if [[ -z "$PROJECT_ID" ]]; then
  PROJECT_ID="${GCP_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
fi
[[ -z "$PROJECT_ID" ]] && error "No GCP project set. Use --project or: gcloud config set project PROJECT_ID"
[[ "$PROJECT_ID" == "your-gcp-project-id" ]] && \
  error "GCP_PROJECT_ID is still the template's placeholder: set your project id, or use --project"

# ─── Helpers ──────────────────────────────────────────────────────────────────
# Errors from functions run inside $(...) go to stderr: error() writes to stdout, which a
# command substitution would swallow into the variable instead of showing.
die() { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

fetch_keys_json() {
  # A secret with no version yet is an empty store (the first issue creates it). Any other
  # read failure stops the script: treating it as empty made the next write keep only the
  # new key, revoking every other tenant's.
  local out err_file err
  err_file="$(mktemp)"
  if out=$(gcloud secrets versions access latest \
             --secret="$SECRET_NAME" --project="$PROJECT_ID" 2>"$err_file"); then
    rm -f "$err_file"
    printf '%s' "$out"
    return 0
  fi
  if grep -q "NOT_FOUND" "$err_file"; then
    rm -f "$err_file"
    echo "{}"
    return 0
  fi
  err="$(tr '\n' ' ' < "$err_file")"
  rm -f "$err_file"
  die "Could not read secret '${SECRET_NAME}': ${err}— stopping so no key is lost."
}

detect_backend() {
  # The proxy validates keys against the store PROXY_KEYS_BACKEND names on the deployed
  # service; unset means the Secret Manager blob.
  local svc_json
  svc_json=$(gcloud run services describe "$SERVICE_NAME" --project="$PROJECT_ID" \
               --region="$REGION" --format=json 2>/dev/null) || return 1
  echo "$svc_json" | python3 -c "
import json, sys
svc = json.load(sys.stdin)
containers = svc.get('spec', {}).get('template', {}).get('spec', {}).get('containers') or [{}]
env = containers[0].get('env') or []
value = next((e.get('value', '') for e in env if e.get('name') == 'PROXY_KEYS_BACKEND'), '')
print((value or 'blob').strip().lower())
"
}

require_blob_backend() {
  # This script writes only the Secret Manager blob. A proxy whose config sets
  # key_store.backend: postgres reads the proxy_keys table instead, and nothing here can see
  # that setting: so a blob write needs an explicit --backend blob.
  if [[ -z "$BACKEND" ]]; then
    die "This script writes only the Secret Manager blob and cannot see whether the proxy \
validates keys against a config-selected Postgres store (key_store.backend: postgres), where a \
blob write would change nothing the proxy reads. Use the admin API (--api URL, or set \
TOKENLEAN_ADMIN_URL), or re-run with --backend blob to confirm the proxy uses the blob store."
  fi
  if [[ "$BACKEND" == "postgres" ]]; then
    die "--backend postgres: this script cannot write the Postgres key store. Use the admin API \
(--api URL) instead."
  fi
  warn "--backend blob: writing the Secret Manager blob. If the proxy's config sets \
key_store.backend: postgres, this changes nothing it reads; use --api URL there."
  local detected
  if detected=$(detect_backend) && [[ "$detected" == "postgres" ]]; then
    die "'${SERVICE_NAME}' validates keys against Postgres (PROXY_KEYS_BACKEND=postgres). This \
script only writes the Secret Manager blob, so it would change nothing the proxy reads. Issue and \
revoke keys through the admin API (--api URL) or the admin console instead."
  fi
}

store_keys_json() {
  local json="$1"
  if gcloud secrets describe "$SECRET_NAME" --project="$PROJECT_ID" &>/dev/null; then
    echo -n "$json" | gcloud secrets versions add "$SECRET_NAME" \
      --data-file=- --project="$PROJECT_ID" &>/dev/null
  else
    echo -n "$json" | gcloud secrets create "$SECRET_NAME" \
      --project="$PROJECT_ID" --replication-policy=automatic --data-file=- &>/dev/null
  fi
}

# ─── Commands ─────────────────────────────────────────────────────────────────
cmd_issue() {
  # --tenant is preferred; --user is accepted as an alias for the tenant id so
  # existing callers (e.g. gcp-deploy.sh `issue --user admin`) keep working.
  local tenant="${TENANT_ID:-$USER_ID}"
  [[ -z "$tenant" ]] && error "--tenant (or --user) is required for issue"

  # Pricing tier is bound to the key and flows into usage_events.pricing_tier →
  # invoicing. Two tiers only — free (self-host / $0 floor) or enterprise (managed SaaS).
  # Reject typos here so a bad tier never silently bills at the wrong card.
  case "$TIER" in
    free|enterprise) ;;
    *) error "--tier must be free|enterprise (got '${TIER}')" ;;
  esac
  # Same rule as the proxy's create_key (tenancy.teams.valid_team_name).
  if [[ -n "$TEAM" ]]; then
    if [[ "$TEAM" == "default" || ! "$TEAM" =~ ^[A-Za-z0-9._-]{1,64}$ ]]; then
      error "--team must be 1-64 characters of A-Z a-z 0-9 . _ - and not 'default' (got '${TEAM}')"
    fi
  fi
  require_blob_backend

  local raw_key
  raw_key="tok-$(openssl rand -hex 24)"
  local key_hash
  key_hash=$(echo -n "$raw_key" | sha256sum | awk '{print $1}')

  local existing_json
  existing_json=$(fetch_keys_json)

  # Warn if this tenant already has a key (new-format dict or legacy string).
  if echo "$existing_json" | TENANT="$tenant" python3 -c "
import os, sys, json
d = json.load(sys.stdin)
t = os.environ['TENANT']
def owner(v): return v.get('tenant_id') if isinstance(v, dict) else v
print(any(owner(v) == t for v in d.values()))" 2>/dev/null | grep -q "True"; then
    warn "Tenant '${tenant}' already has a key. Issuing a new one (old key remains valid until revoked)."
  fi

  # Add new key hash → {tenant_id, tier, admin?, gateway?, team?} mapping (new format).
  local updated_json
  updated_json=$(echo "$existing_json" | KEY_HASH="$key_hash" TENANT="$tenant" TIER="$TIER" ADMIN="$ADMIN" GATEWAY="$GATEWAY" TEAM="$TEAM" python3 -c "
import os, sys, json, datetime
d = json.load(sys.stdin)
meta = {
    'tenant_id': os.environ['TENANT'],
    'tier': os.environ['TIER'],
    'created': datetime.datetime.utcnow().isoformat() + 'Z',
}
if os.environ.get('ADMIN') == 'true':
    meta['admin'] = True
# A gateway key may set X-Team (a customer's API gateway that stamps the team per
# request); every other key's X-Team is ignored. Trust flag — issuance only.
if os.environ.get('GATEWAY') == 'true':
    meta['gateway'] = True
# The key's team binding: it outranks any X-Team the caller sends.
if os.environ.get('TEAM'):
    meta['team'] = os.environ['TEAM']
d[os.environ['KEY_HASH']] = meta
print(json.dumps(d))
")

  store_keys_json "$updated_json"
  success "Key issued for tenant: ${tenant} (tier=${TIER}, admin=${ADMIN}, gateway=${GATEWAY}${TEAM:+, team=${TEAM}})"
  echo ""
  echo -e "${GREEN}╔══════════════════════════════════════════════════════╗${NC}"
  echo -e "${GREEN}║  Proxy API Key — share this with the developer       ║${NC}"
  echo -e "${GREEN}╠══════════════════════════════════════════════════════╣${NC}"
  echo -e "${GREEN}║${NC}  Tenant:  ${tenant}  (tier=${TIER}, admin=${ADMIN})"
  echo -e "${GREEN}║${NC}  Key:     ${raw_key}"
  echo -e "${GREEN}║${NC}"
  echo -e "${GREEN}║${NC}  Usage:"
  echo -e "${GREEN}║${NC}    Authorization: Bearer ${raw_key}"
  echo -e "${GREEN}╚══════════════════════════════════════════════════════╝${NC}"
  warn "Store this key securely — it cannot be retrieved again."
}

cmd_revoke() {
  local tenant="${TENANT_ID:-$USER_ID}"
  [[ -z "$tenant" ]] && error "--tenant (or --user) is required for revoke"
  require_blob_backend

  local existing_json
  existing_json=$(fetch_keys_json)

  # The count comes back on stderr, through a file only this run can write (a fixed
  # /tmp/revoke_count could be planted by another user of the host).
  local updated_json removed count_file
  count_file="$(mktemp)"
  updated_json=$(echo "$existing_json" | TENANT="$tenant" python3 -c "
import os, sys, json
d = json.load(sys.stdin)
t = os.environ['TENANT']
def owner(v): return v.get('tenant_id') if isinstance(v, dict) else v
before = len(d)
d = {k: v for k, v in d.items() if owner(v) != t}
print(json.dumps(d))
print(before - len(d), file=sys.stderr)
" 2>"$count_file")
  removed=$(cat "$count_file")
  rm -f "$count_file"

  if [[ "$removed" -eq 0 ]]; then
    warn "No keys found for tenant '${tenant}' — nothing to revoke."
    exit 0
  fi

  store_keys_json "$updated_json"
  success "Revoked ${removed} key(s) for tenant: ${tenant}"
}

cmd_list() {
  local existing_json
  existing_json=$(fetch_keys_json)

  echo -e "${BLUE}Current proxy key holders (tenants):${NC}"
  echo "$existing_json" | python3 -c "
import sys, json
from collections import Counter
d = json.load(sys.stdin)
if not d:
    print('  (no keys issued)')
else:
    def label(v):
        if isinstance(v, dict):
            t = v.get('tenant_id', '?'); admin = ' [admin]' if v.get('admin') else ''
            return f\"{t} (tier={v.get('tier','?')}){admin}\"
        return f'{v} (legacy)'
    counts = Counter(label(v) for v in d.values())
    for lbl, count in sorted(counts.items()):
        print(f'  {lbl}  ({count} key(s))')
    tenants = {(v.get('tenant_id') if isinstance(v, dict) else v) for v in d.values()}
    print(f'\nTotal: {len(d)} key hash(es) across {len(tenants)} tenant(s)')
"
}

# ─── Dispatch ─────────────────────────────────────────────────────────────────
case "$COMMAND" in
  issue)  cmd_issue  ;;
  revoke) cmd_revoke ;;
  list)   cmd_list   ;;
esac
