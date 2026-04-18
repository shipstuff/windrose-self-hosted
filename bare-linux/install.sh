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

UI_BIND="${UI_BIND:-0.0.0.0}"
UI_PORT="${UI_PORT:-28080}"
UI_PASSWORD="${UI_PASSWORD:-}"
UI_ENABLE_ADMIN_WITHOUT_PASSWORD="${UI_ENABLE_ADMIN_WITHOUT_PASSWORD:-false}"

log() { printf '[install] %s\n' "$*"; }

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
log "writing env file ${WINDROSE_ENV_FILE}"
install -d -o root -g "${WINDROSE_GROUP}" -m 0750 "${WINDROSE_ENV_DIR}"
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
EOF
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
echo "  Tail game logs:    sudo journalctl -fu windrose-game"
echo "  Tail UI logs:      sudo journalctl -fu windrose-ui"
echo "  Admin console:     http://${UI_BIND}:${UI_PORT}/"
echo "  Env file (edit):   ${WINDROSE_ENV_FILE}"
echo "  Game data lives:   ${WINDROSE_HOME}/windrose/"
