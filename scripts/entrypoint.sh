#!/bin/bash
set -euo pipefail

timestamp() {
  date +"%Y-%m-%d %H:%M:%S,%3N"
}

: "${STEAM_APP_ID:=4129620}"
: "${WINDROSE_PATH:=${HOME}/windrose}"
: "${WINDROSE_SERVER_DIR:=${WINDROSE_PATH}/WindowsServer}"
: "${WINDROSE_SERVER_CONFIG:=${WINDROSE_SERVER_DIR}/R5/ServerDescription.json}"
: "${WINDROSE_SAVE_ROOT:=${WINDROSE_SERVER_DIR}/R5/Saved/SaveProfiles/Default/RocksDB}"
: "${WINDROSE_LAUNCH_STRATEGY:=shipping}"
: "${GE_PROTON_VERSION:=10-34}"
: "${GE_PROTON_URL:=https://github.com/GloriousEggroll/proton-ge-custom/releases/download/GE-Proton${GE_PROTON_VERSION}/GE-Proton${GE_PROTON_VERSION}.tar.gz}"
: "${GE_PROTON_SEED_ROOT:=/usr/local/share/proton-seed}"
: "${STEAMCMD_PATH:=${HOME}/steamcmd}"
: "${STEAM_SDK64_PATH:=${HOME}/.steam/sdk64}"
: "${STEAM_SDK32_PATH:=${HOME}/.steam/sdk32}"
: "${STEAM_COMPAT_CLIENT_INSTALL_PATH:=${STEAMCMD_PATH}}"
: "${STEAM_COMPAT_DATA_PATH:=${STEAMCMD_PATH}/steamapps/compatdata/${STEAM_APP_ID}}"
# Proton is exec'd at the bottom of this script; it reads these from
# its environment, not from its args. `: "${X:=val}"` only sets in the
# current shell's local variables and does not export to children. The
# Dockerfile ENV directive handles this implicitly, but systemd's
# EnvironmentFile / the bare-linux installer don't — so export here.
export STEAM_COMPAT_CLIENT_INSTALL_PATH STEAM_COMPAT_DATA_PATH STEAMCMD_PATH \
       STEAM_SDK64_PATH STEAM_SDK32_PATH WINDROSE_SERVER_DIR HOME

: "${SERVER_NAME:=Windrose Server}"
: "${INVITE_CODE:=}"
: "${IS_PASSWORD_PROTECTED:=false}"
: "${SERVER_PASSWORD:=}"
: "${MAX_PLAYER_COUNT:=4}"
: "${WORLD_ISLAND_ID:=default-world}"
: "${WORLD_NAME:=Default Windrose World}"
: "${WORLD_PRESET_TYPE:=Medium}"
: "${P2P_PROXY_ADDRESS:=}"
if [ -z "${P2P_PROXY_ADDRESS}" ] || [ "${P2P_PROXY_ADDRESS}" = "0.0.0.0" ]; then
  # Auto-detect the host's LAN-facing IP. The classic UDP-connect trick:
  # a SOCK_DGRAM connect() to a public address doesn't send any packets
  # but sets the socket's local endpoint to whatever IP the kernel would
  # route out of, which IS the LAN interface IP under hostNetwork. Works
  # across k8s, compose, bare-Linux — no interface enumeration, no ip(8)
  # dependency (we get it for free via python3 which is already installed
  # for Proton).
  detected="$(python3 -c 'import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
  s.connect(("8.8.8.8", 53))
  print(s.getsockname()[0])
finally:
  s.close()' 2>/dev/null || true)"
  if [ -n "${detected}" ] && [ "${detected}" != "0.0.0.0" ]; then
    echo "$(timestamp) INFO: P2P_PROXY_ADDRESS auto-detected as ${detected} (primary route-out interface)"
    P2P_PROXY_ADDRESS="${detected}"
  else
    echo "$(timestamp) WARNING: P2P_PROXY_ADDRESS auto-detect failed; falling back to 0.0.0.0. Clients will silently bounce to the main menu after ~10 s. Set P2P_PROXY_ADDRESS explicitly via values / env."
    P2P_PROXY_ADDRESS="0.0.0.0"
  fi
fi

: "${FILES_WAIT_TIMEOUT_SECONDS:=0}"   # 0 = wait forever for the UI sidecar to populate
: "${FILES_WAIT_POLL_SECONDS:=5}"
: "${WINDROSE_CONFIG_MODE:=env}"
# Default R5NetDriver replication tick rate seeded on first boot if no
# Saved/Config/WindowsServer/Engine.ini exists yet. 60 is the sweet
# spot for Windrose's co-op sailing/combat — 2x smoother positional
# updates than stock 30 Hz, without the 4x bandwidth/CPU overhead of
# 120. Operators can override via env (this value) or by editing the
# generated Engine.ini directly; entrypoint only seeds if the file is
# absent, never overwrites an existing one.
: "${NET_SERVER_MAX_TICK_RATE:=60}"
# How to get the server binary. `steamcmd` (default) pulls app id 4129620
# anonymously on every boot — auto-patches. `files` skips SteamCMD and
# waits for the operator to populate ${WINDROSE_SERVER_DIR} via the UI
# upload or kubectl cp (the previous BYO behavior).
: "${WINDROSE_SERVER_SOURCE:=steamcmd}"

if [ "${ENTRYPOINT_SLEEP:-0}" = "1" ]; then
  echo "$(timestamp) INFO: ENTRYPOINT_SLEEP=1; holding container for diagnostics"
  exec sleep infinity
fi

