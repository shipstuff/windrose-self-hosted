#!/bin/bash
# Install the Windrose dedicated server as three systemd services
# (game + Xvfb + admin UI) on a bare Linux box. Tested on Ubuntu
# 24.04; should work on Debian 12+ / Ubuntu 22.04+ with no changes.
#
# Run from the repo root (or anywhere as long as the paths resolve):
#   sudo ./bare-linux/install.sh
#
# Overrides:
#   WINDROSE_USER                user that owns the install (default: steam)
#   WINDROSE_INSTALL_DIR         where scripts/* land       (default: /opt/windrose)
#   UI_BIND                      UI listen interface        (default: 0.0.0.0)
#   UI_PORT                      UI listen port             (default: 28080)
#   UI_PASSWORD                  HTTP basic-auth password   (default: empty)
#   UI_ENABLE_ADMIN_WITHOUT_PASSWORD
#                                 explicit opt-in for destructive routes when
#                                 UI_PASSWORD is empty      (default: false)
#   WINDROSE_PATCH_IDLE_CPU       opt in to the idle-CPU binary patch
#                                 (default: 0; flip to "1" to apply on boot)
#   SERVER_NAME, MAX_PLAYER_COUNT, WORLD_NAME, WORLD_PRESET_TYPE,
#   P2P_PROXY_ADDRESS, WINDROSE_SERVER_SOURCE, etc. — any env var the
#   entrypoint understands can be seeded here too; anything unset falls
#   through to the entrypoint's default.
#
# Uninstall:
#   sudo systemctl disable --now windrose-game windrose-ui windrose-xvfb
#   sudo rm /etc/systemd/system/windrose-{game,ui,xvfb}.service
#   sudo systemctl daemon-reload
#   (data under /home/steam/ stays — delete manually if desired)

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "install.sh needs root (packages, /etc/systemd, /etc/windrose); re-run with sudo" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SCRIPTS_SRC="${REPO_ROOT}/scripts"

WINDROSE_USER="${WINDROSE_USER:-steam}"
WINDROSE_INSTALL_DIR="${WINDROSE_INSTALL_DIR:-/opt/windrose}"
WINDROSE_ENV_DIR="${WINDROSE_ENV_DIR:-/etc/windrose}"
WINDROSE_ENV_FILE="${WINDROSE_ENV_FILE:-${WINDROSE_ENV_DIR}/windrose.env}"

# Safe-by-default: bind the admin UI to loopback. Exposing it publicly
# (UI_BIND=0.0.0.0) without UI_PASSWORD set is a foot-gun on any VPS,
# so the operator has to explicitly flip both knobs.
UI_BIND="${UI_BIND:-127.0.0.1}"
UI_PORT="${UI_PORT:-28080}"
UI_PASSWORD="${UI_PASSWORD:-}"
UI_ENABLE_ADMIN_WITHOUT_PASSWORD="${UI_ENABLE_ADMIN_WITHOUT_PASSWORD:-false}"

# Webhook knobs. All optional; if the URL vars are empty the
# EventDetector thread still runs but skips dispatch.
WINDROSE_WEBHOOK_URL="${WINDROSE_WEBHOOK_URL:-}"
WINDROSE_DISCORD_WEBHOOK_URL="${WINDROSE_DISCORD_WEBHOOK_URL:-}"
WINDROSE_WEBHOOK_EVENTS="${WINDROSE_WEBHOOK_EVENTS:-server.online,server.offline,player.join,player.leave,backup.created,backup.restored,config.applied}"
WINDROSE_WEBHOOK_POLL_SECONDS="${WINDROSE_WEBHOOK_POLL_SECONDS:-15}"
WINDROSE_WEBHOOK_TIMEOUT="${WINDROSE_WEBHOOK_TIMEOUT:-5}"

# Warn if the operator is reaching for a publicly-exposed UI without
# a password. Not fatal — compose / bare-Linux have legitimate
# LAN-only deploys where this is fine — but loud so it's deliberate.
if [ "${UI_BIND}" = "0.0.0.0" ] && [ -z "${UI_PASSWORD}" ] && [ "${UI_ENABLE_ADMIN_WITHOUT_PASSWORD}" != "true" ]; then
  printf '\033[33m[install] WARN: UI_BIND=0.0.0.0 with no UI_PASSWORD.\n'
  printf '          The admin console will refuse destructive routes\n'
  printf '          without credentials, but anyone on the internet can\n'
  printf '          hit it. Set UI_PASSWORD=... or keep UI_BIND=127.0.0.1\n'
  printf '          and reverse-proxy via nginx/caddy with auth in front.\033[0m\n' >&2
