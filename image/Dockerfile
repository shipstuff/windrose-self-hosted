FROM debian:13-slim

ARG CONTAINER_GID=10000
ARG CONTAINER_UID=10000

ENV DEBIAN_FRONTEND="noninteractive"
# Windrose Dedicated Server. Steam app id 4129620. Default bootstrap is
# anonymous SteamCMD (WINDROSE_SERVER_SOURCE=steamcmd, validated on the
# in-cluster canary and on the bare-Linux DO droplet). The UI sidecar
# (see ui/) also accepts uploads for operators running modded / pre-
# release builds (WINDROSE_SERVER_SOURCE=files).
ENV STEAM_APP_ID="4129620"
ENV HOME="/home/steam"
ENV WINDROSE_PATH="/home/steam/windrose"
ENV WINDROSE_SERVER_DIR="${WINDROSE_PATH}/WindowsServer"
# The game process creates/updates its ServerDescription.json under R5/,
# not at the app root. Point our env-mode patcher at that path.
ENV WINDROSE_SERVER_CONFIG="${WINDROSE_SERVER_DIR}/R5/ServerDescription.json"
ENV WINDROSE_SAVE_ROOT="${WINDROSE_SERVER_DIR}/R5/Saved/SaveProfiles/Default/RocksDB"
ENV EXTERNAL_CONFIG="0"
ENV GE_PROTON_VERSION="10-34"
ENV GE_PROTON_URL="https://github.com/GloriousEggroll/proton-ge-custom/releases/download/GE-Proton${GE_PROTON_VERSION}/GE-Proton${GE_PROTON_VERSION}.tar.gz"
ENV GE_PROTON_SEED_ROOT="/usr/local/share/proton-seed"
ENV STEAMCMD_PATH="/home/steam/steamcmd"
ENV STEAM_SDK64_PATH="/home/steam/.steam/sdk64"
ENV STEAM_SDK32_PATH="/home/steam/.steam/sdk32"
ENV STEAM_COMPAT_CLIENT_INSTALL_PATH="$STEAMCMD_PATH"
ENV STEAM_COMPAT_DATA_PATH="${STEAMCMD_PATH}/steamapps/compatdata/${STEAM_APP_ID}"
ENV UMU_ID="0"

RUN groupadd -g $CONTAINER_GID steam \
    && useradd -g $CONTAINER_GID -u $CONTAINER_UID -m steam \
    && dpkg --add-architecture i386 \
    && apt-get update \
    && apt-get install --no-install-recommends -y \
        procps \
        ca-certificates \
        winbind \
        dbus \
        libfreetype6 \
        libgnutls30 \
        xvfb \
        curl \
        jq \
        tar \
        gzip \
        locales \
        lib32gcc-s1 \
        python3 \
    && echo 'LANG="en_US.UTF-8"' > /etc/default/locale \
    && echo "en_US.UTF-8 UTF-8" >> /etc/locale.gen \
    && locale-gen \
    && rm -f /etc/machine-id \
    && dbus-uuidgen --ensure=/etc/machine-id \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p "${GE_PROTON_SEED_ROOT}" \
    && echo "Baking GE-Proton${GE_PROTON_VERSION} into image seed cache..." \
    && curl -fsSL "${GE_PROTON_URL}" | tar zxf - -C "${GE_PROTON_SEED_ROOT}"

COPY --chmod=755 entrypoint.sh /usr/local/bin/entrypoint.sh
COPY ServerDescription_example.json /usr/local/share/ServerDescription_example.json
COPY WorldDescription_example.json /usr/local/share/WorldDescription_example.json

# Admin console. Python stdlib-only HTTP server — no pip deps. Baked
# into the same image as the game binary so the UI sidecar runs via a
# command override. Previously this was a separate `windrose-ui` image
# with busybox httpd + CGI scripts; consolidated into one image to drop
# the build + version complexity.
COPY --chown=10000:10000 ui/ /opt/windrose-ui/
RUN chmod 755 /opt/windrose-ui/server.py

USER steam

RUN mkdir -p "$WINDROSE_PATH" \
    && mkdir -p "${STEAMCMD_PATH}/compatibilitytools.d" \
    && mkdir -p "${STEAMCMD_PATH}/steamapps/compatdata/${STEAM_APP_ID}" \
    && mkdir -p /home/steam/.steam

WORKDIR /home/steam
# The entrypoint `exec`s Proton as its final step, so Proton becomes PID 1
# (replacing bash) and handles its own signals. No init shim needed.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