init_steamcmd() {
  mkdir -p "${STEAMCMD_PATH}" "${STEAMCMD_PATH}/compatibilitytools.d" "${STEAMCMD_PATH}/steamapps/compatdata" "${HOME}/.steam"
  if [ ! -x "${STEAMCMD_PATH}/steamcmd.sh" ]; then
    echo "$(timestamp) INFO: Installing SteamCMD runtime (for steamclient.so only; no app depot download)"
    curl -sqL https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz | tar zxf - -C "${STEAMCMD_PATH}"
    chmod +x "${STEAMCMD_PATH}/steamcmd.sh"
  fi
  mkdir -p "${STEAMCMD_PATH}/linux64" "${STEAMCMD_PATH}/linux32"
  rm -rf "${STEAM_SDK64_PATH}" "${STEAM_SDK32_PATH}"
  ln -snf "${STEAMCMD_PATH}/linux64" "${STEAM_SDK64_PATH}"
  ln -snf "${STEAMCMD_PATH}/linux32" "${STEAM_SDK32_PATH}"
  ln -snf "${STEAM_SDK64_PATH}/steamclient.so" "${STEAM_SDK64_PATH}/steamservice.so"
  ln -snf "${STEAM_SDK32_PATH}/steamclient.so" "${STEAM_SDK32_PATH}/steamservice.so"
}

init_proton() {
  local proton_dir="${STEAMCMD_PATH}/compatibilitytools.d/GE-Proton${GE_PROTON_VERSION}"
  local seed_dir="${GE_PROTON_SEED_ROOT}/GE-Proton${GE_PROTON_VERSION}"
  if [ ! -x "${proton_dir}/proton" ]; then
    mkdir -p "${STEAMCMD_PATH}/compatibilitytools.d"
    if [ -x "${seed_dir}/proton" ]; then
      echo "$(timestamp) INFO: Seeding GE-Proton ${GE_PROTON_VERSION} from image cache"
      cp -a "${seed_dir}" "${proton_dir}"
      return 0
    fi
    echo "$(timestamp) INFO: Downloading GE-Proton ${GE_PROTON_VERSION}"
    curl -sqL "${GE_PROTON_URL}" | tar zxf - -C "${STEAMCMD_PATH}/compatibilitytools.d/"
  fi
}

server_files_present() {
  [ -f "${WINDROSE_SERVER_DIR}/WindroseServer.exe" ] \
    || [ -f "${WINDROSE_SERVER_DIR}/R5/Binaries/Win64/WindroseServer-Win64-Shipping.exe" ]
}

# Pull (and keep up-to-date) the Windrose Dedicated Server via SteamCMD
# app id 4129620. This is anonymous-downloadable — no Steam account /
# license required. Preserves R5/Saved and R5/ServerDescription.json
# across updates (identity + world). Returns 0 on success, 1 on failure
# so the caller can fall back to files-import.
install_via_steamcmd() {
  local app_id="${WINDROSE_STEAM_APP_ID:-4129620}"
  local exe="${WINDROSE_SERVER_DIR}/R5/Binaries/Win64/WindroseServer-Win64-Shipping.exe"

  # Patch-preservation short-circuit: if the idle-CPU patch is active
  # and the binary is already patched, skip SteamCMD. Empirically, each
  # SteamCMD run reverts the patch ~every other boot (Steam detects
  # the patched bytes don't match its cached manifest and "corrects"
  # them), so the entrypoint was re-patching on every restart. Skipping
  # SteamCMD when the desired state already exists eliminates that
  # thrash. WINDROSE_STEAMCMD_FORCE=1 forces a normal run — use it
  # when you want to pull a Windrose game update.
  if [ "${WINDROSE_STEAMCMD_FORCE:-0}" != "1" ] && [ -f "${exe}" ] \
     && command -v python3 >/dev/null 2>&1; then
    local override_file="${WINDROSE_PATCH_OVERRIDE_FILE:-${WINDROSE_SERVER_DIR}/R5/.idle-patch-override}"
    local override=""
    [ -f "${override_file}" ] && override="$(tr -d '[:space:]' < "${override_file}" | head -c 16)"
    local effective="${WINDROSE_PATCH_IDLE_CPU:-0}"
    [ "${override}" = "enabled" ] && effective="1"
    [ "${override}" = "disabled" ] && effective="0"
    if [ "${effective}" = "1" ]; then
      local script
      script="$(_find_patch_script)"
      if [ -n "${script}" ]; then
        local state
        state="$(python3 "${script}" "${exe}" --print-state 2>/dev/null | jq -r '.state // "unknown"' 2>/dev/null)"
        if [ "${state}" = "patched" ]; then
          echo "$(timestamp) INFO: Binary already patched (idle-CPU) — skipping SteamCMD to preserve the patch. Set WINDROSE_STEAMCMD_FORCE=1 to pull a Windrose update."
          return 0
        fi
      fi
    fi
  fi

  # validate re-hashes every file — slow + not needed unless operator is
  # debugging corruption. Default off; set WINDROSE_STEAMCMD_VALIDATE=1
  # to enable. Also forced on if we see a partial install (dir exists but
  # binary missing).
  local validate_arg=""
  if [ "${WINDROSE_STEAMCMD_VALIDATE:-0}" = "1" ]; then
    validate_arg="validate"
  elif [ -d "${WINDROSE_SERVER_DIR}" ] \
       && [ ! -f "${exe}" ] \
       && [ -n "$(ls -A "${WINDROSE_SERVER_DIR}" 2>/dev/null)" ]; then
    # Partial install detected; force validate to clean it up.
    validate_arg="validate"
  fi

  local preserve_dir="/tmp/windrose-preserve-$$"
  mkdir -p "${preserve_dir}"

  echo "$(timestamp) INFO: Stashing identity + save before SteamCMD app_update"
  if [ -d "${WINDROSE_SERVER_DIR}/R5/Saved" ]; then
    mv "${WINDROSE_SERVER_DIR}/R5/Saved" "${preserve_dir}/Saved"
  fi
  for f in ServerDescription.json WorldDescription.json; do
    if [ -f "${WINDROSE_SERVER_DIR}/R5/${f}" ]; then
      cp -a "${WINDROSE_SERVER_DIR}/R5/${f}" "${preserve_dir}/${f}"
    fi
  done

  echo "$(timestamp) INFO: SteamCMD +app_update ${app_id} ${validate_arg:-(no-validate)}"
  mkdir -p "${WINDROSE_SERVER_DIR}"
  # Arg order matters: +force_install_dir MUST precede +login anonymous.
  # Valve's client literally prints "Please use force_install_dir before
  # logon!" and fails app_update with "Missing configuration" otherwise.
  # (Caught on an Ubuntu 24.04 bare-Linux install where the order-invert
  # was a hard failure; Debian-13-in-container happens to tolerate the
  # wrong order sometimes but don't count on it.)
  local rc=0
  "${STEAMCMD_PATH}/steamcmd.sh" \
    +force_install_dir "${WINDROSE_SERVER_DIR}" \
    +login anonymous \
    +app_update "${app_id}" ${validate_arg} \
    +quit || rc=$?

  echo "$(timestamp) INFO: Restoring identity + save from ${preserve_dir}"
  mkdir -p "${WINDROSE_SERVER_DIR}/R5"
  if [ -d "${preserve_dir}/Saved" ]; then
    rm -rf "${WINDROSE_SERVER_DIR}/R5/Saved"
    mv "${preserve_dir}/Saved" "${WINDROSE_SERVER_DIR}/R5/Saved"
  fi
  for f in ServerDescription.json WorldDescription.json; do
    if [ -f "${preserve_dir}/${f}" ]; then
      cp -a "${preserve_dir}/${f}" "${WINDROSE_SERVER_DIR}/R5/${f}"
    fi
  done
  rm -rf "${preserve_dir}"

  if [ "${rc}" -ne 0 ]; then
    echo "$(timestamp) ERROR: SteamCMD exited with ${rc}"
    return 1
  fi
  if ! server_files_present; then
    echo "$(timestamp) ERROR: SteamCMD succeeded but server binary not found at ${WINDROSE_SERVER_DIR}"
    return 1
  fi
  echo "$(timestamp) INFO: SteamCMD install complete at ${WINDROSE_SERVER_DIR}"
  return 0
}

