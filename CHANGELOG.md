# Changelog

All notable changes to tonearm are documented here. Format:
[keep-a-changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/) until 1.0 (then the [freeze
policy](docs/deprecation.md) governs breaking changes).

Release notes for each version are drafted by `scripts/release_notes.py`
(merged PRs since the previous tag) and edited by hand before publishing.

## [0.8.0] — 2026-11-13 — The Freeze

The release where the API surface becomes a contract: documented
deprecation policy, enforced by CI. Docs site live; batch reads; the
catalog goes worldwide.

### Added
- **The freeze** (#50, #126–128): deprecation policy
  ([docs/deprecation.md](docs/deprecation.md)) — one minor cycle of
  `Deprecation:`/`Sunset:` headers, then the route dies; the pinnable
  surface marker (`/health` `surface` + `X-Tonearm-Surface`); the
  OpenAPI contract gate (CI diffs every PR against
  [api/openapi.json](api/openapi.json), breaking changes fail without
  the `api-breaking` label).
- **Batch reads** (#183): `POST /api/batch` — N reads in one round trip
  (a dashboard render was 5 tunnel round trips; now it's 1). Entries
  `{path, params}`, answers `[{status, body}]` with per-entry failure
  isolation; reads only, cap 10.
- **Multi-storefront catalog** (#184): `?storefront=` (validated ISO
  alpha-2) on every catalog route — regional charts, search,
  availability; library/me routes stay on the account's home
  storefront. Live-verified us/gb/de.
- **The docs site** (#129–131): mkdocs-material on GitHub Pages,
  deployed on release tags; the API reference generates from the
  OpenAPI snapshot with a route-coverage check; env table + field
  notes generate from this README.
- **CHANGELOG + notes automation** (#138): this file (keep-a-changelog)
  and `scripts/release_notes.py` (drafts from squash-merge history).
- **e2b sandbox template** (`sandbox/`): the full runtime (chrome,
  pulse, ffmpeg, Xvfb) as a 2 vCPU / 4GB sandbox template for safe
  development.

### Fixed
- Audio pipeline teardown: a duplicate `Pipeline.stop()` (smuggled in
  by the #162 rename) defeated the #177 process-group kill —
  parecord/ffmpeg lingered forever after the last stream listener
  left (#200).
- CDP deadlock: the renderer watchdog armed only on a READY MusicKit
  tab, so a renderer that died before first-ready wedged the whole API
  layer forever. The watchdog now arms when the websocket connects;
  CDP recv slices tightened to 5s (deadlines were overshooting ~60s)
  (#201).
- `TONEARM_CHROME_EXTRA` env: extra chrome args for constrained hosts.
- The passthrough route's operationId flip-flopped by hash order
  between processes (the contract gate's first catch, fixed at root).
- `fastapi` pinned exactly — schema output varies by patch version and
  the snapshot must not drift.

## [Unreleased]

### Added
- (nothing yet)

## [0.9.0] — 2026-11-16 — Install & Harness (Tidbyt stable)

Tidbyt Gen 2 support is now stable/released, and the install + offline
regression story lands for bare metal and CI.

### Added
- **Tidbyt Gen 2 firmware & device integration** (`firmware/tidbyt-nowplaying/`, `/api/device/tidbyt/*`): the dead-cloud pixel display becomes a tonearm
  now-playing screen, stream speaker, and gesture remote (1 tap toggle / 2
  taps next / 3 taps smart-back / hold playlist 1 / double hold playlist 2).
  ESPHome pinned 2025.8.0; device authenticates with its own revocable scoped
  key (OTA/log shipping/reporting/cmd polling endpoints).
- **Install script** (`scripts/install.sh`, #132): venv bootstrap, dependency
  install, 0700 Chrome profile dir, Chrome/audio-dep checks; safe to re-run.
- **systemd unit generator** (`scripts/systemd_unit.py`, #133): emits a user
  or system unit with proper detachment/restart policy, `XDG_RUNTIME_DIR`/
  pulse env for audio, non-root defaults, and explicit key/profile handling.
- **Fresh-VM runbook** (`docs/install.md`, #134): timed, documented proof path
  for bare-metal installs.
- **Offline route harness** (`tests/test_route_harness.py`,
  `scripts/harness_routes.py`, #53/#135/#136): fixture-driven regression over
  a mock server via plain urllib (no TestClient, no Chrome/Pulse/X, no Apple),
  runnable in CI/unittest against synthetic fixtures.
- **Live-vs-mock parity procedure** (`docs/harness-parity.md`, #137): manual
  spot-check guide to catch fixture/value/ordering rot the model checks can't.
- **Deploy docs: tunnel era** (`docs/deploy-tunnels.md`, #190): LAN vs
  Tailscale Serve (tailnet) vs Tailscale Funnel (public, SSE-safe) vs
  Cloudflare quick tunnels (pages work, SSE starved — avoid for live UI),
  including key-prompt flow notes.

## [0.7.0] — 2026-11-13 — Multi-user

### Added
- **Multi-key auth** (#192): scoped keys (`read`/`write`) in a 0600
  hash-only registry, per-key 60s rate ceilings (429 + `Retry-After`),
  per-key SSE connection caps, per-key request metrics, and a
  `python3 -m tonearm keys` CLI. The master key keeps full access.
- **Audit trail** (#193): every authenticated mutation is recorded
  (store table + 1000-ring fallback), `GET /api/audit` master-only,
  bodies never logged.
- **Session truth in `/health`** (#194): `session: ok|expired|unknown` —
  a dead session reads 502 `session_expired` with a "run login" hint
  instead of lying healthy.
- Docker images on ghcr for 0.7.0, generated clients refreshed.

## [0.6.2] — 2026-11-13 — Live-session hardening

### Fixed
- CDP busy-storm: busy-waiters no longer close the shared websocket
  (they retry without touching the socket) (#174).
- Apple 429s map to `rate_limited` with `Retry-After` (#175).
- The release watcher skips stale boots and runs on a 180s time budget
  (#176); pipeline subprocesses die with the server (#177).
- Progress-proof plays: the history writer only records plays that
  actually progressed (#178).

## [0.6.1] / [0.6.0] — 2026-11-12 — Automation

### Added
- Webhooks + scheduled digest emails (#164)
- Multi-key groundwork and generated clients (#165, #166, #168)
- The release watcher, smart playlists, and Home Assistant recipes (#167)

## [0.5.x] — 2026-11-12 — Listening experience

### Added
- Queue management (`PUT /api/playback/queue`), opus/ogg formats,
  loudnorm, playhead-aligned joins (#152)

### Fixed
- The queue-reorder MusicKit dance, live-tested (#159)

## [0.4.0] — 2026-11-12 — Typed API & clients

### Added
- Pydantic response models on every JSON route (CI-enforced), the
  standard error envelope, generated TypeScript/Python clients (#195
  continues this), tarball releases.

## [0.3.0] — 2026-11-12 — Memory (the local store)

### Added
- Local SQLite store: library, plays (history writer), snapshots;
  `TONEARM_STORE=1`.

## [0.2.x] — 2026-11-12 — Bulletproof

### Added
- Read cache with single-flight coalescing, 429 backoff, watchdog,
  SSE live events.

## [0.1.0] — 2026-11-11 — First public release (as ampd)

### Added
- Playback control, library/catalog search, favorites, the demo page,
  audio streaming at `/api/audio/stream.mp3`.

[Unreleased]: https://github.com/CloudCompile/tonearm/compare/v0.9.0...HEAD
[0.9.0]: https://github.com/CloudCompile/tonearm/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/CloudCompile/tonearm/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/CloudCompile/tonearm/compare/v0.6.2...v0.7.0
[0.6.2]: https://github.com/CloudCompile/tonearm/compare/v0.6.1...v0.6.2
[0.6.1]: https://github.com/CloudCompile/tonearm/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/CloudCompile/tonearm/compare/v0.5.1...v0.6.0
[0.5.x]: https://github.com/CloudCompile/tonearm/compare/v0.4.0...v0.5.1
[0.4.0]: https://github.com/CloudCompile/tonearm/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/CloudCompile/tonearm/compare/v0.2.1...v0.3.0
[0.2.x]: https://github.com/CloudCompile/tonearm/compare/v0.1.0...v0.2.1
[0.1.0]: https://github.com/CloudCompile/tonearm/releases/tag/v0.1.0
