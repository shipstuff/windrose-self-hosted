#!/usr/bin/env bash
# Unit tests for scripts/reconcile-engine-ini.sh.
#
# The previous seed-once Engine.ini logic was a dishonest knob: env var
# NET_SERVER_MAX_TICK_RATE only took effect on pristine installs, so
# any deployment with an existing Engine.ini stayed at whatever tick
# rate was on disk — bug #2 on the 2026-04-21 rollout. Prod was stuck
# at 30 despite the 60 default because Engine.ini predated the env
# knob. The fix reconciles on every boot using shadow-stamp; these
# tests make sure that stays honest.
#
# Scenarios:
#   1. Env unset → script is a no-op (no file created).
#   2. Fresh install (no Engine.ini) → seeds with env value in all
#      three sections, drops matching shadow.
#   3. Existing file with stale value + no shadow → reconciles to env
#      (the prod-like case: old install predates the shadow mechanism).
#   4. Existing file matches shadow → reconciles to new env value.
#   5. Operator hand-edited (live != shadow) → leaves operator value
#      alone, logs "keeping operator-modified".
#   6. Reconcile is idempotent — same value on repeated runs is a no-op.
#   7. Operator's unrelated keys/comments under the same sections are
#      preserved across reconcile (we only touch our two keys).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="${REPO_ROOT}/scripts/reconcile-engine-ini.sh"

_tmp="$(mktemp -d)"
trap 'rm -rf "${_tmp}"' EXIT

_run() {
  local case="$1"; shift
  if "$@" >/dev/null; then
    echo "  PASS  ${case}"
  else
    echo "  FAIL  ${case}"
    return 1
  fi
}