wait_for_files() {
  if server_files_present; then
    echo "$(timestamp) INFO: WindowsServer files present at ${WINDROSE_SERVER_DIR}"
    return 0
  fi
  echo "$(timestamp) INFO: WindowsServer files not present; waiting for UI sidecar (or kubectl cp) to populate ${WINDROSE_SERVER_DIR}"
  local waited=0
  while true; do
    if server_files_present; then
      echo "$(timestamp) INFO: WindowsServer files appeared after ${waited}s"
      return 0
    fi
    if [ "${FILES_WAIT_TIMEOUT_SECONDS}" -gt 0 ] && [ "${waited}" -ge "${FILES_WAIT_TIMEOUT_SECONDS}" ]; then
      echo "$(timestamp) ERROR: WindowsServer files still missing after ${waited}s (FILES_WAIT_TIMEOUT_SECONDS=${FILES_WAIT_TIMEOUT_SECONDS})"
      exit 1
    fi
    sleep "${FILES_WAIT_POLL_SECONDS}"
    waited=$((waited + FILES_WAIT_POLL_SECONDS))
  done
}

maybe_disable_sentry() {
  : "${DISABLE_SENTRY:=1}"
  local sentry_dir="${WINDROSE_SERVER_DIR}/R5/Plugins/3rdParty/Sentry"
  local disabled_dir="${WINDROSE_SERVER_DIR}/R5/Plugins/3rdParty/Sentry.DISABLED"
  if [ "${DISABLE_SENTRY}" != "1" ]; then
    return 0
  fi
  if [ -d "${sentry_dir}" ]; then
    echo "$(timestamp) INFO: Disabling Sentry plugin (Crashpad hard-aborts the process under Proton; set DISABLE_SENTRY=0 to keep it)"
    rm -rf "${disabled_dir}"
    mv "${sentry_dir}" "${disabled_dir}"
  fi
}

