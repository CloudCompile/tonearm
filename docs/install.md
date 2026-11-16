# Install & bare metal

tonearm runs as a long-lived process driving a real, logged-in Chrome session.
On a fresh VM or bare-metal box, the goal is: **venv + deps + private profile
dir + a unit that survives logout**.

## Quick install (script)

From a checkout of the repo:

```bash
git clone https://github.com/CloudCompile/tonearm.git ~/tonearm
cd ~/tonearm
bash scripts/install.sh --dir ~/tonearm --systemd
```

The script (`scripts/install.sh`, #132) is safe to re-run. It:

- creates a Python venv (`--venv` to override),
- installs `requirements.txt` (skip with `--no-deps`),
- bootstraps the Chrome profile dir as **0700** (the profile holds your Apple
  Music session — treat it as a secret),
- checks for Chrome/Chromium and warns about audio deps (`ffmpeg`,
  `pulseaudio`, `Xvfb`) only needed when `TONEARM_AUDIO=1`,
- optionally emits `~/.config/systemd/user/tonearm.service` (`--systemd`).

Then sign in once with a visible browser and start the server:

```bash
TONEARM_PROFILE=~/.config/tonearm/chrome-profile ~/tonearm/.venv/bin/python -m tonearm.login
~/tonearm/.venv/bin/python -m tonearm
```

## systemd unit generator

For more control (audio env, extra knobs, system vs user unit), use the
generator (#133):

```bash
python3 scripts/systemd_unit.py --dir ~/tonearm --venv ~/tonearm/.venv --audio \
  -o ~/.config/systemd/user/tonearm.service --enable-hint
systemctl --user daemon-reload
systemctl --user enable --now tonearm.service
loginctl enable-linger "$USER"   # keep it running across logouts
```

The generated unit encodes the hard-won ops lessons:

- **Detachment/restarts:** `Type=simple`, `Restart=on-failure`, bounded stop
  timeout — the service owns its session (no shell-trap/TTY hazards).
- **Pulse/audio:** `XDG_RUNTIME_DIR=%t` is always set; `--audio` adds the
  private sink/display env so `TONEARM_AUDIO=1` works under systemd.
- **Non-root & secrets:** run as the owning user; the Chrome profile dir is the
  sensitive artifact (0700). Set `TONEARM_API_KEY` before exposing beyond
  localhost.

Check status/logs:

```bash
systemctl --user status tonearm
journalctl --user -u tonearm -f
```

## Fresh-VM runbook (proof it works, #134)

A documented, timed run on a clean box. Record your timings when you execute it.

1. **Provision** a fresh Debian/Ubuntu VM (2 CPU / 4 GB is comfortable; Chrome is
   the heavy part). SSH in as a non-root user.
2. **System deps:** `sudo apt-get update && sudo apt-get install -y python3 python3-venv git`
   plus Chrome/Chromium (and `ffmpeg pulseaudio pulseaudio-utils xvfb` if you
   want audio streaming). *Time: ~2–5 min depending on image/cache.*
3. **Clone + install:** `git clone https://github.com/CloudCompile/tonearm.git ~/tonearm && cd ~/tonearm && bash scripts/install.sh --dir ~/tonearm --systemd`.
   *Time: ~1–3 min (pip deps).*
4. **Sign in:** run `python -m tonearm.login` once from a session that can show
   a browser (or copy an existing profile into `~/.config/tonearm/chrome-profile`).
   *Time: ~1 min (manual).*
5. **Start:** `systemctl --user daemon-reload && systemctl --user enable --now tonearm && loginctl enable-linger $USER`.
6. **Verify:** `curl -s http://127.0.0.1:8188/health` returns `{"status":"ok",...}` and
   `curl -s http://127.0.0.1:8188/api/library/songs?limit=1` responds. *End-to-end
   (steps 2–6, excluding manual login time): typically < 10 min on a warm image.*

If anything fails, `journalctl --user -u tonearm -n 200 --no-pager` and the
install script's warnings are the first places to look. Re-running the installer
is safe.

## Docker

For containerized installs (the reference setup), see the README and
`docker-compose.yml`. Prebuilt multi-arch images publish to
`ghcr.io/cloudcompile/tonearm` on release tags.
