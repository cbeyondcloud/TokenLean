#!/usr/bin/env bash
# Smoke-test a running TokenLean LITE stack. Exit 0 when every check passes.
#
#   ./smoke.sh            # model: SMOKE_MODEL, else the mock (after up --mock), else a model
#                         # for the first provider key set in .env.lite
#
# Checks: /health; an OpenAI chat completion (x-tokenlean-request-id + x-tokenlean-cost-usd,
# and no x-tokenlean-model-substituted: the model asked for is the model that answered); the
# same streamed (the request-id header, and the final `: x-tokenlean-cost` frame, cost to 6
# decimals, before [DONE]); an Anthropic /v1/messages call (x-api-key auth, x-tokenlean-*
# headers); a usage row for each of the three calls, through /admin/usage-export; then a
# team-bound key issued through /admin/keys: served, its usage row carries its team, its
# team's spend cap (set through /admin/routing, then rolled back; skipped when an overlay is
# already set) answers 429, and once revoked it gets 401.
# Live mode makes about six small real calls (a fraction of a cent). Engine-independent: HTTP only.
set -euo pipefail
umask 077   # the header files below hold the proxy key
# shellcheck source=_lib.sh
. "$(cd "$(dirname "$0")" && pwd)/_lib.sh"

case "${1:-}" in
  -h | --help)
    lite_usage "$0"
    exit 0
    ;;
esac

lite_load_env
[ -n "${TOKENLEAN_API_KEY:-}" ] || lite_die "TOKENLEAN_API_KEY is empty - run ./up.sh first"
command -v curl >/dev/null 2>&1 || lite_die "curl is required"

MODE=live
if [ -f "$LITE_STATE_DIR/stack.env" ]; then
  MODE=$(sed -n 's/^LITE_MODE=//p' "$LITE_STATE_DIR/stack.env" | tr -d '\r')
fi
MODEL=${SMOKE_MODEL:-}
if [ -z "$MODEL" ]; then
  if [ "$MODE" = mock ]; then MODEL=lite-mock-1
  elif [ -n "${LLM_KEY_OPENAI:-}" ]; then MODEL=gpt-4o-mini
  elif [ -n "${LLM_KEY_ANTHROPIC:-}" ]; then MODEL=claude-haiku-4-5
  elif [ -n "${LLM_KEY_GEMINI:-}${LLM_KEY_GOOGLE:-}" ]; then MODEL=gemini-2.5-flash
  else lite_die "no provider key in .env.lite: set one, set SMOKE_MODEL, or use ./up.sh --mock"
  fi
fi

BASE=$(lite_base_url)
TMP=$(mktemp -d 2>/dev/null || mktemp -d -t lite-smoke)
trap 'rm -rf "$TMP"' EXIT
# The key goes to curl in header files (curl -H @file), never as an argument: arguments are
# visible to every user of the machine.
printf 'Authorization: Bearer %s\n' "$TOKENLEAN_API_KEY" >"$TMP/auth-bearer"
printf 'x-api-key: %s\n' "$TOKENLEAN_API_KEY" >"$TMP/auth-x-api-key"
NONCE="$(date +%s)-$$"   # a fresh prompt per run, so the provider path runs (not the cache)
FAILS=0
IDS=""

pass() { echo "PASS  $1"; }
fail() {
  echo "FAIL  $1"
  FAILS=$((FAILS + 1))
}
# These never fail (a missing file or header yields ""), so `X=$(...)` cannot trip set -e:
# a missing value is reported as a FAIL line instead of ending the script.
status_of() { { head -n 1 "$1" 2>/dev/null || true; } | awk '{print $2}'; }
header() { { grep -i "^$2:" "$1" 2>/dev/null || true; } | head -n 1 | cut -d: -f2- | tr -d ' \r'; }
frame_field() {   # a field of the stream's `: x-tokenlean-cost {...}` frame
  { grep '^: x-tokenlean-cost ' "$1" 2>/dev/null || true; } | head -n 1 \
    | sed -n "s/.*\"$2\":\"\([^\"]*\)\".*/\1/p"
}
snippet() { head -c 300 "$1" 2>/dev/null || true; }