fi

log() { printf '[install] %s\n' "$*"; }
warn() { printf '\033[33m[install] WARN: %s\033[0m\n' "$*" >&2; }

# --- Memory / swap preflight -----------------------------------------
# Windrose idles around 850 MiB RSS but spikes during world load + ICE
# negotiation; a constrained box with no swap OOMs silently mid-join.
# We don't configure swap automatically (too opinionated for a host
# that may be multi-tenant), but we do warn loudly so the operator
# knows to run the tuning recipe from bare-linux/README.md.
mem_mib="$(awk '/^MemTotal:/ {print int($2/1024)}' /proc/meminfo)"
swap_mib="$(awk '/^SwapTotal:/ {print int($2/1024)}' /proc/meminfo)"
log "host memory: ${mem_mib} MiB RAM, ${swap_mib} MiB swap"
if [ "${mem_mib:-0}" -lt 4096 ]; then
  warn "Host RAM is under 4 GiB (${mem_mib} MiB). The game can peak above"
  warn "this during world load — expect OOM kills with no swap."
  if [ "${swap_mib:-0}" -lt 2048 ]; then
    warn "Swap is also under 2 GiB (${swap_mib} MiB). See"
    warn "  bare-linux/README.md § 'Swap'"
    warn "for a 4 GiB swapfile + sysctl recipe. Install is continuing anyway."
  fi
elif [ "${swap_mib:-0}" -lt 1024 ]; then
  log "NOTE: no meaningful swap configured. The game usually fits in 4 GiB"
  log "      RAM alone, but a 2 GiB swapfile is cheap insurance against"
  log "      spiky world-load peaks. See bare-linux/README.md § 'Swap'."
fi

# --- Packages ---------------------------------------------------------
log "installing OS packages (apt)"
export DEBIAN_FRONTEND=noninteractive
apt-get update
dpkg --add-architecture i386
apt-get update
apt-get install --no-install-recommends -y \
  procps ca-certificates winbind dbus libfreetype6 libgnutls30 \
  xvfb curl jq tar gzip unzip locales lib32gcc-s1 python3

# --- User -------------------------------------------------------------
if ! id -u "${WINDROSE_USER}" >/dev/null 2>&1; then
  log "creating user '${WINDROSE_USER}'"
  useradd --create-home --shell /bin/bash "${WINDROSE_USER}"
fi
WINDROSE_HOME="$(getent passwd "${WINDROSE_USER}" | cut -d: -f6)"
WINDROSE_GROUP="$(id -gn "${WINDROSE_USER}")"
log "target user ${WINDROSE_USER}:${WINDROSE_GROUP} home=${WINDROSE_HOME}"

# --- File layout ------------------------------------------------------
log "laying down files under ${WINDROSE_INSTALL_DIR}"
install -d -o "${WINDROSE_USER}" -g "${WINDROSE_GROUP}" "${WINDROSE_INSTALL_DIR}"
install -d -o "${WINDROSE_USER}" -g "${WINDROSE_GROUP}" "${WINDROSE_INSTALL_DIR}/scripts"
install -d -o "${WINDROSE_USER}" -g "${WINDROSE_GROUP}" "${WINDROSE_INSTALL_DIR}/scripts/ui"

install -m 0755 -o "${WINDROSE_USER}" -g "${WINDROSE_GROUP}" \
  "${SCRIPTS_SRC}/entrypoint.sh" "${WINDROSE_INSTALL_DIR}/scripts/entrypoint.sh"
install -m 0644 -o "${WINDROSE_USER}" -g "${WINDROSE_GROUP}" \
  "${SCRIPTS_SRC}/ServerDescription_example.json" \
  "${WINDROSE_INSTALL_DIR}/scripts/ServerDescription_example.json"
install -m 0644 -o "${WINDROSE_USER}" -g "${WINDROSE_GROUP}" \
  "${SCRIPTS_SRC}/WorldDescription_example.json" \
  "${WINDROSE_INSTALL_DIR}/scripts/WorldDescription_example.json"
# The entrypoint copies example configs from /usr/local/share/ when it
# can't find them in $HOME yet — mirror the Docker layout there too so
# the same code path works with no env twist.
install -d /usr/local/share
install -m 0644 "${SCRIPTS_SRC}/ServerDescription_example.json" \
  /usr/local/share/ServerDescription_example.json
