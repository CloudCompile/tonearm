# Tidbyt trip checklist — demo away from home

Firmware ≥ **0.4.34**: the trip hotspot is Wi-Fi network **#1** (two entries,
both cases), home networks follow, self-healing audio keeps the speaker alive
when the stream drops.

## Before you leave (do this while still on Bluey)

1. Confirm the device is on 0.4.34+ and reporting:
   ```sh
   KEY=$(cat ~/.config/tonearm/opencode-key.txt)
   curl -s "http://127.0.0.1:8200/api/device/tidbyt/status?api_key=$KEY"
   ```
   Expect `version: 0.4.34`, a fresh `received_at`, and `ssid: Bluey`.
2. Power-cycle it once. It should come back, join Wi-Fi, show the current
   track within ~30s and start audio within ~20s. That single cycle proves the
   whole boot path (Wi-Fi → display → stream).
3. Play something so the artwork/backdrop cache is warm:
   `tonearm play <song>`.

## Hotspot requirements (the usual demo-killers)

- **2.4 GHz only.** The ESP32 has no 5 GHz radio.
  - iPhone: Settings → Personal Hotspot → **Maximize Compatibility: ON**.
  - Android: pick 2.4 GHz in hotspot settings if offered.
- **Open vs password.** `secrets.yaml` lists `CJ's HOTSPOT | FREE WIFI` with a
  **blank password**, i.e. the firmware expects an *open* network. iPhone
  personal hotspots always have a password, so if this is an iPhone hotspot the
  device will fail to join until we add the real password and publish a new
  build. Set it open, or tell me the password.
- **WPA2, not WPA3-only.** ESP32 Arduino joins WPA2 fine; a WPA3-only network
  will not associate.
- **No captive portal.** A venue "free wifi" login page counts as *connected
  but offline* — the panel would show `offline`.

## On site

1. Hotspot on, device powered, wait ~30s.
2. Panel: track title scrolls if long, artist under it, progress bar at the
   bottom. If the bottom says `offline`, Wi-Fi is up but the relay is not
   reachable (check the `ssid` field in the status JSON).
3. Sound: the device pulls the MP3 stream from the relay. It retries
   automatically after a dropped connection (5 tries per 10 min window).

## If something is wrong

| Symptom | Do this |
|---|---|
| Panel blank / frozen, no heartbeat | Power-cycle. A hung OTA download is the usual cause. |
| Shows `offline` but Wi-Fi connected | Relay/network path. Check `curl http://20.221.210.24/tidbyt-relay/api/playback/now-playing` → `401` means it is alive. |
| Silent, but display updates | `tonearm_ok` path is fine → audio stream dropped. Wait 10s for self-heal, or power-cycle. |
| Won't join hotspot | 2.4 GHz off, wrong password, or WPA3-only. |

## Server side (this box)

- `tonearm` runs as a **systemd user service**, `Restart=always`, enabled at
  boot (linger is on):
  ```sh
  export XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
  systemctl --user status tonearm.service
  ```
- Health: `curl http://127.0.0.1:8200/health` → `status: ok`, `browser: authorized`.
- Relay + OTA are served by Caddy on `http://20.221.210.24` (`/tidbyt-relay/*`
  proxies to 8200, `/tidbyt-updates/*` serves the manifest and binaries).