echo "lite smoke: $BASE  model=$MODEL  mode=$MODE"

# 1. health
if curl -fsS "$BASE/health" 2>/dev/null | grep -q '"status":"ok"'; then pass "health"; else fail "health"; fi

# 2. OpenAI chat completion
BODY="{\"model\":\"$MODEL\",\"max_tokens\":16,\"messages\":[{\"role\":\"user\",\"content\":\"Reply with the word pong. ($NONCE a)\"}]}"
curl -sS -D "$TMP/h1" -o "$TMP/b1" -H @"$TMP/auth-bearer" \
  -H "Content-Type: application/json" -d "$BODY" "$BASE/v1/chat/completions" || true
ID1=$(header "$TMP/h1" x-tokenlean-request-id)
COST1=$(header "$TMP/h1" x-tokenlean-cost-usd)
SUB1=$(header "$TMP/h1" x-tokenlean-model-substituted)
if [ "$(status_of "$TMP/h1")" = 200 ] && [ -n "$ID1" ] && [ -n "$COST1" ] && [ -z "$SUB1" ] \
  && grep -q '"choices"' "$TMP/b1" 2>/dev/null; then
  pass "chat completion (cost \$$COST1, model not substituted)"
  IDS="$IDS $ID1"
else
  fail "chat completion (status $(status_of "$TMP/h1"), request-id '${ID1}', cost '${COST1}', substituted '${SUB1}'): $(snippet "$TMP/b1")"
fi

# 3. OpenAI chat completion, streamed
BODY="{\"model\":\"$MODEL\",\"max_tokens\":16,\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"Reply with the word pong. ($NONCE b)\"}]}"
curl -sSN -D "$TMP/h2" -o "$TMP/b2" -H @"$TMP/auth-bearer" \
  -H "Content-Type: application/json" -d "$BODY" "$BASE/v1/chat/completions" || true
ID2=$(frame_field "$TMP/b2" request_id)
COST2=$(frame_field "$TMP/b2" cost_usd)
HID2=$(header "$TMP/h2" x-tokenlean-request-id)   # sent with the headers, before any chunk
# The cost frame comes right before [DONE], with the cost to 6 decimal places.
FRAME_LINE=$({ grep -n '^: x-tokenlean-cost ' "$TMP/b2" 2>/dev/null || true; } | head -n 1 | cut -d: -f1)
DONE_LINE=$({ grep -n '^data: \[DONE\]' "$TMP/b2" 2>/dev/null || true; } | head -n 1 | cut -d: -f1)
if [ "$(status_of "$TMP/h2")" = 200 ] && [ -n "$ID2" ] && [ "$HID2" = "$ID2" ] \
  && [ -n "$FRAME_LINE" ] && [ -n "$DONE_LINE" ] && [ "$FRAME_LINE" -lt "$DONE_LINE" ] \
  && printf '%s' "$COST2" | grep -Eq '^[0-9]+\.[0-9]{6}$'; then
  pass "chat completion stream (request-id header, final cost frame \$$COST2 before [DONE])"
  IDS="$IDS $ID2"
else
  fail "chat completion stream (status $(status_of "$TMP/h2"), header id '${HID2}', frame id '${ID2}', cost '${COST2}', frame line '${FRAME_LINE}', [DONE] line '${DONE_LINE}'): $(snippet "$TMP/b2")"
fi

# 4. Anthropic messages
BODY="{\"model\":\"$MODEL\",\"max_tokens\":16,\"messages\":[{\"role\":\"user\",\"content\":\"Reply with the word pong. ($NONCE c)\"}]}"
curl -sS -D "$TMP/h3" -o "$TMP/b3" -H @"$TMP/auth-x-api-key" \
  -H "anthropic-version: 2023-06-01" -H "Content-Type: application/json" -d "$BODY" \
  "$BASE/v1/messages" || true
