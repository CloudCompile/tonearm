#!/usr/bin/env python3
"""systemd user-unit generator for tonearm (#52, #133).

Emits a unit that encodes hard-won ops lessons:
- proper detachment under systemd (Type=simple, Restart=on-failure)
- XDG_RUNTIME_DIR guaranteed for the user pulse/audio path
- non-root by default; Chrome profile treated as the sensitive artifact
- explicit env placeholders (API key, profile, audio knobs)

Usage:
  python3 scripts/systemd_unit.py --dir ~/tonearm --venv ~/tonearm/.venv
  python3 scripts/systemd_unit.py --user --enable-hint
"""

from __future__ import annotations

import argparse
import os
import pwd
import sys
from pathlib import Path


def default_user() -> str:
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        return os.environ.get("USER", "tonearm")


def render(args) -> str:
    workdir = Path(args.dir).expanduser()
    venv = Path(args.venv).expanduser() if args.venv else workdir / ".venv"
    profile = Path(args.profile).expanduser()
    python = venv / "bin" / "python"
    user = args.user or default_user()

    env_lines = [
        "Environment=XDG_RUNTIME_DIR=%t",
        f"Environment=TONEARM_PROFILE={profile}",
        "Environment=TONEARM_AUTO_CHROME=1",
    ]
    if args.api_key_env:
        env_lines.append("Environment=TONEARM_API_KEY=%i")  # loaded from credentials
    else:
        env_lines.append("# Environment=TONEARM_API_KEY=change-me  # set before non-localhost exposure")
    if args.audio:
        env_lines += [
            "Environment=TONEARM_AUDIO=1",
            "Environment=TONEARM_AUDIO_DISPLAY=:2",
            "Environment=TONEARM_AUDIO_SINK=tonearm_audio",
            "Environment=PULSE_SERVER=unix:%t/pulse/native",
        ]
    if args.extra_env:
        for item in args.extra_env:
            env_lines.append(f"Environment={item}")

    lines = [
        "[Unit]",
        "Description=tonearm — self-hosted Apple Music API",
        "Documentation=https://cloudcompile.github.io/tonearm/",
        "After=network-online.target sound.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        f"User={user}" if args.system_unit else "# user unit: runs as the owning user",
        f"WorkingDirectory={workdir}",
        f"ExecStart={python} -m tonearm",
        "Restart=on-failure",
        "RestartSec=5",
        "TimeoutStopSec=20",
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        *env_lines,
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    # drop the placeholder user comment line cleanly for user units
    return "\n".join(l for l in lines if l != "# user unit: runs as the owning user") + "\n"


def main() -> int:
    p = argparse.ArgumentParser(description="Generate a tonearm systemd unit")
    p.add_argument("--dir", default=str(Path.home() / "tonearm"), help="working directory / install root")
    p.add_argument("--venv", default=None, help="venv path (default: <dir>/.venv)")
    p.add_argument("--profile", default=str(Path.home() / ".config/tonearm/chrome-profile"),
                   help="Chrome profile dir (sensitive: holds session)")
    p.add_argument("--user", default=None, help="User= for system units")
    p.add_argument("--system-unit", action="store_true", help="emit a system unit (adds User=)")
    p.add_argument("--audio", action="store_true", help="include audio pipeline env (Xvfb/pulse/sink)")
    p.add_argument("--api-key-env", action="store_true",
                   help="load API key via systemd credentials (%i placeholder)")
    p.add_argument("--extra-env", action="append", default=[], help="extra Environment=KEY=VALUE (repeatable)")
    p.add_argument("--output", "-o", default=None, help="write to file instead of stdout")
    p.add_argument("--enable-hint", action="store_true", help="print enable/linger hints to stderr")
    args = p.parse_args()

    unit = render(args)
    if args.output:
        out = Path(args.output).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(unit, encoding="utf-8")
        print(str(out))
    else:
        sys.stdout.write(unit)

    if args.enable_hint:
        target = args.output or "~/.config/systemd/user/tonearm.service"
        print("Hints:", file=sys.stderr)
        print("  systemctl --user daemon-reload", file=sys.stderr)
        print(f"  systemctl --user enable --now {Path(target).name}", file=sys.stderr)
        print(f"  loginctl enable-linger {args.user or default_user()}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
