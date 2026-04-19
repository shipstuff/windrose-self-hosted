# Bare Linux Install

Run Windrose directly on a spare Linux box (Ubuntu 22.04+ / Debian 12+) as
three systemd system services: game + Xvfb + admin UI. Validated on an
Ubuntu 24.04 DigitalOcean droplet with 2 cores / 4 GiB RAM; should run
anywhere the [`image/Dockerfile`](../image/Dockerfile) deps are available.

## Sizing

An upstream idle-CPU bug in the dedicated server (UE5 task-worker
busy-spin, tracked on the [community thread](https://steamcommunity.com/app/3041230/discussions/0/807974232125564069/))
eats ~1.82 cores before any player is connected. Until that's fixed
upstream, the box needs real headroom above it. Validated on
DigitalOcean droplets 2026-04-18/19:

| Box                | Verdict                                                                 |
| ------------------ | ----------------------------------------------------------------------- |
| 1 vCPU / any RAM   | **Unplayable.** Idle bug pegs the single core; nothing left for the P2P handshake. Client sits in `UePreloginVerified`, Coturn resets after ~180 s, user bounces to menu. |
| 2 vCPU / 2 GB      | **Unplayable (RAM-bound).** Boots and idles clean for ~9 min, then a delayed ~500 MiB allocation blows past available RAM; the kernel locks up on page reclamation. |
| 2 vCPU / 4 GB, fresh world | **Marginal.** On slower shared vCPUs (typical DO / Hetzner / Linode small tiers) the idle bug + terrain generation saturate both cores during the first-connect handshake; Coturn times out at ~180 s and the client bounces to menu. |
| 2 vCPU / 4 GB, **with a pre-loaded save** | **Works reliably.** Pre-generating the world on a faster box and importing the save into the small host sidesteps the worst of the CPU spike. **Recommended path for any small-VPS deploy** — see § Pre-loading below. |
| ≥3 vCPU / 4 GB     | **Comfortable.** Real headroom above the idle bug; fresh-world first-connects complete cleanly. |

Two distinct floors:
- **CPU ≥ 2 vCPU** — the idle bug burns ~1.82 cores on dispatch that has
  no network peer to pace against, so 1 vCPU has nothing left for the
  handshake burst. The bug is not reachable from Engine.ini /
  ConsoleVariables.ini / launch args / Proton env vars; the Shipping
  binary is stripped enough that most of the relevant CVars aren't even
  compiled in. See `memory/windrose_idle_cpu_known_bug.md` for the
  exhaustive negative-result list.
- **RAM ≥ 4 GB** — 2 GB boxes survive only until a delayed working-set
  step-up ~9 min into steady state. Swap doesn't save them.

Memory sizing looks scarier than it is — the game's 2.7 GiB RSS is
mostly cold memory-mapped paks that park to swap harmlessly (see § Swap
and `memory_footprint_cold_pages.md`) — but the 9-minute step-up still
sets the RAM floor above 2 GB regardless of how much swap you provision.

## Quick Install

From a checkout of this repo, as root on the target host:

```bash
sudo ./bare-linux/install.sh
```

Defaults are safe: non-root services, UI on loopback, no public exposure.
See § What The Install Includes for the full picture.

### What The Install Includes

- **OS packages**: `xvfb`, `python3`, `winbind`, `dbus`, `libfreetype6`,
  `libgnutls30`, `lib32gcc-s1` (i386 enabled for Proton), `curl`, `jq`,
  `tar`, `gzip`, `unzip`.
- **A dedicated `steam` user**, created if missing. The install script
  needs root (apt, systemd, `/etc/windrose/`), but **the three services
  all run as `steam`, not root** — SteamCMD, GE-Proton, the game, and
  the admin console never see root privileges. Override the user with
  `WINDROSE_USER=...` at install time if you want to reuse an existing
  account.
- **Three systemd system units**, all running as `steam`:
  - `windrose-xvfb.service` — virtual display on `:99`
  - `windrose-game.service` — the game under GE-Proton
  - `windrose-ui.service`   — the Python admin console (stdlib, no deps)
- **Files under `/opt/windrose/`** (the installed code) and
  **`/home/steam/windrose/`** (the PVC-equivalent: game binaries,
  saves, backups). `/etc/windrose/windrose.env` holds all runtime knobs,
  owned by `root:steam` with mode `0640` so the `steam` user can read
  but not rewrite it.
- **The admin UI binds to `127.0.0.1` by default**. That's safe on any
  VPS out of the box — nothing listens externally, nothing can be
  probed. Reach it via SSH tunnel:
  ```bash
  ssh -L 28080:127.0.0.1:28080 root@<your host>
  # then browse http://127.0.0.1:28080/ locally
  ```
  To expose over LAN/WAN, **set `UI_PASSWORD` first**, then bind to
  `0.0.0.0`:
  ```bash
  sudo UI_BIND=0.0.0.0 UI_PASSWORD='hunter2' ./bare-linux/install.sh
  ```
  The installer warns if you pass `UI_BIND=0.0.0.0` without a password.

On first boot the game service runs SteamCMD anonymously to pull app
`4129620` (~3 GiB) into `/home/steam/windrose/WindowsServer/`, then
hands off to Proton. Subsequent restarts are fast — SteamCMD only
re-checks for updates.

Tail it:

```bash
sudo journalctl -fu windrose-game
sudo journalctl -fu windrose-ui
```

## Overrides

All env vars are read from `/etc/windrose/windrose.env`. The installer
seeds sensible defaults; override at install time with env vars prefixed
to the install command:

```bash
sudo UI_PASSWORD='hunter2' SERVER_NAME='Salty Seas' MAX_PLAYER_COUNT=6 \
     ./bare-linux/install.sh
```

Or just edit the env file and `systemctl restart windrose-game` after.

| Install env var | Default | Purpose |
|---|---|---|
| `WINDROSE_USER` | `steam` | owner of the install + services |
| `WINDROSE_INSTALL_DIR` | `/opt/windrose` | where `image/` lands |
| `UI_BIND` | `127.0.0.1` | UI listen iface. Loopback-only by default — reach it via SSH tunnel or reverse-proxy. Set to `0.0.0.0` to expose; the installer warns if you do so without `UI_PASSWORD`. |
| `UI_PORT` | `28080` | UI listen port |
| `UI_PASSWORD` | empty | HTTP basic-auth password. Strongly recommended for any publicly-reachable host. |
| `UI_ENABLE_ADMIN_WITHOUT_PASSWORD` | `false` | explicit opt-in for destructive routes when no password is set — LAN-only |
| `SERVER_NAME` | `Windrose Bare-Linux` | informational |
| `MAX_PLAYER_COUNT` | `4` | 4 is the vendor guide; up to 10 with more RAM |
| `WORLD_NAME` | `Default Windrose World` | display name |
| `WORLD_PRESET_TYPE` | `Medium` | `Easy`, `Medium`, `Hard`, `Custom` |
| `P2P_PROXY_ADDRESS` | auto-detected | ICE host candidate. Leave empty unless the host is multi-homed. |
| `WINDROSE_SERVER_SOURCE` | `steamcmd` | `steamcmd` (anonymous app_update) or `files` (BYO tarball via UI) |

## Pre-loading The World (recommended for small VPSes)

On a small host (2 vCPU / 4 GB on a typical shared-vCPU VPS), letting
the server generate a fresh world *on the first player's connect* is
the failure mode. The idle-CPU bug already eats ~1.82 cores; adding
terrain generation on top of that saturates both cores and the P2P
handshake datagrams can't drain before Coturn resets the relay.
Result: user bounces to menu, retry sometimes works once the world
is partially cached, sometimes doesn't.

**The workaround is boring and reliable**: generate the world on a
beefier box first, then drop the resulting `Saved/` + identity into
the small host. The server skips the generation spike on first
connect and the handshake completes comfortably even under the
idle-bug ceiling.

The "beefier box" can be anything that handles fresh-world gen
cleanly (your workstation, a short-lived 4 vCPU droplet, an existing
k8s cluster). Spin up Windrose there, connect once to generate the
world, disconnect, stop the server, and tar the save + identity:

```bash
# On the beefy source host:
sudo systemctl stop windrose-game
cd /home/steam/windrose/WindowsServer/R5
sudo tar -czf /tmp/windrose-warm-save.tgz ServerDescription.json Saved
sudo systemctl start windrose-game   # source can come back online

# Drop it on your local workstation:
scp root@<beefy-host>:/tmp/windrose-warm-save.tgz ~/
```

Then on the small-VPS target, after `./bare-linux/install.sh` has
finished its SteamCMD pull:

```bash
sudo systemctl stop windrose-game
sudo rm -rf /home/steam/windrose/WindowsServer/R5/Saved
sudo rm -f  /home/steam/windrose/WindowsServer/R5/ServerDescription.json
sudo tar -xzf ~/windrose-warm-save.tgz \
  -C /home/steam/windrose/WindowsServer/R5/
sudo chown -R steam:steam /home/steam/windrose/WindowsServer/R5/
sudo systemctl start windrose-game
```

First boot after the restore re-registers with Windrose's backend
using the preserved `PersistentServerId`, so you keep the same
island and invite code.

## Migrating An Existing Save

Save data + backend identity (`PersistentServerId` → island binding) live
under `/home/steam/windrose/WindowsServer/R5/`:

```
ServerDescription.json            — identity (PSID, InviteCode, WorldIslandId)
Saved/SaveProfiles/Default/...    — RocksDB world state, per-world descriptions
```

To move a save from k8s / compose to bare Linux: stop the source server,
tar those two paths, extract on the target over the freshly SteamCMD'd
install. First boot after restore will register with the backend using
the preserved PSID and hand back the original island.

```bash
# On the source (e.g. a k8s pod):
kubectl -n games exec windrose-0 -c windrose-ui -- \
  tar -czf /tmp/save.tgz -C /home/steam/windrose/WindowsServer/R5 \
  ServerDescription.json Saved
kubectl -n games cp windrose-0:/tmp/save.tgz ./save.tgz -c windrose-ui

# On the target (after ./bare-linux/install.sh has finished SteamCMD):
sudo systemctl stop windrose-game
sudo rm -rf /home/steam/windrose/WindowsServer/R5/Saved
sudo rm -f  /home/steam/windrose/WindowsServer/R5/ServerDescription.json
sudo tar -xzf ./save.tgz -C /home/steam/windrose/WindowsServer/R5/
sudo chown -R steam:steam /home/steam/windrose/WindowsServer/R5/
sudo systemctl start windrose-game
```

See the repo README's *World Island ID is backend-assigned* caveat for
why preserving `ServerDescription.json` specifically matters.

## Swap

The installer deliberately does not touch swap — too opinionated for a
host that may be multi-tenant. It does warn if the host has under 4 GiB
RAM with under 2 GiB swap, which is the configuration that OOM-kills the
game mid-world-load.

Recommended recipe for a dedicated Windrose host under 6 GiB combined
RAM+swap (DigitalOcean / Hetzner / Linode droplets in that tier):

```bash
sudo fallocate -l 4G /swapfile
sudo chmod 0600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
# Low swappiness: swap as OOM cushion, not routine paging.
# vfs_cache_pressure=50 keeps inode/dentry cache warmer — UE5 paks
# touch a lot of small files through Proton.
sudo tee /etc/sysctl.d/90-windrose-swap.conf <<'EOF'
vm.swappiness=10
vm.vfs_cache_pressure=50
EOF
sudo sysctl --system
```

You'll see `Swap: 0 used` under normal operation; it only kicks in when
the game's RSS spikes on world load + backend handshake.

## Files And Services Layout

```
/opt/windrose/image/entrypoint.sh          # game launcher
/opt/windrose/image/ui/server.py           # admin console
/opt/windrose/image/ui/{index.html,app.js,app.css}
/etc/windrose/windrose.env                 # runtime env (root-rw, group-r for steam)
/home/steam/windrose/                      # game data (WindowsServer/, saves, backups)
/home/steam/steamcmd/                      # SteamCMD + GE-Proton compat data
/etc/systemd/system/windrose-{xvfb,game,ui}.service
```

The admin console writes backups into `/home/steam/backups/<utc>/`
(same convention as the k8s deployment).

## Uninstall

```bash
sudo systemctl disable --now windrose-game windrose-ui windrose-xvfb
sudo rm /etc/systemd/system/windrose-{game,ui,xvfb}.service
sudo systemctl daemon-reload
# Data under /home/steam/ stays — delete manually if desired:
# sudo userdel -r steam
```
