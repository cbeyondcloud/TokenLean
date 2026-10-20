#!/usr/bin/env bash
# Smoke-test a running TokenLean LITE stack. Exit 0 when every check passes.
#
#   ./smoke.sh            # model: SMOKE_MODEL, else the mock (after up --mock), else a model
#                         # for the first provider key set in .env.lite
#
# Checks: /health; an OpenAI chat completion (x-tokenlean-request-id + x-tokenlean-cost-usd);
# the same streamed (the final `: x-tokenlean-cost` frame and [DONE]); an Anthropic
# /v1/messages call (x-api-key auth, x-tokenlean-* headers); then a usage row for each of the
# three calls, through /admin/usage-export. TokenLean has no /v1/responses endpoint.
# Live mode makes three small real calls (a fraction of a cent). Engine-independent: HTTP only.
set -euo pipefail
# shellcheck source=_lib.sh
. "$(cd "$(dirname "$0")" && pwd)/_lib.sh"

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
curl -sS -D "$TMP/h1" -o "$TMP/b1" -H "Authorization: Bearer $TOKENLEAN_API_KEY" \
  -H "Content-Type: application/json" -d "$BODY" "$BASE/v1/chat/completions" || true
ID1=$(header "$TMP/h1" x-tokenlean-request-id)
COST1=$(header "$TMP/h1" x-tokenlean-cost-usd)
if [ "$(status_of "$TMP/h1")" = 200 ] && [ -n "$ID1" ] && [ -n "$COST1" ] && grep -q '"choices"' "$TMP/b1" 2>/dev/null; then
  pass "chat completion (cost \$$COST1)"
  IDS="$IDS $ID1"
else
  fail "chat completion (status $(status_of "$TMP/h1"), request-id '${ID1}', cost '${COST1}'): $(snippet "$TMP/b1")"
fi

# 3. OpenAI chat completion, streamed
BODY="{\"model\":\"$MODEL\",\"max_tokens\":16,\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"Reply with the word pong. ($NONCE b)\"}]}"
curl -sSN -D "$TMP/h2" -o "$TMP/b2" -H "Authorization: Bearer $TOKENLEAN_API_KEY" \
  -H "Content-Type: application/json" -d "$BODY" "$BASE/v1/chat/completions" || true
ID2=$(frame_field "$TMP/b2" request_id)
COST2=$(frame_field "$TMP/b2" cost_usd)
if [ "$(status_of "$TMP/h2")" = 200 ] && [ -n "$ID2" ] && grep -q '^data: \[DONE\]' "$TMP/b2" 2>/dev/null; then
  pass "chat completion stream (final cost frame \$$COST2)"
  IDS="$IDS $ID2"
else
  fail "chat completion stream (status $(status_of "$TMP/h2"), no cost frame or [DONE]): $(snippet "$TMP/b2")"
fi

# 4. Anthropic messages
BODY="{\"model\":\"$MODEL\",\"max_tokens\":16,\"messages\":[{\"role\":\"user\",\"content\":\"Reply with the word pong. ($NONCE c)\"}]}"
curl -sS -D "$TMP/h3" -o "$TMP/b3" -H "x-api-key: $TOKENLEAN_API_KEY" \
  -H "anthropic-version: 2023-06-01" -H "Content-Type: application/json" -d "$BODY" \
  "$BASE/v1/messages" || true
ID3=$(header "$TMP/h3" x-tokenlean-request-id)
COST3=$(header "$TMP/h3" x-tokenlean-cost-usd)
if [ "$(status_of "$TMP/h3")" = 200 ] && [ -n "$ID3" ] && [ -n "$COST3" ] && grep -q '"type":"message"' "$TMP/b3" 2>/dev/null; then
  pass "messages (cost \$$COST3)"
  IDS="$IDS $ID3"
else
  fail "messages (status $(status_of "$TMP/h3"), request-id '${ID3}', cost '${COST3}'): $(snippet "$TMP/b3")"
fi

# 5. a usage row for each call (rows are written just after each response)
if [ -n "$IDS" ]; then
  FOUND=0
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    curl -sS -o "$TMP/b4" -X POST -H "Authorization: Bearer $TOKENLEAN_API_KEY" \
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

if [ "$FAILS" -gt 0 ]; then
  echo "lite smoke: $FAILS check(s) failed"
  exit 1
fi
echo "lite smoke: all checks passed"
