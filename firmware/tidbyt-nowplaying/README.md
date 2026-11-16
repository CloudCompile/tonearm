# Tonearm Tidbyt firmware (`firmware/tidbyt-nowplaying/`)

ESPHome firmware and support configs for running a **Tidbyt Gen 2 (ESP32)** as a Tonearm companion device:

- **Now-playing display (HUB75 64×32):** title/artist over a dimmed color artwork backdrop, progress bar, and playback indicator.
- **Onboard speaker audio:** streams Tonearm audio (`/api/audio/stream.mp3`, `join_pad=0`) via the I2S media player when Wi‑Fi/DMA conditions allow.
- **Touch gestures (remote control):** control Tonearm playback/playlists from the top pad.
- **Offline-friendly behavior:** when Tonearm/Wi‑Fi is unreachable, the device keeps cached last-known track/art/progress, shows explicit offline state, can display an SNTP clock view (if time was previously synced), and supports limited local actions.
- **OTA + status reporting:** checks/applies OTA updates and reports compact device status back to Tonearm.

The main production configuration is **`tidbyt-minimal-a2dp.yaml`** (ESPHome node name kept for OTA/device identity continuity).

> **Build environment:** use **ESPHome 2025.8.0** (validated for this HUB75 + Arduino + I2S media stack). Prefer building/flashing workflows in an **e2b sandbox** (see repo `AGENTS.md`) rather than on the production VPS.

## Current behavior (main firmware, 0.4.27 line)

### Display
- HUB75 matrix (FM6126A, 64×32), known-good Gen 2 pin mapping, complete-frame redraw.
- Shows title, artist, dimmed artwork backdrop (server-generated raw RGB565 backdrop), progress bar, and a small playing indicator.
- Progress is **interpolated locally** between 5s server polls and shifted by `audio_offset_ms` (default 1500ms, configurable in substitutions) so the bar better matches audible playback.
- **Offline UX:** if now-playing data is stale/unreachable, the screen indicates offline while keeping cached content; tap can toggle between cached track view and clock view (clock requires prior SNTP sync).

### Audio
- Starts audio after Wi‑Fi connect with a guarded delay (DMA/heap checks, once-per-boot attempt logic) and streams from `${tonearm_base}/api/audio/stream.mp3?join_pad=0&api_key=...`.
- Audio is best-effort and depends on network/Tonearm availability and device memory conditions.

### Touch gestures
| Gesture | Online action | Offline/local behavior |
|---|---|---|
| 1 tap | Play / pause toggle | Toggle offline view (last track ↔ clock) |
| 2 taps | Next track | No-op |
| 3 taps | Back (restart if >10s, else previous) | No-op |
| Hold ~0.8–2.5s, release | Shuffle **Favorites** and play it | Cycle panel brightness locally |
| Hold, release, hold again | Shuffle **Heavy Rotation** and play it | No-op |

Default playlist IDs are set in substitutions (`playlist_1_id` = Favorites, `playlist_2_id` = Heavy Rotation); adjust for your library.

## Control center

Tonearm includes a **Tidbyt Control Center** web UI at **`/tidbyt`** (and raw device status at `/tidbyt-status`) for:
- Monitoring device status (firmware/version, state, last report age, uptime, Wi‑Fi RSSI, DMA heap, IP)
- Now playing view + transport controls (play/pause/prev/next/back)
- Volume control and shuffle toggles
- Library search and manual playback
- Playlist browsing/playback
- Arming OTA updates for the device

Open it on your Tonearm host (e.g. `http://<tonearm-host>:8200/tidbyt`) and set the API key/base URL in “connection” if required.

## Key files

- **`tidbyt-minimal-a2dp.yaml`** — main production firmware (display + speaker + offline mode + gestures + OTA/reporting). Stable ESPHome node name.
- **`tidbyt-status.yaml`** — lightweight remote status/bootstrap image.
- **`tidbyt-bootstrap.yaml`**, **`tidbyt-migrate.yaml`**, **`tidbyt-recovery.yaml`**, **`tidbyt-rescue.yaml`**, **`provision.yaml`** — provisioning/migration/recovery/bootstrap variants for specific device states.
- **`tidbyt-display*.yaml`, `tidbyt.yaml`** — additional/legacy display or integration variants; review before use.
- **`external_components/`** — local components/helpers used by the firmware (HUB75 wrapper integration, art buffer/helpers, etc.).
- **`dual_ota*.csv`** — partition tables (select the correct one for the device’s flash/partition layout).

## Configuration

Edit substitutions at the top of the target YAML (especially `tidbyt-minimal-a2dp.yaml`):
- `tonearm_base` — Tonearm base URL/reachable relay endpoint used by the device
- `tonearm_key` — device-scoped API key
- `playlist_1_id`, `playlist_2_id` — playlist IDs for hold gestures
- `audio_offset_ms` — progress-bar lag compensation to match audible delay

Wi‑Fi credentials and secrets are provided via ESPHome secrets (e.g. `!secret wifi_ssid`, `!secret wifi_password`, `api_encryption_key`).

## Build & flash notes

1. Use **ESPHome 2025.8.0** (newer ESPHome releases changed components used by this stack).
2. Validate/compile from this directory, preferably in an **e2b sandbox**:
   ```bash
   pip install "esphome==2025.8.0"
   esphome config tidbyt-minimal-a2dp.yaml
   esphome compile tidbyt-minimal-a2dp.yaml
   ```
3. First flash is typically over USB (Chrome Web ESPHome / `esphome run` with device attached). Subsequent updates can be delivered via OTA (manifest/arm flow) when the device is online and reachable.
4. 2.4 GHz Wi‑Fi only. Hardware/pin mappings are Gen 2-specific and follow the known-good community mapping for this project.

## Operational notes

- OTA versions should only move forward; do not publish numeric downgrades over newer firmware.
- If Tonearm is down/unreachable, playback/streaming control requires connectivity to return, but the device retains offline display/cache/local UI behavior.
- “Display stability first”: avoid changing audio/routing/timing flags during display-only experiments.
- Device-specific tuning (timing, thresholds, offsets) may vary by hardware/network conditions.

## Status

- Main firmware line: **0.4.27** (local config/build line as of this sweep; published OTA state may differ by environment).
- On-device behavior depends on your Tidbyt hardware, network, and Tonearm server availability.
