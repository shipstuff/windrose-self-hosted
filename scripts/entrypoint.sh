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

# Direct IP Connection (introduced in the 2026-04 Windrose patch).
# When USE_DIRECT_CONNECTION=true, the game advertises a raw IP:port
# instead of going through Windrose's connectivity services — players
# bypass the backend matchmaker and invite codes stop working, clients
# connect purely via your IP. Documented as an "advanced" option
# requiring manual port forwarding on the operator's router. If unset
# entirely, we leave all four Direct-IP fields alone in env-mode so
# existing operators aren't silently opted in.
: "${USE_DIRECT_CONNECTION:=}"
: "${DIRECT_CONNECTION_SERVER_ADDRESS:=}"
: "${DIRECT_CONNECTION_SERVER_PORT:=7777}"
: "${DIRECT_CONNECTION_PROXY_ADDRESS:=0.0.0.0}"
# When Direct IP is enabled and the server address isn't set, reuse the
# same auto-detected IP as P2pProxyAddress — they're both "my LAN/WAN-
# facing IP" from the same host's perspective, and forcing the operator
# to set it twice is just a foot-gun.
if [ "${USE_DIRECT_CONNECTION}" = "true" ] && [ -z "${DIRECT_CONNECTION_SERVER_ADDRESS}" ]; then
  DIRECT_CONNECTION_SERVER_ADDRESS="${P2P_PROXY_ADDRESS}"
  echo "$(timestamp) INFO: DIRECT_CONNECTION_SERVER_ADDRESS reusing P2P_PROXY_ADDRESS (${DIRECT_CONNECTION_SERVER_ADDRESS})"
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

steamcmd_preserved_files=(ServerDescription.json WorldDescription.json .mods.json .mods.staged.json)
steamcmd_preserved_dirs=("Content/Paks/~mods" "Content/Paks/~mods.disabled" ".mods-staging")

preserve_r5_state() {
  local src_r5="$1"
  local dst="$2"

  for f in "${steamcmd_preserved_files[@]}"; do
    if [ -f "${src_r5}/${f}" ]; then
      cp -a "${src_r5}/${f}" "${dst}/${f}"
    fi
  done
  for rel in "${steamcmd_preserved_dirs[@]}"; do
    if [ -d "${src_r5}/${rel}" ]; then
      mkdir -p "${dst}/$(dirname "${rel}")"
      cp -a "${src_r5}/${rel}" "${dst}/${rel}"
    fi
  done
}