_live_value() {
  local file="$1" section="$2" key="$3"
  awk -v s="${section}" -v k="${key}" '
    $0 == s { in_s=1; next }
    /^\[/ { in_s=0 }
    in_s && $0 ~ "^" k "=" { sub("^" k "=", ""); print; exit }
  ' "${file}"
}

# --- scenarios -----------------------------------------------------------

test_noop_when_env_unset() {
  local ini="${_tmp}/ini1" shadow="${_tmp}/shadow1"
  ( unset NET_SERVER_MAX_TICK_RATE; "${SCRIPT}" "${ini}" "${shadow}" )
  [ ! -f "${ini}" ] && [ ! -f "${shadow}" ]
}

test_seeds_fresh_install() {
  local ini="${_tmp}/ini2" shadow="${_tmp}/shadow2"
  NET_SERVER_MAX_TICK_RATE=90 "${SCRIPT}" "${ini}" "${shadow}"
  [ "$(_live_value "${ini}" '[/Script/R5SocketSubsystem.R5NetDriver]'    NetServerMaxTickRate)" = "90" ] &&
  [ "$(_live_value "${ini}" '[/Script/OnlineSubsystemUtils.IpNetDriver]' NetServerMaxTickRate)" = "90" ] &&
  [ "$(_live_value "${ini}" '[ConsoleVariables]'                         't.MaxFPS')"           = "90" ]
}

test_reconciles_stale_existing_no_shadow() {
  # Simulates prod: Engine.ini pinned at 30, no shadow file (install
  # predates the shadow mechanism). This is the exact bug we just hit.
  local ini="${_tmp}/ini3" shadow="${_tmp}/shadow3"
  cat > "${ini}" <<'EOF'
[/Script/Engine.Engine]
bSmoothFrameRate=True

[/Script/OnlineSubsystemUtils.IpNetDriver]
NetServerMaxTickRate=30

[ConsoleVariables]
t.MaxFPS=30
EOF
  NET_SERVER_MAX_TICK_RATE=120 "${SCRIPT}" "${ini}" "${shadow}" >/dev/null
  [ "$(_live_value "${ini}" '[/Script/OnlineSubsystemUtils.IpNetDriver]' NetServerMaxTickRate)" = "120" ] &&
  [ "$(_live_value "${ini}" '[ConsoleVariables]' t.MaxFPS)" = "120" ] &&
  [ -f "${shadow}" ]
}

test_reconciles_when_live_matches_shadow() {
  local ini="${_tmp}/ini4" shadow="${_tmp}/shadow4"
  NET_SERVER_MAX_TICK_RATE=60 "${SCRIPT}" "${ini}" "${shadow}" >/dev/null
  [ "$(_live_value "${ini}" '[ConsoleVariables]' t.MaxFPS)" = "60" ] || return 1
  # Second run with a new env value: live matches shadow → reconcile.
  NET_SERVER_MAX_TICK_RATE=120 "${SCRIPT}" "${ini}" "${shadow}" >/dev/null
  [ "$(_live_value "${ini}" '[ConsoleVariables]' t.MaxFPS)" = "120" ]
}

test_preserves_operator_override() {
  local ini="${_tmp}/ini5" shadow="${_tmp}/shadow5"
  NET_SERVER_MAX_TICK_RATE=60 "${SCRIPT}" "${ini}" "${shadow}" >/dev/null
  # Operator hand-edits NetServerMaxTickRate to 90 (divergence from
  # shadow at 60). Next reconcile with env=120 should NOT touch it.
  sed -i 's/NetServerMaxTickRate=60/NetServerMaxTickRate=90/g' "${ini}"
  NET_SERVER_MAX_TICK_RATE=120 "${SCRIPT}" "${ini}" "${shadow}" >/dev/null
  [ "$(_live_value "${ini}" '[/Script/OnlineSubsystemUtils.IpNetDriver]' NetServerMaxTickRate)" = "90" ]
}

test_idempotent_same_value() {
  local ini="${_tmp}/ini6" shadow="${_tmp}/shadow6"
  NET_SERVER_MAX_TICK_RATE=60 "${SCRIPT}" "${ini}" "${shadow}" >/dev/null
  local first_hash second_hash
  first_hash="$(sha256sum "${ini}" | cut -d' ' -f1)"
  NET_SERVER_MAX_TICK_RATE=60 "${SCRIPT}" "${ini}" "${shadow}" >/dev/null
  second_hash="$(sha256sum "${ini}" | cut -d' ' -f1)"
  [ "${first_hash}" = "${second_hash}" ]
}

test_preserves_unrelated_keys() {
  local ini="${_tmp}/ini7" shadow="${_tmp}/shadow7"
  cat > "${ini}" <<'EOF'
[/Script/Engine.Engine]
bSmoothFrameRate=True
bUseFixedFrameRate=False
bAllowMultiThreadedAnimationUpdate=True

[/Script/OnlineSubsystemUtils.IpNetDriver]
NetServerMaxTickRate=30
MaxClientRate=100000
MaxInternetClientRate=100000

[ConsoleVariables]
t.MaxFPS=30
net.AllowAsyncLoading=1
EOF
  NET_SERVER_MAX_TICK_RATE=120 "${SCRIPT}" "${ini}" "${shadow}" >/dev/null
  # Our two keys updated:
  [ "$(_live_value "${ini}" '[/Script/OnlineSubsystemUtils.IpNetDriver]' NetServerMaxTickRate)" = "120" ] || return 1
  [ "$(_live_value "${ini}" '[ConsoleVariables]' t.MaxFPS)" = "120" ] || return 1
  # Unrelated keys preserved:
  [ "$(_live_value "${ini}" '[/Script/Engine.Engine]' bSmoothFrameRate)" = "True" ] || return 1
  [ "$(_live_value "${ini}" '[/Script/OnlineSubsystemUtils.IpNetDriver]' MaxClientRate)" = "100000" ] || return 1
  [ "$(_live_value "${ini}" '[ConsoleVariables]' net.AllowAsyncLoading)" = "1" ] || return 1
}

# --- run them ------------------------------------------------------------

echo "Engine.ini reconcile tests:"
fail=0
_run "env unset → no-op"                       test_noop_when_env_unset                  || fail=1
_run "fresh install seeds all 3 sections"      test_seeds_fresh_install                  || fail=1
_run "stale existing + no shadow (prod repro)" test_reconciles_stale_existing_no_shadow  || fail=1
_run "live matches shadow → reconciles"        test_reconciles_when_live_matches_shadow  || fail=1
_run "operator override preserved"             test_preserves_operator_override          || fail=1
_run "same value is idempotent (no rewrite)"   test_idempotent_same_value                || fail=1
_run "unrelated keys / comments preserved"     test_preserves_unrelated_keys             || fail=1

if [ ${fail} -ne 0 ]; then exit 1; fi
echo
echo "all Engine.ini reconcile tests passed"
