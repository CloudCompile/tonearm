"""Local persistence — the v0.3 "Memory" phase. sqlite3 stdlib, WAL.

Design constraints (the hard-won kind, encoded):

* OFF by default. Every store-backed surface answers 501 with an honest
  hint when TONEARM_STORE is unset — opt-in surfaces must never fake it.
* ONE flat lock around the connection (created with check_same_thread=
  False). No nested locking, no re-entrant cleverness — the same discipline
  that keeps the CDP layer alive.
* The history writer adds ZERO new eval kinds: it consumes the same
  stream-hook buffer the SSE listeners drain (window.__tonearmBuf), on a slow
  cadence, and skips ticks entirely while the watchdog reports a stall.
  Persistence must never compete with the renderer it is recording.
* Plays dedupe on (track_id, started_at) — a restart, an overlapping drain,
  or a replayed hook event must never double-write a play.
* Library rows are keyed by LIBRARY id (i.xxx): fresh rows often lack a
  catalog id, and removals are keyed in this space. `catalog_id` is kept
  when known but is never the identity.

Schema (versioned via _meta.schema_version; migrate chain below):
  _meta(key TEXT PRIMARY KEY, value TEXT)
  plays(id INTEGER PRIMARY KEY AUTOINCREMENT,
        track_id TEXT NOT NULL, title TEXT, artist TEXT, album TEXT,
        artwork TEXT, duration_ms INTEGER, started_at REAL NOT NULL,
        UNIQUE(track_id, started_at))
  library(library_id TEXT PRIMARY KEY, catalog_id TEXT, name TEXT,
          artist TEXT, album TEXT, duration_ms INTEGER, kind TEXT,
          attributes TEXT,               -- raw Apple attributes JSON
          first_seen REAL, last_seen REAL, last_changed REAL)
  cursors(name TEXT PRIMARY KEY, value TEXT, updated_at REAL)

The `library` table holds the CURRENT known state with watermarks
(first_seen / last_seen / last_changed), updated by the periodic snapshot
walk. Diffing "the two newest snapshots" (ROADMAP wording) is computed from
the watermarks: added = first_seen > since; updated = last_changed > since
and not added; removed = last_seen <= since. One table, no snapshot forks.
"""
import json
import logging
import os
import secrets
import sqlite3
import threading
import time

from .config import config
from .metrics import METRICS

log = logging.getLogger("tonearm.store")

SCHEMA_VERSION = 4

_MIGRATIONS = {
    1: """
    CREATE TABLE IF NOT EXISTS _meta (
        key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS plays (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        track_id TEXT NOT NULL,
        title TEXT, artist TEXT, album TEXT, artwork TEXT,
        duration_ms INTEGER,
        started_at REAL NOT NULL,
        UNIQUE(track_id, started_at));
    CREATE INDEX IF NOT EXISTS idx_plays_started ON plays(started_at);
    CREATE TABLE IF NOT EXISTS scrobbles (
        track_id TEXT NOT NULL,
        started_at REAL NOT NULL,
        scrobbled_at REAL NOT NULL,
        UNIQUE(track_id, started_at));
    CREATE TABLE IF NOT EXISTS library (
        library_id TEXT PRIMARY KEY,
        catalog_id TEXT, name TEXT, artist TEXT, album TEXT,
        duration_ms INTEGER, kind TEXT, attributes TEXT,
        first_seen REAL, last_seen REAL, last_changed REAL);
    CREATE INDEX IF NOT EXISTS idx_library_last_seen ON library(last_seen);
    CREATE TABLE IF NOT EXISTS cursors (
        name TEXT PRIMARY KEY, value TEXT, updated_at REAL);
    """,
    # v0.6 automation: the release watcher's per-artist state (the
    # r.xxx -> catalog mapping cache + last-seen release, #107) and the
    # smart-playlist definitions (#44). Both are durable server state on
    # the user's volume, so they live in the same db, same lock, same
    # migration chain — no second persistence layer.
    2: """
    CREATE TABLE IF NOT EXISTS watcher_artists (
        artist_id TEXT PRIMARY KEY,      -- library artist id (r.xxx)
        artist_name TEXT,
        catalog_artist_id TEXT,          -- NULL until the mapping resolves
        catalog_artist_name TEXT,
        last_album_id TEXT,              -- newest release already SEEN (#107)
        last_album_name TEXT,
        last_release_date TEXT,          -- ISO date string, Apple format
        checked_at REAL,
        updated_at REAL);
    CREATE TABLE IF NOT EXISTS smartlists (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT,
        rules TEXT,                      -- JSON: [{"field","op","value"}]
        sort TEXT,
        limit_n INTEGER,
        refresh_s INTEGER,
        playlist_id TEXT,                -- library playlist once materialized
        last_eval REAL, last_sync REAL, last_count INTEGER,
        created_at REAL, updated_at REAL);
    """,
    # v0.7 multi-user: the per-key audit trail — append-only, who-did-what
    # (method + path + status + key; request BODIES are never logged — a
    # playlist name is the user's business, not the audit log's).
    3: """
    CREATE TABLE IF NOT EXISTS audit (
        ts REAL NOT NULL,
        key_id TEXT NOT NULL,
        method TEXT NOT NULL,
        path TEXT NOT NULL,
        status INTEGER NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);
    """,
    # v1.0 party mode: isolated invite/guest credentials, suggestions and
    # votes. Only hashes of invite and guest secrets are persisted.
    4: """
    CREATE TABLE IF NOT EXISTS parties (
        id TEXT PRIMARY KEY,
        invite_hash TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'open',
        current_track_id TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        host_key_id TEXT NOT NULL DEFAULT 'anonymous');
    CREATE TABLE IF NOT EXISTS party_guests (
        id TEXT PRIMARY KEY,
        party_id TEXT NOT NULL REFERENCES parties(id) ON DELETE CASCADE,
        token_hash TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        joined_at REAL NOT NULL,
        last_action REAL NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS party_suggestions (
        id TEXT PRIMARY KEY,
        party_id TEXT NOT NULL REFERENCES parties(id) ON DELETE CASCADE,
        guest_id TEXT NOT NULL REFERENCES party_guests(id),
        catalog_id TEXT NOT NULL,
        title TEXT NOT NULL,
        artist TEXT,
        artwork TEXT,
        state TEXT NOT NULL DEFAULT 'queued',
        created_at REAL NOT NULL,
        played_at REAL);
    CREATE TABLE IF NOT EXISTS party_votes (
        suggestion_id TEXT NOT NULL REFERENCES party_suggestions(id) ON DELETE CASCADE,
        guest_id TEXT NOT NULL REFERENCES party_guests(id) ON DELETE CASCADE,
        created_at REAL NOT NULL,
        PRIMARY KEY(suggestion_id, guest_id));
    CREATE INDEX IF NOT EXISTS idx_party_suggestions ON party_suggestions(
        party_id, state, created_at);
    """,
}