restore_r5_state() {
  local src="$1"
  local dst_r5="$2"

  mkdir -p "${dst_r5}"
  for f in "${steamcmd_preserved_files[@]}"; do
    if [ -f "${src}/${f}" ]; then
      cp -a "${src}/${f}" "${dst_r5}/${f}"
    fi
  done
  for rel in "${steamcmd_preserved_dirs[@]}"; do
    if [ -d "${src}/${rel}" ]; then
      rm -rf "${dst_r5:?}/${rel}"
      mkdir -p "$(dirname "${dst_r5:?}/${rel}")"
      cp -a "${src}/${rel}" "${dst_r5:?}/${rel}"
    fi
  done
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
  # Always pass `validate` to app_update. Costs ~30s on a populated
  # 3 GB tree (re-checksum, no download), gives us idempotent install
  # health on every boot, and matches the official Windrose FAQ
  # command. Not worth conditionalizing.

  # Stash under WINDROSE_PATH (on the PVC), NOT /tmp — /tmp is
  # container-ephemeral, so a kubelet OOM-kill / eviction / node
  # preempt between the stash and the restore would wipe user data
  # forever. Prod data loss 2026-04-20 was this exact failure mode:
  # container died mid-flow, /tmp got wiped on next container start,
  # next entrypoint logged "No save version found" and let the game
  # bootstrap a fresh world on top of missing saves.
  local preserve_dir="${WINDROSE_PATH}/.steamcmd-preserve-$$"
  # Resume an abandoned preserve_dir from a prior aborted run. If any
  # .steamcmd-preserve-* exists, it means a previous entrypoint stashed
  # data but didn't get to restore it — restore now before doing
  # anything else. Belt-and-braces for partial-failure recovery.
  for stray in "${WINDROSE_PATH}"/.steamcmd-preserve-*; do
    [ -d "${stray}" ] || continue
    [ "${stray}" = "${preserve_dir}" ] && continue
    if [ -d "${stray}/Saved" ] && [ ! -d "${WINDROSE_SERVER_DIR}/R5/Saved" ]; then
      echo "$(timestamp) WARNING: Found abandoned preserve_dir ${stray} from a prior interrupted boot; restoring before proceeding"
      mkdir -p "${WINDROSE_SERVER_DIR}/R5"
      mv "${stray}/Saved" "${WINDROSE_SERVER_DIR}/R5/Saved"
      restore_r5_state "${stray}" "${WINDROSE_SERVER_DIR}/R5"
    fi
    rm -rf "${stray}"
  done
  mkdir -p "${preserve_dir}"

  echo "$(timestamp) INFO: Stashing identity + save before SteamCMD app_update (preserve_dir ${preserve_dir})"
  if [ -d "${WINDROSE_SERVER_DIR}/R5/Saved" ]; then
    mv "${WINDROSE_SERVER_DIR}/R5/Saved" "${preserve_dir}/Saved"
  fi
  preserve_r5_state "${WINDROSE_SERVER_DIR}/R5" "${preserve_dir}"

  mkdir -p "${WINDROSE_SERVER_DIR}"
  # SteamCMD's anonymous app_update is flaky on the first attempt
  # within a container — fails with "ERROR! Failed to install app
  # '4129620' (Missing configuration)" + exit 8 (which the script
  # confusingly still reports as exit 0 sometimes), then succeeds on
  # the very next attempt with no other change. Verified clean-room
  # 2026-04-27: attempt #1 FAIL, #2 OK + downloads, #3 resumes at
  # whatever % #2 reached. Probably a Steam-side anonymous-license
  # caching quirk that warms up after the first call. Retry up to 3
  # times; each attempt resumes the partial download so we don't pay
  # the bandwidth twice.
  #
  # Existing populated installs are unaffected — verify-only runs
  # don't go through the failing licensing path. So canary / prod /
  # VPS kept working, masking the regression for fresh-install
  # operators.
  #
  # Stdin heredoc instead of +command-line chain — the two are
  # roughly equivalent, but heredoc reads more like the official
  # Windrose / Steam dedicated-server FAQ examples that operators
  # paste into interactive SteamCMD when debugging.
  local rc=1
  local attempt
  for attempt in 1 2 3; do
    echo "$(timestamp) INFO: SteamCMD app_update ${app_id} validate (attempt ${attempt})"
    rc=0
    "${STEAMCMD_PATH}/steamcmd.sh" <<EOF || rc=$?
force_install_dir ${WINDROSE_SERVER_DIR}
login anonymous
app_update ${app_id} validate
quit
EOF
    if server_files_present; then
      rc=0
      break
    fi
    if [ "${attempt}" -lt 3 ]; then
      echo "$(timestamp) WARNING: SteamCMD app_update attempt ${attempt} did not produce server binary; retrying"
      sleep 2
    fi
  done

  echo "$(timestamp) INFO: Restoring identity + save from ${preserve_dir}"
  mkdir -p "${WINDROSE_SERVER_DIR}/R5"
  if [ -d "${preserve_dir}/Saved" ]; then
    rm -rf "${WINDROSE_SERVER_DIR}/R5/Saved"
    mv "${preserve_dir}/Saved" "${WINDROSE_SERVER_DIR}/R5/Saved"
  fi
  restore_r5_state "${preserve_dir}" "${WINDROSE_SERVER_DIR}/R5"
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

# Optional: maintain a PATCHED COPY of the shipping EXE alongside the
# original, and launch that instead of the original. This throttles a
# Boost.Asio drain-loop spin (see scripts/patch-idle-cpu.py for the
# derivation) that otherwise burns ~2 CPU cores when no client is
# connected. Sibling-file approach keeps Steam's manifest-managed
# original untouched — SteamCMD doesn't revert anything because there's
# nothing to revert. On each boot we md5 the original and compare to
# the source md5 recorded alongside the patched sibling; rebuild iff
# the source changed (Windrose update) or the sibling is missing.
#
# Off by default. Enable via WINDROSE_PATCH_IDLE_CPU=1. UI override
# via $WINDROSE_PATCH_OVERRIDE_FILE ("disabled" forces OFF, "enabled"
# forces ON). Failures (patch script missing, python3 missing,
# signature can't be found in a future build) log a WARNING and fall
# back to launching the original — we'd rather boot with a busy
# server than not boot at all.
#
# Exports IDLE_PATCH_EFFECTIVE_EXE as the path to launch (either the
# patched sibling or the original). The bottom-of-file exec uses it.
maybe_patch_idle_cpu() {
  : "${WINDROSE_PATCH_IDLE_CPU:=0}"
  local exe="${WINDROSE_SERVER_DIR}/R5/Binaries/Win64/WindroseServer-Win64-Shipping.exe"
  local patched="${exe%.exe}.patched.exe"
  local source_md5_file="${exe%.exe}.patched-source.md5"
  local override_file="${WINDROSE_PATCH_OVERRIDE_FILE:-${WINDROSE_SERVER_DIR}/R5/.idle-patch-override}"
  export IDLE_PATCH_EFFECTIVE_EXE="${exe}"

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
    # Patch OFF: drop the patched sibling if it's around so the next
    # launch uses the original. Cheap: it's just a sibling file.
    if [ -f "${patched}" ] || [ -f "${source_md5_file}" ]; then
      echo "$(timestamp) INFO: Idle-CPU patch effective=OFF; removing patched sibling"
      rm -f "${patched}" "${source_md5_file}"
    fi
    return 0
  fi

  # Patch ON. Preflight the script + interpreter before touching anything.
  if [ ! -f "${exe}" ]; then
    echo "$(timestamp) WARNING: Idle-CPU patch requested but ${exe} is missing"
    return 0
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    echo "$(timestamp) WARNING: Idle-CPU patch requested but python3 not available; launching original"
    return 0
  fi
  local script
  script="$(_find_patch_script)"
  if [ -z "${script}" ]; then
    echo "$(timestamp) WARNING: Idle-CPU patch requested but patch-idle-cpu.py not found; launching original"
    return 0
  fi

  # Source md5. If it matches the md5 recorded alongside an existing
  # patched sibling, the sibling is still valid — reuse.
  local current_md5 cached_md5
  current_md5="$(md5sum "${exe}" | awk '{print $1}')"
  cached_md5="$(cat "${source_md5_file}" 2>/dev/null || true)"
  if [ -f "${patched}" ] && [ "${cached_md5}" = "${current_md5}" ]; then
    echo "$(timestamp) INFO: Patched sibling at ${patched} is current (source md5 ${current_md5}); reusing"
    IDLE_PATCH_EFFECTIVE_EXE="${patched}"
    return 0
  fi

  # Need to rebuild. Either the source changed (Windrose update) or
  # the sibling is absent.
  if [ -f "${patched}" ]; then
    echo "$(timestamp) INFO: Source md5 changed (${cached_md5:-\(none\)} → ${current_md5}); rebuilding patched sibling"
    rm -f "${patched}" "${source_md5_file}"
  else
    echo "$(timestamp) INFO: Building patched sibling at ${patched} (source md5 ${current_md5})"
  fi

  cp -a "${exe}" "${patched}"
  # --idempotent lets us handle the upgrade case (host came from the
  # old in-place model where the original was modified): cp produced
  # an already-patched sibling, and idempotent exits 0 on that rather
  # than erroring. The cached source md5 we record will be the
  # patched md5 for that first boot, and will self-correct the next
  # time Steam restores the original to its manifest state.
  if python3 "${script}" "${patched}" --idempotent; then
    echo "${current_md5}" > "${source_md5_file}"
    echo "$(timestamp) INFO: Patched sibling ready; launching ${patched} in place of ${exe}"
    IDLE_PATCH_EFFECTIVE_EXE="${patched}"
  else
    echo "$(timestamp) WARNING: Patch build failed; removing incomplete sibling and launching original"
    rm -f "${patched}" "${source_md5_file}"
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
    # Safety net for prod-data-loss pattern 2026-04-20: if ANY prior boot
    # left a marker saying "this install already had a world", refuse to
    # bootstrap a fresh one without explicit operator consent. Prevents
    # silent data loss when a mid-SteamCMD container kill wipes Saved/.
    # The marker is dropped below on every successful boot-with-a-world.
    local had_world_marker="${WINDROSE_PATH}/.had-world-once"
    if [ -f "${had_world_marker}" ] && [ "${WINDROSE_ALLOW_FRESH_WORLD:-0}" != "1" ]; then
      cat >&2 <<EOF
$(timestamp) ERROR: Saved/ is empty but this install previously had a
  world (marker at ${had_world_marker}). Refusing to bootstrap a fresh
  world on top of what looks like lost data — that's the very failure
  mode this guard exists to catch.

  Most likely cause: a prior boot's SteamCMD stash-and-restore was
  interrupted mid-flow. Check ${WINDROSE_PATH}/.steamcmd-preserve-*
  for an orphan that might be restorable. Or restore from
  /home/steam/backups/ via POST /api/backups/<id>/restore.

  To override (e.g. you genuinely want a fresh world on an install
  that previously had one, and you've already backed anything up):
  set WINDROSE_ALLOW_FRESH_WORLD=1 in the environment and restart.
EOF
      exit 1
    fi
    echo "$(timestamp) INFO: No save version found; letting the server bootstrap its first world"
    return 0
  fi
  # Saved/ is populated — drop a marker so the next boot knows this
  # install has seen a world. The "safety net" above trips on this.
  touch "${WINDROSE_PATH}/.had-world-once"
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
        echo "$(timestamp) INFO: env-mode: no shadow stamp present — treating live values as operator-owned and seeding shadow from the live file. Future boots will stamp env into keys that still match the shadow."
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
      local stamp_dc_use="false" stamp_dc_addr="false" stamp_dc_port="false" stamp_dc_proxy="false"
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
      # Direct IP fields — only stamp when USE_DIRECT_CONNECTION is set
      # to any value (true/false). An unset env var means the operator
      # hasn't opted into the feature; we leave all four Direct-IP keys
      # exactly as they are on disk so existing deployments aren't
      # silently flipped into Direct IP mode on upgrade.
      if [ -n "${USE_DIRECT_CONNECTION}" ]; then
        _decide stamp_dc_use '.ServerDescription_Persistent.UseDirectConnection' bool \
          '.ServerDescription_Persistent.UseDirectConnection = $useDirect' 'UseDirectConnection'
        _decide stamp_dc_addr '.ServerDescription_Persistent.DirectConnectionServerAddress' string \
          '.ServerDescription_Persistent.DirectConnectionServerAddress = $dcAddr' 'DirectConnectionServerAddress'
        _decide stamp_dc_port '.ServerDescription_Persistent.DirectConnectionServerPort' number \
          '.ServerDescription_Persistent.DirectConnectionServerPort = $dcPort' 'DirectConnectionServerPort'
        _decide stamp_dc_proxy '.ServerDescription_Persistent.DirectConnectionProxyAddress' string \
          '.ServerDescription_Persistent.DirectConnectionProxyAddress = $dcProxy' 'DirectConnectionProxyAddress'
      fi

      # Normalize USE_DIRECT_CONNECTION to a real JSON bool so jq
      # --argjson doesn't reject a missing/empty value. Defaults to
      # false when the env is unset (the jq filter only runs if the
      # operator opted in via one of the stamp flags anyway).
      local use_direct_bool="false"
      if [ "${USE_DIRECT_CONNECTION}" = "true" ] || [ "${USE_DIRECT_CONNECTION}" = "1" ]; then
        use_direct_bool="true"
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
          --argjson useDirect "${use_direct_bool}" \
          --arg dcAddr "${DIRECT_CONNECTION_SERVER_ADDRESS}" \
          --argjson dcPort "${DIRECT_CONNECTION_SERVER_PORT}" \
          --arg dcProxy "${DIRECT_CONNECTION_PROXY_ADDRESS}" \
          "${jq_filter}" \
          "${WINDROSE_SERVER_CONFIG}" > "${WINDROSE_SERVER_CONFIG}.tmp" \
          && mv "${WINDROSE_SERVER_CONFIG}.tmp" "${WINDROSE_SERVER_CONFIG}"
      fi

      # Refresh shadow. Two cases:
      #
      # (a) First boot (no prior shadow): seed shadow with the LIVE
      #     ServerDescription_Persistent values, NOT the env values.
      #     This is the key correctness property — if we seeded with
      #     env intent, any live-vs-env mismatch on the next boot
      #     would be treated as "operator-modified forever" and env
      #     mode would stop doing its job silently. Snapshotting live
      #     means next boot sees live==shadow (stamp fires, env
      #     applies) OR live!=shadow (operator genuinely changed
      #     something between boots, preserve).
      #
      # (b) Steady state: for each managed key we stamped, record the
      #     env value we just wrote; for each key we skipped, preserve
      #     the old shadow value so the divergence marker survives.
      #     A corrupt shadow file is treated as absent → falls through
      #     to first-boot semantics, which is the safest recovery.
      if [ "${first_boot}" = "true" ]; then
        jq '{ ServerDescription_Persistent: (.ServerDescription_Persistent // {}) }' \
          "${WINDROSE_SERVER_CONFIG}" > "${shadow}.tmp" \
          && mv "${shadow}.tmp" "${shadow}" \
          && echo "$(timestamp) INFO: env-mode: seeded shadow from live ${WINDROSE_SERVER_CONFIG}"
      else
        local shadow_base='{}'
        if jq empty "${shadow}" >/dev/null 2>&1; then
          shadow_base="$(cat "${shadow}")"
        else
          echo "$(timestamp) WARNING: env-mode: shadow at ${shadow} is not valid JSON; re-seeding from live on next boot"
          rm -f "${shadow}"
          return 0
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
          --argjson useDirect "${use_direct_bool}" \
          --arg dcAddr "${DIRECT_CONNECTION_SERVER_ADDRESS}" \
          --argjson dcPort "${DIRECT_CONNECTION_SERVER_PORT}" \
          --arg dcProxy "${DIRECT_CONNECTION_PROXY_ADDRESS}" \
          --arg s_pwp "${stamp_pwprotected}" \
          --arg s_pw "${stamp_password}" \
          --arg s_name "${stamp_name}" \
          --arg s_max "${stamp_max}" \
          --arg s_proxy "${stamp_proxy}" \
          --arg s_invite "${stamp_invite}" \
          --arg s_island "${stamp_island}" \
          --arg s_dcUse "${stamp_dc_use}" \
          --arg s_dcAddr "${stamp_dc_addr}" \
          --arg s_dcPort "${stamp_dc_port}" \
          --arg s_dcProxy "${stamp_dc_proxy}" '
          ($base.ServerDescription_Persistent // {}) as $b
          | { ServerDescription_Persistent: (
              $b
              | (if $s_pwp    == "true" then .IsPasswordProtected = $protected else . end)
              | (if $s_pw     == "true" then .Password            = $password  else . end)
              | (if $s_name   == "true" then .ServerName          = $name      else . end)
              | (if $s_max    == "true" then .MaxPlayerCount      = $maxPlayers else . end)
              | (if $s_proxy  == "true" then .P2pProxyAddress     = $proxy     else . end)
              | (if $s_invite == "true" then .InviteCode          = $invite    else . end)
              | (if $s_island == "true" then .WorldIslandId       = $islandId  else . end)
              | (if $s_dcUse   == "true" then .UseDirectConnection          = $useDirect else . end)
              | (if $s_dcAddr  == "true" then .DirectConnectionServerAddress = $dcAddr    else . end)
              | (if $s_dcPort  == "true" then .DirectConnectionServerPort   = $dcPort    else . end)
              | (if $s_dcProxy == "true" then .DirectConnectionProxyAddress = $dcProxy   else . end)
            ) }' > "${shadow}.tmp" && mv "${shadow}.tmp" "${shadow}"
      fi
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

# Clean up stale wine processes from a prior boot of THIS instance.
# wineserver and its children routinely detach (ppid=1), so systemd's
# cgroup-level SIGTERM can't always reach them — a fresh `systemctl
# restart` then finds the new proton stuck in waitforexitandrun behind
# an old wineserver. Scan /proc for processes whose environ carries the
# tag we wrote on our last boot and kill those. Tag lives in a
# WINDROSE_PATH-scoped pidfile so multi-tenant hosts (other Windrose
# installs, other wine apps) are unaffected — we only match processes
# we spawned ourselves.
_instance_tag_file="${WINDROSE_PATH}/.last-instance-tag"
if [ -f "${_instance_tag_file}" ]; then
  _stale_tag="$(cat "${_instance_tag_file}" 2>/dev/null || true)"
  if [ -n "${_stale_tag}" ]; then
    _killed=0
    for _p in /proc/[0-9]*; do
      [ -r "${_p}/environ" ] || continue
      if tr '\0' '\n' < "${_p}/environ" 2>/dev/null \
           | grep -qxF "WINDROSE_INSTANCE_TAG=${_stale_tag}"; then
        kill -9 "${_p##*/}" 2>/dev/null && _killed=$((_killed + 1)) || true
      fi
    done
    if [ "${_killed}" -gt 0 ]; then
      echo "$(timestamp) INFO: Killed ${_killed} stale wine process(es) from prior boot (tag ${_stale_tag})"
      sleep 1  # give wineserver a beat to unwind before we respawn it
    fi
  fi
  rm -f "${_instance_tag_file}"
fi

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
  while [ -f "${maint_flag}" ]; do sleep "${WINDROSE_MAINTENANCE_POLL_SECONDS:-2}" & wait $!; done
  echo "$(timestamp) INFO: Maintenance flag cleared; proceeding with normal boot."
fi

migrate_saves_on_version_change
ensure_world_layout
reconcile_server_config
maybe_disable_sentry
maybe_patch_idle_cpu

if [ -f "${WINDROSE_SERVER_DIR}/R5/.mods.staged.json" ]; then
  _ui_server="${WINDROSE_UI_SERVER:-/opt/windrose-ui/server.py}"
  if [ ! -f "${_ui_server}" ] && [ -f "/opt/windrose/server.py" ]; then
    _ui_server="/opt/windrose/server.py"
  fi
  if [ -f "${_ui_server}" ]; then
    python3 "${_ui_server}" --reconcile-staged-mods 2>&1 | while IFS= read -r _line; do
      echo "$(timestamp) ${_line}"
    done
  else
    echo "$(timestamp) ERROR: Staged mods are pending, but admin server.py was not found for startup reconciliation"
    exit 1
  fi
fi

# Reconcile Engine.ini's NetServerMaxTickRate + t.MaxFPS from
# NET_SERVER_MAX_TICK_RATE env on every boot. Uses a shadow-stamp
# pattern identical to the ServerDescription.json reconcile: we remember
# the value we last wrote; if the live file still matches the shadow
# we stamp the new env value, otherwise we assume the operator hand-
# edited Engine.ini and leave their value alone.
#
# Why reconcile instead of seed-once: earlier builds only seeded when
# Engine.ini was absent, so NET_SERVER_MAX_TICK_RATE was a dead knob
# on any existing install — a bug, not a feature. Operators couldn't
# change tick rate via env/helm without hand-editing on disk.
#
# Windrose's actual driver is UR5NetDriver (under /Script/
# R5SocketSubsystem.R5NetDriver); the stock UIpNetDriver section is
# mirrored as belt-and-suspenders so pre-init reads pick up either.
# t.MaxFPS (in [ConsoleVariables]) caps the main loop frequency —
# on a dedicated server with no rendering, THAT is what drives the
# game tick, so we bump it together with NetServerMaxTickRate to
# avoid "ticks rate capped at t.MaxFPS despite NetServerMaxTickRate
# being higher".
# Delegate to reconcile-engine-ini.sh (sibling script) so the logic
# is unit-testable in isolation — see tests/test_engine_ini_reconcile.sh.
# Uses shadow-stamp semantics: the env value is written every boot but
# operator hand-edits to the same keys are preserved once detected.
_reconcile_script="${WINDROSE_RECONCILE_ENGINE_INI:-$(dirname "$0")/reconcile-engine-ini.sh}"
# Docker image layout installs under /usr/local/bin/; fall back to that.
if [ ! -x "${_reconcile_script}" ]; then
  _reconcile_script="/usr/local/bin/reconcile-engine-ini.sh"
fi
if [ -x "${_reconcile_script}" ] && [ -n "${NET_SERVER_MAX_TICK_RATE:-}" ]; then
  # Shadow lives next to Engine.ini so it travels with the PVC. R5/ is
  # the canonical save-root path (${WINDROSE_SERVER_DIR}/R5 is the same
  # thing server.py calls R5_DIR — don't use that name here, entrypoint
  # doesn't define it and set -u would blow up).
  "${_reconcile_script}" \
    "${WINDROSE_SERVER_DIR}/R5/Saved/Config/WindowsServer/Engine.ini" \
    "${WINDROSE_SERVER_DIR}/R5/.engine-ini-shadow" 2>&1 | while IFS= read -r _line; do
      echo "$(timestamp) ${_line}"
    done
fi

if [ "${WINDROSE_LAUNCH_STRATEGY}" = "launcher" ]; then
  EXE="${WINDROSE_SERVER_DIR}/WindroseServer.exe"
else
  EXE="${WINDROSE_SERVER_DIR}/R5/Binaries/Win64/WindroseServer-Win64-Shipping.exe"
fi
# maybe_patch_idle_cpu sets IDLE_PATCH_EFFECTIVE_EXE to the sibling
# .patched.exe when active, else leaves it at the original. Shipping
# launch strategy is the only one with a sibling patch today.
if [ "${WINDROSE_LAUNCH_STRATEGY}" != "launcher" ] && [ -n "${IDLE_PATCH_EFFECTIVE_EXE:-}" ]; then
  EXE="${IDLE_PATCH_EFFECTIVE_EXE}"
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

# Tag every process we're about to spawn (via Proton + wineserver tree)
# with a boot-unique marker. Next boot's entrypoint uses this to find
# and kill any stragglers that detached from systemd's cgroup (see the
# top-of-main cleanup block). The tag is exported, so all descendants
# — including ones wineserver reparents to init — inherit it in their
# /proc/<pid>/environ.
WINDROSE_INSTANCE_TAG="$$-$(date +%s)"
export WINDROSE_INSTANCE_TAG
mkdir -p "$(dirname "${_instance_tag_file}")"
echo "${WINDROSE_INSTANCE_TAG}" > "${_instance_tag_file}"

echo "$(timestamp) INFO: exec'ing Proton (becomes PID 1; Xvfb is in the xvfb sidecar; R5.log tail is streaming to stderr). Launch args: -log ${SERVER_LAUNCH_ARGS}"
cd "${WINDROSE_SERVER_DIR}"
exec "${STEAMCMD_PATH}/compatibilitytools.d/GE-Proton${GE_PROTON_VERSION}/proton" \
  waitforexitandrun "${EXE}" -log "${launch_args[@]}"
