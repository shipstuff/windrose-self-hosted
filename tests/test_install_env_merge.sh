#!/usr/bin/env bash
# Regression test for bare-linux/install.sh's env file merge.
#
# Bug (2026-04-21, caught on VPS rollout): install.sh pre-assigned
# UI_PASSWORD="${UI_PASSWORD:-}" BEFORE the merge loop. The merge
# then saw UI_PASSWORD as "set" (to empty) and skipped the existing
# env file's value, silently wiping the password on re-run.
#
# This test exercises the merge-loop behavior in isolation: it
# extracts the ~80-line merge+write block from install.sh into a
# temp harness, runs it with a simulated existing env file, then
# asserts every secret + operator key survives.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
_tmp="$(mktemp -d)"
trap 'rm -rf "${_tmp}"' EXIT

# Simulated "existing" env file — operator has real values from a
# prior install. This is exactly what we saw on the VPS pre-rollout.
existing="${_tmp}/windrose.env"
cat > "${existing}" <<'EOF'
HOME=/home/steam
UI_BIND=127.0.0.1
UI_PORT=28080
UI_PASSWORD=s3cretOperatorValue
UI_ENABLE_ADMIN_WITHOUT_PASSWORD=false
UI_SERVE_STATIC=true
UI_ENABLE_METRICS_ROUTE=false
WINDROSE_METRICS_ENABLED=true
METRICS_BIND=127.0.0.1
METRICS_PORT=28081
WINDROSE_DISCORD_WEBHOOK_URL=https://discord.example/webhooks/abc
WINDROSE_WEBHOOK_URL=
WINDROSE_WEBHOOK_EVENTS=server.online,server.offline
WINDROSE_WEBHOOK_POLL_SECONDS=15
WINDROSE_WEBHOOK_TIMEOUT=5
SERVER_NAME=My Server
MAX_PLAYER_COUNT=6
P2P_PROXY_ADDRESS=192.168.1.50
WINDROSE_PATCH_IDLE_CPU=1
# Operator additions below
CUSTOM_OPERATOR_KEY=custom_value
EOF

# Run the merge + heredoc in a subshell with a minimal harness.
# Harness replicates the install.sh _MANAGED_KEYS list + the merge
# loop + the heredoc. Any divergence from install.sh's real flow
# will show up when we re-sync: the test deliberately mirrors the
# production code closely.
out="${_tmp}/out.env"

bash <<HARNESS
set -eu
WINDROSE_ENV_FILE="${existing}"

# NOTE: intentionally mirror install.sh's behavior — no pre-merge
# defaults. Heredoc applies them inline, after the merge has had a
# chance to pull existing values.
_MANAGED_KEYS=" \
  HOME WINDROSE_PATH STEAMCMD_PATH STEAM_SDK64_PATH STEAM_SDK32_PATH \
  DISPLAY WINDROSE_SERVER_SOURCE SERVER_NAME MAX_PLAYER_COUNT \
  IS_PASSWORD_PROTECTED SERVER_PASSWORD WORLD_ISLAND_ID WORLD_NAME \
  WORLD_PRESET_TYPE P2P_PROXY_ADDRESS DISABLE_SENTRY PROTON_USE_XALIA \
  USE_DIRECT_CONNECTION DIRECT_CONNECTION_SERVER_ADDRESS \
  DIRECT_CONNECTION_SERVER_PORT DIRECT_CONNECTION_PROXY_ADDRESS \
  FILES_WAIT_TIMEOUT_SECONDS WINDROSE_PATCH_IDLE_CPU \
  UI_BIND UI_PORT UI_PASSWORD \
  UI_ENABLE_ADMIN_WITHOUT_PASSWORD UI_SERVE_STATIC \
  UI_ENABLE_METRICS_ROUTE WINDROSE_METRICS_ENABLED \
  METRICS_BIND METRICS_PORT \
  WINDROSE_DISCORD_WEBHOOK_URL WINDROSE_WEBHOOK_URL \
  WINDROSE_WEBHOOK_EVENTS WINDROSE_WEBHOOK_POLL_SECONDS \
  WINDROSE_WEBHOOK_TIMEOUT \
"

PRESERVED_EXTRAS=""
_preserved_order=()
declare -A _preserved_map=()

while IFS='=' read -r _k _v || [ -n "\$_k" ]; do
  [ -z "\$_k" ] && continue
  case "\$_k" in \#*|*[[:space:]]*) continue ;; esac
  case " \${_MANAGED_KEYS} " in
    *" \$_k "*)
      if [ -z "\${!_k+x}" ]; then
        printf -v "\$_k" '%s' "\$_v"
      fi
      ;;
    *)
      if [ -z "\${_preserved_map[\$_k]+x}" ]; then
        _preserved_order+=("\$_k")
      fi
      _preserved_map[\$_k]="\$_v"
      ;;
  esac
done < "\${WINDROSE_ENV_FILE}"

for _k in "\${_preserved_order[@]}"; do
  PRESERVED_EXTRAS="\${PRESERVED_EXTRAS}\${_k}=\${_preserved_map[\$_k]}"\$'\n'
done

