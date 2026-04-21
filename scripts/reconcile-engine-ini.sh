#!/usr/bin/env bash
# Idempotent reconciler for Windrose's Engine.ini — keeps
# NetServerMaxTickRate + t.MaxFPS in sync with the
# NET_SERVER_MAX_TICK_RATE env var on every boot, while preserving any
# hand-edits the operator made to other keys.
#
# Shadow-stamp pattern: we write a sibling file listing the values we
# last wrote. On the next run we compare the live Engine.ini value
# against that shadow. If they match, we update to the new env value.
# If they diverge, the operator hand-edited — leave it alone.
#
# Args:
#   $1 = Engine.ini path (will be created if missing + env set)
#   $2 = shadow file path
# Env:
#   NET_SERVER_MAX_TICK_RATE = integer. When unset, this script is a
#                              no-op so operators without the knob
#                              opted in stay on whatever they had.
#
# Why separate from entrypoint.sh: keeps the 80-line awk reconcile
# loop testable in isolation (tests/test_engine_ini_reconcile.sh) —
# the previous seed-once logic was a dishonest knob (env var only
# took effect on pristine installs) and shipped to prod in the state
# where NetServerMaxTickRate was pinned at 30 with no way to bump it
# via env. Catching regressions like that needs a unit test, not
# manual rediscovery on the next deploy.
set -euo pipefail

engine_ini="${1:?engine_ini path required}"
shadow_ini="${2:?shadow path required}"

# No env = no-op. Some operators want to manage Engine.ini entirely
# by hand; leaving the env unset preserves that.
if [ -z "${NET_SERVER_MAX_TICK_RATE:-}" ]; then
  exit 0
fi

want="${NET_SERVER_MAX_TICK_RATE}"
mkdir -p "$(dirname "${engine_ini}")"

# Seed path: fresh install. Write the three sections we care about
# and drop a shadow matching what we wrote.
if [ ! -f "${engine_ini}" ]; then
  cat > "${engine_ini}" <<EOF
; Seeded by windrose-self-hosted entrypoint on $(date -uIseconds).
; NetServerMaxTickRate + t.MaxFPS reconciled from NET_SERVER_MAX_TICK_RATE
; on every boot (shadow-stamp preserves your manual edits to other keys).

[/Script/R5SocketSubsystem.R5NetDriver]
NetServerMaxTickRate=${want}

[/Script/OnlineSubsystemUtils.IpNetDriver]
NetServerMaxTickRate=${want}

[ConsoleVariables]
t.MaxFPS=${want}
EOF
  printf 'NetServerMaxTickRate=%s\nt.MaxFPS=%s\n' "${want}" "${want}" > "${shadow_ini}"
  echo "[engine-ini] seeded ${engine_ini} with NetServerMaxTickRate=${want}"
  exit 0
fi

# Reconcile path: upsert keys under their sections. awk tracks the
# current section header + whether we've already stamped in it, so
# duplicates get collapsed + the first matching key gets rewritten.
_stamp_key() {
  local section="$1" key="$2" value="$3"
  awk -v section="${section}" -v key="${key}" -v value="${value}" '
    BEGIN { in_sec=0; stamped=0; added_section=0 }
    /^\[/ {
      if (in_sec && !stamped) { print key "=" value; stamped=1 }
      in_sec = ($0 == section)
      print; next
    }
    in_sec && $0 ~ "^" key "=" {
      if (!stamped) { print key "=" value; stamped=1 }
      next
    }
    { print }
    END {
      if (!added_section && !stamped) {
        # Section exists but key wasnt there, OR section doesnt exist.
        # awk cant tell after the fact; caller can re-check.
        if (in_sec) { print key "=" value }
        else { print ""; print section; print key "=" value }
      }
    }
  ' "${engine_ini}" > "${engine_ini}.tmp" && mv "${engine_ini}.tmp" "${engine_ini}"
}

_read_live() {
  local section="$1" key="$2"
  awk -v s="${section}" -v k="${key}" '
    $0 == s { in_s=1; next }
    /^\[/ { in_s=0 }
    in_s && $0 ~ "^" k "=" { sub("^" k "=", ""); print; exit }
  ' "${engine_ini}"
}

_read_shadow() {
  local key="$1"
  [ -f "${shadow_ini}" ] || return 0
  awk -F= -v k="${key}" '$1 == k { print $2; exit }' "${shadow_ini}"
}

_reconcile_one() {
  local section="$1" key="$2"
  local live shadow
  live="$(_read_live "${section}" "${key}")"
  shadow="$(_read_shadow "${key}")"
  if [ "${live}" = "${want}" ]; then
    return 0  # already there
  elif [ -z "${live}" ] || [ -z "${shadow}" ] || [ "${live}" = "${shadow}" ]; then
    _stamp_key "${section}" "${key}" "${want}"
    echo "[engine-ini] reconciled ${key}=${want} in ${section} (was '${live:-absent}')"
  else
    echo "[engine-ini] keeping operator-modified ${key}=${live} in ${section} (env=${want}, shadow=${shadow})"
  fi
}

_reconcile_one '[/Script/R5SocketSubsystem.R5NetDriver]'     'NetServerMaxTickRate'
_reconcile_one '[/Script/OnlineSubsystemUtils.IpNetDriver]'  'NetServerMaxTickRate'
_reconcile_one '[ConsoleVariables]'                          't.MaxFPS'

# Shadow reflects OUR intent — not the live value — so a future env
# change can tell "operator-didnt-touch" (live == shadow) from
# "operator overrode" (live != shadow).
printf 'NetServerMaxTickRate=%s\nt.MaxFPS=%s\n' "${want}" "${want}" > "${shadow_ini}"
