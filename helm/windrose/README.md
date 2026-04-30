# Windrose Helm Chart

Helm install path for a Windrose dedicated server on Kubernetes. This chart
renders the same three-container StatefulSet (game + Xvfb + admin UI)
that the plain manifests in the repo root produce — same defaults, same
`hostNetwork: true`, same shared-PID pod. Use this if you prefer Helm
values over raw YAML; use the root manifests if you prefer kustomize.

## Install

From this repo:

```bash
helm upgrade --install windrose ./helm/windrose \
  --namespace games --create-namespace
```

From the published OCI chart (once a `v*` tag is pushed and CI runs
[`publish-chart.yml`](../../.github/workflows/publish-chart.yml)):

```bash
helm upgrade --install windrose oci://ghcr.io/shipstuff/charts/windrose \
  --version 0.2.1 \
  --namespace games --create-namespace
```

On first boot the game container runs SteamCMD anonymously against app
`4129620` and pulls `WindowsServer/` onto the PVC (~3 GiB, ~10 min on
a typical cluster uplink). Subsequent restarts skip re-download — it's
an `app_update` check.

Hit the admin UI through the Ingress (default hostname `windrose.local`)
or port-forward: `kubectl -n games port-forward svc/windrose 28080:28080`.

## Typical Overrides

The common knobs you'll actually set:

```bash
helm upgrade --install windrose ./helm/windrose \
  --namespace games --create-namespace \
  --set serverConfig.serverName="Salty Seas" \
  --set serverConfig.maxPlayerCount=4 \
  --set worldConfig.name="Salty Seas" \
  --set worldConfig.presetType=Hard
```

See [`values.yaml`](values.yaml) for the full list — that file has
inline comments for every field.

For anything more than a handful of overrides, maintain a
`values-local.yaml` and apply with `-f`:

```bash
helm upgrade --install windrose ./helm/windrose \
  --namespace games --create-namespace \
  -f values-local.yaml
```

`values-local*.yaml` is gitignored.

## Password-Protect The Server

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

## Server Config Modes

`serverConfig.mode` picks who owns `ServerDescription.json`:

| Mode      | Behavior                                                                                        |
| --------- | ----------------------------------------------------------------------------------------------- |
| `env` (default) | Seed from the example `ServerDescription.json`, patch known keys from env on every start. Preserves operator edits via a shadow-stamp divergence marker: if `ServerName` / `WorldName` / etc. drift from the last env-intent we wrote, the entrypoint leaves the drifted keys alone. Good for chart-driven deploys where you want env vars to mostly win but UI edits to survive. |
| `managed` | Render `serverConfig.inlineJson` into a ConfigMap, merge `Password` from the optional Secret. Operator owns the chart values; the PVC copy is regenerated on every boot. Good for GitOps-style deploys where the JSON is checked into your values.yaml alongside the rest of the release. |
| `mutable` | The entrypoint does not touch `ServerDescription.json` at all. The file must already exist on the PVC (from a prior boot, an uploaded tarball, or a manually-seeded file). All edits — UI, ldb, hand-edit — are authoritative. Good for operators who want zero env-driven reconciliation. |

### Using `mutable` mode successfully

1. Start with `env` or `managed` mode on first install so the file is created (or upload a tarball that contains `ServerDescription.json`).
2. Flip `serverConfig.mode: mutable` in your values override and `helm upgrade`.
3. Edit config from the admin UI's Server Config card. Hit *Stage* then *Apply + restart* — the UI atomically promotes `ServerDescription.staged.json` → `ServerDescription.json` and signals the game to restart.
4. Env vars (`SERVER_NAME`, `MAX_PLAYER_COUNT`, etc.) are **ignored** in this mode; they stay as documentation of the initial values but the live file is the source of truth.

If you ever need to re-seed from env, flip back to `env` mode temporarily, helm upgrade, let the entrypoint stamp, then flip back to `mutable`.