ID3=$(header "$TMP/h3" x-tokenlean-request-id)
COST3=$(header "$TMP/h3" x-tokenlean-cost-usd)
SUB3=$(header "$TMP/h3" x-tokenlean-model-substituted)
if [ "$(status_of "$TMP/h3")" = 200 ] && [ -n "$ID3" ] && [ -n "$COST3" ] && [ -z "$SUB3" ] \
  && grep -q '"type":"message"' "$TMP/b3" 2>/dev/null; then
  pass "messages (cost \$$COST3, model not substituted)"
  IDS="$IDS $ID3"
else
  fail "messages (status $(status_of "$TMP/h3"), request-id '${ID3}', cost '${COST3}', substituted '${SUB3}'): $(snippet "$TMP/b3")"
fi

# 5. a usage row for each call (rows are written just after each response)
if [ -n "$IDS" ]; then
  FOUND=0
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    curl -sS -o "$TMP/b4" -X POST -H @"$TMP/auth-bearer" \
      -H "Content-Type: application/json" -d '{}' "$BASE/admin/usage-export" || true
    MISSING=""
    for id in $IDS; do grep -q "\"request_id\":\"$id\"" "$TMP/b4" || MISSING="$MISSING $id"; done
    if [ -z "$MISSING" ]; then FOUND=1; break; fi
    sleep 2
  done
  if [ "$FOUND" = 1 ]; then pass "usage rows ($(echo $IDS | wc -w | tr -d ' ') of 3 calls)"; else fail "usage rows missing for:$MISSING"; fi
else
  fail "usage rows (no call succeeded)"
fi

# 6. a team-bound key, issued through /admin/keys (keys live in Postgres): it is served, its
# usage row carries its team, its team's spend cap is enforced, and once revoked it is refused.
json_field() { sed -n "s/.*\"$2\":\"\([^\"]*\)\".*/\1/p" "$1" 2>/dev/null | head -n 1; }
TENANT=${LITE_TENANT_ID:-LITE-LOC-01}
TEAM="lite-smoke-$NONCE"
printf 'Authorization: Bearer %s\n' "$TOKENLEAN_ADMIN_KEY" >"$TMP/auth-admin"
curl -sS -D "$TMP/h5" -o "$TMP/b5" -X POST -H @"$TMP/auth-admin" -H "Content-Type: application/json" \
  -d "{\"tenant_id\":\"$TENANT\",\"team\":\"$TEAM\",\"label\":\"lite smoke\"}" "$BASE/admin/keys" || true
