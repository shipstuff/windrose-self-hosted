# TODO — Pre-commit cleanup

Helm deploy + 3-container pod + **client-connect** all verified working end-to-end 2026-04-17. Remaining loose ends before the first commit.

## Client-connect: SOLVED (2026-04-17)

End-to-end verified: LAN client joined, ICE consent passed on host pair, ~5 min gameplay, clean farewell. Fix: **`P2pProxyAddress` must be set to a real reachable IP, not `0.0.0.0`.** The game uses it verbatim as its ICE host candidate and Windows clients reject the wildcard. See `memory/p2p_proxy_address_is_the_fix.md` for the full post-mortem (incl. the wasted LD_PRELOAD `getsockname` shim — the game doesn't use getsockname at all; host candidate comes straight from config).

In this repo: Helm + plain StatefulSet default `P2P_PROXY_ADDRESS` to the node's `status.hostIP` via Downward API (correct under `hostNetwork: true`). docker-compose requires `P2P_PROXY_ADDRESS` to be set explicitly via `.env` (no sensible default for compose). Entrypoint logs a loud WARNING if it ever sees `0.0.0.0`.

**WAN connectivity is validated** — friend on a different network connected and played 2026-04-17. ICE's srflx + relay candidates cover WAN clients automatically (server sends STUN via Coturn, backend advertises the resulting `WAN_IP:<port>` srflx + Coturn relay). No port-forwarding or router config required on the server side; the Windrose backend orchestrates the handshake.

## Sidecar shutdown (AppArmor-proof)

`preStop: ["sh","-c","kill -TERM 1 ..."]` on `xvfb` and `windrose-ui` is a no-op: AppArmor's containerd profile applies a stricter policy to the container's original PID 1 than to the shell kubelet spawns for `preStop`, so the TERM is silently dropped. Fix: drop `preStop` and wrap each sidecar's ENTRYPOINT in a tiny trap shell so kubelet's own SIGTERM (sent from the root namespace, not AppArmor-confined) reaches PID 1.

- `ui/entrypoint.sh`: replace the `exec busybox httpd …` tail with a trap-and-wait wrapper so SIGTERM forwards.
- xvfb container `command:` in StatefulSet/Helm: same treatment on the `Xvfb :99` line.
- Delete both `preStop` blocks.
- Target: `kubectl delete pod windrose-0 --wait=true` under 15 s with the current 45 s grace period.

## Pipe R5.log to container stdout

`kubectl logs windrose-0 -c windrose` shows only the entrypoint + Proton prelude, then goes silent — the game writes to `/home/steam/windrose/WindowsServer/R5/Saved/Logs/R5.log`, which never reaches the container's stdout/stderr, so everything useful (backend disconnects, fatal asserts, ICE candidate enumeration, player-add events) is invisible to `kubectl logs` and anyone just eyeballing the pod.

Fix: tail the current R5.log to the container's stderr from the entrypoint. A `tail -F` backgrounded before `exec proton` would work (no SIGCHLD race — proton is still the exec'd PID 1, and tail is a normal child). On rotation (the game closes the current log and opens a new R5.log with the old one renamed to R5-backup-<timestamp>.log), `tail -F` follows by path and picks up the new file automatically.

Belt-and-suspenders: the UI sidecar already has access to the PVC; if the game container's tail is fragile, add a `tail -F R5.log` to the UI entrypoint that streams into its stderr. Gives operators two paths to see the game's own output.

## Remaining hygiene

- `ui/cgi-bin/status.sh` depends on `shareProcessNamespace: true` for the `pgrep` check — already set on both the Helm chart and plain StatefulSet; just needs a comment in status.sh referencing the requirement.
- `tools/services/api/` is a stub README for the deferred live-stats API. Decide: keep as placeholder, or fold the concept into the UI sidecar.

## README / CLAUDE.md / AGENTS.md

Needs an update pass now that the architecture and the P2P fix are settled:

- 3-container architecture diagram (game + xvfb + windrose-ui), shared emptyDir X11 socket.
- GE-Proton pinned to 10-34 (bump in lock-step with Windrose patches).
- Helm install flow (primary) with overrides used today: `image.repository`, `uiImage.repository`, `persistence.existingClaim`, `fullnameOverride`, `createNamespace=false`.
- **P2pProxyAddress guidance** — the single most important operator-facing setting. Explain: host candidate, why 0.0.0.0 breaks, Downward API behavior, override for multi-homed nodes.
- Remove references to removed env (`FILES_IMPORT_MODE`, `UI_ENABLED`, `SAVE_IMPORT_*`) that pre-dated the sidecar split.
- Replace the "2-min restart is expected" line with "backend register succeeds on GE-Proton 10-34+".
- `kubectl cp` and ingress-upload flows still work; document both.

## CI

`.github/workflows/ci.yml` still runs on the old paths:

- Add shellcheck for `ui/` scripts.
- Publish matrix already builds `windrose-server` — extend to build + push `windrose-ui` too.
- Helm template check: extend to cover `--set xvfb.enabled=false` and `--set serverConfig.p2pProxyAddress=192.0.2.1` so the Downward-API branch is rendered.

## Idle CPU — known upstream bug, backstopped with cgroup cap

Empty dedicated server spins ~2 cores on the main loop; a client connection drops it to 130-250 mcpu because the NetDriver starts pacing. Community-wide known issue, devs aware, no fix published: https://steamcommunity.com/app/3041230/discussions/0/807974232125564069/ (23 posts, same symptom, zero fixes). See `memory/windrose_idle_cpu_known_bug.md` for the full post-mortem including everything that was tried and failed.

**Current workaround (applied 2026-04-17):** `resources.game.limits.cpu: 500m` in Helm values. The kernel throttles the busy-loop at 500m; with a client connected the game's actual usage (130-250 mcpu) is well under that, so no gameplay impact. Chart default left uncapped but the values.yaml comment strongly recommends uncommenting for ≤2-vCPU hosts.

**If you want to try a real fix** (not urgent — cap works): the community research surfaced one UE code path we haven't attempted that explicitly sleeps between frames: `[/Script/Engine.Engine] bSmoothFrameRate=True` with `SmoothedFrameRateRange=(LowerBound=(Type=Inclusive,Value=10),UpperBound=(Exclusive,Value=30))`. We briefly tested it on 2026-04-17 — didn't drop CPU on this build, but that observation is preliminary. Also worth trying launch arg `-usefixedtimestep -fps=30` (lowercase `-fps`, paired with `-usefixedtimestep`) — UE docs suggest `FParse` expects the lowercase form.

If either works, wire it via `SERVER_LAUNCH_ARGS` (already plumbed through entrypoint/chart) and remove the cgroup cap — but keep the cap as the documented fallback for operators on older Windrose builds.

## Admin console polish (follow-ups on the Python rewrite)

Smaller refinements on top of what shipped 2026-04-18. Keep them together — they're one coherent UX pass:

1. **Bug: `backendRegion` comes back as `""`** in `/api/status`. My Python tail-scan of `R5.log` for `r5coopapigateway-([a-z]+)-release` is finding nothing on the canary. Check whether (a) the log is rotating past the chunk we read, (b) the regex isn't matching this build's log lines, or (c) we're reading before the gRPC CreateChannel line lands. The shell status.sh had the same grep and worked; diff the two. Populate field reliably so the UI stops showing `-`.
2. **Banner copy:** drop the "Player identifiers are redacted by default — click show to reveal" sentence from the authed-admin banner. The masked AccountIds are self-describing; operators don't need a tooltip.
3. **Rename title** "Windrose Admin Console" → **"Windrose Self-Hosted Admin Console"** in `<title>` and the `<h1>`.
4. **Manual update copy** (`<details id="updateCard">`): the current blurb says "Required only for offline / air-gapped deploys." Replace with something modder-oriented: "Upload a custom / modded `WindowsServer` tarball to replace the stock binary pulled by SteamCMD. Useful for community mods or pinning a specific build." Air-gapped is still covered implicitly; modders are the more common real use case.
5. **Public vs admin views** — current UI is all-or-nothing behind basic auth. Split:
   - **Public view** (no auth required): Invite code, password-protected status, server status pills (running / uptime / region / world / playercount), connected player names (no AccountIds), Resources card. Everything read-only.
   - **Admin view** (behind auth): everything in the current UI including Config editor, Worlds edit, Backups, Manual update, Stop button, unmasked AccountIds.
   Implementation sketch:
   - Split `/api/status` into `/api/status` (always open, limited fields) and `/api/status/admin` (auth, full fields incl. AccountIds + allowDestructive state).
   - HTML renders minimal card set on public view; "Sign in" button triggers the browser's basic-auth prompt (via a 401-returning trigger request to `/api/status/admin`). Once auth'd, re-fetch with credentials, show full UI.
   - Server-side: redact AccountIds in the public status response; decide whether to expose invite code publicly (probably yes — invite is meant to be shared).