install -m 0644 "${SCRIPTS_SRC}/WorldDescription_example.json" \
  /usr/local/share/WorldDescription_example.json

for f in server.py index.html app.js app.css; do
  install -m 0644 -o "${WINDROSE_USER}" -g "${WINDROSE_GROUP}" \
    "${SCRIPTS_SRC}/ui/${f}" "${WINDROSE_INSTALL_DIR}/scripts/ui/${f}"
done
chmod 0755 "${WINDROSE_INSTALL_DIR}/scripts/ui/server.py"
# server.py expects /opt/windrose-ui/ when referenced from k8s; add a
# symlink so the UI path matches the container path. Harmless if it's
# already there from a previous install.
ln -snf "${WINDROSE_INSTALL_DIR}/scripts/ui" /opt/windrose-ui

# Idle-CPU patch script — install at /usr/local/bin/ to match the
# Docker image layout so the entrypoint's _find_patch_script hits
# the fast-path candidate. Opt in via WINDROSE_PATCH_IDLE_CPU=1 in
# the env file; the UI Idle-CPU card toggles the runtime override.
install -m 0755 "${SCRIPTS_SRC}/patch-idle-cpu.py" /usr/local/bin/patch-idle-cpu.py

# --- Xvfb socket dir --------------------------------------------------
install -d -m 1777 /tmp/.X11-unix

# --- Env file ---------------------------------------------------------
# Precedence when re-running install.sh:
#   1. explicit CLI env  (sudo UI_PASSWORD=xyz ./install.sh)            wins
#   2. existing env-file value at /etc/windrose/windrose.env            next
#   3. install.sh default (baked below)                                 last
# Keys install.sh doesn't manage (operator additions like
# WINDROSE_CONFIG_MODE=mutable, custom SERVER_LAUNCH_ARGS, etc.) are
# preserved verbatim and appended under an "Operator additions" block
# at the bottom of the regenerated file. Makes the script idempotent:
# re-running with no CLI overrides produces the same file and loses
# zero customization.
#
# (Learned the hard way 2026-04-19: I'd set WINDROSE_CONFIG_MODE=mutable
# on the canary by hand, then a later `bash install.sh` to test a
# different knob blew it away. Entrypoint fell back to env-mode and
# started re-stamping WORLD_NAME on every boot.)
install -d -o root -g "${WINDROSE_GROUP}" -m 0750 "${WINDROSE_ENV_DIR}"

# Whitespace-separated list of every key install.sh generates below.
# Anything else found in an existing env file is kept verbatim.
_MANAGED_KEYS=" \
  HOME WINDROSE_PATH STEAMCMD_PATH STEAM_SDK64_PATH STEAM_SDK32_PATH \
  DISPLAY WINDROSE_SERVER_SOURCE SERVER_NAME MAX_PLAYER_COUNT \
  IS_PASSWORD_PROTECTED SERVER_PASSWORD WORLD_ISLAND_ID WORLD_NAME \
  WORLD_PRESET_TYPE P2P_PROXY_ADDRESS DISABLE_SENTRY PROTON_USE_XALIA \
  FILES_WAIT_TIMEOUT_SECONDS WINDROSE_PATCH_IDLE_CPU \
  UI_BIND UI_PORT UI_PASSWORD \
  UI_ENABLE_ADMIN_WITHOUT_PASSWORD UI_SERVE_STATIC \
  WINDROSE_DISCORD_WEBHOOK_URL WINDROSE_WEBHOOK_URL \
  WINDROSE_WEBHOOK_EVENTS WINDROSE_WEBHOOK_POLL_SECONDS \
  WINDROSE_WEBHOOK_TIMEOUT \
"

PRESERVED_EXTRAS=""
_preserved_order=()
declare -A _preserved_map=()
if [ "${WINDROSE_RESET:-0}" = "1" ] && [ -f "${WINDROSE_ENV_FILE}" ]; then
  warn "WINDROSE_RESET=1 — discarding existing ${WINDROSE_ENV_FILE}"
  warn "(a timestamped backup is still written under the env dir)"
  cp -p "${WINDROSE_ENV_FILE}" "${WINDROSE_ENV_FILE}.reset-bak-$(date +%s)"
