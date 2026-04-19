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
  --version 0.1.0 \
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
| `env` (default) | Seed from the example `ServerDescription.json`, patch known keys from env on every start. Good for single-world deployments driven by chart values. |
| `managed` | Render `serverConfig.inlineJson` into a ConfigMap, merge `Password` from the optional Secret. Good for operators who want the full config declaratively. |
| `mutable` | Require a pre-existing `ServerDescription.json` on the PVC and leave it alone. Good for multi-world deployments where the admin UI owns config edits. |

If you're editing per-world `WorldDescription.json` via the admin UI
on a multi-world setup, use `mutable` mode — `env` mode patches the
active world's name/preset on every restart (single-world assumption),
which would overwrite UI edits.

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
