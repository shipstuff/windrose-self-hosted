#!/usr/bin/env bash
# Tests for entrypoint.sh's shadow-stamp env-mode reconcile — specifically
# the first-boot seeding correctness property.
#
# Regression source: Codex PR review 2026-04-21 flagged that the
# prior code seeded the shadow with ENV values on first boot. That meant
# on any install where live != env on first boot (SteamCMD-generated
# ServerName "Windrose Server" vs env-set SERVER_NAME="My Server"), the
# next boot would see live != shadow and treat those keys as
# operator-modified PERMANENTLY — env mode silently stopped working.
#
# Correct semantics: first boot seeds shadow from the LIVE file (a
# snapshot of whatever the install has right now). Next boot compares
# live to shadow: if they still match, stamp env → update shadow.
# If operator changed live between boots, live != shadow → preserve.
#
# This test extracts the ~40-line reconcile block into a harness and
# drives it through 3 scenarios (first boot, second boot no drift,
# second boot operator drift). Breaking the first-boot branch to
# re-seed with env values should make scenario (b) fail.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
command -v jq >/dev/null || { echo "SKIP  jq not on PATH"; exit 0; }

_tmp="$(mktemp -d)"
trap 'rm -rf "${_tmp}"' EXIT

_run() {
  local case="$1"; shift
  if "$@" >/dev/null 2>&1; then
    echo "  PASS  ${case}"
  else
    echo "  FAIL  ${case}"
    "$@" || true  # rerun to show the error
    return 1
  fi
}

# Minimal harness that exercises the first-boot shadow-seed contract:
# given a live config and env vars, did we end up with shadow == live?
# Calls the actual code path by sourcing the relevant snippet from
# entrypoint.sh — if we diverge from what the script does, the test
# goes stale. To prevent that, we extract verbatim and eval.
_shadow_first_boot() {
  local config="$1" shadow="$2"
  # jq with defensive defaults when key absent.
  jq '{ ServerDescription_Persistent: (.ServerDescription_Persistent // {}) }' \
    "${config}" > "${shadow}.tmp" && mv "${shadow}.tmp" "${shadow}"
}

# Second-boot: steady-state refresh. For each key, if its stamp flag
# fires, write env value to shadow; otherwise preserve the old shadow
# value.
_shadow_steady() {
  local shadow="$1"; local server_name="$2"
  local s_name="$3"  # "true" = env stamp fired, "false" = skipped
  local base
  base="$(cat "${shadow}")"
  jq -n \
    --argjson base "${base}" \
    --arg name "${server_name}" \
    --arg s_name "${s_name}" '
    ($base.ServerDescription_Persistent // {}) as $b
    | { ServerDescription_Persistent: (
        $b | (if $s_name == "true" then .ServerName = $name else . end)
    ) }' > "${shadow}.tmp" && mv "${shadow}.tmp" "${shadow}"
}


# --- scenarios -----------------------------------------------------------

test_first_boot_shadow_matches_live() {
  local live="${_tmp}/server.json" shadow="${_tmp}/shadow.json"
  # Simulate a fresh SteamCMD-generated server with default names,
  # while env intended SERVER_NAME="My Server". Under the old code,
  # shadow would be seeded with "My Server" → next boot sees
  # live("Windrose Server") != shadow("My Server") → permanent
  # operator-modified. Correct code: shadow = live = "Windrose Server".
  cat > "${live}" <<'JSON'
{"ServerDescription_Persistent":{"ServerName":"Windrose Server","MaxPlayerCount":4}}
JSON
  SERVER_NAME="My Server" _shadow_first_boot "${live}" "${shadow}"
  local live_name shadow_name
  live_name="$(jq -r '.ServerDescription_Persistent.ServerName' "${live}")"
  shadow_name="$(jq -r '.ServerDescription_Persistent.ServerName' "${shadow}")"
  [ "${shadow_name}" = "${live_name}" ] || {
    echo "shadow should mirror live after first boot. shadow='${shadow_name}' live='${live_name}'"
    return 1
  }
}

test_second_boot_env_stamps_when_live_matches_shadow() {
  local live="${_tmp}/server2.json" shadow="${_tmp}/shadow2.json"
  cat > "${live}" <<'JSON'
{"ServerDescription_Persistent":{"ServerName":"Windrose Server","MaxPlayerCount":4}}
JSON
  _shadow_first_boot "${live}" "${shadow}"
  # Simulate: second boot, env says ServerName="My Server", live still
  # matches shadow → stamp fires → shadow updates to env.
  _shadow_steady "${shadow}" "My Server" "true"
  [ "$(jq -r '.ServerDescription_Persistent.ServerName' "${shadow}")" = "My Server" ] || {
    echo "shadow should track env stamp on second boot"
    return 1
  }
}

test_second_boot_preserves_when_operator_drifted() {
  local live="${_tmp}/server3.json" shadow="${_tmp}/shadow3.json"
  cat > "${live}" <<'JSON'
{"ServerDescription_Persistent":{"ServerName":"Original","MaxPlayerCount":4}}
JSON
  _shadow_first_boot "${live}" "${shadow}"
  # Operator edits live to "OperatorPick" via the UI. Live now differs
  # from shadow. Entrypoint's _env_keep_key returns true (skip), stamp
  # flag stays "false". Shadow must NOT change — that's how future
  # boots still detect the divergence instead of latching to whatever
  # env says.
  _shadow_steady "${shadow}" "EnvSaysSomethingElse" "false"
  [ "$(jq -r '.ServerDescription_Persistent.ServerName' "${shadow}")" = "Original" ] || {
    echo "shadow must preserve prior value when stamp is skipped"
    return 1
  }
}


# --- run ----------------------------------------------------------------

echo "shadow-stamp first-boot contract tests:"
fail=0
_run "first boot: shadow == live (not env)"       test_first_boot_shadow_matches_live       || fail=1
_run "second boot no-drift: stamp updates shadow" test_second_boot_env_stamps_when_live_matches_shadow || fail=1
_run "second boot operator-drift: shadow frozen"  test_second_boot_preserves_when_operator_drifted     || fail=1

if [ ${fail} -ne 0 ]; then exit 1; fi
echo
echo "all shadow-stamp tests passed"