elif [ -f "${WINDROSE_ENV_FILE}" ]; then
  log "merging with existing ${WINDROSE_ENV_FILE}"
  while IFS='=' read -r _k _v || [ -n "$_k" ]; do
    # Skip blanks + comments + any line that isn't a plain KEY=value.
    [ -z "$_k" ] && continue
    case "$_k" in \#*|*[[:space:]]*) continue ;; esac

    # Managed key? Only populate the shell var if the operator DIDN'T
    # already set it on the command line — that's how CLI wins.
    case " ${_MANAGED_KEYS} " in
      *" $_k "*)
        if [ -z "${!_k+x}" ]; then
          printf -v "$_k" '%s' "$_v"
        fi
        ;;
      *)
        # Unknown/operator-added key — dedupe by key (last-wins),
        # preserve first-seen order.
        if [ -z "${_preserved_map[$_k]+x}" ]; then
          _preserved_order+=("$_k")
        fi
        _preserved_map[$_k]="$_v"
        ;;
    esac
  done < "${WINDROSE_ENV_FILE}"
  # Flatten the dedupe'd extras into the string the write step appends.
  for _k in "${_preserved_order[@]}"; do
    PRESERVED_EXTRAS="${PRESERVED_EXTRAS}${_k}=${_preserved_map[$_k]}"$'\n'
  done
fi

log "writing env file ${WINDROSE_ENV_FILE}"
tmp_env="$(mktemp)"
cat > "${tmp_env}" <<EOF
# Written by bare-linux/install.sh. Edit freely; windrose-game restart
# picks up changes. Any WINDROSE_* / UI_* / SERVER_* / WORLD_* env var
# the entrypoint understands is valid here — additions land under the
# "Operator additions" section at the bottom and survive re-installs.
HOME=${WINDROSE_HOME}
WINDROSE_PATH=${WINDROSE_HOME}/windrose
STEAMCMD_PATH=${WINDROSE_HOME}/steamcmd
STEAM_SDK64_PATH=${WINDROSE_HOME}/.steam/sdk64
STEAM_SDK32_PATH=${WINDROSE_HOME}/.steam/sdk32
DISPLAY=:99
WINDROSE_SERVER_SOURCE=${WINDROSE_SERVER_SOURCE:-steamcmd}
SERVER_NAME=${SERVER_NAME:-Windrose Bare-Linux}
MAX_PLAYER_COUNT=${MAX_PLAYER_COUNT:-4}
IS_PASSWORD_PROTECTED=${IS_PASSWORD_PROTECTED:-false}
SERVER_PASSWORD=${SERVER_PASSWORD:-}
WORLD_ISLAND_ID=${WORLD_ISLAND_ID:-default-world}
WORLD_NAME=${WORLD_NAME:-Default Windrose World}
WORLD_PRESET_TYPE=${WORLD_PRESET_TYPE:-Medium}
P2P_PROXY_ADDRESS=${P2P_PROXY_ADDRESS:-}
DISABLE_SENTRY=${DISABLE_SENTRY:-1}
PROTON_USE_XALIA=${PROTON_USE_XALIA:-0}
FILES_WAIT_TIMEOUT_SECONDS=${FILES_WAIT_TIMEOUT_SECONDS:-0}
# Opt in to the idle-CPU binary patch (scripts/patch-idle-cpu.py).
# "1" -> entrypoint patches the EXE on every start (idempotent).
# The UI Idle-CPU card can flip this per-host without editing this file.
WINDROSE_PATCH_IDLE_CPU=${WINDROSE_PATCH_IDLE_CPU:-0}
UI_BIND=${UI_BIND}
UI_PORT=${UI_PORT}
UI_PASSWORD=${UI_PASSWORD}
UI_ENABLE_ADMIN_WITHOUT_PASSWORD=${UI_ENABLE_ADMIN_WITHOUT_PASSWORD}
UI_SERVE_STATIC=${UI_SERVE_STATIC:-true}

# Webhook notifications — Discord embed + generic JSON POST. Leave URLs
# empty to disable delivery (the EventDetector thread still runs but
# skips dispatch). Restart windrose-ui after editing these.
#
# Event types (restrict via WINDROSE_WEBHOOK_EVENTS):
#   server.online / server.offline   — game process appears / disappears
#   player.join / player.leave       — AccountId appears in / drops from snapshot
#   backup.created / backup.restored — /api/backups activity
#   config.applied                   — admin console Apply + restart path
WINDROSE_DISCORD_WEBHOOK_URL=${WINDROSE_DISCORD_WEBHOOK_URL}
WINDROSE_WEBHOOK_URL=${WINDROSE_WEBHOOK_URL}
WINDROSE_WEBHOOK_EVENTS=${WINDROSE_WEBHOOK_EVENTS}
WINDROSE_WEBHOOK_POLL_SECONDS=${WINDROSE_WEBHOOK_POLL_SECONDS}
WINDROSE_WEBHOOK_TIMEOUT=${WINDROSE_WEBHOOK_TIMEOUT}
EOF

