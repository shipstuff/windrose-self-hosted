#!/bin/bash
# End-to-end API test against the canary pod's admin console.
#
# Usage: ./test_api.sh [pod] [ns] [port]
# Env:
#   UI_AUTH=admin:canary  — basic-auth creds; empty = unauth (then auth'd routes expect 401)
#   RUN_STOP=1            — also POST /api/server/stop (disruptive, off by default)
#
# Default target: windrose-canary-0 / games / 28081 (canary admin UI)
set -u

POD="${1:-windrose-canary-0}"
NS="${2:-games}"
PORT="${3:-28081}"
BASE="http://127.0.0.1:${PORT}"
UI_AUTH="${UI_AUTH:-}"
RUN_STOP="${RUN_STOP:-0}"

pass=0; fail=0
ok()   { printf '\e[32m✓\e[0m %s\n' "$*"; pass=$((pass+1)); }
bad()  { printf '\e[31m✗\e[0m %s\n' "$*"; fail=$((fail+1)); }
info() { printf '  %s\n' "$*"; }

AUTH_FLAG=""
[ -n "$UI_AUTH" ] && AUTH_FLAG="-u $UI_AUTH"
AUTHED=$([ -n "$UI_AUTH" ] && echo "yes" || echo "no")

curlc()  { kubectl exec -n "$NS" "$POD" -c windrose-ui -- sh -c "$*" 2>&1; }
curl_s() { curlc "curl -s $AUTH_FLAG -o /dev/null -w '%{http_code}' $*"; }
curl_b() { curlc "curl -s $AUTH_FLAG $*"; }

# The first element of each tuple is the "expected code" in auth mode.
# In unauth mode, we expect 401 for everything except /healthz + OPTIONS.
expect() {
  local got="$1" want_auth="$2" want_unauth="${3:-401}" label="$4"
  local want
  if [ "$AUTHED" = "yes" ]; then want="$want_auth"; else want="$want_unauth"; fi
  if [ "$got" = "$want" ]; then
    ok "$label → $got (expected $want in $AUTHED-auth mode)"
  else
    bad "$label → $got (expected $want in $AUTHED-auth mode)"
  fi
}

echo "== $BASE  (auth: $AUTHED) =="

# Always-open — public, never requires auth.
expect "$(curl_s "$BASE/healthz")"       "200" "200" "GET /healthz"
expect "$(curl_s "$BASE/api/status")"    "200" "200" "GET /api/status"
expect "$(curl_s "$BASE/")"              "200" "200" "GET /"
expect "$(curl_s "$BASE/app.css")"       "200" "200" "GET /app.css"
expect "$(curl_s "$BASE/app.js")"        "200" "200" "GET /app.js"

# Auth-gated — require Authorization header.
expect "$(curl_s "$BASE/api/invite")"    "200" "401" "GET /api/invite"
expect "$(curl_s "$BASE/api/config")"    "200" "401" "GET /api/config"
expect "$(curl_s "$BASE/api/backups")"   "200" "401" "GET /api/backups"
expect "$(curl_s "$BASE/does-not-exist")" "404" "401" "GET /does-not-exist"

# Public status payload should omit AccountIds when unauthed.
if [ "$AUTHED" = "no" ]; then
  body="$(curl_b "$BASE/api/status")"
  # Either no players or players lack accountId. Absence-of-key is what
  # we want — players[].accountId should not appear in the public view.
  if echo "$body" | grep -q '"accountId"'; then
    bad "public /api/status should not expose accountId"
  else
    ok "public /api/status omits accountId"
  fi
  echo "$body" | grep -q '"authenticated": false' && ok "public /api/status reports authenticated=false" \
    || bad "public /api/status missing authenticated=false"
fi

# Only do body-inspection + destructive calls when authenticated.
if [ "$AUTHED" = "yes" ]; then
  body="$(curl_b "$BASE/api/status")"
  echo "$body" | grep -q '"serverRunning"' && ok "status has serverRunning" || bad "status missing serverRunning"
  echo "$body" | grep -q '"cpuLimitMcpu"'  && ok "status has cpuLimitMcpu"  || bad "status missing cpuLimitMcpu"
  echo "$body" | grep -q '"allowDestructive"' && ok "status has allowDestructive" || bad "missing allowDestructive"

  cfg="$(curl_b "$BASE/api/config")"
  echo "$cfg" | grep -q '"live"'   && ok "config has live"   || bad "config missing live"
  echo "$cfg" | grep -q '"worlds"' && ok "config has worlds" || bad "config missing worlds"

  # Schema validation (valid + invalid).
  ok_sample='{"ServerDescription_Persistent":{"ServerName":"t","MaxPlayerCount":4,"IsPasswordProtected":false,"Password":"","P2pProxyAddress":"","PersistentServerId":"1006E66345DA6416AA7A7E90A32630B4","InviteCode":"c030f708","WorldIslandId":"7A0A41E9616A4394A19B5F21A99C12B7"}}'
  resp="$(curlc "curl -s $AUTH_FLAG -X POST -H 'Content-Type: application/json' -d '$ok_sample' $BASE/api/config/validate")"
  echo "$resp" | grep -q '"valid": true' && ok "validate: valid passes" || bad "validate: valid rejected ($resp)"
  bad_sample='{"ServerDescription_Persistent":{"ServerName":"t","MaxPlayerCount":99}}'
  resp="$(curlc "curl -s $AUTH_FLAG -X POST -H 'Content-Type: application/json' -d '$bad_sample' $BASE/api/config/validate")"
  echo "$resp" | grep -q '"valid": false' && ok "validate: invalid rejected" || bad "validate: invalid passed ($resp)"

  # Destructive — creates a real backup. We accept 200 only.
  expect "$(curl_s "-X POST $BASE/api/backups")" "200" "401" "POST /api/backups"

  # World config on the active world (if any).
  active="$(echo "$cfg" | python3 -c 'import json,sys
try: w=json.load(sys.stdin).get("worlds",[])
except: w=[]
print(w[0]["islandId"] if w else "")')"
  if [ -n "$active" ]; then
    expect "$(curl_s "$BASE/api/worlds/$active/config")" "200" "401" "GET /api/worlds/{id}/config"
  else
    info "no worlds on disk — skipping world config test"
  fi

  # Optional: intentionally stop the server. Kubelet will restart the container.
  if [ "$RUN_STOP" = "1" ]; then
    expect "$(curl_s "-X POST $BASE/api/server/stop")" "200" "401" "POST /api/server/stop"
    info "sent stop — pod will restart"
  else
    info "skipping POST /api/server/stop (set RUN_STOP=1 to include)"
  fi

  # Unauth should still be rejected even when we have creds: probe an
  # admin-only endpoint without the flag. /api/status is intentionally
  # public (returns a redacted view) so probe /api/config instead.
  unauth_code="$(curlc "curl -s -o /dev/null -w '%{http_code}' $BASE/api/config")"
  [ "$unauth_code" = "401" ] && ok "unauth probe on /api/config still 401" || bad "unauth probe → $unauth_code (expected 401)"
fi

echo
echo "== $pass passed, $fail failed =="
[ "$fail" -eq 0 ]