TEAM_KEY=$(json_field "$TMP/b5" key)
TEAM_KEY_ID=$(json_field "$TMP/b5" key_id)
if [ "$(status_of "$TMP/h5")" = 201 ] && [ -n "$TEAM_KEY" ] && [ -n "$TEAM_KEY_ID" ]; then
  pass "team key issued (team $TEAM, key_id $TEAM_KEY_ID)"
  printf 'Authorization: Bearer %s\n' "$TEAM_KEY" >"$TMP/auth-team"
  team_call() {   # $1 = suffix; leaves headers in $TMP/h6$1 and the body in $TMP/b6$1
    curl -sS -D "$TMP/h6$1" -o "$TMP/b6$1" -H @"$TMP/auth-team" -H "Content-Type: application/json" \
      -d "{\"model\":\"$MODEL\",\"max_tokens\":16,\"messages\":[{\"role\":\"user\",\"content\":\"Reply with the word pong. ($NONCE team $1)\"}]}" \
      "$BASE/v1/chat/completions" || true
  }
  # A key issued through one worker or replica reaches the others within
  # key_store.refresh_seconds (5 s): a 401 is retried for that long.
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
    team_call a
    [ "$(status_of "$TMP/h6a")" = 401 ] || break
    sleep 1
  done
  TID=$(header "$TMP/h6a" x-tokenlean-request-id)
  if [ "$(status_of "$TMP/h6a")" = 200 ] && [ -n "$TID" ]; then
    ROW=""
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      curl -sS -o "$TMP/b7" -X POST -H @"$TMP/auth-team" -H "Content-Type: application/json" \
        -d '{}' "$BASE/admin/usage-export" || true
      ROW=$({ grep "\"request_id\":\"$TID\"" "$TMP/b7" 2>/dev/null || true; } | head -n 1)
      if [ -n "$ROW" ]; then break; fi
      sleep 2
    done
    case "$ROW" in
      *"\"team\":\"$TEAM\""*) pass "team key served; its usage row carries team $TEAM" ;;
      *) fail "team key usage row (request $TID): ${ROW:-not found}" ;;
    esac
  else
    fail "team key call (status $(status_of "$TMP/h6a")): $(snippet "$TMP/b6a")"
  fi

  # The team's cap, set through /admin/routing only while the routing overlay is empty (a PUT
  # replaces the whole overlay); rolled back to that empty version afterwards.
  curl -sS -o "$TMP/b8" -H @"$TMP/auth-admin" "$BASE/admin/routing" || true
  RVER=$(sed -n 's/^{"version":\([0-9]*\),.*/\1/p' "$TMP/b8" 2>/dev/null | head -n 1)
  if [ -n "$RVER" ] && grep -q '"fragment":{}' "$TMP/b8" 2>/dev/null; then
    curl -sS -D "$TMP/h9" -o "$TMP/b9" -X PUT -H @"$TMP/auth-admin" -H "Content-Type: application/json" \
      -d "{\"expected_version\":$RVER,\"comment\":\"lite smoke\",\"fragment\":{\"tenants\":{\"$TENANT\":{\"teams\":{\"$TEAM\":{\"monthly_spend_cap_usd\":0.000001}}}}}}" \
      "$BASE/admin/routing" || true
    if [ "$(status_of "$TMP/h9")" = 200 ]; then
      CAPPED=""
      LAST=""
      # The first call's cost reaches the team's counter just after it, and the overlay reaches
      # every worker within admin_api.routing.poll_seconds (5 s).
      for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
        team_call "b$i"
        LAST=$(status_of "$TMP/h6b$i")
        if [ "$LAST" = 429 ] && grep -q team_spend_cap_exceeded "$TMP/b6b$i"; then
          CAPPED=1
          break
        fi
        sleep 1
      done
      if [ -n "$CAPPED" ]; then pass "team spend cap enforced (429 team_spend_cap_exceeded)"
      else fail "team spend cap not enforced (last status $LAST)"; fi
      curl -sS -D "$TMP/h10" -o "$TMP/b10" -X POST -H @"$TMP/auth-admin" -H "Content-Type: application/json" \
        -d "{\"version\":$RVER,\"comment\":\"lite smoke: undo\"}" "$BASE/admin/routing/rollback" || true
      if [ "$(status_of "$TMP/h10")" != 200 ]; then
        fail "routing overlay not rolled back to version $RVER (status $(status_of "$TMP/h10")): $(snippet "$TMP/b10")"
      fi
    else
      fail "team spend cap (PUT /admin/routing status $(status_of "$TMP/h9")): $(snippet "$TMP/b9")"
    fi
  else
    echo "SKIP  team spend cap (a routing overlay is set; smoke does not replace it)"
  fi

  curl -sS -D "$TMP/h11" -o "$TMP/b11" -X DELETE -H @"$TMP/auth-admin" \
    "$BASE/admin/keys/$TEAM_KEY_ID" || true
  # Refused at once by the worker that revoked it, and by every other within
  # key_store.refresh_seconds (5 s).
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
    team_call c
    [ "$(status_of "$TMP/h6c")" != 401 ] || break
    sleep 1
  done
  if [ "$(status_of "$TMP/h11")" = 200 ] && [ "$(status_of "$TMP/h6c")" = 401 ]; then
    pass "team key revoked and refused"
  else
    fail "team key revoke (DELETE status $(status_of "$TMP/h11"), then call status $(status_of "$TMP/h6c"))"
  fi
else
  fail "team key issue (POST /admin/keys status $(status_of "$TMP/h5")): $(snippet "$TMP/b5")"
fi

if [ "$FAILS" -gt 0 ]; then
  echo "lite smoke: $FAILS check(s) failed"
  exit 1
fi
echo "lite smoke: all checks passed"
