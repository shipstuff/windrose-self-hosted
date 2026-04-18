# Bare Linux Install (deferred)

A systemd-based install path similar to the one shipped by
`enshrouded-self-hosted/bare-linux/` is planned but not yet implemented.

The Kubernetes StatefulSet and Docker Compose paths in the repo root are the
supported install options at the moment. The same `image/entrypoint.sh` will
drive the bare-Linux install once it lands here, so any behavior changes to
that script should assume it will eventually run under `systemd --user`.

If you need a bare-Linux install right now, the short version is:

1. Pack the Windrose server files on your workstation:
   `bash tools/pack-windowsserver.sh ~/windrose-server.tgz`
2. Install Debian 13 (or compatible), the deps listed in
   [`image/Dockerfile`](../image/Dockerfile) (`busybox procps winbind dbus libfreetype6 curl jq unzip tar gzip`).
3. Copy `image/entrypoint.sh` and `image/ServerDescription_example.json` onto
   the host.
4. Unpack the tarball into `$HOME/windrose/WindowsServer/`.
5. Run `image/entrypoint.sh` with `FILES_IMPORT_MODE=0` and `UI_BIND=127.0.0.1`.

A first-class installer will be tracked as a follow-up.