Order: do 1–4 in a single small patch (copy + a bug fix), then 5 as its own commit since it changes the auth boundary.

## Admin console refactor — NEXT MAJOR

Current UI is busybox-httpd + CGI shell scripts in a separate image. Works but has accumulated pain: per-request shell fork, stale log parsing, hard to add real-time features, two images to build/version, no auth, no config editor, no multi-world support.

### Rewrite as a Python stdlib HTTP server (no-deps)

One file: `ui/app.py`. Uses `http.server` / `BaseHTTPRequestHandler` or (slightly nicer) `uvicorn`/`asgiref` — but the no-deps constraint says stick with stdlib. Responsibilities:

- `/` → serve `index.html`
- `/api/status` → JSON (what `status.sh` emits today)
- `/api/upload` → streaming POST handler for WindowsServer.tar.gz (replaces `upload.sh`)
- `/api/saves/download` → streaming tarball of R5/Saved (replaces `download_saves.sh`)
- `/api/config` → GET current server config, PUT staged changes
- `/api/config/apply` → POST to validate staged + schedule restart
- `/api/backups` → GET list, POST to create manual backup
- `/api/backups/{id}/restore` → POST
- `/api/worlds` → GET list, per-world config endpoints
- `/api/events` → SSE stream of log-tail highlights (joins, leaves, crashes, fatal asserts) for a live feed
- All handlers share one in-process state cache (last status, backend region, throttle stats) — no per-request shell exec, no tail -n 5000 repeatedly.

### Collapse to one Docker image

Delete `ui/Dockerfile`. Bake the Python app into `image/` alongside the game entrypoint. Helm values gain a `uiEntrypoint: /usr/local/bin/ui-app.py` override so the ui container spec runs the same image with a different command. Benefits:
- One CI job, one publish target (`ghcr.io/shipstuff/windrose`).
- Image layer caching makes the UI container free on the same node.
- No more "which image has the patched status.sh" confusion we hit today.
- Docker-compose + bare-Linux benefit most — one image to pull.

### Admin auth + privacy

- `UI_PASSWORD` env → HTTP basic auth required when set. Unset = open (compose default on loopback).
- `UI_ALLOW_DESTRUCTIVE` flag → gates the upload / restart / backup-restore endpoints. Default false for extra safety.
- AccountId mask by default in the HTML (already landed 2026-04-17 with click-to-reveal).
- Drop "Windrose Self-Hosted" branding → **Admin Console** with a permanent banner (already landed).
- Expose a `/healthz` endpoint that's always open for liveness.

### Config staging + apply-on-restart

- GET `/api/config` returns the current `ServerDescription.json` + world configs + resources visible to the operator.
- PUT `/api/config` stages changes to a disk-backed pending-changes file (`R5/Saved/ServerDescription.staged.json`).
- POST `/api/config/apply` validates the staged file, swaps it in place of the live config, and triggers a pod restart (via the same sentinel-based shutdown mechanism in the "Better game-update flow" TODO).
- UI shows diff of pending vs live, with an "Apply on next restart" button.
- Chart password secret continues to be managed separately (never edited via UI).

### Backup management