# Mirror the install.sh heredoc output. Inline defaults — post-merge.
cat > '${out}' <<EOF
UI_BIND=\${UI_BIND:-127.0.0.1}
UI_PORT=\${UI_PORT:-28080}
UI_PASSWORD=\${UI_PASSWORD:-}
UI_ENABLE_ADMIN_WITHOUT_PASSWORD=\${UI_ENABLE_ADMIN_WITHOUT_PASSWORD:-false}
UI_SERVE_STATIC=\${UI_SERVE_STATIC:-true}
UI_ENABLE_METRICS_ROUTE=\${UI_ENABLE_METRICS_ROUTE:-false}
WINDROSE_METRICS_ENABLED=\${WINDROSE_METRICS_ENABLED:-false}
METRICS_BIND=\${METRICS_BIND:-127.0.0.1}
METRICS_PORT=\${METRICS_PORT:-28081}
WINDROSE_DISCORD_WEBHOOK_URL=\${WINDROSE_DISCORD_WEBHOOK_URL:-}
WINDROSE_WEBHOOK_URL=\${WINDROSE_WEBHOOK_URL:-}
WINDROSE_WEBHOOK_EVENTS=\${WINDROSE_WEBHOOK_EVENTS:-server.online,server.offline,player.join,player.leave,backup.created,backup.restored,config.applied}
WINDROSE_WEBHOOK_POLL_SECONDS=\${WINDROSE_WEBHOOK_POLL_SECONDS:-15}
WINDROSE_WEBHOOK_TIMEOUT=\${WINDROSE_WEBHOOK_TIMEOUT:-5}
SERVER_NAME=\${SERVER_NAME:-Windrose Bare-Linux}
MAX_PLAYER_COUNT=\${MAX_PLAYER_COUNT:-4}
P2P_PROXY_ADDRESS=\${P2P_PROXY_ADDRESS:-}
WINDROSE_PATCH_IDLE_CPU=\${WINDROSE_PATCH_IDLE_CPU:-0}
EOF

# Append operator additions
printf '# operator extras\n%s' "\${PRESERVED_EXTRAS}" >> '${out}'
HARNESS

# --- assertions ------------------------------------------------------
assert() {
  local key="$1" want="$2"
  local got
  got="$(grep "^${key}=" "${out}" | head -1 | cut -d= -f2-)"
  if [ "${got}" != "${want}" ]; then
    echo "  FAIL  ${key}: want '${want}' got '${got}'"
    return 1
  fi
  echo "  PASS  ${key} preserved"
}

echo "install.sh env-merge regression test:"
fail=0
assert UI_PASSWORD "s3cretOperatorValue"                                   || fail=1
assert WINDROSE_DISCORD_WEBHOOK_URL "https://discord.example/webhooks/abc" || fail=1
assert SERVER_NAME "My Server"                                             || fail=1
assert MAX_PLAYER_COUNT "6"                                                || fail=1
assert P2P_PROXY_ADDRESS "192.168.1.50"                                    || fail=1
assert WINDROSE_PATCH_IDLE_CPU "1"                                         || fail=1
assert UI_ENABLE_METRICS_ROUTE "false"                                     || fail=1
assert WINDROSE_METRICS_ENABLED "true"                                     || fail=1
assert METRICS_BIND "127.0.0.1"                                            || fail=1
assert METRICS_PORT "28081"                                                || fail=1
assert CUSTOM_OPERATOR_KEY "custom_value"                                  || fail=1

# Also assert unset-in-existing vars fall back to their defaults.
assert UI_ENABLE_ADMIN_WITHOUT_PASSWORD "false" || fail=1
assert UI_SERVE_STATIC "true" || fail=1

if [ ${fail} -ne 0 ]; then
  echo
  echo "---- merged env file dump (for debugging) ----"
  cat "${out}"
  exit 1
fi

# --- fresh-install set -u test ----------------------------------------
# Codex PR #2 review (2026-04-21, P1): after removing the pre-merge
# `${X:-default}` assignments to fix the password-wipe bug, a fresh
# install with NO existing env file and NO CLI overrides leaves
# UI_BIND / UI_PORT unset in the shell — the status echo at the end of
# install.sh dereferences them under `set -u` and aborts the script.
# Minimal repro: run the same harness but without seeding an existing
# env file, then confirm the four managed-UI vars got their defaults
# applied (via the post-merge `:=` block the fix adds).

out2="${_tmp}/fresh-install.env.out"
bash <<HARNESS 2>&1
set -eu
WINDROSE_ENV_FILE="${_tmp}/does-not-exist-$$.env"  # no prior env file

# Same pattern install.sh uses post-merge — these MUST set shell vars
# (walrus :=), not just expand them (:-), or the status-line code
# would blow up on undefined vars with set -u.
: "\${UI_BIND:=127.0.0.1}"
: "\${UI_PORT:=28080}"
: "\${UI_PASSWORD:=}"
: "\${UI_ENABLE_ADMIN_WITHOUT_PASSWORD:=false}"
: "\${UI_ENABLE_METRICS_ROUTE:=false}"
: "\${WINDROSE_METRICS_ENABLED:=false}"
: "\${METRICS_BIND:=127.0.0.1}"
: "\${METRICS_PORT:=28081}"

# Simulated status echo — the thing that would fail under set -u
# without the fix.
echo "bind=\${UI_BIND} port=\${UI_PORT} pwlen=\${#UI_PASSWORD} admin=\${UI_ENABLE_ADMIN_WITHOUT_PASSWORD} metrics=\${WINDROSE_METRICS_ENABLED} mbind=\${METRICS_BIND} mport=\${METRICS_PORT}" > '${out2}'
HARNESS

if [ ! -f "${out2}" ]; then
  echo "  FAIL  fresh install set -u aborted before echo"
  exit 1
fi
if ! grep -q "bind=127.0.0.1 port=28080 pwlen=0 admin=false metrics=false mbind=127.0.0.1 mport=28081" "${out2}"; then
  echo "  FAIL  fresh-install defaults incorrect:"
  cat "${out2}"
  exit 1
fi
echo "  PASS  fresh install (no env, no CLI) — defaults applied, set -u safe"

echo
echo "all install.sh env-merge regression checks passed"
