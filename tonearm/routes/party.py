"""Invite-only listening party API.

Party keys are independent, hashed, and constrained to party suggestion/vote
operations. They are deliberately not accepted by the general API auth guard.
"""
import hashlib
import secrets
import threading
import time
import logging
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, Response

from .. import musickit as mk
from .. import keys as keys_mod
from ..browser import BrowserError
from ..config import config
from ..models import (PartyAction, PartyCreate, PartyCreateResult, PartyJoin,
                      PartyJoinResult, PartySearch, PartyState,
                      PartySuggestionCreate, PartyVote)
from ..store import StoreDisabled, store

router = APIRouter(prefix="/party", tags=["party"])
log = logging.getLogger("tonearm.party")


def _enabled():
    if not config.party_enabled:
        raise HTTPException(404, "party mode is disabled (set TONEARM_PARTY=1)")
    if not config.store_enabled:
        raise HTTPException(501, "party mode requires TONEARM_STORE=1")


def _digest(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def _host_allowed(request, party):
    identity = getattr(request.state, "identity", None)
    return (identity is None or identity.is_master or
            identity.key_id == party.get("host_key_id"))


def _guest(request, party_id=None):
    _enabled()
    token = request.headers.get("X-Party-Key", "")
    if not token:
        raise HTTPException(401, "pass X-Party-Key from the party join response")
    try:
        guest = store.party_guest_by_token(_digest(token))
    except StoreDisabled as e:
        raise HTTPException(501, str(e))
    if not guest or (party_id and guest["party_id"] != party_id):
        raise HTTPException(401, "invalid or expired party key")
    try:
        party = store.party_get(guest["party_id"])
    except StoreDisabled as e:
        raise HTTPException(501, str(e))
    if not party or not _host_allowed(request, party):
        raise HTTPException(401, "party host credential is no longer valid")
    return guest


def _state(party_id, guest_id=""):
    try:
        result = store.party_public_state(party_id, guest_id)
    except StoreDisabled as e:
        raise HTTPException(501, str(e))
    if not result:
        raise HTTPException(404, "party not found")
    return result


def _join_count(party_id):
    return store._require_conn().execute(
        "SELECT COUNT(*) FROM party_guests WHERE party_id=? AND active=1",
        (party_id,)).fetchone()[0]


@router.post("", response_model=PartyCreateResult)
def create(body: PartyCreate, request: Request, response: Response):
    """Create one active party; host API auth/write scope applies."""
    _enabled()
    name = body.name.strip()
    if not name or len(name) > 80:
        raise HTTPException(400, "party name must be 1–80 characters")
    party_id = "party_" + secrets.token_urlsafe(9)
    invite = secrets.token_urlsafe(24)
    party_key = secrets.token_urlsafe(32)
    try:
        identity = getattr(request.state, "identity", None)
        host_key_id = "master" if identity is None or identity.is_master \
            else identity.key_id
        party = store.party_create(party_id, _digest(invite), name, host_key_id)
        if not store.party_join(party_id, "host_" + secrets.token_urlsafe(8),
                                _digest(party_key), "Host"):
            raise HTTPException(500, "could not initialize party host")
    except ValueError as e:
        raise HTTPException(409, str(e))
    except StoreDisabled as e:
        raise HTTPException(501, str(e))
    response.set_cookie(f"party_audio_{party_id}", party_key, httponly=True,
        secure=request.url.scheme == "https", samesite="lax",
        path=f"/api/party/{party_id}/audio", max_age=60 * 60 * 24)
    root = str(request.base_url).rstrip("/")
    return {"party_id": party["id"], "invite": invite, "party_key": party_key,
            "invite_url": root + f"/party/join?invite={invite}", "name": name}


@router.get("/active", response_model=PartyState)
def active(request: Request):
    """Host view of the active party; ordinary API auth rules apply."""
    _enabled()
    try:
        party = store.party_active()
    except StoreDisabled as e:
        raise HTTPException(501, str(e))
    if not party:
        raise HTTPException(404, "no active party")
    if not _host_allowed(request, party):
        raise HTTPException(403, "active party belongs to another API key")
    return _state(party["id"])


@router.post("/join", response_model=PartyJoinResult)
def join(body: PartyJoin, request: Request, response: Response):
    """Exchange a one-time invite for a party-only guest key."""
    _enabled()
    name = body.name.strip()
    if not name or len(name) > 40:
        raise HTTPException(400, "guest name must be 1–40 characters")
    try:
        party = store.party_by_invite(_digest(body.invite))
        if not party:
            raise HTTPException(404, "invite is invalid or party is closed")
        guest_id = "guest_" + secrets.token_urlsafe(8)
        party_key = secrets.token_urlsafe(32)
        if not store.party_join(party["id"], guest_id, _digest(party_key), name):
            raise HTTPException(409, "party is full (20 guests maximum)")
    except StoreDisabled as e:
        raise HTTPException(501, str(e))
    response.set_cookie(f"party_audio_{party['id']}", party_key,
        httponly=True, secure=request.url.scheme == "https", samesite="lax",
        path=f"/api/party/{party['id']}/audio", max_age=60 * 60 * 24)
    return {"party_id": party["id"], "party_key": party_key,
            "party_url": f"/party/{party['id']}", "name": name}


@router.get("/{party_id}", response_model=PartyState)
def state(party_id: str, request: Request):
    guest = _guest(request, party_id)
    return _state(party_id, guest["id"])


@router.get("/{party_id}/search", response_model=PartySearch)
def search(party_id: str, request: Request, term: str):
    """Catalog-only guest search; exposes no library/personal data."""
    guest = _guest(request, party_id)
    term = term.strip()
    if len(term) < 2 or len(term) > 100:
        raise HTTPException(400, "search term must be 2–100 characters")
    try:
        data = mk.api_music(
            f"/v1/catalog/us/search?term={quote(term)}&types=songs&limit=10")
    except BrowserError as e:
        raise HTTPException(502, "catalog search unavailable") from e
    songs = (data.get("results", {}).get("songs", {}) or {}).get("data", [])
    return {"items": [{"id": s.get("id"), "title":
            (s.get("attributes") or {}).get("name"), "artist":
            (s.get("attributes") or {}).get("artistName"), "artwork":
            ((s.get("attributes") or {}).get("artwork") or {}).get("url")}
            for s in songs]}


@router.post("/{party_id}/suggest", response_model=PartyAction)
def suggest(party_id: str, body: PartySuggestionCreate, request: Request):
    guest = _guest(request, party_id)
    try:
        allowed = store.party_guest_action(guest["id"])
    except StoreDisabled as e:
        raise HTTPException(501, str(e))
    if not allowed:
        raise HTTPException(429, "please wait before another party action")
    catalog_id = body.catalog_id.strip()
    if not catalog_id or len(catalog_id) > 100 or any(c in catalog_id for c in "/?#"):
        raise HTTPException(400, "catalog_id must be a catalog song id")
    try:
        data = mk.api_music(f"/v1/catalog/us/songs/{catalog_id}")
    except BrowserError as e:
        raise HTTPException(404 if "404" in str(e) else 502,
                            "catalog song unavailable")
    row = (data.get("data") or [{}])[0]
    if row.get("type") != "songs":
        raise HTTPException(400, "party suggestions must be catalog songs")
    attrs = row.get("attributes") or {}
    try:
        created = store.party_suggest(party_id, guest["id"], catalog_id,
            attrs.get("name") or catalog_id, attrs.get("artistName"),
            attrs.get("artwork", {}).get("url"))
    except ValueError as e:
        raise HTTPException(409, str(e))
    if not created:
        raise HTTPException(404, "party is closed")
    return {"ok": True, "detail": "suggestion added" if created[1]
            else "song already suggested"}


@router.post("/{party_id}/suggestions/{suggestion_id}/vote",
             response_model=PartyAction)
def vote(party_id: str, suggestion_id: str, body: PartyVote, request: Request):
    guest = _guest(request, party_id)
    try:
        if not store.party_guest_action(guest["id"]):
            raise HTTPException(429, "please wait before another party action")
        if body.vote and store.party_guest_vote_count(party_id, guest["id"]) >= 10:
            raise HTTPException(409, "each guest may have up to 10 active votes")
    except StoreDisabled as e:
        raise HTTPException(501, str(e))
    result = (store.party_vote(party_id, suggestion_id, guest["id"])
              if body.vote else
              store.party_unvote(party_id, suggestion_id, guest["id"]))
    if result is None:
        raise HTTPException(404, "queued suggestion not found")
    return {"ok": True, "detail": "vote recorded" if body.vote else "vote removed"}


@router.delete("/{party_id}/suggestions/{suggestion_id}", response_model=PartyAction)
def remove_suggestion(party_id: str, suggestion_id: str, request: Request):
    _enabled()
    try:
        party = store.party_get(party_id)
    except StoreDisabled as e:
        raise HTTPException(501, str(e))
    if not party or not _host_allowed(request, party):
        raise HTTPException(403, "only the party host may remove suggestions")
    try:
        removed = store.party_remove_suggestion(party_id, suggestion_id)
    except StoreDisabled as e:
        raise HTTPException(501, str(e))
    if not removed:
        raise HTTPException(404, "queued suggestion not found")
    from .. import webhooks
    webhooks.emit("party.suggestion_removed", {"party_id": party_id,
                 "suggestion_id": suggestion_id})
    return {"ok": True}


@router.delete("/{party_id}/guests/{guest_id}", response_model=PartyAction)
def revoke_guest(party_id: str, guest_id: str, request: Request):
    _enabled()
    try:
        party = store.party_get(party_id)
    except StoreDisabled as e:
        raise HTTPException(501, str(e))
    if not party or not _host_allowed(request, party):
        raise HTTPException(403, "only the party host may remove guests")
    try:
        revoked = store.party_guest_revoke(party_id, guest_id)
    except StoreDisabled as e:
        raise HTTPException(501, str(e))
    if not revoked:
        raise HTTPException(404, "active guest not found")
    return {"ok": True}


@router.post("/{party_id}/close", response_model=PartyAction)
def close(party_id: str, request: Request, response: Response):
    """Host-only: ordinary API write authentication is enforced globally."""
    _enabled()
    try:
        party = store.party_get(party_id)
    except StoreDisabled as e:
        raise HTTPException(501, str(e))
    if not party or not _host_allowed(request, party):
        raise HTTPException(403, "only the party host may close this party")
    try:
        closed = store.party_close(party_id)
    except StoreDisabled as e:
        raise HTTPException(501, str(e))
    if not closed:
        raise HTTPException(404, "active party not found")
    response.delete_cookie(f"party_audio_{party_id}",
                           path=f"/api/party/{party_id}/audio")
    return {"ok": True}


@router.post("/{party_id}/rotate-invite", response_model=PartyCreateResult)
def rotate_invite(party_id: str, request: Request, response: Response):
    _enabled()
    try:
        party = store.party_get(party_id)
        if not party or not _host_allowed(request, party):
            raise HTTPException(403, "only the party host may rotate the invite")
        invite = secrets.token_urlsafe(24)
        if not store.party_rotate_invite(party_id, _digest(invite)):
            raise HTTPException(404, "active party not found")
        store.party_rotate_guest_keys(party_id)
        host_key = secrets.token_urlsafe(32)
        host_guest_id = "host_" + secrets.token_urlsafe(8)
        if not store.party_join(party_id, host_guest_id, _digest(host_key), "Host"):
            raise HTTPException(409, "party guest limit reached")
    except StoreDisabled as e:
        raise HTTPException(501, str(e))
    response.set_cookie(f"party_audio_{party_id}", host_key, httponly=True,
        secure=request.url.scheme == "https", samesite="lax",
        path=f"/api/party/{party_id}/audio", max_age=60 * 60 * 24)
    host_guest_id = store.party_host_guest_id(party_id)
    if host_guest_id:
        store.party_guest_revoke(party_id, host_guest_id)
        store.party_join(party_id, host_guest_id, _digest(host_key), "Host")
    root = str(request.base_url).rstrip("/")
    return {"party_id": party_id, "invite": invite, "party_key": host_key,
            "invite_url": root + f"/party/join?invite={invite}",
            "name": party["name"]}


@router.get("/{party_id}/audio", include_in_schema=False)
async def party_audio(party_id: str, request: Request):
    """Party-key-gated alias to the host's live MP3 stream."""
    try:
        token = request.cookies.get(f"party_audio_{party_id}", "")
        guest = store.party_guest_for_party(party_id, _digest(token)) if token else None
    except StoreDisabled as e:
        raise HTTPException(501, str(e))
    if not guest or guest["party_id"] != party_id:
        raise HTTPException(401, "invalid or expired party key")
    try:
        party = store.party_get(party_id)
    except StoreDisabled as e:
        raise HTTPException(501, str(e))
    if not party or not _host_allowed(request, party):
        raise HTTPException(401, "party host credential is no longer valid")
    from fastapi.responses import StreamingResponse
    from ..audio import FORMATS, AudioError, broadcaster
    from ..config import config
    if config.mock_enabled:
        raise HTTPException(404, "audio is unavailable in mock mode")
    spec = FORMATS["mp3"]
    try:
        q, meta = broadcaster.subscribe("mp3", None, None, True)
    except AudioError as e:
        raise HTTPException(400, str(e))

    async def gen():
        try:
            import asyncio
            while not await request.is_disconnected():
                current_guest = store.party_guest_for_party(
                    party_id, _digest(token))
                if not current_guest:
                    return
                try:
                    yield await asyncio.to_thread(q.get, timeout=1.0)
                except Exception:
                    continue
        finally:
            import asyncio
            await asyncio.to_thread(broadcaster.unsubscribe, q)
    return StreamingResponse(gen(), media_type=spec["type"],
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no",
                 "X-Party-Guest": guest["id"],
                 "X-Tonearm-Join-Pad-Ms": str(meta["pad_ms"]),
                 "X-Tonearm-Join-Playhead-Ms": "" if meta["playhead_ms"] is None
                    else str(int(meta["playhead_ms"])),
                 "X-Tonearm-Join-Stale": "1" if meta["stale"] else "0"})