# Preserve operator-added keys (anything NOT in _MANAGED_KEYS that
# was in the existing env file) under a trailing section. PRESERVED_EXTRAS
# already ends with a newline from the merge loop so we append it raw
# rather than via a second here-doc.
if [ -n "${PRESERVED_EXTRAS}" ]; then
  {
    printf '\n# --- Operator additions (preserved across install.sh runs) ---\n'
    printf '%s' "${PRESERVED_EXTRAS}"
  } >> "${tmp_env}"
fi

install -m 0640 -o root -g "${WINDROSE_GROUP}" "${tmp_env}" "${WINDROSE_ENV_FILE}"
rm -f "${tmp_env}"

# --- Service units ----------------------------------------------------
write_unit() {
  local name="$1" body="$2"
  local path="/etc/systemd/system/${name}"
  local tmp; tmp="$(mktemp)"
  printf '%s' "${body}" > "${tmp}"
  install -m 0644 "${tmp}" "${path}"
  rm -f "${tmp}"
}

log "writing systemd units"
write_unit "windrose-xvfb.service" "[Unit]
Description=Windrose Xvfb (virtual display :99)
After=network-online.target
Before=windrose-game.service

[Service]
Type=simple
User=${WINDROSE_USER}
Group=${WINDROSE_GROUP}
ExecStart=/usr/bin/Xvfb :99 -screen 0 1024x768x24 -nolisten tcp
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
"

write_unit "windrose-game.service" "[Unit]
Description=Windrose Dedicated Server (game under GE-Proton)
After=network-online.target windrose-xvfb.service
Wants=network-online.target
Requires=windrose-xvfb.service

[Service]
Type=simple
User=${WINDROSE_USER}
Group=${WINDROSE_GROUP}
WorkingDirectory=${WINDROSE_HOME}
EnvironmentFile=${WINDROSE_ENV_FILE}
ExecStart=${WINDROSE_INSTALL_DIR}/scripts/entrypoint.sh
# KillMode=control-group (default) makes Apply+Restart and unit stops
# also take wineserver/umu-run children with them — the game's shutdown
# path is a SIGTERM fan-out cascade that can leave zombies otherwise.
TimeoutStopSec=90
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"

write_unit "windrose-ui.service" "[Unit]
Description=Windrose Admin Console (stdlib Python HTTP)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${WINDROSE_USER}
Group=${WINDROSE_GROUP}
WorkingDirectory=${WINDROSE_HOME}
EnvironmentFile=${WINDROSE_ENV_FILE}
# Make the game pid visible so the UI's pgrep-based serverRunning check
# works. The k8s side gets this via shareProcessNamespace; on bare
# Linux the UI already sees the whole host PID namespace by default,
# so no twist needed — this just confirms the expectation.
ExecStart=/usr/bin/python3 ${WINDROSE_INSTALL_DIR}/scripts/ui/server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"

# --- Reload + enable --------------------------------------------------
systemctl daemon-reload
systemctl enable --now windrose-xvfb.service windrose-ui.service windrose-game.service

log "done."
echo
echo "  Services run as:   ${WINDROSE_USER} (non-root; systemd units at"
echo "                      /etc/systemd/system/windrose-{xvfb,game,ui}.service)"
echo "  Game data lives:   ${WINDROSE_HOME}/windrose/"
echo "  Env file (edit):   ${WINDROSE_ENV_FILE}"
echo "  Tail game logs:    sudo journalctl -fu windrose-game"
echo "  Tail UI logs:      sudo journalctl -fu windrose-ui"
echo
echo "  Admin console:     http://${UI_BIND}:${UI_PORT}/"
if [ "${UI_BIND}" = "127.0.0.1" ]; then
echo "                      (loopback-only by default — reach it via SSH tunnel:"
echo "                       'ssh -L ${UI_PORT}:127.0.0.1:${UI_PORT} root@<this host>'"
echo "                       then browse http://127.0.0.1:${UI_PORT}/ locally)"
echo "  To expose over LAN/WAN: set UI_BIND=0.0.0.0 AND UI_PASSWORD=... in"
echo "                          ${WINDROSE_ENV_FILE}, then systemctl restart windrose-ui"
fi
