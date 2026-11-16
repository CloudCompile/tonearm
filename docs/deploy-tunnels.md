# Deploy: the tunnel era

tonearm serves plain HTTP on `TONEARM_HOST:TONEARM_PORT` (default
`127.0.0.1:8188`). Exposing it beyond your machine is a reverse-proxy/tunnel
choice — **set `TONEARM_API_KEY` before exposing it publicly** (the bind guard
warns on non-localhost binds without one). Hard-won notes from live use (#190):

| Option | Reach | SSE (`/api/stream`) | Audio (`/api/audio/stream.mp3`) | Notes |
|---|---|---|---|---|
| **Plain LAN** | same network | yes | yes | Simplest. Bind `0.0.0.0` (or a LAN IP) with an API key; clients use `http://<lan-ip>:8188`. |
| **Tailscale Serve** | your tailnet only | yes | yes | Private, authenticated by tailnet identity. Great default for personal devices. |
| **Tailscale Funnel** | public internet | yes | yes | Public HTTPS URL; passes SSE and long-lived audio streams reliably. Use an API key. |
| **Cloudflare quick tunnel** (`trycloudflare`) | public (ephemeral) | **no (starved)** | unreliable | Pages load but the live playhead/SSE is starved by bot-fight/edge buffering — **not suitable** for a live UI. Fine only for a quick one-shot page check. |

## Plain LAN

```bash
TONEARM_HOST=0.0.0.0 TONEARM_API_KEY='change-me' python3 -m tonearm
# clients: http://192.168.x.y:8188  (key via Authorization: Bearer or ?api_key=)
```

Keep it on a trusted network or front it with TLS. The PWA/prompter flow asks for
the key once and stores it locally.

## Tailscale Serve (tailnet-only, recommended private path)

```bash
python3 -m tonearm &                      # bound to localhost:8188
tailscale serve --bg 8188                 # tailnet-only https://<node>.<tailnet>.ts.net
# check: tailscale serve status
# remove: tailscale serve --https=443 off
```

Everything (SSE, audio, the PWA) works because traffic stays on your tailnet with
no bot-fight layer. Ideal for your own phone/laptop.

## Tailscale Funnel (public)

```bash
python3 -m tonearm &
tailscale funnel --bg 8188                # public https://<node>.<tailnet>.ts.net
# requires funnel enabled for your tailnet/node; set TONEARM_API_KEY first
```

Funnel passes SSE and the audio stream — this is the reliable public option. Share
the URL; guests use the key-prompt flow (the API key, entered once in the UI, is
sent as `Authorization: Bearer` and as `?api_key=` for EventSource which cannot
set headers).

## Cloudflare quick tunnels — avoid for live use

`cloudflared tunnel --url http://localhost:8188` gives an ephemeral public URL,
but Cloudflare's bot-fight/edge behavior starves long-lived SSE: pages render,
the live playhead and event stream do not. Use quick tunnels only for a quick
static/page sanity check; for anything live use **Tailscale Funnel** or a proper,
SSE-friendly reverse proxy you control.

## General reverse-proxy notes

- Preserve streaming: disable response buffering for `/api/stream` (SSE) and
  `/api/audio/stream.*`; allow long-lived connections and chunked transfer.
- Forward `Authorization` and pass query strings (`?api_key=` is required for
  EventSource clients).
- Terminate TLS at the proxy if you like; tonearm itself speaks plain HTTP.
- Never expose an unauthenticated instance to the public internet.