@router.get("/{party_id}/events", include_in_schema=False)
async def party_events(party_id: str, request: Request):
    """SSE snapshot/update feed under a party-only credential."""
    from fastapi.responses import StreamingResponse
    import json
    import asyncio
    guest = _guest(request, party_id)

    async def events():
        while not await request.is_disconnected():
            guest_now = _guest(request, party_id)
            state = _state(party_id, guest_now["id"])
            yield "event: party\ndata: " + json.dumps(state) + "\n\n"
            await asyncio.sleep(5)
    return StreamingResponse(events(), media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


class PartySteerer(threading.Thread):
    """Add the vote leader to MusicKit's play-next slot near track end."""

    def __init__(self, cadence_s=5):
        super().__init__(daemon=True, name="tonearm-party-steerer")
        self.cadence_s = cadence_s
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def tick(self):
        party = store.party_active()
        if not party:
            return
        host_key_id = party.get("host_key_id")
        if host_key_id not in ("master", "anonymous"):
            host_key = next((k for k in keys_mod.registry.list()
                             if k["id"] == host_key_id and not k["revoked"]), None)
            if not host_key:
                return
        now = mk.now_playing()
        track_id = now.get("id")
        if track_id and party.get("current_track_id") != track_id:
            store.party_track_changed(party["id"], track_id)
        if not now.get("isPlaying"):
            return
        duration = now.get("durationMs") or now.get("duration_ms")
        elapsed = now.get("elapsedMs") or now.get("elapsed_ms")
        if duration is None or elapsed is None or duration - elapsed > 20_000:
            return
        queue = mk.queue()
        if any(item.get("id") and item.get("id") != track_id
               for item in queue or []):
            return  # don't jump ahead of tracks already in the host's queue
        scheduled = store.party_schedule_next(party["id"])
        if not scheduled:
            return
        try:
            mk.queue_add(scheduled["catalog_id"], play_next=True)
        except Exception:
            store.party_unschedule(scheduled["suggestion_id"])
            raise

    def run(self):
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception as e:
                # The party is optional; a transient renderer error must not
                # affect playback or take down the server.
                log.warning("party steering tick failed: %s", e)
            self._stop_event.wait(self.cadence_s)