# Optional: patch the shipping EXE to throttle the idle-spin loop in
# `boost::asio::detail::socket_select_interrupter::reset()`. Without this,
# two game threads burn ~91% CPU each when no player is connected (pure
# userspace busy-loop on an Asio socket-pair drain; confirmed via
# strace showing 0 syscalls in 3 s on either thread). The patch injects
# a `Sleep(1)` call at the loop-continue tail — each iteration now
# yields to the kernel via pselect6, cutting idle CPU from ~200% to ~5%
# on the canary. Isolated to 43 bytes (5 at the site + 38 in CC padding);
# auto-derive locates the patch site via a unique 9-byte signature and
# parses PE imports to find KERNEL32!Sleep; reverts cleanly via
# `--revert`. See `scripts/patch-idle-cpu.py` for the derivation details.
#
# Off by default — operator opts in by setting WINDROSE_PATCH_IDLE_CPU=1.
# The UI can override this decision without a helm roll by writing
# `disabled` to `$WINDROSE_PATCH_OVERRIDE_FILE` (default
# `$WINDROSE_SERVER_DIR/R5/.idle-patch-override`); that takes effect on
# next container restart. Failures (missing python3, signature not
# found, etc.) are warnings — we'd rather boot with a busy server than
# not boot at all.
maybe_patch_idle_cpu() {
  : "${WINDROSE_PATCH_IDLE_CPU:=0}"
  local exe="${WINDROSE_SERVER_DIR}/R5/Binaries/Win64/WindroseServer-Win64-Shipping.exe"
  local override_file="${WINDROSE_PATCH_OVERRIDE_FILE:-${WINDROSE_SERVER_DIR}/R5/.idle-patch-override}"

  # UI override takes precedence over env var so operators can flip the
  # patch off without a helm/values round-trip. `disabled` forces OFF;
  # `enabled` forces ON (on top of env=0); any other content is ignored.
  local override=""
  if [ -f "${override_file}" ]; then
    override="$(tr -d '[:space:]' < "${override_file}" | head -c 16)"
  fi
  local effective="${WINDROSE_PATCH_IDLE_CPU}"
  if [ "${override}" = "disabled" ]; then
    effective="0"
    echo "$(timestamp) INFO: Idle-CPU patch override='disabled' at ${override_file}; forcing OFF"
  elif [ "${override}" = "enabled" ]; then
    effective="1"
    echo "$(timestamp) INFO: Idle-CPU patch override='enabled' at ${override_file}; forcing ON"
  fi

  if [ "${effective}" != "1" ]; then
    # Whenever the effective mode is OFF, try to revert so a previously
    # patched binary doesn't stay patched through env changes / override
    # flips / a fresh SteamCMD pull landing onto an already-patched tree.
    # --revert --idempotent is a no-op on an unpatched binary, so it's
    # safe to invoke unconditionally.
    if [ -f "${exe}" ]; then
      local script
      script="$(_find_patch_script)"
      if [ -n "${script}" ] && command -v python3 >/dev/null 2>&1; then
        python3 "${script}" "${exe}" --revert --idempotent 2>&1 | \
          sed "s/^/$(timestamp) patch: /" || true
      fi
    fi
    return 0
  fi

  local script
  script="$(_find_patch_script)"
  if [ ! -f "${exe}" ]; then
    echo "$(timestamp) WARNING: Idle-CPU patch requested but ${exe} is missing"
    return 0
  fi
  if [ -z "${script}" ]; then
    echo "$(timestamp) WARNING: Idle-CPU patch requested but patch-idle-cpu.py not found"
    return 0
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    echo "$(timestamp) WARNING: Idle-CPU patch requested but python3 not available"
    return 0
  fi

  echo "$(timestamp) INFO: Applying idle-CPU patch via ${script} (auto-derive; drops idle CPU from ~200% to ~5%)"
  if ! python3 "${script}" "${exe}" --idempotent; then
    echo "$(timestamp) WARNING: Idle-CPU patch failed; binary left unmodified"
  fi
}

_find_patch_script() {
  # In-image location first, then alongside the entrypoint (bare-Linux
  # installs co-locate them under /opt/windrose/scripts/).
  local candidates=(
    "/usr/local/bin/patch-idle-cpu.py"
    "$(dirname "$0")/patch-idle-cpu.py"
  )
  for c in "${candidates[@]}"; do
    if [ -f "${c}" ]; then
      echo "${c}"
      return 0
    fi
  done
  echo ""
}

detect_save_version() {
  local root="${WINDROSE_SAVE_ROOT}"
  [ -d "${root}" ] || { echo ""; return; }
  find "${root}" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null | sort -V | tail -n1
}

