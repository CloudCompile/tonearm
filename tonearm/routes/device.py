"""Small authenticated device telemetry inbox for remote Tidbyt development.

Device initiated only: the ESP32 POSTs a compact heartbeat over its existing
outbound HTTP relay. We retain the latest report per device in process memory;
no log credentials, secrets, or arbitrary request headers are stored.
"""
from __future__ import annotations

import threading
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/device", tags=["device"])
_lock = threading.Lock()
_last: dict[str, dict] = {}
_opencode: dict[str, dict] = {}
# OTA trigger (v0.4.13): armed version per device; the device polls /cmd and
# runs update.perform when it sees cmd=update and its version is behind.
_update_requested: dict[str, dict] = {}


class DeviceReport(BaseModel):
    device: str = Field(default="tidbyt", max_length=40)
    version: str | None = Field(default=None, max_length=48)
    ip: str | None = Field(default=None, max_length=48)
    wifi_rssi: int | None = None
    free_heap: int | None = None
    uptime_s: int | None = None
    reset_reason: str | None = Field(default=None, max_length=64)
    ota_status: str | None = Field(default=None, max_length=32)
    state: str | None = Field(default=None, max_length=64)
    psram_bytes: str | None = Field(default=None, max_length=20)
    flash_bytes: str | None = Field(default=None, max_length=20)
    internal_heap: str | None = Field(default=None, max_length=20)
    internal_largest_block: str | None = Field(default=None, max_length=20)
    dma_heap: str | None = Field(default=None, max_length=20)
    bt_deferred: str | None = Field(default=None, max_length=8)
    bt_started: str | None = Field(default=None, max_length=8)


class DeviceAck(BaseModel):
    ok: bool
    received_at: float


class DeviceStatusSnapshot(BaseModel):
    devices: dict[str, dict]
    opencode: dict[str, dict]


class OpenCodeReport(BaseModel):
    session_id: str = Field(max_length=80)
    state: str = Field(default="busy", max_length=24)
    project: str = Field(default="", max_length=18)
    activity: str = Field(default="working", max_length=20)


class DeviceCmd(BaseModel):
    cmd: str = Field(description="Command for the device: 'update' or 'none'")


@router.post("/tidbyt/report", response_model=DeviceAck)
def report_tidbyt(body: DeviceReport, request: Request):
    identity = getattr(request.state, "identity", None)
    report = body.model_dump()
    report["received_at"] = time.time()
    report["key_id"] = identity.describe() if identity else "keyless"
    with _lock:
        _last[body.device] = report
        # Auto-disarm the OTA trigger once the device reports the target.
        req = _update_requested.get(body.device)
        if req and req.get("version") and body.version == req.get("version"):
            del _update_requested[body.device]
    return {"ok": True, "received_at": report["received_at"]}


@router.post("/tidbyt/logs")
async def report_logs(request: Request):
    """v0.4.13: device ships its ESPHome log lines over WiFi (plain text,
    one line per line). Appended into the serial bridge's console ring so
    the existing /serial page and console API show them — no USB needed."""
    from ..serial_bridge import _console

    identity = getattr(request.state, "identity", None)
    if identity is None or not identity.can_write:
        raise HTTPException(403, "write key required")
    raw = (await request.body()).decode("utf-8", "replace")
    n = 0
    for line in raw.splitlines()[:120]:
        line = line.strip()
        if not line:
            continue
        _console.append(f"[wifi] {line[:240]}")
        n += 1
    return {"ok": True, "stored": n}


@router.post("/tidbyt/update")
async def trigger_update(request: Request, body: dict):
    """Arm the OTA trigger: the device picks it up via /tidbyt/cmd within
    one poll interval and runs update.perform itself."""
    identity = getattr(request.state, "identity", None)
    if identity is None or not identity.can_write:
        raise HTTPException(403, "write key required")
    device = str(body.get("device", "tidbyt-a2dp"))[:40]
    version = str(body.get("version", ""))[:48]
    with _lock:
        _update_requested[device] = {"version": version, "armed_at": time.time()}
    return {"ok": True, "armed": device, "version": version}


@router.get("/tidbyt/cmd", response_model=DeviceCmd)
async def poll_cmd(device: str = "tidbyt-a2dp", version: str = ""):
    """Device-initiated poll (keyed like report). Returns cmd=update while
    a trigger is armed and unexpired for a version newer than reported."""
    with _lock:
        req = _update_requested.get(device)
        if req:
            expired = time.time() - req.get("armed_at", 0) > 1800
            caught_up = bool(req.get("version")) and req["version"] == version
            if expired or caught_up:
                del _update_requested[device]
                req = None
    return {"cmd": "update" if req else "none"}


@router.get("/tidbyt/status", response_model=DeviceStatusSnapshot)
def tidbyt_status():
    now = time.time()
    with _lock:
        sessions = {key: dict(value) for key, value in _opencode.items()}
        # OpenCode may exit/reload without a final idle event. Never leave a
        # stale BUSY indicator pinned forever on the Tidbyt.
        for value in sessions.values():
            if now - value.get("received_at", 0) > 120:
                value["state"] = "idle"
                value["activity"] = "idle"
        return {"devices": dict(_last), "opencode": sessions}


@router.post("/tidbyt/opencode")
def report_opencode(body: OpenCodeReport, request: Request):
    """Accept only a short, allowlisted session summary; never accept
    prompts, code, tool arguments, or arbitrary event payloads."""
    identity = getattr(request.state, "identity", None)
    report = {
        "session_id": body.session_id,
        "state": body.state if body.state in {"busy", "idle", "error"} else "busy",
        "project": body.project[:18],
        "activity": body.activity[:20],
        "received_at": time.time(),
        "key_id": identity.describe() if identity else "keyless",
    }
    with _lock:
        _opencode[body.session_id] = report
        # Bound retained sessions; this is an activity display, not an archive.
        if len(_opencode) > 12:
            oldest = min(_opencode, key=lambda k: _opencode[k]["received_at"])
            del _opencode[oldest]
    return {"ok": True, "received_at": report["received_at"]}
