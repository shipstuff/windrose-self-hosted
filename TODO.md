# TODO

Remaining work on top of what's already shipped. Living doc — trim as things
land, add as things come up.

## Upstream-bug-blocked

- **Idle-CPU bug.** UE5 task workers in `WindroseServer-Win64-Shipping.exe`
  busy-spin in userspace with zero syscalls, burning ~1.82 cores before any
  player connects. Investigated thoroughly across 15+ Engine.ini / launch-arg /
  Proton-env knobs + `WINE_CPU_TOPOLOGY` pinning + localhost phantom client —
  none reachable from our side, binary is heavily stripped of the relevant
  CVars. See `memory/windrose_idle_cpu_known_bug.md` + `memory/windrose_phantom_client_not_feasible.md`
  for the exhaustive dead-ends. Tracker: [Steam community thread](https://steamcommunity.com/app/3041230/discussions/0/807974232125564069/).
  Practical sizing guidance already in `README.md` + `bare-linux/README.md`.

  If upstream ships a fix, revisit: ≤2 vCPU hosts may become viable
  without caveats, and we should post findings to the community
  thread so other ops have a head start.

## First release

Everything's in the tree; what's left is shipping it to strangers.

- Push repo to GitHub (currently local-only on the operator's box).
- Tag `v0.1.0`. `publish-images.yml` + `publish-chart.yml` are wired to fire
  on `v*` tags — should produce `ghcr.io/shipstuff/windrose-server:0.1.0` +
  `oci://ghcr.io/shipstuff/charts/windrose:0.1.0` automatically.
- Verify CI green on first push: `ci.yml` runs shellcheck / yamllint /
  json-lint / kustomize / helm-template / python-compile. Fix anything that
  doesn't pass on GitHub's runners (local works ≠ GHA works).
- Verify external consumption: `helm install windrose
  oci://ghcr.io/shipstuff/charts/windrose --version 0.1.0 -n games` from a
  clean shell should end with a running pod.

## UI polish

Small, coherent UX follow-ups on top of the current admin console.

- **Roll out split persistence for HA.** Validate and migrate canary/prod to a
  runtime/cache volume plus a small durable state volume so replicated CSI only
  carries save/config identity state, not SteamCMD, Proton, and WindowsServer
  cache.
- **SSE log stream**. UI's "Log" card today shows ephemeral client-side
  events. Since the game-container's stdout has `tail -F R5.log`, we can
  expose `/api/logs/stream` as a Server-Sent-Events endpoint from
  `server.py` that tails the same file and streams to the browser.
  Turns the Log card into a live in-process feed of backend events,
  joins/leaves, fatal asserts.
- **Clone world / Delete world** buttons per-row in the Worlds card.
  Destructive-flag gated. Clone = copy `Worlds/<id>/` tree under a new
  islandId + new `WorldDescription.json`. Delete = move to backups + remove.
- **Staged-world apply ordering** — today `/api/config/apply` swaps server
  config + all staged worlds in one shot. If the active world's islandId
  changed in the server stage AND a world-specific staging exists, the
  apply order matters. Low-probability corner case but worth a validation
  pass.

## `install.sh --update` + version-pinned UI bundles

Today's bare-linux flow re-copies UI assets from whatever local
checkout install.sh is run from — so operators running an older
`windrose-src-test/` directory reinstall old UI files even when
the repo has new ones. Discovered 2026-04-19 when the canary's
admin console didn't pick up dark mode until we scp'd fresh assets
by hand.

Plan:
1. **Tagged artifact bundle.** On every `v*` tag, CI builds a
   `windrose-ui-<version>.tar.gz` (`server.py` + `ui/*` + `scripts/entrypoint.sh`
   + `scripts/*_example.json`) and attaches it as a GitHub release
   asset (or publishes as a GHCR OCI artifact alongside the chart).
2. **`install.sh --update [<version>]` flag.** Downloads the tagged
   bundle to a temp dir, invokes the normal install flow pointed at
   that dir, reuses the merge-semantics we already have so env +
   services survive. Default version = latest release tag (queried
   via `gh release view --json tagName` or a plain redirect URL).
   `install.sh --update v0.1.3` pins a specific tag.
3. **Version pin in `/etc/windrose/windrose.env`** (`WINDROSE_INSTALLED_VERSION=`)
   so `systemctl status` / `journalctl` context makes it obvious what
   version is live.
4. **`bare-linux/README.md § Updating`** section explaining the
   upgrade flow, the tag-pinning option, and the rollback path
   (re-run with the previous tag).

Until this lands, the workflow is `cd <checkout> && git pull && sudo ./bare-linux/install.sh`.

## Watchdog for backend-stuck state

Observed 2026-04-17: Windrose's backend returned transient 503 / severed
gRPC stream; game binary went silent with no further retries, clients
couldn't connect. The `EventDetector` thread already tails log state —
extend it to detect the stuck pattern (`GcStream is broken`, no
`OnServerRegistered` within N min after restart) and either fire a
`backend.stuck` webhook or surface a "Re-auth" button on the UI that
kills the game process to trigger kubelet / systemd restart.

## Deferred

- **Authenticated SteamCMD fetch.** Anonymous app_update works fine for
  stock Windrose now; this is only useful if a Windrose update ever goes
  behind auth. Steam 2FA makes scripted `+login` painful. Leave alone
  unless forced.
- **Stats API as a sidecar**. `scripts/services/api/` is a stub. Could host
  a Prometheus `/metrics` endpoint driven by the same R5.log tail the UI
  uses. Low priority; Grafana etc. aren't typical for a 4-player game.
- **RocksDB hot-merge** for save imports without a restart. Complex; the
  current stop-restart upload path is fine.
