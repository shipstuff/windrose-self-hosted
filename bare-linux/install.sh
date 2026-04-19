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
#   WINDROSE_INSTALL_DIR         where image/* lands        (default: /opt/windrose)
#   UI_BIND                      UI listen interface        (default: 0.0.0.0)
#   UI_PORT                      UI listen port             (default: 28080)
#   UI_PASSWORD                  HTTP basic-auth password   (default: empty)
#   UI_ENABLE_ADMIN_WITHOUT_PASSWORD
#                                 explicit opt-in for destructive routes when
#                                 UI_PASSWORD is empty      (default: false)
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
IMAGE_SRC="${REPO_ROOT}/image"

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
install -d -o "${WINDROSE_USER}" -g "${WINDROSE_GROUP}" "${WINDROSE_INSTALL_DIR}/image"
install -d -o "${WINDROSE_USER}" -g "${WINDROSE_GROUP}" "${WINDROSE_INSTALL_DIR}/image/ui"

install -m 0755 -o "${WINDROSE_USER}" -g "${WINDROSE_GROUP}" \
  "${IMAGE_SRC}/entrypoint.sh" "${WINDROSE_INSTALL_DIR}/image/entrypoint.sh"
install -m 0644 -o "${WINDROSE_USER}" -g "${WINDROSE_GROUP}" \
  "${IMAGE_SRC}/ServerDescription_example.json" \
  "${WINDROSE_INSTALL_DIR}/image/ServerDescription_example.json"
install -m 0644 -o "${WINDROSE_USER}" -g "${WINDROSE_GROUP}" \
  "${IMAGE_SRC}/WorldDescription_example.json" \
  "${WINDROSE_INSTALL_DIR}/image/WorldDescription_example.json"
# The entrypoint copies example configs from /usr/local/share/ when it
# can't find them in $HOME yet — mirror the Docker layout there too so
# the same code path works with no env twist.
install -d /usr/local/share
install -m 0644 "${IMAGE_SRC}/ServerDescription_example.json" \
  /usr/local/share/ServerDescription_example.json
install -m 0644 "${IMAGE_SRC}/WorldDescription_example.json" \
  /usr/local/share/WorldDescription_example.json

for f in server.py index.html app.js app.css; do
  install -m 0644 -o "${WINDROSE_USER}" -g "${WINDROSE_GROUP}" \
    "${IMAGE_SRC}/ui/${f}" "${WINDROSE_INSTALL_DIR}/image/ui/${f}"
done
chmod 0755 "${WINDROSE_INSTALL_DIR}/image/ui/server.py"
# server.py expects /opt/windrose-ui/ when referenced from k8s; add a
# symlink so the UI path matches the container path. Harmless if it's
# already there from a previous install.
ln -snf "${WINDROSE_INSTALL_DIR}/image/ui" /opt/windrose-ui

# --- Xvfb socket dir --------------------------------------------------
install -d -m 1777 /tmp/.X11-unix

# --- Env file ---------------------------------------------------------
# Never clobber an existing env file — operators edit /etc/windrose/windrose.env
# directly for things the installer doesn't know about (WINDROSE_CONFIG_MODE,
# BLACKHOLE_REGIONS, custom launch args, rotated passwords). Re-running
# install.sh used to silently overwrite the whole file and wipe those
# customizations — we learned that the hard way 2026-04-19 when re-running
# install.sh on the canary to test webhook env-var pass-through reset
# WINDROSE_CONFIG_MODE=mutable back to default env, which then started
# stamping WORLD_NAME from env on every restart.
install -d -o root -g "${WINDROSE_GROUP}" -m 0750 "${WINDROSE_ENV_DIR}"
if [ -f "${WINDROSE_ENV_FILE}" ]; then
  warn "${WINDROSE_ENV_FILE} already exists — preserving as-is."
  warn "If you want to reset with the latest installer defaults, rm the file first and re-run install.sh."
  # Skip env-file writing entirely. Services are (re)started below — the
  # existing env values take effect on that restart.
else
log "writing env file ${WINDROSE_ENV_FILE}"
tmp_env="$(mktemp)"
cat > "${tmp_env}" <<EOF
# Written by bare-linux/install.sh. Edit freely; windrose-game restart
# picks up changes. Any WINDROSE_* / UI_* / SERVER_* / WORLD_* env var
# the entrypoint understands is valid here.
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
install -m 0640 -o root -g "${WINDROSE_GROUP}" "${tmp_env}" "${WINDROSE_ENV_FILE}"
rm -f "${tmp_env}"
fi

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
ExecStart=${WINDROSE_INSTALL_DIR}/image/entrypoint.sh
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
ExecStart=/usr/bin/python3 ${WINDROSE_INSTALL_DIR}/image/ui/server.py
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
