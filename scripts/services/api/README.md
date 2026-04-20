# Live Stats API (deferred)

The Enshrouded self-hosted bundle ships a lightweight stats API that probes
the server over Steam A2S / game-UDP and exposes player counts, latency, and
local host load at `/v1/stats`.

Windrose does not expose a Steam query port and uses NAT punch-through rather
than a stable well-known port, so the same probe approach does not translate
directly.

Follow-up work for this path:

1. Identify what signals are observable:
   - process presence (`pgrep WindroseServer-Win64-Shipping`)
   - log markers (parse `R5/Saved/Logs/*.log` for player join/leave)
   - Proton / wineserver CPU/memory from `/proc`
   - Saved world directory size + mtime
2. Build a log-tail based probe (no UDP A2S).
3. Port the `/v1/stats`, `/healthz`, landing page, and webhook/Discord delivery
   pattern from `enshrouded-self-hosted/tools/services/api/`.

Until this lands, the UI sidecar at port 28080 surfaces the invite code,
file-presence, server-process state, and save-version via `/cgi-bin/status.sh`
— see [`ui/`](../../ui/).
