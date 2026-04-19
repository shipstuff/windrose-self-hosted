# Bare Linux Install

Run Windrose directly on a spare Linux box (Ubuntu 22.04+ / Debian 12+) as
three systemd system services: game + Xvfb + admin UI. Validated on an
Ubuntu 24.04 DigitalOcean droplet with 2 cores / 4 GiB RAM; should run
anywhere the [`image/Dockerfile`](../image/Dockerfile) deps are available.

## Sizing

An upstream idle-CPU bug in the dedicated server (UE5 task-worker
busy-spin, tracked on the [community thread](https://steamcommunity.com/app/3041230/discussions/0/807974232125564069/))
eats ~1.82 cores before any player is connected. Until that's fixed
upstream, the box needs real headroom above it. Concretely (validated
on DigitalOcean droplets 2026-04-18):

| Box                | Verdict                                                                 |
| ------------------ | ----------------------------------------------------------------------- |
| 1 vCPU / any RAM   | **Unplayable.** Even with the mitigation below, there's no second core for the handshake. May become viable if the upstream bug is patched. |
| 2 vCPU / 2 GB      | **Unplayable (RAM-bound).** Boots and idles clean for ~9 min, then a delayed ~500 MiB allocation blows past available RAM; the kernel locks up on page reclamation. |
| 2 vCPU / 4 GB      | **Works**, as long as `WINE_CPU_TOPOLOGY=1:0` is set (install.sh auto-enables it on ≤2 vCPU). |
| ≥3 vCPU / 4 GB     | **Comfortable.** Mitigation not needed; leave `WINE_CPU_TOPOLOGY` empty so the game can use extra compute during peaks. |

Two distinct floors: **CPU ≥ 2 vCPU *with* the mitigation** (because
the upstream idle-CPU bug otherwise eats ~1.82 cores), **and**
**RAM ≥ 4 GB** (because there's a delayed working-set step-up ~9 min
into steady state that 2 GB hosts can't absorb, swap or no swap).

### The idle-CPU mitigation (auto-enabled on ≤2 vCPU)

Windrose's dedicated server has an upstream bug: two threads named
`GameThread` busy-spin in userspace — zero syscalls for 5+ seconds
at a time — burning ~1.82 cores before any player connects. It's
not reachable from Engine.ini / ConsoleVariables.ini / launch args;
the Shipping build doesn't even ship with the relevant CVars
compiled in. See `memory/windrose_idle_cpu_known_bug.md` for the
exhaustive list of things that don't work.

**What *does* work** is telling Wine to report a single-core CPU
topology to the Windows process: `WINE_CPU_TOPOLOGY=1:0`. The spin
collapses to ~99% of one core instead of pegging two, and the other
core stays free for the P2P handshake burst. Measured effect:

- Idle host load: 195% → **99%**
- UI-sidecar latency p95: 36 ms → **14 ms**

`install.sh` writes `WINE_CPU_TOPOLOGY=1:0` into
`/etc/windrose/windrose.env` when it detects `nproc ≤ 2`, and leaves
it empty otherwise. Override either way with an explicit env var at
install time:

```bash
sudo WINE_CPU_TOPOLOGY=1:0 ./bare-linux/install.sh   # force-on
sudo WINE_CPU_TOPOLOGY=""  ./bare-linux/install.sh   # force-off
```

Memory sizing is less scary than the 2.7 GiB RSS number suggests —
most of it is cold pages that park to swap happily (see § Swap and
`memory_footprint_cold_pages.md`) — but the 9-minute step-up still
sets the RAM floor above 2 GB regardless of how generous your swap is.

## Quick Install

From a checkout of this repo, as root on the target host:

```bash
sudo ./bare-linux/install.sh
```

The installer:

- Installs OS packages: `xvfb`, `python3`, `winbind`, `dbus`, `libfreetype6`,
  `libgnutls30`, `lib32gcc-s1` (i386 enabled for Proton), and supporting
  tools (`curl`, `jq`, `tar`, `gzip`, `unzip`).
- Creates a dedicated non-root `steam` user.
- Drops the repo's [`image/entrypoint.sh`](../image/entrypoint.sh) +
  [`image/ui/`](../image/ui/) assets under `/opt/windrose/`.
- Writes `/etc/windrose/windrose.env` with all runtime knobs.
- Enables + starts three systemd units:
  - `windrose-xvfb.service` — virtual display on `:99`
  - `windrose-game.service` — the game under GE-Proton
  - `windrose-ui.service`   — the Python admin console on port `28080`

On first boot the game service runs SteamCMD anonymously to pull app
`4129620` (~3 GiB) into `/home/steam/windrose/WindowsServer/`, then
hands off to Proton. Subsequent restarts are fast — SteamCMD only
re-checks for updates.

Tail it:

```bash
sudo journalctl -fu windrose-game
sudo journalctl -fu windrose-ui
```

Hit the admin console at `http://<host>:28080/`.

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
| `UI_BIND` | `0.0.0.0` | UI listen iface (drop to `127.0.0.1` + reverse-proxy for TLS) |
| `UI_PORT` | `28080` | UI listen port |
| `UI_PASSWORD` | empty | HTTP basic-auth password. Strongly recommended for any publicly-reachable host. |
| `UI_ENABLE_ADMIN_WITHOUT_PASSWORD` | `false` | explicit opt-in for destructive routes when no password is set — LAN-only |
| `SERVER_NAME` | `Windrose Bare-Linux` | informational |
| `MAX_PLAYER_COUNT` | `4` | 4 is the vendor guide; up to 10 with more RAM |
| `WORLD_NAME` | `Default Windrose World` | display name |
| `WORLD_PRESET_TYPE` | `Medium` | `Easy`, `Medium`, `Hard`, `Custom` |
| `P2P_PROXY_ADDRESS` | auto-detected | ICE host candidate. Leave empty unless the host is multi-homed. |
| `WINDROSE_SERVER_SOURCE` | `steamcmd` | `steamcmd` (anonymous app_update) or `files` (BYO tarball via UI) |

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