**`env` mode already preserves UI edits** via the shadow-stamp (since PR #2). You only need `mutable` if you want the stronger guarantee that the entrypoint will *never* touch the file — for example when running multi-world setups where every world's metadata is managed via the UI's per-world editor, or when an operator is doing something the env-mode reconciler doesn't know about (injecting a modded `WorldPresetType`, etc.).

## Override `P2pProxyAddress`

Auto-detect via UDP-connect usually picks the right interface. If
it doesn't (multi-homed hosts, WAN-first deploys):

```bash
helm upgrade windrose ./helm/windrose --reuse-values \
  --set serverConfig.p2pProxyAddress=203.0.113.7
```

See the root README's *Critical Setting: `P2pProxyAddress`* section for
why this matters — it's the ICE host candidate the game advertises to
clients verbatim.

## Game-Patch Update Flow

When Windrose ships an update, the default `serverConfig.source: steamcmd`
handles it automatically — on the next pod restart the entrypoint runs
`app_update 4129620`, pulls the new `WindowsServer/`, and the save's
versioned path gets migrated forward.

For operators running modded / pre-release builds
(`serverConfig.source: files`), the admin UI's **Manual Update** card
accepts a tarball. The upload preserves `R5/Saved/`,
`ServerDescription.json` (identity), and `WorldDescription.json`, and
snapshots the old tree to `/home/steam/backups/<utc>/`. Restart the
game container afterward:

```bash
kubectl -n games delete pod windrose-0
```

Entrypoint detects any `<GameVersion>` bump and migrates worlds forward
automatically.

## Webhook Notifications (Discord + Generic)

Root README's *Send Notifications Via Discord Or Generic Webhooks*
section describes the event set. Helm wiring:

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
    # Or inline:
    # discordUrl: "https://discord.com/api/webhooks/…"
    # url: "https://my-listener.example.com/windrose"
```

`urlSecret` / `discordUrlSecret` take precedence over `url` / `discordUrl`
when both are set.

## Evict A Flaky Backend Region

Windrose's gRPC gateways in KR / EU / RU occasionally hit 502s and
the game's dedicated-server binary fatal-asserts when it does. Point
the affected region at a black-hole IP to force ping-based selection
to pick another:

```bash
helm upgrade windrose ./helm/windrose --reuse-values \
  --set 'blackholeRegions={kr}'
```

Revert with `--set 'blackholeRegions={}'` once the region recovers.
See `memory/windrose_backend_region_selection.md` for diagnostic
signals.

## Retrieve The Invite Code

Via UI: the big code on the **Invite** card.

Via CLI (from a privileged operator host):

```bash
kubectl -n games exec windrose-0 -c windrose-ui -- \
  jq -r .ServerDescription_Persistent.InviteCode \
  /home/steam/windrose/WindowsServer/R5/ServerDescription.json
```

Via port-forward + auth'd curl:

```bash
kubectl -n games port-forward svc/windrose 28080:28080 &
curl -s -u admin:"${PASSWORD}" http://127.0.0.1:28080/api/invite
```

## Uninstall

```bash
helm -n games uninstall windrose
# PVC survives the helm uninstall by default — delete it explicitly if desired:
kubectl -n games delete pvc windrose-data
```

## Values Reference

Every value the chart accepts, what it does, and its default. The runtime
env vars these map to are also documented in the main
[README § Configure Server Runtime](../../README.md#configure-server-runtime) —
the chart is a thin wrapper around those vars plus Kubernetes-level knobs
(resources, ingress, etc.).

### Deployment topology

| Value | Default | Purpose |
|---|---|---|
| `namespace` | `games` | Target namespace (used when `createNamespace: true`). |
| `createNamespace` | `true` | Let the chart create the namespace. Turn off when installing alongside other charts that own it. |
| `replicaCount` | `1` | Always 1 — the dedicated server is stateful + backend-registered. |
| `terminationGracePeriodSeconds` | `45` | How long kubelet waits after SIGTERM for Proton's save-on-exit cascade to finish. |
| `hostNetwork` | `true` | Needed so the game's NAT-punched UDP binds to the node's real IP, not an overlay. |
| `nodeSelector` | `{}` | Pin to a node when the PVC is node-local (e.g. `local-path-provisioner`). |
| `hostAliases` | `[]` | Literal DNS pinning; not needed in most setups. |
| `blackholeRegions` | `[]` | Subset of `["kr","eu","ru"]` — the chart adds a `hostAliases` entry routing each listed region's gateway to `192.0.2.1` so the game's region-picker skips it. Revert after the flaky gateway recovers. |
| `imagePullSecrets` | `[]` | Standard k8s; needed for private registries. |

### Image

| Value | Default | Purpose |
|---|---|---|
| `image.repository` | `ghcr.io/shipstuff/windrose-server` | Single image for game + UI + xvfb sidecars. |
| `image.tag` | `latest` | |
| `image.pullPolicy` | `Always` | |
| `uiImage.{repository,tag,pullPolicy}` | `""` | Deprecated override; leave empty to reuse `image.*` for the UI sidecar. |

### Persistence

| Value | Default | Purpose |
|---|---|---|
| `persistence.existingClaim` | `""` | If set, use an existing PVC instead of creating one. |
| `persistence.size` | `20Gi` | New-PVC size request. `WindowsServer/` is ~3 GiB; saves + backups + GE-Proton cache add headroom. |
| `persistence.accessMode` | `ReadWriteOnce` | |
| `persistence.storageClassName` | `""` | `""` = cluster default. |
| `persistence.subPath` | `steam-root` | Mount subdir; gives the pod a clean `/home/steam` view. |

### Service + Ingress

| Value | Default | Purpose |
|---|---|---|
| `service.type` | `ClusterIP` | UI reaches clients via Ingress or port-forward by default. |
| `service.port` | `28080` | Listening port for the UI. |
| `service.publishNotReadyAddresses` | `true` | Keep UI reachable while the game container restarts. |
| `ingress.enabled` | `true` | |
| `ingress.className` | `nginx` | |
| `ingress.hosts` | `[windrose.local]` | |
| `ingress.annotations` | nginx tuning for 8 GiB upload cap, 1h upstream timeout, streaming bodies | Sized for the UI upload + saves-download endpoints. |
| `ingress.tls` | `[]` | Standard k8s TLS block. |

### Game runtime

| Value | Default | Purpose |
|---|---|---|
| `serverConfig.mode` | `env` | See main README. |
| `serverConfig.launchStrategy` | `shipping` | `shipping` or `launcher`. |
| `serverConfig.source` | `steamcmd` | `steamcmd` or `files`. |
| `serverConfig.launchArgs` | `-FPS=60` | Extra args passed to the game binary after `-log`. |
| `serverConfig.serverName` | `Windrose Server` | |
| `serverConfig.inviteCode` | `""` | Empty = backend mints one. |
| `serverConfig.isPasswordProtected` | `false` | |
| `serverConfig.maxPlayerCount` | `4` | |
| `serverConfig.p2pProxyAddress` | `""` | Empty = entrypoint auto-detects. Override to a literal IP only for multi-homed / air-gapped hosts. |
| `serverConfig.passwordSecret.{name,key}` | `""`, `password` | Source the server password from a k8s Secret instead of inline. |
| `serverConfig.inlineJson` | partial seed | Only read when `mode: managed`. |
| `worldConfig.islandId` | `default-world` | |
| `worldConfig.name` | `Default Windrose World` | |
| `worldConfig.presetType` | `Medium` | `Easy` / `Medium` / `Hard` / `Custom`. |
| `protonUseXalia` | `"0"` | Keep off; Xalia crashes on headless Proton. |
| `patchIdleCpu` | `"0"` | Legacy workaround for older/pinned server builds before Windrose's official idle-CPU fix. Current SteamCMD installs should keep `"0"`. `"1"` enables the experimental binary patch at boot; see [main README § Caveats](../../README.md). |
| `filesWaitTimeoutSeconds` | `"0"` | 0 = wait forever. |
| `xvfb.enabled` | `true` | Keep on; the game container requires a display. |
| `xvfb.display` | `99` | Canary releases should use a different number (:98, :97…) to avoid hostNetwork collisions. |

### Admin UI

| Value | Default | Purpose |
|---|---|---|
| `ui.password` | `""` | Inline HTTP basic auth password. Prefer `passwordSecret` for anything more than a lab. |
| `ui.passwordSecret.{name,key}` | `""`, `password` | Secret-backed password. Chart reads `key` from the named Secret. |
| `ui.enableAdminWithoutPassword` | `false` | LAN-only / firewalled escape hatch for destructive routes when no password is set. |
| `ui.serveStatic` | `true` | `false` = only `/api/*` from the Python server; bring your own nginx for the bundle. |
| `ui.webhooks.events` | `server.online,server.offline,player.join,player.leave` | Comma-separated; add `backup.created`, `backup.restored`, `config.applied` as needed. |
| `ui.webhooks.pollSeconds` | `15` | Event detector polling cadence. |
| `ui.webhooks.timeout` | `5` | HTTP POST timeout. |
| `ui.webhooks.url` / `.urlSecret.{name,key}` | `""` | Generic JSON POST target (inline or Secret-backed). |
| `ui.webhooks.discordUrl` / `.discordUrlSecret.{name,key}` | `""` | Discord embed URL (inline or Secret-backed). |

### Resources

| Value | Default | Purpose |
|---|---|---|
| `resources.game.requests` | `cpu: 2000m`, `memory: 4Gi` | Scheduling floor. |
| `resources.game.limits.memory` | `16Gi` | Matches the official 10-player ceiling. |
| `resources.game.limits.cpu` | *(unset)* | Leave unset to let the patch pace the idle loop; set a cap (e.g. `500m`) as belt-and-braces. |
| `resources.xvfb.{requests,limits}` | `cpu: 50m`/`memory: 32Mi` → `memory: 256Mi` | Xvfb is tiny. |
| `resources.ui.{requests,limits}` | `cpu: 10m`/`memory: 32Mi` → `memory: 256Mi` | 256Mi covers `/api/idle-cpu-patch` scanning an unknown-MD5 binary; steady state is ~20 MB. |

### Security

| Value | Default | Purpose |
|---|---|---|
| `securityContext.{runAsUser,runAsGroup,fsGroup}` | `10000` | Non-root throughout; `fsGroup` lets the PVC be owned by the same id. |
| `securityContext.fsGroupChangePolicy` | `OnRootMismatch` | Avoids recursive PVC ownership walks on every mount when the volume root already has the expected group. |
| `containerSecurityContext.allowPrivilegeEscalation` | `false` | |
| `containerSecurityContext.readOnlyRootFilesystem` | `false` | SteamCMD writes to `$HOME`; can't be read-only. |
| `containerSecurityContext.capabilities.drop` | `[ALL]` | The UI sidecar re-adds `CAP_KILL` in its own spec (needed to signal the sibling game process under shared PID namespace). |