- List `/home/steam/backups/*` with timestamps + sizes.
- POST `/api/backups` → run the preserve-save-plus-identity snapshot on demand.
- POST `/api/backups/{id}/restore` → swap in place, requires confirmation + "destructive" flag enabled.
- Keep N most recent (already done in upload.sh; move to a retention policy knob).
- Download a backup as tarball.

### Multi-world support

Per `DedicatedServer.md`, a server can host multiple worlds (each under `R5/Saved/SaveProfiles/Default/RocksDB/<GameVersion>/Worlds/<IslandId>/` with its own `WorldDescription.json`). Today we treat it as single-world. Real support:

- `/api/worlds` → list all worlds discovered on disk; show which is "active" (matches `ServerDescription.WorldIslandId`).
- Per-world page: edit `WorldDescription.json` (name, preset, difficulty multipliers per the 10 fields documented in `DedicatedServer.md`).
- "Switch active world" → stages change to `ServerDescription.WorldIslandId`, restarts.
- "Clone world" / "Delete world" (destructive-flag gated).

### Implementation ordering

1. Python rewrite + single-image consolidation (biggest — everything downstream builds on this).
2. Admin auth (env-var basic auth, then harden later).
3. Config GET/staging.
4. Backup management (builds on existing preserve logic).
5. Multi-world (most complex; needs lifecycle coordination with the game's RocksDB handle).

## Canary cleanup (when main is cut over)

- Scale `windrose-canary` StatefulSet to 0 — preserves the PVC for later reuse.
  ```bash
  kubectl -n games scale statefulset/windrose-canary --replicas=0
  ```
- Do NOT delete the PVC; the chart's `persistence.existingClaim: ""` default creates a new one if re-deployed without pinning. Update `values-canary-local.yaml` to pin `persistence.existingClaim: windrose-canary-data` so re-running `helm upgrade --install` reuses the stored save.
- To resume canary: `kubectl -n games scale statefulset/windrose-canary --replicas=1` — picks up whatever image is tagged `:canary`.

## Better game-update flow — PRIORITY

**Incident 2026-04-17:** manual update ceremony (scale down → rm -rf WindowsServer preserving R5/Saved → kubectl cp → flatten nested dir → scale up) wiped `ServerDescription.json` along with the binary. On restart the game minted a fresh `PersistentServerId`, the backend assigned a new `WorldIslandId`, and the preserved save became orphaned. Had to jq-restore PSID/WorldIslandId/InviteCode by hand and delete the pod. Operator also didn't get their `serverName` / `isPasswordProtected` / `maxPlayerCount` values applied because the game regenerated `ServerDescription.json` on first boot after identity was lost.

**Immediate fix applied:** `ui/cgi-bin/upload.sh` now preserves `R5/ServerDescription.json` + `R5/WorldDescription.json` alongside `R5/Saved/` across the upload replacement, and drops a timestamped snapshot into `/home/steam/backups/<utc>/` (keeps last 5). That's the minimum correctness fix.

**Still needed** for full "one-button update" UX:

1. **Version-mismatch detection.** Log-tail watchdog parses `R5.log` for the client-connect error pattern indicating version skew (capture the exact log line the next time it happens; current memory just says "client and server don't match"). UI shows a red banner with an "Update" CTA when it fires.
2. **UI-orchestrated restart.** Upload currently ends with "now restart the pod yourself." Can't `pkill` across containers (stripped CAP_KILL + same-UID signal fails under the shared-PID-namespace + seccomp). Options:
   - **Sentinel-based**: upload.sh writes `/tmp/windrose-restart` on the PVC; the game container's entrypoint supervises a watcher that `kill -TERM` itself when the sentinel appears. Requires a side-process inside the game container that doesn't race Proton's PID-1. A thin bash supervisor spawning proton as a child + watching the sentinel could work.
   - **Kubernetes-native**: the UI sidecar gets a ServiceAccount with `patch pods` RBAC limited to its own StatefulSet, and the CGI calls `kubectl rollout restart`. Cleanest on k8s, doesn't help compose.
   - **Service-level**: run a tiny `windrose-operator` sidecar with `CAP_KILL` whose only job is "signal the game container on request." Smallest blast radius.
   Pick sentinel-based for portability; it works identically in k8s + compose + bare-linux.
3. **Version hash comparison.** UI fetches a well-known server-side manifest (`WindroseServer-Win64-Shipping.exe` mtime + size, or hash of a stable UE pak) and lets the operator drag-drop the same file from their install for a one-way diff. Green "in sync" / red "update me" on the status card.
4. **Rollback button.** `cgi-bin/rollback.sh` restores the most-recent `/home/steam/backups/<utc>/` into place, reverse of the upload.sh swap. Safety net for bad patches.
5. **Authenticated SteamCMD fetch.** Operator-supplied Steam creds in a Secret → `steamcmd +login … +app_update 4129620`. Blocked by 2FA ergonomics; revisit if someone finds a repeatable flow. Low priority because `kubectl cp` / UI upload is fine as baseline.

Items 1, 2, 4 share the same log-tail / sentinel / backup plumbing the watchdog needs, so build them together.

## Backend-instability watchdog + UI "Re-auth" button

Observed 2026-04-17: Windrose's backend returned transient 503 (EU auth endpoint) and severed the long-lived gRPC stream (KR). The game binary responds by either fatal-asserting (exit 3 → kubelet restart) or giving up silently — stuck state with no further retries, clients can't connect until operator intervenes. Both are Windrose-side bugs we can't patch, but we can detect and recover.

Two parts:

1. **Watchdog**: a tiny log-tail loop in the UI sidecar (or a new `windrose-watchdog` sidecar) that parses `R5.log` for the stuck pattern — `GcStream is broken`, `Server Authorization failed`, `ErrorMessage 'Internal error'` — and, if no successful `OnServerRegistered` appears within N minutes, either signals the game container (with `shareProcessNamespace: true` we can `pkill` it and let kubelet restart) or writes a sentinel file the operator/CGI can act on.
2. **UI button**: a CGI endpoint `cgi-bin/reauth.sh` that triggers the restart on demand. With `shareProcessNamespace: true` the sidecar can `pkill -TERM WindroseServer-Win64-Shipping.exe` and Proton will respawn via the container's `exec proton waitforexitandrun`. Expose as a "Re-auth with backend" button on the info card, visible when `serverRunning=true` but `inviteCode=""` (a useful "stuck" heuristic) or just always-available.

Implementation note: the log-tail approach is similar to what the deferred stats API needs, so design them together.

## Nice-to-haves (defer)

- **Webhooks** — port the enshrouded-self-hosted webhook model (Discord + generic POST). Most useful events: server started, player joined/left, world migration on version change, crash/restart. Source of events: `R5.log` tail (no RCON / query API exists). Sidecar pattern: a tiny log-follower in the UI container or a new sidecar that parses the log and POSTs. Likely extension of `tools/services/api/` once that stub grows up.
- **Save upload that updates RocksDB** — upload a save archive via the UI and have it land as the active world on the running server. Two real paths:
  1. **Pre-start overwrite** (simpler): stop game, rsync uploaded save to `R5/Saved/SaveProfiles/Default/RocksDB/<GameVersion>/Worlds/<id>/`, start game. Doesn't require RocksDB lock cooperation but interrupts gameplay.
  2. **Hot RocksDB merge** (harder): open the server's live DB via a tool-side `ldb` put against each row we care about. Requires coordinating with the game's DB lock — unclear whether Windrose holds it exclusively. RocksDB tooling is confirmed working (see memory `windrose_island_identity`); the `R5BLIsland`, `R5BLBuilding`, `R5BLActor_*` column families are readable/writable.
  **Cross-cutting gotcha**: uploaded saves don't change the server's WorldIslandId because island identity is backend-assigned from PSID. Either we repair PSID before registration (if we can find where it's stored), or accept that the uploaded save is only meaningful when assigned to the server's existing island ID.
- Bare-Linux systemd install path (still stubbed in `bare-linux/README.md`).
- Stats API resurrection as a separate sidecar, now that the file-based heartbeat approach is clear from the `status.sh` shared-PID-namespace precedent.
- Multi-region `hostAliases` for offline / restricted-DNS environments (commented examples in Helm values — not needed in public-DNS environments, confirmed working without).
