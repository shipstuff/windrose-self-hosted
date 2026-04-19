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
  # validate re-hashes every file — slow + not needed unless operator is
  # debugging corruption. Default off; set WINDROSE_STEAMCMD_VALIDATE=1
  # to enable. Also forced on if we see a partial install (dir exists but
  # binary missing).
  local validate_arg=""
  if [ "${WINDROSE_STEAMCMD_VALIDATE:-0}" = "1" ]; then
    validate_arg="validate"
  elif [ -d "${WINDROSE_SERVER_DIR}" ] \
       && [ ! -f "${WINDROSE_SERVER_DIR}/R5/Binaries/Win64/WindroseServer-Win64-Shipping.exe" ] \
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
# on the canary. Patch is idempotent (script rejects already-patched
# binary), isolated to 43 bytes (5 at the site + 38 in CC padding),
# and reverts cleanly via `--revert`. See
# `tools/patch-idle-cpu.py` for the derivation + rollback path.
#
# Off by default — operator opts in by setting WINDROSE_PATCH_IDLE_CPU=1.
# When ON, failure to patch (wrong build md5, missing python3) is a
# warning, not a fatal: we'd rather boot with a busy server than not
# boot at all.
maybe_patch_idle_cpu() {
  : "${WINDROSE_PATCH_IDLE_CPU:=0}"
  if [ "${WINDROSE_PATCH_IDLE_CPU}" != "1" ]; then
    return 0
  fi
  local exe="${WINDROSE_SERVER_DIR}/R5/Binaries/Win64/WindroseServer-Win64-Shipping.exe"
  # In-image location (installed by Dockerfile to /usr/local/bin).
  local script="/usr/local/bin/patch-idle-cpu.py"
  if [ ! -f "${script}" ]; then
    # Fall back to tools/ in the repo — useful for bare-Linux installs
    # where the operator ran the installer from a repo checkout.
    script="$(dirname "$0")/patch-idle-cpu.py"
  fi
  if [ ! -f "${script}" ]; then
    script="$(dirname "$0")/../tools/patch-idle-cpu.py"
  fi
  if [ ! -f "${exe}" ]; then
    echo "$(timestamp) WARNING: Idle-CPU patch requested but ${exe} is missing"
    return 0
  fi
  if [ ! -f "${script}" ]; then
    echo "$(timestamp) WARNING: Idle-CPU patch requested but patch-idle-cpu.py not found in image"
    return 0
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    echo "$(timestamp) WARNING: Idle-CPU patch requested but python3 not available"
    return 0
  fi
  # Is it already patched? The script rejects a second apply, so test
  # by checking md5 against both known states — cheaper than dry-run.
  local current_md5
  current_md5="$(md5sum "${exe}" | awk '{print $1}')"
  local original_md5="61e320a6a45f4ac539f2c5d0f7b7ff2c"
  local patched_md5="b1796533f22603ad2f2da021033e3f9f"
  if [ "${current_md5}" = "${patched_md5}" ]; then
    echo "$(timestamp) INFO: Idle-CPU patch already applied (md5 matches patched build); skipping"
    return 0
  fi
  if [ "${current_md5}" != "${original_md5}" ]; then
    echo "$(timestamp) WARNING: Shipping EXE md5 (${current_md5}) doesn't match the build this patch was derived against (${original_md5}). Skipping patch — re-derive offsets for the new build before re-enabling."
    return 0
  fi
  echo "$(timestamp) INFO: Applying idle-CPU patch via ${script} (drops idle CPU from ~200% to ~5%)"
  if ! python3 "${script}" "${exe}"; then
    echo "$(timestamp) WARNING: Idle-CPU patch failed; binary left unmodified"
  fi
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
    echo "$(timestamp) INFO: Patching WorldDescription at ${world_desc} (active island ${target_island})"
    jq \
      --arg name "${WORLD_NAME}" \
      --arg preset "${WORLD_PRESET_TYPE}" \
      '.WorldDescription.WorldName = $name | .WorldDescription.WorldPresetType = $preset' \
      "${world_desc}" > "${world_desc}.tmp" && mv "${world_desc}.tmp" "${world_desc}"
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
      local jq_filter='.ServerDescription_Persistent.IsPasswordProtected = $protected
        | .ServerDescription_Persistent.Password = $password
        | .ServerDescription_Persistent.ServerName = $name
        | .ServerDescription_Persistent.MaxPlayerCount = $maxPlayers
        | .ServerDescription_Persistent.P2pProxyAddress = $proxy'
      if [ -n "${INVITE_CODE}" ]; then
        jq_filter="${jq_filter} | .ServerDescription_Persistent.InviteCode = \$invite"
      fi
      if [ -n "${WORLD_ISLAND_ID}" ] && [ "${WORLD_ISLAND_ID}" != "default-world" ]; then
        jq_filter="${jq_filter} | .ServerDescription_Persistent.WorldIslandId = \$islandId"
      fi
      jq \
        --arg name "${SERVER_NAME}" \
        --arg invite "${INVITE_CODE}" \
        --argjson protected "${protected_bool}" \
        --arg password "${SERVER_PASSWORD}" \
        --arg islandId "${WORLD_ISLAND_ID}" \
        --argjson maxPlayers "${MAX_PLAYER_COUNT}" \
        --arg proxy "${P2P_PROXY_ADDRESS}" \
        "${jq_filter}" \
        "${WINDROSE_SERVER_CONFIG}" > "${WINDROSE_SERVER_CONFIG}.tmp" && mv "${WINDROSE_SERVER_CONFIG}.tmp" "${WINDROSE_SERVER_CONFIG}"
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
# 30 FPS — without a connected client the main loop has no pacing source
# and burns whole cores on dispatch overhead. `-FPS=N` tells UE to sleep
# between ticks. When a client is connected, NetServerMaxTickRate (30Hz
# UE default) already paces the loop and this cap is a no-op. Set to
# empty string to disable, or to a different arg list (e.g.
# "-FPS=60 -ExecCmds=..." ) for experimentation.
: "${SERVER_LAUNCH_ARGS:=-FPS=30}"
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
