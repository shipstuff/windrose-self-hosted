# How To Install A Windrose Self-Hosted Server

Open-source deployment bundle for running a Windrose dedicated server on Kubernetes or with Docker Compose. Runs the Windows-only dedicated server binary under Proton.

This is a community project. It is not affiliated with or endorsed by the Windrose developers. The only hosting Windrose officially supports is Nitrado — see the [Windrose FAQ](https://playwindrose.com/faq/).

## What Makes This Different

Anonymous SteamCMD cannot pull the Windrose dedicated-server depot (app id `4129620` is gated), so this repo does not try. Instead, the operator packs the `WindowsServer` folder from their own Steam install once, uploads it through a small browser UI the server exposes on first boot, and the server takes it from there. Save data is preserved across re-uploads (game updates).

The pod runs three containers:

- **`windrose`** — the game itself under GE-Proton. Only runs the game binary; no backgrounded work in its shell (Proton hates shell job-control races with Xvfb).
- **`xvfb`** — a dedicated X display server on `:99`, shared into the game container via an `emptyDir` at `/tmp/.X11-unix`. Lives in its own container so its signal space can't interfere with Proton.
- **`windrose-ui`** — a stdlib-only Python admin console (served from the same image as the game container via `python3 /opt/windrose-ui/server.py`). Exposes the invite-code card, server/players/resources status, config editor, backups, per-world editor, manual `WindowsServer` upload, and Discord/generic webhook dispatch. Shares the PVC with the game container so both see the same filesystem.

All three share the pod's PID namespace (`shareProcessNamespace: true`) so the UI sidecar can `pgrep` for the game process.

Other Windrose dockerizations exist — this one leans on patterns we already operate in [`enshrouded-self-hosted`](https://github.com/shipstuff/enshrouded-self-hosted): GE-Proton, non-root container, PVC-backed persistence, host networking, Helm + plain manifests + Docker Compose in sync.

## Choose An Install Path

- [Install On Kubernetes With Helm (primary)](#install-on-kubernetes-with-helm)
- [Install On Kubernetes With Plain Manifests Or Kustomize](#install-on-kubernetes-with-plain-manifests-or-kustomize)
- [Install With Docker Compose](#install-with-docker-compose)

A bare-Linux `systemd --user` path is planned ([`bare-linux/README.md`](bare-linux/README.md)).

## Published Images And Helm Chart

- `ghcr.io/shipstuff/windrose-server` — game container
- `ghcr.io/shipstuff/windrose-ui` — UI sidecar
- `oci://ghcr.io/shipstuff/charts/windrose` — Helm chart

## Critical Setting: `P2pProxyAddress`

The single knob operators most need to understand. The dedicated server advertises this value verbatim to Windrose's backend as its ICE **host candidate** — the address clients attempt to connect to. If it is `0.0.0.0`, the Windows client rejects it (`WSAEFAULT 1214`), the UE P2P consent-check times out after ~10 s, and players **silently bounce back to the main menu with no error**.

**Default behavior: auto-detect.** When `P2P_PROXY_ADDRESS` is unset (or `0.0.0.0`), the entrypoint opens a `SOCK_DGRAM` socket, `connect()`s it to `8.8.8.8:53` (no packets sent), and reads back `getsockname()` — the kernel's answer to "what source IP would I use to reach the internet from this host." Under `hostNetwork: true` that's the LAN/WAN-facing interface, which is exactly what we want to advertise. Works the same in k8s, compose, bare-Linux — no Downward API needed, no interface enumeration, no iproute2 dependency.

**When to override** (`serverConfig.p2pProxyAddress` in Helm, `P2P_PROXY_ADDRESS` in compose, env var on bare-Linux):

- Multi-homed hosts where the default-route interface isn't the one you want clients to hit.
- WAN-first deployments where you'd rather advertise the public IP than the LAN IP (most operators don't need this — ICE's srflx candidate handles WAN via STUN automatically).
- Air-gapped environments where `8.8.8.8:53` isn't reachable and auto-detect falls back to `0.0.0.0`.

## Step 0: Get The WindowsServer Bundle Into The Pod

The Windrose dedicated-server depot (app id `4129620`) is **gated** — anonymous SteamCMD cannot download it. Bring your own `WindowsServer/` folder from a Steam install of Windrose (~2.8 GiB uncompressed; contains the UE5 engine + all server content). One-time per cluster; the PVC persists across restarts. Game patches require a re-upload of just `WindowsServer/` (saves survive).

### Locate your `WindowsServer/` folder

The folder is inside your Steam install of Windrose, at `<Steam library>/steamapps/common/Windrose/R5/Builds/WindowsServer/`. Typical `<Steam library>` location by platform:

| Platform | Typical path |
|---|---|
| Windows | `C:\Program Files (x86)\Steam` (or wherever you set the Steam library) |
| WSL (operator shell on Windows) | `/mnt/c/Program Files (x86)/Steam` (Windows Steam surfaced into WSL) |
| Linux (Steam + Proton) | `~/.steam/steam` or `~/.local/share/Steam` |

If Steam has multiple library folders, the exact per-library location is recorded in `steamapps/libraryfolders.vdf`. The `tools/pack-windowsserver.sh` script parses that file automatically on WSL and Linux.

### A. Direct copy via `kubectl cp` (recommended for LAN)

Apply the chart first so the pod is running in the files-waiting state:

```bash
helm upgrade --install windrose ./helm/windrose --namespace games --create-namespace
kubectl -n games wait --for=condition=Ready pod/windrose-0 --timeout=5m || true
```

Then push the folder straight into the PVC via the UI sidecar. Pick the command for your workstation:

**Windows (PowerShell):**
```powershell
kubectl -n games cp `
  "C:\Program Files (x86)\Steam\steamapps\common\Windrose\R5\Builds\WindowsServer" `
  "windrose-0:/home/steam/windrose/WindowsServer" `
  -c windrose-ui
```

**WSL:**
```bash
kubectl -n games cp \
  "/mnt/c/Program Files (x86)/Steam/steamapps/common/Windrose/R5/Builds/WindowsServer" \
  windrose-0:/home/steam/windrose/WindowsServer \
  -c windrose-ui
```

**Linux (Steam Proton install):**
```bash
STEAM_DIR="${STEAM_DIR:-$HOME/.steam/steam}"
kubectl -n games cp \
  "$STEAM_DIR/steamapps/common/Windrose/R5/Builds/WindowsServer" \
  windrose-0:/home/steam/windrose/WindowsServer \
  -c windrose-ui
```

(Copying via the `windrose-ui` sidecar avoids the game container's tighter PATH/tools.) The entrypoint polls for the binary and proceeds to launch as soon as it appears. `kubectl cp` streams through the k8s API and has no practical size limit.

### B. Pack-and-upload via the UI (recommended for remote / compose / bare-Linux)

Pack `WindowsServer/` into a tarball on your workstation, then POST it to `/api/upload`. The UI at `http://windrose.local` (or whatever hostname you configured for the Ingress) has a file picker that does the same POST.

**Windows (PowerShell — `tar.exe` ships with Windows 10+):**
```powershell
$src = 'C:\Program Files (x86)\Steam\steamapps\common\Windrose\R5\Builds'
tar.exe -czf "$HOME\windrose-server.tgz" -C $src WindowsServer
```

**WSL:**
```bash
tar -czf ~/windrose-server.tgz \
  -C "/mnt/c/Program Files (x86)/Steam/steamapps/common/Windrose/R5/Builds" \
  WindowsServer
# Or use the helper script, which auto-locates via libraryfolders.vdf:
bash tools/pack-windowsserver.sh ~/windrose-server.tgz
```

**Linux (Steam Proton install):**
```bash
STEAM_DIR="${STEAM_DIR:-$HOME/.steam/steam}"
tar -czf ~/windrose-server.tgz \
  -C "$STEAM_DIR/steamapps/common/Windrose/R5/Builds" \
  WindowsServer
# Or: bash tools/pack-windowsserver.sh ~/windrose-server.tgz
```

Then upload either through the browser or via curl:

```bash
curl --fail --data-binary @~/windrose-server.tgz \
  -H 'Content-Type: application/octet-stream' \
  -H 'X-Filename: windrose-server.tgz' \
  http://windrose.local/api/upload
```

(PowerShell equivalent uses `Invoke-WebRequest -InFile ... -ContentType ... -Headers @{...}`.)

### C. Authenticated SteamCMD (not implemented)

`steamcmd +login <user> +app_update 4129620` works if the Steam account owns the game, but Steam 2FA makes it hard to automate. Not wired up today.

## Install On Kubernetes With Helm

```bash
helm upgrade --install windrose ./helm/windrose \
  --namespace games --create-namespace
```

Install the published OCI chart instead (once released):

```bash
helm upgrade --install windrose oci://ghcr.io/shipstuff/charts/windrose \
  --version 0.1.0 \
  --namespace games --create-namespace
```

Typical overrides:

```bash
helm upgrade --install windrose ./helm/windrose \
  --namespace games --create-namespace \
  --set serverConfig.serverName="Salty Seas" \
  --set serverConfig.maxPlayerCount=4 \
  --set worldConfig.islandId=saltyseas \
  --set worldConfig.presetType=Hard
```

Password-protect with a Secret:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: windrose-password
  namespace: games
stringData:
  password: "replace-me"
```

```bash
helm upgrade --install windrose ./helm/windrose \
  --namespace games --create-namespace \
  --set serverConfig.isPasswordProtected=true \
  --set serverConfig.passwordSecret.name=windrose-password
```

Three config-ownership modes, mirroring the Enshrouded chart:

- `serverConfig.mode=env` (default) — seed the example `ServerDescription.json`, patch known keys from env on every start.
- `serverConfig.mode=managed` — render `serverConfig.inlineJson` into a ConfigMap, merge `Password` from the optional Secret.
- `serverConfig.mode=mutable` — require a pre-existing `ServerDescription.json` on the PVC and leave it alone.

Override `P2pProxyAddress` if auto-detect picks the wrong interface (multi-homed hosts, WAN-first deployments):

```bash
helm upgrade windrose ./helm/windrose --reuse-values \
  --set serverConfig.p2pProxyAddress=203.0.113.7
```

## Install On Kubernetes With Plain Manifests Or Kustomize

```bash
kubectl apply -k .
```

Creates the `games` namespace, a 20 GiB PVC, the StatefulSet (`hostNetwork: true`, `nodeSelector: kubernetes.io/hostname: worker-01` as an example — edit to match your node, or drop the selector if persistence is on network storage), a ClusterIP Service for the UI (`publishNotReadyAddresses: true` so the UI stays reachable during game restarts), and an nginx Ingress for `windrose.local`.

After the pod is running and WindowsServer is uploaded (Step 0), open `http://windrose.local` — the Invite card shows the six-character code.

Raw access without the Ingress:

```bash
kubectl -n games port-forward svc/windrose 28080:28080
```

## Install With Docker Compose

Optional `.env` for common overrides (auto-detect handles `P2P_PROXY_ADDRESS` on its own unless you're multi-homed or need a specific IP):

```ini
SERVER_NAME=My Windrose
MAX_PLAYER_COUNT=4
WORLD_ISLAND_ID=my-island
# P2P_PROXY_ADDRESS=192.168.1.100  # override if auto-detect picks the wrong interface
# UI_BIND=0.0.0.0                  # uncomment to expose UI on LAN
```

Then:

```bash
docker compose up -d
```

Build locally instead of pulling:

```bash
docker compose up -d --build
```

All three containers come up: `windrose` (game, `network_mode: host`), `xvfb` (display server), `windrose-ui` (UI on `127.0.0.1:28080` by default).

## Configure Server Runtime

The server config lives at `/home/steam/windrose/WindowsServer/R5/ServerDescription.json`. In `env` mode the entrypoint patches known keys from env on every start.

| Env var | Default | Purpose |
|---|---|---|
| `SERVER_NAME` | `Windrose Server` | Informational |
| `INVITE_CODE` | generated | Six-plus character alphanumeric code. Backend mints one if unset. |
| `IS_PASSWORD_PROTECTED` | `false` | Toggle password gate |
| `SERVER_PASSWORD` | `` | Password when `IS_PASSWORD_PROTECTED=true` |
| `MAX_PLAYER_COUNT` | `4` | 4 per official guide; up to 10 with more RAM |
| `WORLD_ISLAND_ID` | `default-world` | Matches folder under `Saved/.../RocksDB/<GameVersion>/Worlds/`. See caveat below — backend assigns the real ID. |
| `WORLD_NAME` | `Default Windrose World` | Display name |
| `WORLD_PRESET_TYPE` | `Medium` | `Easy`, `Medium`, `Hard`, `Custom` |
| `P2P_PROXY_ADDRESS` | auto-detected (UDP-connect getsockname trick; falls back to `0.0.0.0` if the trick fails) | The ICE host candidate advertised to clients. Override only if auto-detect picks the wrong interface. |
| `WINDROSE_CONFIG_MODE` | `env` | `env`, `managed`, or `mutable` |
| `WINDROSE_LAUNCH_STRATEGY` | `shipping` | `shipping` (headless, recommended) or `launcher` (`WindroseServer.exe`) |
| `FILES_WAIT_TIMEOUT_SECONDS` | `0` | 0 = wait forever for WindowsServer files |
| `PROTON_USE_XALIA` | `0` | Xalia crashes on our headless Proton; leave off. |
| `DISABLE_SENTRY` | `1` | Crashpad hard-aborts under wine; keep Sentry disabled unless you're debugging. |
| `UI_BIND` | `127.0.0.1` (compose) / `0.0.0.0` (k8s) | UI httpd bind address |
| `UI_PORT` | `28080` | UI httpd port |

## Update The Server On Game Patch

When Windrose ships a patch, the dedicated-server binary bumps its `<GameVersion>` save-path segment. Entrypoint migrates worlds forward automatically when it sees ≥2 version folders under `RocksDB/`.

### Preferred: re-upload via the UI (or its API)

The UI's upload endpoint (`/api/upload`) preserves the save, `ServerDescription.json` (the server's **identity** — PersistentServerId, WorldIslandId, InviteCode), and `WorldDescription.json` across replacement, and snapshots the old tree to `/home/steam/backups/<utc>/` (last 5 kept). This is the only flow that keeps your save tied to your old island; a bare wipe orphans it (see caveat below).

**Via the UI:**

1. Update Windrose via Steam on your workstation.
2. Pack the new `WindowsServer/`:
   ```bash
   tar -czf ~/windrose-server.tgz \
     -C "/mnt/c/Program Files (x86)/Steam/steamapps/common/Windrose/R5/Builds" \
     WindowsServer
   ```
3. Open `http://windrose.local`, drop the tarball into the Upload form, click **Upload update**.
4. Restart the game container to load the new binary:
   - k8s: `kubectl -n games delete pod windrose-0`
   - compose: `docker compose restart windrose`
5. On next boot the entrypoint detects the new `<GameVersion>` folder and migrates worlds forward. Backend sees your preserved PSID and hands back the same island.

**Via curl (same endpoint, scriptable):**

```bash
tar -czf ~/windrose-server.tgz \
  -C "/mnt/c/Program Files (x86)/Steam/steamapps/common/Windrose/R5/Builds" \
  WindowsServer

curl --fail --data-binary @~/windrose-server.tgz \
  -H 'Content-Type: application/octet-stream' \
  -H 'X-Filename: windrose-server.tgz' \
  http://windrose.local/api/upload

# Then restart the game container:
kubectl -n games delete pod windrose-0
```

The endpoint accepts `.tar.gz` / `.tar` / `.zip`, auto-detects format by magic bytes first + filename second. Response is plain-text and ends with the backup path for rollback. Restart orchestration from the UI/API is a planned TODO — today the restart is the one remaining manual step.

### Fallback: kubectl cp

Faster on LAN, but **requires you to preserve `ServerDescription.json` manually** or your save gets orphaned. Use this recipe exactly:

```bash
# Back up identity + save to a safe path outside WindowsServer/.
kubectl -n games exec windrose-0 -c windrose-ui -- sh -c '
  ts=$(date -u +%Y%m%dT%H%M%SZ)
  mkdir -p /home/steam/backups/$ts
  cp -a /home/steam/windrose/WindowsServer/R5/Saved              /home/steam/backups/$ts/
  cp -a /home/steam/windrose/WindowsServer/R5/ServerDescription.json /home/steam/backups/$ts/ 2>/dev/null || true
  cp -a /home/steam/windrose/WindowsServer/R5/WorldDescription.json  /home/steam/backups/$ts/ 2>/dev/null || true
  echo "$ts" > /home/steam/backups/latest
'

# Scale down, clear, scale up into file-wait state.
kubectl -n games scale statefulset/windrose --replicas=0
kubectl -n games scale statefulset/windrose --replicas=1
kubectl -n games wait --for=condition=Ready pod/windrose-0 --timeout=5m
kubectl -n games exec windrose-0 -c windrose-ui -- sh -c '
  rm -rf /home/steam/windrose/WindowsServer
  mkdir -p /home/steam/windrose/WindowsServer
'

# Copy fresh files straight in (empty target → no nesting).
kubectl -n games cp \
  "/mnt/c/Program Files (x86)/Steam/steamapps/common/Windrose/R5/Builds/WindowsServer/." \
  windrose-0:/home/steam/windrose/WindowsServer \
  -c windrose-ui

# Restore save + identity on top of the fresh tree.
kubectl -n games exec windrose-0 -c windrose-ui -- sh -c '
  ts=$(cat /home/steam/backups/latest)
  mkdir -p /home/steam/windrose/WindowsServer/R5
  cp -a /home/steam/backups/$ts/Saved              /home/steam/windrose/WindowsServer/R5/
  cp -a /home/steam/backups/$ts/ServerDescription.json /home/steam/windrose/WindowsServer/R5/ 2>/dev/null || true
  cp -a /home/steam/backups/$ts/WorldDescription.json  /home/steam/windrose/WindowsServer/R5/ 2>/dev/null || true
'
```

### Why identity matters

The game stores its `PersistentServerId` in `ServerDescription.json`. On registration, Windrose's backend looks up the island keyed off PSID — your save's `WorldIslandId` is tied to the PSID that originally owned it. If you nuke `ServerDescription.json` as part of an update, the game mints a fresh PSID on next boot, the backend hands you a brand-new island, and your old save sits on disk untied to any server the backend knows about. See `memory/windrose_island_identity.md`.

## Retrieve The Invite Code

From the UI: the big code on the **Invite** card.

From the CLI:

```bash
# k8s
kubectl -n games exec windrose-0 -c windrose-ui -- \
  jq -r .ServerDescription_Persistent.InviteCode \
  /home/steam/windrose/WindowsServer/R5/ServerDescription.json

# via port-forward
kubectl -n games port-forward svc/windrose 28080:28080 &
curl -s -u admin:$PASSWORD http://127.0.0.1:28080/api/invite
```

## Send Notifications Via Discord Or Generic Webhooks

The admin console's UI container runs a small event detector in a background thread. Every `WINDROSE_WEBHOOK_POLL_SECONDS` (default 15 s) it diffs game state against the previous snapshot and fires events. Events are best-effort — they are dispatched from short-lived threads so a slow webhook cannot stall the poller, and failures are logged to the container's stderr but otherwise swallowed.

Event types:

| Event             | Fires when                                                         |
| ----------------- | ------------------------------------------------------------------ |
| `server.online`   | The game process appears (post-restart or first boot).             |
| `server.offline`  | The game process goes away (crash, `stop`, pod eviction).          |
| `player.join`     | A new `AccountId` appears in the connected-players snapshot.       |
| `player.leave`    | An `AccountId` drops out of the connected-players snapshot.        |
| `backup.created`  | The admin console's **Create backup now** or `POST /api/backups`.  |
| `backup.restored` | A backup is restored via `POST /api/backups/{id}/restore`.         |
| `config.applied`  | Config changes are applied via **Apply + restart**.                |

Restrict the fired set with `WINDROSE_WEBHOOK_EVENTS` (comma-separated). Empty URLs disable delivery entirely — leave both URLs unset to suppress webhooks even if the event list is populated.

Two targets are supported and fire in parallel when both are set:

- `WINDROSE_WEBHOOK_URL` — generic `application/json` POST. Body is `{"event": "…", "timestamp": "…", …event-specific fields}`.
- `WINDROSE_DISCORD_WEBHOOK_URL` — formatted as a Discord embed with a color-coded title and a one-line description.

### Helm

Inline URLs are fine for private clusters. For anything shared, drop them into a Secret and point Helm at it:

```bash
kubectl -n games create secret generic windrose-webhooks \
  --from-literal=discord-webhook-url='https://discord.com/api/webhooks/…'
```

```yaml
# values-local.yaml
ui:
  webhooks:
    events: "server.online,server.offline,player.join,player.leave,backup.created"
    pollSeconds: 15
    timeout: 5
    discordUrlSecret:
      name: windrose-webhooks
      key: discord-webhook-url
    # Or inline (not recommended for real deployments):
    # discordUrl: "https://discord.com/api/webhooks/…"
    # url: "https://my-listener.example.com/windrose"
```

`urlSecret` / `discordUrlSecret` take precedence over the inline `url` / `discordUrl` values when both are set.

### Plain Manifests

The stock `statefulset.yaml` ships commented `secretKeyRef` blocks for `WINDROSE_WEBHOOK_URL` and `WINDROSE_DISCORD_WEBHOOK_URL`. Uncomment either (or both), create the Secret, and apply.

### Docker Compose

Set the env vars on the host or in a `.env` next to `docker-compose.yaml`:

```bash
WINDROSE_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/… \
WINDROSE_WEBHOOK_EVENTS=server.online,server.offline,player.join,player.leave,backup.created \
docker compose up -d
```

### Verifying Delivery

Tail the UI container for dispatch results — each successful POST logs `[webhook:url]` or `[webhook:discord]` with the HTTP status:

```bash
kubectl -n games logs -f windrose-0 -c windrose-ui | grep webhook
```

Trigger a synthetic `backup.created` from the admin console (**Create backup now**) to confirm the plumbing without waiting for a real game transition.

## Validate Local Changes

```bash
kubectl kustomize . >/dev/null
helm lint ./helm/windrose
helm template windrose ./helm/windrose >/dev/null
shellcheck image/entrypoint.sh tools/pack-windowsserver.sh
python3 -m py_compile image/ui/server.py
bash image/ui/test_api.sh  # requires a running canary — see CLAUDE.md
```

CI runs these plus JSON validation and YAML lint.

## Windrose-Specific Caveats

- **Bring your own server files.** Gated depot; auto-update on boot is intentionally not implemented.
- **No official Linux support.** Windows binary only; we run it under GE-Proton.
- **No RCON.** No documented remote-admin protocol. Tune via `ServerDescription.json` / `WorldDescription.json` and restart.
- **Fronting the admin console with nginx.** The Python admin server is fine on its own but plays nicely behind an nginx proxy if you want basic-auth / OIDC / a CDN in front:
  1. **nginx handles auth, Python serves everything.** Leave `ui.password` empty; add `nginx.ingress.kubernetes.io/auth-type: basic` + `auth-secret` + `auth-realm` on the Ingress.
  2. **nginx serves static, Python serves `/api/*`.** Set `ui.serveStatic: false` in values, then bundle the assets into a ConfigMap nginx can mount:
     ```bash
     kubectl -n games create configmap windrose-ui-assets \
       --from-file=index.html=./image/ui/index.html \
       --from-file=app.css=./image/ui/app.css \
       --from-file=app.js=./image/ui/app.js
     ```
     Point nginx's `root` or `alias` at the mount and `proxy_pass` the `/api/*` location to the windrose Service.
- **GE-Proton version matters.** We pin `10-34`. `10-32` hit WSA error 996 (gRPC `UNAVAILABLE`) on backend registration for this UE5 build. If you bump, test backend registration before the registry; see [GE-Proton releases](https://github.com/GloriousEggroll/proton-ge-custom/releases).
- **World Island ID is backend-assigned.** When the server registers with Windrose's backend, the backend mints (or looks up) an island ID keyed off the server's `PersistentServerId`. Game syncs *down* from the backend — it does not discover an island ID *up* from local save files. Dropping a save onto disk doesn't make the server adopt it unless you can preserve the PSID that originally owned that island. See `memory/windrose_island_identity.md`.
- **Sentry/Crashpad is disabled.** Under Proton it hard-aborts the process ~5 s after launch. `DISABLE_SENTRY=1` in the entrypoint renames the plugin to `Sentry.DISABLED`; set `DISABLE_SENTRY=0` only if you're actively debugging Sentry behavior.
- **No US backend gateway.** The Windrose cloud runs auth/gRPC gateways only in KR (AWS ap-northeast-2), EU (AWS eu-central-1), and RU. Their Coturn relay has a US presence (`coturn-us.windrose.support`) but the gateway does not. US operators hit ~150 ms to KR or EU regardless.
- **Windrose backend instability is a thing.** The game binary is brittle against gRPC hiccups: any `RST_STREAM` or `502 Bad Gateway` from a gateway region fatal-asserts the dedicated-server process (`GsStream is broken. Cannot reconnect to Cm`) → exit code 3 → kubelet restart → the same stuck state if that region is still flaking. If you see the game consistently failing against one region, evict it:
  ```bash
  helm upgrade windrose ./helm/windrose --reuse-values \
    --set 'blackholeRegions={kr}'
  ```
  This sets a `hostAliases` entry that points the region's hostname at `192.0.2.1` (unreachable), so the game's ping skips it and falls back to another region. Revert by setting `blackholeRegions={}` once the region recovers. The `memory/windrose_backend_region_selection.md` note has the diagnostic signals.

---

# Contributing And Architecture

Everything below this line is for agents and humans editing this repo. Regular operators can stop here.

`AGENTS.md` and `CLAUDE.md` are symlinks to this file.

## What Lives Where

- `README.md` (this file): operator docs + contributor guide. Also served as `AGENTS.md` / `CLAUDE.md`.
- `TODO.md`: remaining work before/after the first commit.
- `image/`: game-server container image.
- `image/entrypoint.sh`: source of truth for startup — SteamCMD seed (for `steamclient.so` only; no app depot download), Proton seed, files-wait loop, save-version migration, config reconciliation, Xvfb-socket wait, `exec proton`.
- `image/ServerDescription_example.json` / `WorldDescription_example.json`: seed templates used in `env` config mode.
- `image/ui/`: UI sidecar assets (baked into the same game-server image as `/opt/windrose-ui/`). `server.py` is the stdlib-only admin console (status, invite, config editor, backups, per-world editor, webhooks); `index.html` + `app.css` + `app.js` are the browser UI; `test_api.sh` is the canary smoke test.
- `docker-compose.yaml`: three-service local deployment (game + xvfb + ui) with `network_mode: host` on the game.
- `namespace.yaml`, `pvc.yaml`, `statefulset.yaml`, `service.yaml`, `ingress.yaml`: plain Kubernetes manifests.
- `kustomization.yaml`: thin wrapper over the plain manifests.
- `helm/windrose/`: Helm chart mirroring the plain manifests. Keep values, templates, and this README in sync.
- `tools/pack-windowsserver.sh`: operator helper to tar `WindowsServer/` from a Steam install, locating it via `libraryfolders.vdf`.
- `tools/services/api/`: stub for the deferred live-stats API (no Windrose query port exists; would be log-tail-based).
- `bare-linux/`: stub for deferred systemd install path.
- `.github/workflows/`: CI (shellcheck, yamllint, kustomize render, helm template/lint), image publish, chart publish.

## Deployment Surfaces

Three first-class install paths today:

1. Helm via `helm/windrose` — primary path.
2. Plain Kubernetes via root manifests / Kustomize.
3. Docker Compose via `docker-compose.yaml`.

Bare-Linux is a planned fourth surface. When changing ports, env vars, image names, or runtime modes, update all three surfaces; they drift easily because each exposes similar knobs in a different form.

## Startup And Config Model

`image/entrypoint.sh` phases:

1. SteamCMD + Proton seed (fast path from baked cache in the image).
2. `wait_for_files` — block until `WindowsServer/WindroseServer.exe` or its Shipping binary appears. UI sidecar / `kubectl cp` populates the PVC while we wait.
3. Save-version migration: if a new `<GameVersion>` folder exists under `R5/Saved/.../RocksDB/`, copy prior world dirs forward so existing saves load after a game patch.
4. `ensure_world_layout` — patch `WorldDescription.json` for the known world (`env` mode only).
5. `reconcile_server_config` — patch `ServerDescription.json` from env (`env` mode), render from ConfigMap+Secret (`managed` mode), or leave untouched (`mutable` mode).
6. `maybe_disable_sentry` — rename the Sentry plugin directory to `Sentry.DISABLED` so Crashpad doesn't hard-abort the process under Proton.
7. `exec proton waitforexitandrun "${EXE}" -log` — Proton becomes PID 1; Xvfb is in the sibling sidecar.

Important specifics:

- Anonymous SteamCMD cannot fetch app `4129620`. Files-import is the intended bootstrap; there is no `AUTO_UPDATE_ON_BOOT` flag.
- `WINDROSE_CONFIG_MODE=mutable` requires an existing config on disk; the entrypoint will not create one.
- `WINDROSE_LAUNCH_STRATEGY=shipping` (the UE5 shipping binary) is the default for headless stability; `launcher` falls back to `WindroseServer.exe` which shells out to the same binary with a launcher wrapper.
- Xvfb lives in a sibling container. The game container's entrypoint waits up to 10 s for the X11 socket at `/tmp/.X11-unix/X99` before exec'ing Proton.

## UI Model

- One `index.html` drives both the files-import bootstrap and the steady-state admin console. The JS polls `/api/status` every 5 s and shows the upload card when `filesPresent=false`, then hides it and reveals the admin cards once the server is up and the operator signs in.
- `/api/upload` accepts `.tar.gz` / `.tar` / `.zip` containing a `WindowsServer/` tree (or the tree's root). It preserves `R5/Saved/`, `ServerDescription.json`, and `WorldDescription.json` across replacement and snapshots the previous tree to `/home/steam/backups/<utc>/`.
- `/api/status` has two views — the public view (unauth or no auth configured) omits `AccountId` from `players[]` and drops the `allowDestructive` / `stagedConfigPending` hints; the authed view returns the full payload. Admin routes (`/api/config`, `/api/backups`, `/api/upload`, `/api/server/stop`, per-world config) always require auth (401 without it).
- `server.py`'s `EventDetector` thread is what drives the webhooks above; it polls the same state the UI does and fires transition events from `fire_event(...)`.
- Default `UI_BIND=127.0.0.1` in compose; k8s manifests and Helm set it to `0.0.0.0` so the Service / port-forward / Ingress can reach it.

## Kubernetes And Helm Notes

- Plain StatefulSet and the Helm-rendered one must stay semantically aligned.
- Helm defaults: `serverConfig.mode=env`, `hostNetwork=true`, `serverConfig.p2pProxyAddress=""` (entrypoint auto-detects via UDP-connect + getsockname), `service.type=ClusterIP` (Ingress-fronted), `worldConfig.*` split from `serverConfig.*`.
- `shareProcessNamespace: true` is required for the UI sidecar's `pgrep`-based `serverRunning` check.
- `publishNotReadyAddresses: true` on the Service keeps the UI reachable during game-container restarts (useful when uploading a patched WindowsServer).
- `preStop` hooks on sidecars are **not safe**: AppArmor's containerd profile puts the original PID 1 on a stricter policy than the shell kubelet spawns for `preStop`, so `kill -TERM 1` silently drops. Both sidecars' entrypoints are trap-forwarding shells instead — kubelet's SIGTERM reaches the shell from the root namespace and the trap forwards to the child.

## Editing Rules For This Repo

- Prefer small, synchronized changes across docs and deployment surfaces over fixing only one path.
- Never commit real secrets. Passwords and webhook URLs come from env or Kubernetes Secrets.
- If you touch runtime defaults, update this README (and the env-var table above).
- If you change the UI JSON shape (`/api/status`, `/api/config`, etc.), update `image/ui/app.js` (and, where relevant, `image/ui/index.html`) in the same change. The browser and server must agree.
- If you touch published artifact names or tags, update both image and chart workflows in `.github/workflows/`.
- Don't add community-Docker-isms that depend on anonymous SteamCMD depot download — that path is blocked for Windrose and we intentionally do not try.
- `memory/` files under `~/.claude/projects/-home-seslly-seslly-github-windrose-self-hosted/memory/` are Claude Code's persistent notes. When you discover a non-obvious fact about how Windrose or Proton behaves (e.g. the `P2pProxyAddress` / ICE host-candidate fix, the `0.0.0.0 getsockname` dead end, the backend-assigned WorldIslandId), write it there so the next session has the context.

## Testing And Safety

- `windrose-0` in the `games` namespace is the **live validation pod** — expect it to be in use for real gameplay. Any agent-driven testing (shutdown timing, Helm template variants, image smoke) should go into a parallel Helm release (e.g. `windrose-test` in a separate namespace or with `fullnameOverride=windrose-test` and a different `service.port`) so the live pod is untouched.
- Saves live under `R5/Saved/SaveProfiles/Default/RocksDB/<GameVersion>/Worlds/<island>/`. A save's RocksDB can be read with `ldb` from `rocksdb-tools` (installed on the operator's WSL). Column families: `R5BLIsland`, `R5BLBuilding`, `R5BLActor_*`, `R5BLPlayerInWorld`, `R5BLResourceSpawnPoint`, etc. Useful for debugging world corruption, not a substitute for the backend's authoritative state.