class Store:
    """sqlite-backed local state. All methods no-op safely when disabled
    (and read methods raise StoreDisabled so routes can 501 honestly)."""

    def __init__(self, path=None, enabled=None):
        self.enabled = config.store_enabled if enabled is None else enabled
        self.path = path or config.store_db_path()
        self._conn = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------- lifecycle

    def start(self):
        """Open the db (creating it if needed) and arm the history writer."""
        if not self.enabled:
            return
        with self._lock:
            if self._conn is not None:
                return
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
                try:                       # the db dir holds private history
                    os.chmod(d, 0o700)
                except OSError:
                    pass
            self._conn = sqlite3.connect(self.path, check_same_thread=False,
                                         timeout=30)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=10000")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._migrate()
        log.info("store open at %s", self.path)

    def _migrate(self):
        has_meta = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='_meta'"
        ).fetchone() is not None
        current = 0
        if has_meta:
            cur = self._conn.execute(
                "SELECT value FROM _meta WHERE key='schema_version'").fetchone()
            current = int(cur[0]) if cur else 0
        for version in sorted(_MIGRATIONS):
            if version > current:
                self._conn.executescript(_MIGRATIONS[version])
                self._conn.execute(
                    "INSERT INTO _meta(key, value) VALUES('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(version),))
                self._conn.commit()
                log.info("store schema migrated to v%d", version)
        if current > 0:
            # self-heal: an OLD db stamped at a version whose script was
            # later edited (mid-release table additions) never re-runs it —
            # a real install was found missing the scrobbles table exactly
            # this way. CREATE IF NOT EXISTS is idempotent; drift is not.
            self._conn.executescript("".join(_MIGRATIONS[v] for v in
                                             sorted(_MIGRATIONS)))
            self._conn.commit()

    def close(self):
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.commit()
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    def _require_conn(self):
        if not self.enabled or self._conn is None:
            raise StoreDisabled(
                "the local store is disabled — set TONEARM_STORE=1 "
                "(data lives only on your volume either way)")
        return self._conn

    # ------------------------------------------------------------- plays

    def record_play(self, track_id, title, artist, album, artwork,
                    duration_ms, started_at):
        """One play. Idempotent on (track_id, started_at) — INSERT OR IGNORE."""
        conn = self._require_conn()
        with self._lock:
            conn.execute(
                "INSERT OR IGNORE INTO plays(track_id, title, artist, album, "
                "artwork, duration_ms, started_at) VALUES(?,?,?,?,?,?,?)",
                (track_id, title, artist, album, artwork, duration_ms,
                 started_at))
            conn.commit()
        METRICS.inc("tonearm_store_plays_written_total",
                    help_text="play rows persisted (dedupes included)")

    def record_scrobble(self, track_id, started_at):
        """Mark a play as scrobbled. Returns False if it was ALREADY
        recorded — the dedupe that makes restarts never double-scrobble
        (#98): the marker is UNIQUE(track_id, started_at), same key as the
        play itself."""
        conn = self._require_conn()
        with self._lock:
            cur = conn.execute(
                "INSERT OR IGNORE INTO scrobbles(track_id, started_at, "
                "scrobbled_at) VALUES(?,?,?)",
                (track_id, started_at, time.time()))
            conn.commit()
            return cur.rowcount > 0

    def recently_played(self, limit=50, offset=0):
        """The archive: newest first, past Apple's ~20-item cap."""
        conn = self._require_conn()
        with self._lock:
            rows = conn.execute(
                "SELECT track_id, title, artist, album, artwork, duration_ms, "
                "started_at FROM plays ORDER BY started_at DESC, id DESC "
                "LIMIT ? OFFSET ?", (min(limit, 500), max(offset, 0))).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0]
        items = [{"id": r[0], "title": r[1], "artist": r[2], "album": r[3],
                  "artwork": r[4], "duration_ms": r[5],
                  "played_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                             time.gmtime(r[6]))}
                 for r in rows]
        nxt = offset + len(rows)
        return {"total": total, "count": len(items),
                "items": items,
                "next_offset": nxt if nxt < total and items else None}

    # ------------------------------------------------------------- library

    def upsert_library_rows(self, rows):
        """Upsert a snapshot walk batch. `rows` are normalized item dicts
        (mk.item_row shape). Identity = library_id. first_seen set once;
        last_seen bumped every walk; last_changed only on real change."""
        conn = self._require_conn()
        now = time.time()
        with self._lock:
            for r in rows:
                lid = r.get("library_id") or r.get("id")
                if not lid:
                    continue
                attrs = json.dumps(r.get("attributes") or {}, separators=(",", ":"))
                cur = conn.execute(
                    "SELECT name, artist, album, catalog_id, duration_ms, "
                    "attributes, first_seen FROM library WHERE library_id=?",
                    (lid,)).fetchone()
                if cur is None:
                    conn.execute(
                        "INSERT INTO library(library_id, catalog_id, name, "
                        "artist, album, duration_ms, kind, attributes, "
                        "first_seen, last_seen, last_changed) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (lid, r.get("catalog_id"), r.get("name"), r.get("artist"),
                         r.get("album"), r.get("duration_ms"),
                         r.get("type") or r.get("kind"), attrs, now, now, now))
                    continue
                (old_name, old_artist, old_album, old_cat, old_dur,
                 old_attrs, first_seen) = cur
                changed = (old_name != r.get("name") or
                           old_artist != r.get("artist") or
                           old_album != r.get("album") or
                           old_attrs != attrs or
                           (old_cat or None) != (r.get("catalog_id") or None) or
                           old_dur != r.get("duration_ms"))
                conn.execute(
                    "UPDATE library SET catalog_id=?, name=?, artist=?, "
                    "album=?, duration_ms=?, kind=?, attributes=?, "
                    "last_seen=?, last_changed=? WHERE library_id=?",
                    (r.get("catalog_id"), r.get("name"), r.get("artist"),
                     r.get("album"), r.get("duration_ms"),
                     r.get("type") or r.get("kind"), attrs,
                     now, now if changed else first_seen, lid))
            conn.commit()

    def library_rows(self, limit=100000, offset=0):
        conn = self._require_conn()
        with self._lock:
            rows = conn.execute(
                "SELECT library_id, catalog_id, name, artist, album, "
                "duration_ms, kind, attributes, first_seen, last_seen, "
                "last_changed FROM library ORDER BY name LIMIT ? OFFSET ?",
                (limit, offset)).fetchall()
        out = []
        for r in rows:
            out.append({
                "library_id": r[0], "catalog_id": r[1], "name": r[2],
                "artist": r[3], "album": r[4], "duration_ms": r[5],
                "kind": r[6],
                "attributes": json.loads(r[7]) if r[7] else {},
                "first_seen": r[8], "last_seen": r[9], "last_changed": r[10],
            })
        return out

    def library_count(self):
        conn = self._require_conn()
        with self._lock:
            return conn.execute("SELECT COUNT(*) FROM library").fetchone()[0]

    # ---- diff queries (watermark-driven; see module docstring) ----

    def _rows_where(self, where, args, limit, offset, order_by=None):
        conn = self._require_conn()
        with self._lock:
            rows = conn.execute(
                f"SELECT library_id, catalog_id, name, artist, album, "
                f"duration_ms, kind, first_seen, last_seen, last_changed "
                f"FROM library WHERE {where} ORDER BY {order_by or 'last_changed DESC'}, "
                f"library_id LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
            total = conn.execute(
                f"SELECT COUNT(*) FROM library WHERE {where}", args).fetchone()[0]
        out = [{"library_id": r[0], "catalog_id": r[1], "name": r[2],
                "artist": r[3], "album": r[4], "duration_ms": r[5],
                "kind": r[6], "first_seen": r[7], "last_seen": r[8],
                "last_changed": r[9]} for r in rows]
        nxt = offset + len(out)
        return {"total": total, "count": len(out), "items": out,
                "next_offset": nxt if nxt < total and out else None}

    def library_added_since(self, since, limit=100, offset=0):
        """Rows first seen after `since`. since=0 -> the whole library.
        Ordered by first_seen — this is the ADDED order (last_changed would
        float old rows that merely changed; found via the PWA's
        recently-added sort)."""
        return self._rows_where("first_seen > ?", (since,), limit, offset,
                                order_by="first_seen DESC")

    def library_updated_since(self, since, limit=100, offset=0):
        """Rows that CHANGED after `since` (not counting brand-new ones)."""
        return self._rows_where("last_changed > ? AND first_seen <= ?",
                                (since, since), limit, offset)

    def library_removed_since(self, since, limit=100, offset=0):
        """Rows whose most recent walk predates `since` — gone from the
        library as of the last completed snapshot walk."""
        return self._rows_where("last_seen <= ?", (since,), limit, offset)

    # ------------------------------------------------------------- stats
    # Pure local computation — no Apple calls. Aggregation in SQL, never
    # Python loops over the whole table. Plays dedupe on (track_id,
    # started_at) is the writer's guarantee; stats just read.

    def stats(self, since=0.0, limit=25):
        conn = self._require_conn()
        with self._lock:
            tracks = conn.execute(
                "SELECT track_id, title, artist, COUNT(*) AS plays "
                "FROM plays WHERE started_at >= ? GROUP BY track_id "
                "ORDER BY plays DESC, MAX(started_at) DESC LIMIT ?",
                (since, limit)).fetchall()
            artists = conn.execute(
                "SELECT artist, COUNT(*) AS plays FROM plays "
                "WHERE started_at >= ? AND artist IS NOT NULL "
                "GROUP BY artist ORDER BY plays DESC LIMIT ?",
                (since, limit)).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) FROM plays WHERE started_at >= ?",
                (since,)).fetchone()[0]
        return {"window_since": since,
                "total_plays": total,
                "top_tracks": [{"id": r[0], "title": r[1], "artist": r[2],
                                "plays": r[3]} for r in tracks],
                "top_artists": [{"artist": r[0], "plays": r[1]}
                                for r in artists]}

    def plays_series(self, since=0.0):
        """Plays per UTC day — bucketed in SQL for charts."""
        conn = self._require_conn()
        with self._lock:
            rows = conn.execute(
                "SELECT date(started_at, 'unixepoch') AS day, COUNT(*) "
                "FROM plays WHERE started_at >= ? GROUP BY day ORDER BY day",
                (since,)).fetchall()
        return [{"day": r[0], "plays": r[1]} for r in rows]

    # ------------------------------------------------------- watcher state
    # Per-artist watcher state (v0.6 #107): the cached library->catalog
    # mapping and the newest release already seen. The first record for an
    # artist is a silent BASELINE — the watcher decides when to emit.

    def watcher_upsert_artist(self, artist_id, artist_name,
                              catalog_artist_id=None, catalog_artist_name=None,
                              last_album_id=None, last_album_name=None,
                              last_release_date=None):
        conn = self._require_conn()
        now = time.time()
        with self._lock:
            conn.execute(
                "INSERT INTO watcher_artists(artist_id, artist_name, "
                "catalog_artist_id, catalog_artist_name, last_album_id, "
                "last_album_name, last_release_date, checked_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(artist_id) DO UPDATE "
                "SET artist_name=excluded.artist_name, "
                "catalog_artist_id=COALESCE(excluded.catalog_artist_id, "
                "catalog_artist_id), catalog_artist_name=COALESCE("
                "excluded.catalog_artist_name, catalog_artist_name), "
                "last_album_id=COALESCE(excluded.last_album_id, last_album_id), "
                "last_album_name=COALESCE(excluded.last_album_name, "
                "last_album_name), last_release_date=COALESCE("
                "excluded.last_release_date, last_release_date), "
                "checked_at=excluded.checked_at, updated_at=excluded.updated_at",
                (artist_id, artist_name, catalog_artist_id,
                 catalog_artist_name, last_album_id, last_album_name,
                 last_release_date, now, now))
            conn.commit()

    def watcher_artist(self, artist_id):
        conn = self._require_conn()
        with self._lock:
            row = conn.execute(
                "SELECT artist_id, artist_name, catalog_artist_id, "
                "catalog_artist_name, last_album_id, last_album_name, "
                "last_release_date, checked_at FROM watcher_artists "
                "WHERE artist_id=?", (artist_id,)).fetchone()
        if row is None:
            return None
        return {"artist_id": row[0], "artist_name": row[1],
                "catalog_artist_id": row[2], "catalog_artist_name": row[3],
                "last_album_id": row[4], "last_album_name": row[5],
                "last_release_date": row[6], "checked_at": row[7]}

    def watcher_artists(self):
        conn = self._require_conn()
        with self._lock:
            rows = conn.execute(
                "SELECT artist_id, artist_name, catalog_artist_id, "
                "catalog_artist_name, last_album_id, last_album_name, "
                "last_release_date, checked_at FROM watcher_artists "
                "ORDER BY artist_name").fetchall()
        return [{"artist_id": r[0], "artist_name": r[1],
                 "catalog_artist_id": r[2], "catalog_artist_name": r[3],
                 "last_album_id": r[4], "last_album_name": r[5],
                 "last_release_date": r[6], "checked_at": r[7]}
                for r in rows]

    # ---------------------------------------------------------- smartlists

    def smartlist_add(self, list_id, name, description, rules, sort,
                      limit_n, refresh_s):
        conn = self._require_conn()
        now = time.time()
        with self._lock:
            conn.execute(
                "INSERT INTO smartlists(id, name, description, rules, sort, "
                "limit_n, refresh_s, playlist_id, last_eval, last_sync, "
                "last_count, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,?,?)",
                (list_id, name, description, json.dumps(rules),
                 sort, limit_n, refresh_s, now, now))
            conn.commit()

    def smartlist_update(self, list_id, **fields):
        """Partial update; `rules`/dicts are JSON-encoded. Returns the fresh
        row (via smartlist_get semantics) or None if the id is unknown."""
        conn = self._require_conn()
        cols = {"name", "description", "rules", "sort", "limit_n",
                "refresh_s", "playlist_id", "last_eval", "last_sync",
                "last_count"}
        unknown = set(fields) - cols
        if unknown:
            raise ValueError(f"unknown smartlist fields: {sorted(unknown)}")
        with self._lock:
            sets, args = [], []
            for k, v in fields.items():
                sets.append(f"{k}=?")
                if k == "rules" and not isinstance(v, str):
                    v = json.dumps(v)
                args.append(v)
            sets.append("updated_at=?")
            args.append(time.time())
            args.append(list_id)
            conn.execute(f"UPDATE smartlists SET {', '.join(sets)} "
                         f"WHERE id=?", args)
            conn.commit()

    def _smartlist_row(self, row):
        return {"id": row[0], "name": row[1], "description": row[2],
                "rules": json.loads(row[3]) if row[3] else [],
                "sort": row[4], "limit": row[5] or 0,
                "refresh_s": row[6] or 0, "playlist_id": row[7],
                "last_eval": row[8], "last_sync": row[9],
                "last_count": row[10], "created_at": row[11],
                "updated_at": row[12]}

    _SMARTLIST_COLS = ("id, name, description, rules, sort, limit_n, "
                       "refresh_s, playlist_id, last_eval, last_sync, "
                       "last_count, created_at, updated_at")

    def smartlist_get(self, list_id):
        conn = self._require_conn()
        with self._lock:
            row = conn.execute(
                f"SELECT {self._SMARTLIST_COLS} FROM smartlists "
                f"WHERE id=?", (list_id,)).fetchone()
        return self._smartlist_row(row) if row else None

    def smartlist_rows(self):
        conn = self._require_conn()
        with self._lock:
            rows = conn.execute(
                f"SELECT {self._SMARTLIST_COLS} FROM smartlists "
                f"ORDER BY created_at").fetchall()
        return [self._smartlist_row(r) for r in rows]

    def smartlist_delete(self, list_id):
        conn = self._require_conn()
        with self._lock:
            cur = conn.execute("DELETE FROM smartlists WHERE id=?", (list_id,))
            conn.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------- audit

    def audit_append(self, rows):
        """Batch-append audit rows (ts, key_id, method, path, status).
        Append-only: no update, no delete API — the audit trail is the
        point."""
        conn = self._require_conn()
        with self._lock:
            conn.executemany(
                "INSERT INTO audit(ts, key_id, method, path, status) "
                "VALUES(?,?,?,?,?)", rows)
            conn.commit()

    def audit_since(self, since=0.0, limit=200, offset=0):
        conn = self._require_conn()
        with self._lock:
            rows = conn.execute(
                "SELECT ts, key_id, method, path, status FROM audit "
                "WHERE ts > ? ORDER BY ts DESC LIMIT ? OFFSET ?",
                (since, min(limit, 1000), max(offset, 0))).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) FROM audit WHERE ts > ?", (since,)).fetchone()[0]
        out = [{"ts": r[0], "key_id": r[1], "method": r[2], "path": r[3],
                "status": r[4]} for r in rows]
        nxt = offset + len(out)
        return {"total": total, "count": len(out), "items": out,
                "next_offset": nxt if nxt < total and out else None}

    # ------------------------------------------------------------- cursors

    def set_cursor(self, name, value):
        conn = self._require_conn()
        with self._lock:
            conn.execute(
                "INSERT INTO cursors(name, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (name, json.dumps(value), time.time()))
            conn.commit()

    def get_cursor(self, name, default=None):
        conn = self._require_conn()
        with self._lock:
            row = conn.execute(
                "SELECT value FROM cursors WHERE name=?", (name,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return default

    # --------------------------------------------------------------- parties

    @staticmethod
    def _party_row(row):
        if not row:
            return None
        return {"id": row[0], "name": row[1], "state": row[2],
                "current_track_id": row[3], "created_at": row[4],
                "updated_at": row[5], "host_key_id": row[6]}

    def party_create(self, party_id, invite_hash, name, host_key_id="anonymous"):
        conn = self._require_conn()
        now = time.time()
        with self._lock:
            if conn.execute("SELECT 1 FROM parties WHERE state='open' LIMIT 1").fetchone():
                raise ValueError("a party is already open")
            conn.execute("INSERT INTO parties(id, invite_hash, name, created_at, "
                         "updated_at,host_key_id) VALUES(?,?,?,?,?,?)",
                         (party_id, invite_hash, name, now, now, host_key_id))
            conn.commit()
        return self.party_get(party_id)

    def party_get(self, party_id):
        conn = self._require_conn()
        with self._lock:
            row = conn.execute("SELECT id,name,state,current_track_id,created_at,"
                               "updated_at,host_key_id FROM parties WHERE id=?",
                               (party_id,)).fetchone()
        return self._party_row(row)

    def party_by_invite(self, invite_hash):
        conn = self._require_conn()
        with self._lock:
            row = conn.execute("SELECT id FROM parties WHERE invite_hash=? "
                               "AND state='open'", (invite_hash,)).fetchone()
        return self.party_get(row[0]) if row else None

    def party_rotate_invite(self, party_id, invite_hash):
        conn = self._require_conn()
        with self._lock:
            cur = conn.execute("UPDATE parties SET invite_hash=?,updated_at=? "
                               "WHERE id=? AND state='open'",
                               (invite_hash, time.time(), party_id))
            conn.commit()
            return cur.rowcount > 0

    def party_rotate_guest_keys(self, party_id):
        """Invite rotation invalidates every previously issued guest key."""
        conn = self._require_conn()
        with self._lock:
            conn.execute("UPDATE party_guests SET active=0 WHERE party_id=? "
                         "AND name!='Host'", (party_id,))
            conn.execute("DELETE FROM party_votes WHERE suggestion_id IN "
                         "(SELECT id FROM party_suggestions WHERE party_id=? "
                         "AND state='queued')", (party_id,))
            conn.execute("UPDATE party_suggestions SET state='removed' WHERE "
                         "party_id=? AND state='queued'", (party_id,))
            conn.commit()

    def party_host_guest_id(self, party_id):
        conn = self._require_conn()
        with self._lock:
            row = conn.execute("SELECT id FROM party_guests WHERE party_id=? "
                "AND name='Host' AND active=1 ORDER BY joined_at LIMIT 1",
                (party_id,)).fetchone()
        return row[0] if row else None

    def party_join(self, party_id, guest_id, token_hash, name):
        conn = self._require_conn()
        now = time.time()
        with self._lock:
            party = conn.execute("SELECT 1 FROM parties WHERE id=? AND state='open'",
                                 (party_id,)).fetchone()
            if not party:
                return False
            count = conn.execute("SELECT COUNT(*) FROM party_guests WHERE "
                                 "party_id=? AND active=1", (party_id,)).fetchone()[0]
            if count >= 21:  # one host plus at most twenty guests
                return False
            if name == "Host" and count >= 1:
                return False
            conn.execute("INSERT INTO party_guests(id,party_id,token_hash,name,"
                         "joined_at) VALUES(?,?,?,?,?)",
                         (guest_id, party_id, token_hash, name, now))
            conn.commit()
        return True

    def party_guest_by_token(self, token_hash):
        conn = self._require_conn()
        with self._lock:
            row = conn.execute("SELECT g.id,g.name,g.party_id,p.name "
                "FROM party_guests g JOIN parties p ON p.id=g.party_id "
                "WHERE g.token_hash=? AND g.active=1 AND p.state='open'",
                (token_hash,)).fetchone()
        return ({"id": row[0], "name": row[1], "party_id": row[2],
                 "party_name": row[3]} if row else None)

    def party_guest_for_party(self, party_id, token_hash):
        guest = self.party_guest_by_token(token_hash)
        return guest if guest and guest["party_id"] == party_id else None

    def party_guest_action(self, guest_id, cooldown_s=2):
        """Atomic per-guest cooldown for votes/suggestions; False means 429."""
        conn = self._require_conn()
        now = time.time()
        with self._lock:
            row = conn.execute("SELECT last_action FROM party_guests WHERE id=? "
                               "AND active=1", (guest_id,)).fetchone()
            if not row or now - row[0] < cooldown_s:
                return False
            conn.execute("UPDATE party_guests SET last_action=? WHERE id=?",
                         (now, guest_id))
            conn.commit()
            return True

    def party_guest_revoke(self, party_id, guest_id):
        conn = self._require_conn()
        with self._lock:
            cur = conn.execute("UPDATE party_guests SET active=0 WHERE id=? "
                               "AND party_id=? AND active=1",
                               (guest_id, party_id))
            if cur.rowcount:
                conn.execute("DELETE FROM party_votes WHERE guest_id=?", (guest_id,))
            conn.commit()
            return cur.rowcount > 0

    def party_suggest(self, party_id, guest_id, catalog_id, title, artist, artwork):
        conn = self._require_conn()
        sid = "ps_" + secrets.token_urlsafe(9)
        now = time.time()
        with self._lock:
            party = conn.execute("SELECT state FROM parties WHERE id=?",
                                 (party_id,)).fetchone()
            if not party or party[0] != "open":
                return None
            guest = conn.execute("SELECT active FROM party_guests WHERE id=? "
                                 "AND party_id=?", (guest_id, party_id)).fetchone()
            if not guest or not guest[0]:
                return None
            existing = conn.execute("SELECT id FROM party_suggestions WHERE "
                "party_id=? AND catalog_id=? AND state='queued'",
                (party_id, catalog_id)).fetchone()
            if existing:
                return existing[0], False
            active = conn.execute("SELECT COUNT(*) FROM party_suggestions WHERE "
                                  "party_id=? AND state IN ('queued','scheduled')",
                                  (party_id,)).fetchone()[0]
            if active >= 100:
                raise ValueError("party queue is full (100 suggestions)")
            conn.execute("INSERT INTO party_suggestions(id,party_id,guest_id,"
                         "catalog_id,title,artist,artwork,created_at) "
                         "VALUES(?,?,?,?,?,?,?,?)",
                         (sid, party_id, guest_id, catalog_id, title, artist,
                          artwork, now))
            conn.execute("INSERT INTO party_votes VALUES(?,?,?)",
                         (sid, guest_id, now))
            conn.commit()
        return sid, True

    def party_guest_vote_count(self, party_id, guest_id):
        conn = self._require_conn()
        with self._lock:
            row = conn.execute("SELECT COUNT(*) FROM party_votes v JOIN "
                "party_suggestions s ON s.id=v.suggestion_id WHERE "
                "s.party_id=? AND s.state='queued' AND v.guest_id=?",
                (party_id, guest_id)).fetchone()
        return row[0]

    def party_vote(self, party_id, suggestion_id, guest_id):
        conn = self._require_conn()
        with self._lock:
            row = conn.execute("SELECT state FROM party_suggestions WHERE id=? "
                               "AND party_id=?", (suggestion_id, party_id)).fetchone()
            if not row or row[0] != "queued":
                return None
            active = conn.execute("SELECT active FROM party_guests WHERE id=? "
                                  "AND party_id=?", (guest_id, party_id)).fetchone()
            if not active or not active[0]:
                return None
            cur = conn.execute("INSERT OR IGNORE INTO party_votes VALUES(?,?,?)",
                               (suggestion_id, guest_id, time.time()))
            conn.commit()
            return cur.rowcount > 0

    def party_unvote(self, party_id, suggestion_id, guest_id):
        conn = self._require_conn()
        with self._lock:
            cur = conn.execute("DELETE FROM party_votes WHERE suggestion_id=? "
                               "AND guest_id=? AND suggestion_id IN "
                               "(SELECT id FROM party_suggestions WHERE "
                               "party_id=? AND state='queued')",
                               (suggestion_id, guest_id, party_id))
            conn.commit()
            return cur.rowcount > 0

    def party_public_state(self, party_id, guest_id):
        conn = self._require_conn()
        with self._lock:
            rows = conn.execute("SELECT s.id,s.catalog_id,s.title,s.artist,"
                "s.artwork,s.state,s.created_at,g.name,COUNT(v.guest_id),"
                "MAX(CASE WHEN v.guest_id=? THEN 1 ELSE 0 END) "
                "FROM party_suggestions s JOIN party_guests g ON g.id=s.guest_id "
                "LEFT JOIN party_votes v ON v.suggestion_id=s.id "
                "WHERE s.party_id=? GROUP BY s.id ORDER BY "
                "CASE s.state WHEN 'queued' THEN 0 ELSE 1 END, COUNT(v.guest_id) "
                "DESC,s.created_at", (guest_id, party_id)).fetchall()
            party = conn.execute("SELECT id,name,state,current_track_id FROM parties "
                                 "WHERE id=?", (party_id,)).fetchone()
            guests = conn.execute("SELECT id,name FROM party_guests WHERE "
                                  "party_id=? AND active=1 ORDER BY joined_at",
                                  (party_id,)).fetchall()
        if not party:
            return None
        return {"id": party[0], "name": party[1], "state": party[2],
                "current_track_id": party[3],
                "suggestions": [{"id": r[0], "catalog_id": r[1],
                    "title": r[2], "artist": r[3], "artwork": r[4],
                    "state": r[5], "created_at": r[6], "suggested_by": r[7],
                    "votes": r[8], "voted": bool(r[9])} for r in rows],
                "guests": [{"id": r[0], "name": r[1]} for r in guests]}

    def party_active(self):
        conn = self._require_conn()
        with self._lock:
            row = conn.execute("SELECT id FROM parties WHERE state='open' "
                               "ORDER BY created_at DESC LIMIT 1").fetchone()
        return self.party_get(row[0]) if row else None

    def party_schedule_next(self, party_id):
        conn = self._require_conn()
        with self._lock:
            scheduled = conn.execute("SELECT catalog_id FROM party_suggestions "
                "WHERE party_id=? AND state='scheduled' LIMIT 1",
                (party_id,)).fetchone()
            if scheduled:
                return None
            winner = conn.execute("SELECT s.id,s.catalog_id FROM "
                "party_suggestions s LEFT JOIN party_votes v "
                "ON v.suggestion_id=s.id WHERE s.party_id=? AND s.state='queued' "
                "GROUP BY s.id ORDER BY COUNT(v.guest_id) DESC,s.created_at "
                "ASC LIMIT 1", (party_id,)).fetchone()
            if not winner:
                return None
            conn.execute("UPDATE party_suggestions SET state='scheduled' "
                         "WHERE id=?", (winner[0],))
            conn.commit()
            return {"suggestion_id": winner[0], "catalog_id": winner[1]}

    def party_unschedule(self, suggestion_id):
        conn = self._require_conn()
        with self._lock:
            conn.execute("UPDATE party_suggestions SET state='queued' "
                         "WHERE id=? AND state='scheduled'", (suggestion_id,))
            conn.commit()

    def party_track_changed(self, party_id, track_id):
        conn = self._require_conn()
        with self._lock:
            scheduled = conn.execute("SELECT id,catalog_id FROM party_suggestions "
                "WHERE party_id=? AND state='scheduled' LIMIT 1",
                (party_id,)).fetchone()
            if scheduled and scheduled[1] == track_id:
                conn.execute("UPDATE party_suggestions SET state='played',played_at=? "
                             "WHERE id=?", (time.time(), scheduled[0]))
            elif scheduled:
                # Host changed tracks before the party's scheduled item began.
                conn.execute("UPDATE party_suggestions SET state='queued' "
                             "WHERE id=?", (scheduled[0],))
            conn.execute("UPDATE parties SET current_track_id=?,updated_at=? "
                         "WHERE id=?", (track_id, time.time(), party_id))
            conn.commit()

    def party_remove_suggestion(self, party_id, suggestion_id):
        conn = self._require_conn()
        with self._lock:
            cur = conn.execute("UPDATE party_suggestions SET state='removed' "
                "WHERE id=? AND party_id=? AND state='queued'",
                (suggestion_id, party_id))
            conn.commit()
            return cur.rowcount > 0

    def party_close(self, party_id):
        conn = self._require_conn()
        with self._lock:
            cur = conn.execute("UPDATE parties SET state='closed',updated_at=? "
                               "WHERE id=? AND state='open'",
                               (time.time(), party_id))
            conn.commit()
            return cur.rowcount > 0

class StoreDisabled(RuntimeError):
    """Raised by store read methods when the store is off; routes map it to
    an honest 501."""


# --------------------------------------------------------------- diff (pure)

def diff_rows(rows_before, rows_after, before_ts, after_ts):
    """Pure diff between two row lists (mk.item_row-normalized). Identity is
    library_id; rows without one match by (name, artist, album) as a display
    hint only. Returns {'added': [...], 'updated': [...], 'removed': [...]}.
    Timestamps come from the store watermarks, not wall clocks, so a diff
    never lies about when a change landed."""
    def key(r):
        return r.get("library_id") or r.get("id")

    def name_key(r):
        return ((r.get("name") or "").lower(),
                (r.get("artist") or "").lower(),
                (r.get("album") or "").lower())

    before = {key(r): r for r in rows_before if key(r)}
    after = {key(r): r for r in rows_after if key(r)}

    added = [after[k] for k in after
             if k not in before and
             (after[k].get("first_seen") or 0) > before_ts]
    updated = [after[k] for k in after if k in before and
               (after[k].get("last_changed") or 0) > before_ts and
               after[k] not in added]
    removed = [before[k] for k in before if k not in after and
               (before[k].get("last_seen") or 0) <= after_ts]
    # sort each by watermark, newest first, for stable, readable output
    for lst, field in ((added, "first_seen"), (updated, "last_changed"),
                       (removed, "last_seen")):
        lst.sort(key=lambda r: r.get(field) or 0, reverse=True)
    return {"added": added, "updated": updated, "removed": removed,
            "since": before_ts, "until": after_ts}


def _renderer_available(browser_instance):
    """False only on an ACTIVE stall (failed pings / failing renderer).
    A COLD renderer (never connected — last_ping_ts is None) counts as
    available: background work must be able to trigger the first connect,
    or every thread would politely defer to the watchdog while the watchdog
    waits for a first connect — a deadlock of politeness where the archive
    never starts."""
    try:
        wd = browser_instance.health()
    except Exception:
        return False
    if wd.get("last_ok") is False or (wd.get("consecutive_failures") or 0) > 0:
        return False
    return True


# ---------------------------------------------------------------- history
# the history writer: ONE flat daemon thread, drains the stream-hook buffer
# the SSE listeners already use, persists track starts. Skips ticks when the
# watchdog reports a stall (persistence must never kick a limping renderer).

class HistoryWriter(threading.Thread):
    CADENCE_S = 5.0

    def __init__(self, store, browser_instance, mk_mod, cadence_s=None):
        super().__init__(daemon=True, name="tonearm-store-history")
        self.store = store
        self._browser = browser_instance
        self._mk = mk_mod
        self.cadence = cadence_s or self.CADENCE_S
        self._seq = 0
        self._current = None          # last seen track key
        self._playing = None          # last seen is-playing state
        self._pending = None          # (key, started_at) staged at elapsed 0
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def _stalled(self):
        return not _renderer_available(self._browser)

    def process_events(self, entries):
        """Pure event -> play rows. Split from the loop so tests can feed it
        synthetic buffers without any renderer.
        A 'play' = the track key changes while playing (progress ticks and
        same-track state changes are not plays), or playback resumes from a
        pause — BUT only once playback is PROVEN (#178, found live): a
        queue-load fires a transient tick with elapsedMs == 0 even when
        autoplay never delivers sound, and the old code recorded it as a
        play (a Ledger full of plays that made no noise). A staged start
        records only when a later tick shows real progress on the same
        track — with the ORIGINAL start time, not the confirming tick's."""
        recorded = []
        for e in entries:
            self._seq = max(self._seq, e.get("seq", 0))
            key = e.get("id") or e.get("title")
            if not key:
                continue
            playing = e.get("isPlaying") is not False
            el = e.get("elapsedMs") or 0
            started_at = (e.get("ts") or time.time() * 1000) / 1000.0
            was_playing = self._playing
            self._playing = playing
            if not playing:
                self._pending = None      # a paused staged start never happened
                continue
            if key != self._current or was_playing is False:
                self._current = key
                if el > 0:
                    self._pending = None
                    self.store.record_play(
                        track_id=key, title=e.get("title"),
                        artist=e.get("artist"), album=e.get("album"),
                        artwork=e.get("artworkUrl"),
                        duration_ms=e.get("durationMs"),
                        started_at=started_at)
                    recorded.append(key)
                else:
                    self._pending = (key, started_at)
            elif self._pending and self._pending[0] == key and el > 0:
                started_at = self._pending[1]
                self._pending = None
                self.store.record_play(
                    track_id=key, title=e.get("title"),
                    artist=e.get("artist"), album=e.get("album"),
                    artwork=e.get("artworkUrl"),
                    duration_ms=e.get("durationMs"), started_at=started_at)
                recorded.append(key)
        return recorded

    def run(self):
        log.info("history writer armed (cadence %.0fs) — rides the stream "
                 "hook buffer, adds zero eval kinds", self.cadence)
        hooked = False
        while not self._stop.wait(self.cadence):
            if not self.store.enabled:
                continue
            if self._stalled():
                continue
            if not hooked:
                # one-shot: the hook exists only once an SSE client connected
                # (or we install it now). Idempotent JS — checks __tonearmHooked.
                try:
                    self._mk.install_stream_hook()
                    hooked = True
                except Exception as e:
                    log.debug("hook install skipped: %s", e)
                    continue
            try:
                entries = self._mk.drain_stream(self._seq)
            except Exception as e:
                log.debug("history drain skipped: %s", e)
                continue
            if not entries:
                continue
            try:
                got = self.process_events(entries)
                if got:
                    log.debug("history: %d play(s) persisted", len(got))
            except Exception as e:
                log.warning("history write failed (kept alive): %s", e)


# ------------------------------------------------------------- snapshots
# the periodic library walk: ONE bounded thread, single-flight (never overlaps
# itself), stall-gated (a full walk is ~1 eval per 100 songs — it must never
# fight a limping renderer for the eval lock). Records its runs in cursors.

class SnapshotScheduler(threading.Thread):
    """Periodically walks /v1/me/library/songs and upserts the store.
    `walk_fn` is injected so tests can drive it without any renderer."""

    def __init__(self, store, browser_instance, mk_mod, interval=None,
                 walk_fn=None):
        super().__init__(daemon=True, name="tonearm-store-snapshot")
        self.store = store
        self._browser = browser_instance
        self._mk = mk_mod
        self.interval = interval or config.store_sync_interval
        self._walk_fn = walk_fn or self._walk_library
        self._busy = threading.Lock()      # single-flight: walk never overlaps
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def _stalled(self):
        return not _renderer_available(self._browser)

    def _walk_library(self):
        """The real walk: every library song, normalized rows, in batches.
        ~1 eval per 100 songs (Apple's page cap)."""
        def extract(data):
            return [self._mk.item_row(t) for t in data.get("data", [])]
        return self._mk.paginated("/v1/me/library/songs?limit=100", extract,
                                  fresh=True)

    def run_walk(self):
        """One complete walk. Returns a summary dict; safe to call directly
        (the thread calls this too)."""
        if not self._busy.acquire(timeout=0.1):
            return {"ran": False, "reason": "previous walk still running"}
        try:
            t0 = time.time()
            previous = self.store.get_cursor("library.walk") or {}
            rows = self._walk_fn()
            # upsert in batches so one giant commit never pins the lock
            BATCH = 500
            for i in range(0, len(rows), BATCH):
                self.store.upsert_library_rows(rows[i:i + BATCH])
            summary = {"ran": True,
                       "rows": len(rows),
                       "seconds": round(time.time() - t0, 1)}
            # automation: what changed since the PREVIOUS completed walk.
            # Skipped on the first walk (everything would look "added") and
            # capped samples keep the event small on a 9k-song library.
            since = previous.get("completed_at")
            if since:
                added = self.store.library_added_since(since, limit=50)
                updated = self.store.library_updated_since(since, limit=50)
                removed = self.store.library_removed_since(since, limit=50)
                counts = {"added": added["total"],
                          "updated": updated["total"],
                          "removed": removed["total"]}
                if any(counts.values()):
                    try:
                        from .webhooks import emit
                        emit("library.changed",
                             {"since": since, "counts": counts,
                              "added": added["items"],
                              "updated": updated["items"],
                              "removed": removed["items"]})
                    except Exception as e:
                        log.debug("library.changed emit skipped: %s", e)
            summary["completed_at"] = time.time()
            self.store.set_cursor("library.walk", summary)
            METRICS.inc("tonearm_store_library_walks_total",
                        help_text="completed library snapshot walks")
            METRICS.gauge("tonearm_store_library_rows", len(rows),
                          help_text="library rows in the last completed walk")
            return summary
        finally:
            self._busy.release()

    def run(self):
        log.info("snapshot scheduler armed: full library walk every %ds "
                 "(stall-gated, single-flight)", self.interval)
        # first run shortly after boot (renderer still fresh), then on cadence
        waited = 0.0
        while waited < 90 and not self._stop.wait(5):
            waited += 5
        while not self._stop.is_set():
            if self.store.enabled and not self._stalled():
                try:
                    summary = self.run_walk()
                    if summary.get("ran"):
                        log.info("library walk: %d rows in %.1fs",
                                 summary["rows"], summary["seconds"])
                except Exception as e:
                    log.warning("library walk failed (kept alive): %s", e)
            # park in 5s slices so stop() stays responsive
            slept = 0.0
            while slept < self.interval and not self._stop.is_set():
                self._stop.wait(min(5, self.interval - slept))
                slept += 5


# module-level singleton (routes import this; main.py owns its lifecycle)
store = Store()
history_writer: HistoryWriter | None = None   # set by main.py lifespan
snapshot_scheduler: SnapshotScheduler | None = None
export_scheduler = None   # tonearm/exporter.ExportScheduler (set in main.py)