migrate_saves_on_version_change() {
  local root="${WINDROSE_SAVE_ROOT}"
  [ -d "${root}" ] || return 0
  mapfile -t versions < <(find "${root}" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null | sort -V)
  if [ "${#versions[@]}" -lt 2 ]; then return 0; fi
  local newest="${versions[-1]}"
  local prev="${versions[-2]}"
  local new_worlds="${root}/${newest}/Worlds"
  local prev_worlds="${root}/${prev}/Worlds"
  [ -d "${prev_worlds}" ] || return 0
  mkdir -p "${new_worlds}"
  local wd name
  for wd in "${prev_worlds}"/*/; do
    [ -d "${wd}" ] || continue
    name="$(basename "${wd}")"
    if [ ! -d "${new_worlds}/${name}" ]; then
      echo "$(timestamp) INFO: Migrating world ${name} from ${prev} -> ${newest}"
      cp -a "${wd}" "${new_worlds}/"
    fi
  done
}

ensure_world_layout() {
  local version
  version="$(detect_save_version)"
  if [ -z "${version}" ]; then
    echo "$(timestamp) INFO: No save version found; letting the server bootstrap its first world"
    return 0
  fi
  if [ "${WINDROSE_CONFIG_MODE}" != "env" ]; then
    return 0
  fi

  # Resolve the single world this boot's env vars apply to. The older
  # behavior looped over every WorldDescription.json on disk and
  # patched the WorldName / WorldPresetType whenever WORLD_ISLAND_ID
  # was either unset or left at the sentinel "default-world" — which
  # silently cross-contaminated every inactive world's metadata with
  # the active world's SERVER_NAME / WORLD_NAME. On multi-world save
  # trees (e.g. operators who generated a second world via the UI)
  # that clobbers names across restarts.
  #
  # Resolution order:
  #   1. explicit WORLD_ISLAND_ID env (if set and not the sentinel)
  #   2. ServerDescription.json's WorldIslandId (backend-authoritative)
  # If neither resolves we skip patching — the UI editor becomes the
  # authoritative path for per-world metadata.
  local target_island=""
  if [ -n "${WORLD_ISLAND_ID}" ] && [ "${WORLD_ISLAND_ID}" != "default-world" ]; then
    target_island="${WORLD_ISLAND_ID}"
  elif [ -f "${WINDROSE_SERVER_CONFIG}" ]; then
    target_island="$(jq -r '.ServerDescription_Persistent.WorldIslandId // ""' "${WINDROSE_SERVER_CONFIG}" 2>/dev/null)"
  fi
  if [ -z "${target_island}" ]; then
    echo "$(timestamp) INFO: No target WorldIslandId resolved (WORLD_ISLAND_ID unset + no ServerDescription.json yet); skipping WorldDescription env-mode patch"
    return 0
  fi

  local world_desc island_dir island_id patched=0
  while IFS= read -r world_desc; do
    [ -z "${world_desc}" ] && continue
    island_dir="$(dirname "${world_desc}")"
    island_id="$(basename "${island_dir}")"
    if [ "${island_id}" != "${target_island}" ]; then
      continue
    fi
    # Shadow-stamp WorldDescription just like ServerDescription — so a
    # UI-edited WorldName survives restarts. Shadow at
    # WorldDescription.last-env-stamp.json next to the live file. First
    # boot (no shadow) treats live as operator-owned; per-key skip when
    # live diverges from the last env intent we recorded.
    local wshadow="${world_desc%.*}.last-env-stamp.json"
    local wfirst="false"
    if [ ! -f "${wshadow}" ]; then
      wfirst="true"
      echo "$(timestamp) INFO: env-mode: no WorldDescription shadow at ${wshadow} — treating live as operator-owned; seeding shadow from env intent"
    fi
    local wfilter=''
    local ws_name="false" ws_preset="false"
    _world_keep() {
      if [ "${wfirst}" = "true" ]; then echo "true"; return; fi
      local live shadowed
      live="$(jq -r "$1" "${world_desc}" 2>/dev/null)"
      shadowed="$(jq -r "$1" "${wshadow}" 2>/dev/null)"
      if [ "${live}" != "${shadowed}" ]; then echo "true"; else echo "false"; fi
    }
    if [ "$(_world_keep '.WorldDescription.WorldName')" = "false" ]; then
      ws_name="true"
      wfilter=".WorldDescription.WorldName = \$name"
    elif [ "${wfirst}" != "true" ]; then
      echo "$(timestamp) INFO: env-mode: keeping operator-modified WorldName for ${island_id}"
    fi
    if [ "$(_world_keep '.WorldDescription.WorldPresetType')" = "false" ]; then
      ws_preset="true"
      wfilter="${wfilter}${wfilter:+ | }.WorldDescription.WorldPresetType = \$preset"
    elif [ "${wfirst}" != "true" ]; then
      echo "$(timestamp) INFO: env-mode: keeping operator-modified WorldPresetType for ${island_id}"
    fi
    if [ -n "${wfilter}" ]; then
      echo "$(timestamp) INFO: Patching WorldDescription at ${world_desc} (active island ${target_island})"
      jq --arg name "${WORLD_NAME}" --arg preset "${WORLD_PRESET_TYPE}" \
        "${wfilter}" "${world_desc}" > "${world_desc}.tmp" \
        && mv "${world_desc}.tmp" "${world_desc}"
    fi
    # Refresh world shadow: stamp-or-first-boot sets to env intent,
    # skip preserves old shadow value (divergence sticks).
    local wbase='{}'
    if [ -f "${wshadow}" ] && jq empty "${wshadow}" >/dev/null 2>&1; then
      wbase="$(cat "${wshadow}")"
    fi
    jq -n --argjson base "${wbase}" --arg name "${WORLD_NAME}" --arg preset "${WORLD_PRESET_TYPE}" \
      --arg first "${wfirst}" --arg sn "${ws_name}" --arg sp "${ws_preset}" '
      ($base.WorldDescription // {}) as $b
      | ($first == "true") as $seed
      | { WorldDescription: ($b
          | (if $seed or $sn == "true" then .WorldName       = $name   else . end)
          | (if $seed or $sp == "true" then .WorldPresetType = $preset else . end)
        ) }' > "${wshadow}.tmp" && mv "${wshadow}.tmp" "${wshadow}"
    patched=1
  done < <(find "${WINDROSE_SAVE_ROOT}/${version}/Worlds" -maxdepth 2 -name 'WorldDescription.json' 2>/dev/null)

  if [ "${patched}" -eq 0 ]; then
    echo "$(timestamp) INFO: Active island ${target_island} has no WorldDescription.json yet; the game will bootstrap it"
  fi
}

reconcile_server_config() {
  case "${WINDROSE_CONFIG_MODE}" in
    env)
      if [ ! -f "${WINDROSE_SERVER_CONFIG}" ]; then
        echo "$(timestamp) INFO: No ServerDescription.json; letting the server generate one on first launch"
        return 0
      fi
      local protected_bool
      case "${IS_PASSWORD_PROTECTED}" in
        true|True|TRUE|1|yes) protected_bool="true" ;;
        *) protected_bool="false" ;;
      esac

      # Shadow-stamp: only overwrite a key if the on-disk value matches
      # what we wrote for it last boot. If it differs, the operator (or
      # the UI config editor, or an ldb edit) changed it — respect that
      # and skip the re-stamp. Shadow lives at
      # ServerDescription.last-env-stamp.json. For SKIPPED keys we
      # preserve the old shadow value — this keeps the divergence marker
      # alive across boots so we don't re-capture the operator's value
      # into shadow and then clobber it the *following* boot.
      local shadow="${WINDROSE_SERVER_CONFIG%.*}.last-env-stamp.json"
      local first_boot="false"
      if [ ! -f "${shadow}" ]; then
        first_boot="true"
        echo "$(timestamp) INFO: env-mode: no shadow stamp present — treating live values as operator-owned and seeding shadow from env intent. Future boots will re-stamp keys that still match env."
      fi

      # Decide per-key: should we skip (operator diverged) or stamp?
      # Output: "true" = skip, "false" = stamp.
      #
      # Upgrade-safety: on a first boot (no shadow yet) we treat every
      # key as "skip" so we don't clobber whatever the operator may have
      # customized on an existing install. The shadow still gets seeded
      # with env values below, so on the SECOND boot we have a baseline
      # to compare live against: if live still matches env intent, we
      # stamp normally; if operator diverged, we preserve.
      _env_keep_key() {
        local path="$1" kind="$2"
        if [ "${first_boot}" = "true" ]; then echo "true"; return; fi
        local live shadowed
        case "${kind}" in
          string)
            live="$(jq -r "${path}" "${WINDROSE_SERVER_CONFIG}" 2>/dev/null)"
            shadowed="$(jq -r "${path}" "${shadow}" 2>/dev/null)"
            ;;
          bool|number)
            live="$(jq -c "${path}" "${WINDROSE_SERVER_CONFIG}" 2>/dev/null)"
            shadowed="$(jq -c "${path}" "${shadow}" 2>/dev/null)"
            ;;
        esac
        if [ "${live}" != "${shadowed}" ]; then echo "true"; else echo "false"; fi
      }

      # Per-key stamp decisions. bash-3-compatible parallel arrays
      # (associative arrays would need bash 4 declare -A, which we
      # have but prefer not to require).
      local jq_filter=''
      local stamp_pwprotected="false" stamp_password="false" stamp_name="false"
      local stamp_max="false" stamp_proxy="false" stamp_invite="false" stamp_island="false"
      _decide() {
        # Args: flag-var, jq-path, kind, clause, log-label.
        local flag_var="$1" path="$2" kind="$3" clause="$4" label="$5"
        local keep
        keep="$(_env_keep_key "${path}" "${kind}")"
        if [ "${keep}" = "false" ]; then
          printf -v "${flag_var}" 'true'
          jq_filter="${jq_filter}${jq_filter:+ | }${clause}"
        elif [ "${first_boot}" = "true" ]; then
          : # First boot noise — skip logging per-key on upgrade.
        else
          echo "$(timestamp) INFO: env-mode: keeping operator-modified ${label}"
        fi
      }
      _decide stamp_pwprotected '.ServerDescription_Persistent.IsPasswordProtected' bool \
        '.ServerDescription_Persistent.IsPasswordProtected = $protected' 'IsPasswordProtected'
      _decide stamp_password '.ServerDescription_Persistent.Password' string \
        '.ServerDescription_Persistent.Password = $password' 'Password'
      _decide stamp_name '.ServerDescription_Persistent.ServerName' string \
        '.ServerDescription_Persistent.ServerName = $name' 'ServerName'
      _decide stamp_max '.ServerDescription_Persistent.MaxPlayerCount' number \
        '.ServerDescription_Persistent.MaxPlayerCount = $maxPlayers' 'MaxPlayerCount'
      _decide stamp_proxy '.ServerDescription_Persistent.P2pProxyAddress' string \
        '.ServerDescription_Persistent.P2pProxyAddress = $proxy' 'P2pProxyAddress'
      if [ -n "${INVITE_CODE}" ]; then
        _decide stamp_invite '.ServerDescription_Persistent.InviteCode' string \
          '.ServerDescription_Persistent.InviteCode = $invite' 'InviteCode'
      fi
      if [ -n "${WORLD_ISLAND_ID}" ] && [ "${WORLD_ISLAND_ID}" != "default-world" ]; then
        _decide stamp_island '.ServerDescription_Persistent.WorldIslandId' string \
          '.ServerDescription_Persistent.WorldIslandId = $islandId' 'WorldIslandId'
      fi

      if [ -n "${jq_filter}" ]; then
        jq \
          --arg name "${SERVER_NAME}" \
          --arg invite "${INVITE_CODE}" \
          --argjson protected "${protected_bool}" \
          --arg password "${SERVER_PASSWORD}" \
          --arg islandId "${WORLD_ISLAND_ID}" \
          --argjson maxPlayers "${MAX_PLAYER_COUNT}" \
          --arg proxy "${P2P_PROXY_ADDRESS}" \
          "${jq_filter}" \
          "${WINDROSE_SERVER_CONFIG}" > "${WINDROSE_SERVER_CONFIG}.tmp" \
          && mv "${WINDROSE_SERVER_CONFIG}.tmp" "${WINDROSE_SERVER_CONFIG}"
      fi

      # Refresh shadow: for each key we STAMPED, record the env value we
      # wrote. For each key we SKIPPED, preserve the old shadow value so
      # the divergence marker survives. On first boot we seed every key.
      # jq empty guard defends against a malformed shadow from a partial
      # write or manual edit — a corrupt base would otherwise break boot.
      local shadow_base='{}'
      if [ -f "${shadow}" ] && jq empty "${shadow}" >/dev/null 2>&1; then
        shadow_base="$(cat "${shadow}")"
      elif [ -f "${shadow}" ]; then
        echo "$(timestamp) WARNING: env-mode: shadow at ${shadow} is not valid JSON; treating as absent"
        first_boot="true"
      fi
      jq -n \
        --argjson base "${shadow_base}" \
        --arg name "${SERVER_NAME}" \
        --arg invite "${INVITE_CODE}" \
        --argjson protected "${protected_bool}" \
        --arg password "${SERVER_PASSWORD}" \
        --arg islandId "${WORLD_ISLAND_ID}" \
        --argjson maxPlayers "${MAX_PLAYER_COUNT}" \
        --arg proxy "${P2P_PROXY_ADDRESS}" \
        --arg first "${first_boot}" \
        --arg s_pwp "${stamp_pwprotected}" \
        --arg s_pw "${stamp_password}" \
        --arg s_name "${stamp_name}" \
        --arg s_max "${stamp_max}" \
        --arg s_proxy "${stamp_proxy}" \
        --arg s_invite "${stamp_invite}" \
        --arg s_island "${stamp_island}" '
        ($base.ServerDescription_Persistent // {}) as $b
        | ($first == "true") as $seed
        | { ServerDescription_Persistent: (
            $b
            | (if $seed or $s_pwp   == "true" then .IsPasswordProtected = $protected else . end)
            | (if $seed or $s_pw    == "true" then .Password            = $password  else . end)
            | (if $seed or $s_name  == "true" then .ServerName          = $name      else . end)
            | (if $seed or $s_max   == "true" then .MaxPlayerCount      = $maxPlayers else . end)
            | (if $seed or $s_proxy == "true" then .P2pProxyAddress     = $proxy     else . end)
            | (if $s_invite == "true"                then .InviteCode    = $invite   else . end)
            | (if $s_island == "true"                then .WorldIslandId = $islandId else . end)
          ) }' > "${shadow}.tmp" && mv "${shadow}.tmp" "${shadow}"
      ;;
    managed)
      : "${WINDROSE_MANAGED_CONFIG_TEMPLATE:?managed mode requires WINDROSE_MANAGED_CONFIG_TEMPLATE}"
      [ -f "${WINDROSE_MANAGED_CONFIG_TEMPLATE}" ] || { echo "$(timestamp) ERROR: managed template not found at ${WINDROSE_MANAGED_CONFIG_TEMPLATE}"; exit 1; }
      cp "${WINDROSE_MANAGED_CONFIG_TEMPLATE}" "${WINDROSE_SERVER_CONFIG}"
      if [ -n "${WINDROSE_MANAGED_CONFIG_PASSWORD_FILE:-}" ] && [ -f "${WINDROSE_MANAGED_CONFIG_PASSWORD_FILE}" ]; then
        local pw
        pw="$(cat "${WINDROSE_MANAGED_CONFIG_PASSWORD_FILE}")"
        jq --arg pw "${pw}" \
          '.ServerDescription_Persistent.Password = $pw | .ServerDescription_Persistent.IsPasswordProtected = (($pw | length) > 0)' \
          "${WINDROSE_SERVER_CONFIG}" > "${WINDROSE_SERVER_CONFIG}.tmp" && mv "${WINDROSE_SERVER_CONFIG}.tmp" "${WINDROSE_SERVER_CONFIG}"
      fi
      ;;
    mutable)
      [ -f "${WINDROSE_SERVER_CONFIG}" ] || { echo "$(timestamp) ERROR: mutable mode requires existing ${WINDROSE_SERVER_CONFIG}"; exit 1; }
      ;;
    *)
      echo "$(timestamp) ERROR: unsupported WINDROSE_CONFIG_MODE=${WINDROSE_CONFIG_MODE}"
      exit 1
      ;;
  esac
}

# ---- MAIN ----

mkdir -p "${WINDROSE_PATH}" "${STEAM_COMPAT_DATA_PATH}" "${STEAM_COMPAT_DATA_PATH}/pfx"
init_steamcmd
init_proton
mkdir -p "${STEAM_COMPAT_DATA_PATH}" "${STEAM_COMPAT_DATA_PATH}/pfx"

case "${WINDROSE_SERVER_SOURCE}" in
  steamcmd)
    if ! install_via_steamcmd; then
      echo "$(timestamp) WARNING: SteamCMD install failed; falling back to files-import. Set WINDROSE_SERVER_SOURCE=files to skip SteamCMD entirely."
    fi
    ;;
  files)
    echo "$(timestamp) INFO: WINDROSE_SERVER_SOURCE=files; skipping SteamCMD and waiting for the operator to populate ${WINDROSE_SERVER_DIR}"
    ;;
  *)
    echo "$(timestamp) ERROR: unsupported WINDROSE_SERVER_SOURCE=${WINDROSE_SERVER_SOURCE} (expected steamcmd or files)"
    exit 1
    ;;
esac

wait_for_files

# Maintenance mode: UI can toggle this to keep the server stopped
# across restarts without touching values / systemd / docker-compose.
# We loop-sleep so the container stays up but the game binary never
# launches — operator clears the flag (UI button or rm the file) and
# the next restart boots normally.
maint_flag="${WINDROSE_MAINTENANCE_FLAG_FILE:-${WINDROSE_SERVER_DIR}/R5/.maintenance-mode}"
if [ -f "${maint_flag}" ]; then
  echo "$(timestamp) INFO: Maintenance mode active (${maint_flag} present); not launching the game. Clear the flag from the admin UI or 'rm ${maint_flag}' and restart."
  # Stay in the foreground so systemd/kubelet/docker doesn't mark the
  # service failed and restart-loop. SIGTERM from the supervisor wakes
  # us up cleanly.
  trap 'echo "$(timestamp) INFO: SIGTERM received in maintenance mode; exiting."; exit 0' TERM INT
  while [ -f "${maint_flag}" ]; do sleep 30 & wait $!; done
  echo "$(timestamp) INFO: Maintenance flag cleared; proceeding with normal boot."
fi

migrate_saves_on_version_change
ensure_world_layout
reconcile_server_config
maybe_disable_sentry
maybe_patch_idle_cpu

# Seed Engine.ini with the project default net tick rate on first
# boot only. Windrose uses UR5NetDriver, not stock UIpNetDriver, so
# we target /Script/R5SocketSubsystem.R5NetDriver. Stock IpNetDriver
# section mirrored as belt-and-suspenders. Does NOT overwrite an
# operator-customized file.
engine_ini="${WINDROSE_SERVER_DIR}/R5/Saved/Config/WindowsServer/Engine.ini"
if [ ! -f "${engine_ini}" ]; then
  mkdir -p "$(dirname "${engine_ini}")"
  cat > "${engine_ini}" <<EOF
; Seeded by windrose-self-hosted entrypoint on $(date -uIseconds).
; Default R5NetDriver tick rate — 60 Hz is the project-wide default
; (2x smoother than stock 30 Hz, low CPU/bandwidth cost on co-op
; workloads). Override via NET_SERVER_MAX_TICK_RATE env var on the
; next pristine install, or edit this file directly — the entrypoint
; only seeds when the file is absent.
[/Script/R5SocketSubsystem.R5NetDriver]
NetServerMaxTickRate=${NET_SERVER_MAX_TICK_RATE}

[/Script/OnlineSubsystemUtils.IpNetDriver]
NetServerMaxTickRate=${NET_SERVER_MAX_TICK_RATE}
EOF
  echo "$(timestamp) INFO: Seeded ${engine_ini} with NetServerMaxTickRate=${NET_SERVER_MAX_TICK_RATE}"
fi

if [ "${WINDROSE_LAUNCH_STRATEGY}" = "launcher" ]; then
  EXE="${WINDROSE_SERVER_DIR}/WindroseServer.exe"
else
  EXE="${WINDROSE_SERVER_DIR}/R5/Binaries/Win64/WindroseServer-Win64-Shipping.exe"
fi
if [ ! -f "${EXE}" ]; then
  echo "$(timestamp) ERROR: Launch binary not found at ${EXE}"
  exit 1
fi

export WINEDEBUG="${WINEDEBUG:--all}"
echo "$(timestamp) INFO: Startup config $(jq -c \
  --arg configMode "${WINDROSE_CONFIG_MODE}" \
  --arg launchStrategy "${WINDROSE_LAUNCH_STRATEGY}" \
  --arg worldIslandId "${WORLD_ISLAND_ID}" \
  --arg exe "${EXE}" \
  -n '{
    process: "windrose-server",
    configMode: $configMode,
    launchStrategy: $launchStrategy,
    worldIslandId: $worldIslandId,
    exe: $exe
  }')"

# Xvfb is a sibling sidecar container; we share its X11 socket via an emptyDir
# at /tmp/.X11-unix. DISPLAY defaults to :99 (matching the sidecar).
# Wait briefly for the socket so wine doesn't race the display.
export DISPLAY="${DISPLAY:-:99}"
display_num="${DISPLAY#:}"
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if [ -S "/tmp/.X11-unix/X${display_num}" ]; then break; fi
  sleep 1
done
if [ ! -S "/tmp/.X11-unix/X${display_num}" ]; then
  echo "$(timestamp) WARNING: X11 socket /tmp/.X11-unix/X${display_num} not found; xvfb sidecar may not be running"
fi

# Extra launch args for the game binary. Default caps the engine tick to
# 60 FPS — the patch (scripts/patch-idle-cpu.py) is the real fix for the
# unpaced-main-loop spin; this cap is belt-and-braces for hosts that
# don't run the patch. Matches the Helm chart default so compose /
# plain-manifest / bare-Linux deployments all behave identically. Set
# empty to uncap, or to "-FPS=30" for the older behavior.
: "${SERVER_LAUNCH_ARGS:=-FPS=60}"
read -r -a launch_args <<< "${SERVER_LAUNCH_ARGS}"

# Stream R5.log to this container's stderr so `kubectl logs` / `docker logs`
# surface the game's own log output (backend register, ICE negotiation,
# player join/leave, fatal asserts). Without this, kubectl logs shows only
# the entrypoint + Proton prelude and goes silent — everything useful lives
# in R5/Saved/Logs/R5.log on the PVC, invisible to the container runtime.
#
# `tail -F` follows by path: when the game rotates R5.log (closes and opens
# a new one with the old path renamed to R5-backup-<ts>.log), tail reopens
# and picks up the fresh file automatically. The log doesn't exist yet when
# we launch; -F handles that via retry-until-appears.
#
# Backgrounding one child (tail) before `exec proton` is safe — proton takes
# PID 1 via exec and owns the tail as its only child. This is a different
# situation from the earlier Xvfb-as-background-job problem, which was a
# SIGCHLD race in the same shell as proton-also-backgrounded. Here the
# shell is gone after exec; nothing to race.
R5_LOG="${WINDROSE_SERVER_DIR}/R5/Saved/Logs/R5.log"
mkdir -p "$(dirname "${R5_LOG}")"
( tail -n 0 -F "${R5_LOG}" 2>/dev/null | sed -u 's/^/[R5.log] /' >&2 ) &

echo "$(timestamp) INFO: exec'ing Proton (becomes PID 1; Xvfb is in the xvfb sidecar; R5.log tail is streaming to stderr). Launch args: -log ${SERVER_LAUNCH_ARGS}"
cd "${WINDROSE_SERVER_DIR}"
exec "${STEAMCMD_PATH}/compatibilitytools.d/GE-Proton${GE_PROTON_VERSION}/proton" \
  waitforexitandrun "${EXE}" -log "${launch_args[@]}"
