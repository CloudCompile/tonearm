import { get, post, qs, guard, toast, esc, settings } from "./common.js";

const $ = (id) => document.getElementById(id);
const enc = encodeURIComponent;

const FAVORITES_PLAYLIST = "p.0YU08k07L9";
const HEAVY_ROTATION_PLAYLIST = "p.KoKOBXDS3Wm3o9P";

let searchTimer = null;
let volTimer = null;
let lastVolume = 0.5;
let latestNowPlaying = null;

function fmtTime(ms) {
  if (!ms || ms < 0 || !Number.isFinite(ms)) return "0:00";
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${m}:${String(r).padStart(2, "0")}`;
}

function fmtAge(seconds) {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s ago`;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return `${h}h ${m}m ago`;
}

function fmtUptime(s) {
  if (s == null || !Number.isFinite(Number(s))) return "—";
  const sec = Math.max(0, Math.floor(Number(s)));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const r = sec % 60;
  if (h > 0) return `${h}h ${m}m ${r}s`;
  if (m > 0) return `${m}m ${r}s`;
  return `${r}s`;
}

function setPill(text, kind) {
  const pill = $("dev-pill");
  pill.textContent = text;
  pill.className = `pill ${kind || "off"}`;
}

function renderItemRow({ title, subtitle, tag, onclick }) {
  const el = document.createElement("div");
  el.className = "item";
  el.innerHTML = `
    <div class="grow">
      <div class="t">${esc(title || "Untitled")}</div>
      <div class="s">${esc(subtitle || "")}</div>
    </div>
    ${tag ? `<span class="tag">${esc(tag)}</span>` : ""}
  `;
  el.addEventListener("click", onclick);
  return el;
}

async function refreshNowPlaying() {
  const s = await get("/api/playback/now-playing");
  latestNowPlaying = s || {};
  const title = s.title || "Nothing playing";
  const artist = s.artist || "—";
  const album = s.album || "—";
  $("np-title").textContent = title;
  $("np-artist").textContent = artist;
  $("np-album").textContent = album;
  $("np-state").textContent = s.isPlaying ? "Playing" : "Paused / idle";
  $("toggle").textContent = s.isPlaying ? "Pause" : "Play";

  const art = $("art");
  if (s.artworkUrl) {
    art.src = s.artworkUrl;
    art.style.visibility = "visible";
  } else {
    art.removeAttribute("src");
    art.style.visibility = "hidden";
  }

  const elapsed = Number(s.elapsedMs || 0);
  const duration = Number(s.durationMs || 0);
  $("elapsed").textContent = fmtTime(elapsed);
  $("duration").textContent = fmtTime(duration);
  const pct = duration > 0 ? Math.max(0, Math.min(100, (elapsed / duration) * 100)) : 0;
  $("bar").style.width = `${pct}%`;

  if (typeof s.volume === "number" && Number.isFinite(s.volume)) {
    lastVolume = s.volume;
    if (!volTimer) {
      const v = Math.round(s.volume * 100);
      $("volume").value = String(v);
      $("vol-label").textContent = `${v}%`;
    }
  }
}

async function refreshVolume() {
  try {
    const v = await get("/api/playback/volume");
    if (v && typeof v.volume === "number") {
      lastVolume = v.volume;
      const pct = Math.round(v.volume * 100);
      $("volume").value = String(pct);
      $("vol-label").textContent = `${pct}%`;
    }
  } catch {
    // now-playing may already include volume; ignore if unavailable
  }
}

function pickDevice(devices) {
  if (!devices || typeof devices !== "object") return null;
  if (devices["tidbyt-a2dp"]) return { id: "tidbyt-a2dp", ...devices["tidbyt-a2dp"] };
  const entries = Object.entries(devices).map(([id, d]) => ({ id, ...(d || {}) }));
  entries.sort((a, b) => Number(b.received_at || 0) - Number(a.received_at || 0));
  return entries[0] || null;
}

async function refreshDevice() {
  const data = await get("/api/device/tidbyt/status");
  const dev = pickDevice(data.devices || {});
  if (!dev) {
    setPill("device: no report", "stale");
    $("d-device").textContent = "—";
    $("d-version").textContent = "—";
    $("d-state").textContent = "waiting for heartbeat";
    $("d-age").textContent = "—";
    $("d-uptime").textContent = "—";
    $("d-rssi").textContent = "—";
    $("d-dma").textContent = "—";
    $("d-ip").textContent = "—";
    return;
  }

  const nowSec = Date.now() / 1000;
  const age = dev.received_at ? Math.max(0, nowSec - Number(dev.received_at)) : null;
  const online = age != null && age < 150;
  setPill(`device: ${dev.id || dev.device || "tidbyt"} ${online ? "online" : "stale"}`, online ? "live" : "stale");

  $("d-device").textContent = dev.id || dev.device || "—";
  $("d-version").textContent = dev.version || "—";
  $("d-state").textContent = dev.state || "—";
  $("d-age").textContent = fmtAge(age);
  $("d-uptime").textContent = fmtUptime(dev.uptime_s);
  $("d-rssi").textContent = dev.wifi_rssi != null ? `${dev.wifi_rssi} dBm` : "—";
  $("d-dma").textContent = dev.dma_heap != null ? `${dev.dma_heap} B` : "—";
  $("d-ip").textContent = dev.ip || dev.wifi_ip || "—";
}

async function doSearch() {
  const term = $("search").value.trim();
  const out = $("search-results");
  if (!term) {
    out.replaceChildren();
    return;
  }
  const data = await get(`/api/library/search${qs({ term, limit: 12 })}`);
  const rows = [];
  const pushSong = (item) => {
    const id = item.catalog_id || item.id || item.playParams?.id;
    if (!id) return;
    rows.push(renderItemRow({
      title: item.name || item.title || "Song",
      subtitle: [item.artist, item.album].filter(Boolean).join(" • ") || "Song",
      tag: "song",
      onclick: () => guard(() => post(`/api/playback/play-song/${enc(id)}`), "Playing song"),
    }));
  };
  const pushCollection = (item, kind) => {
    const id = item.id || item.playParams?.id;
    if (!id) return;
    rows.push(renderItemRow({
      title: item.name || item.title || kind,
      subtitle: item.artist || item.curator || item.attributes?.artistName || kind,
      tag: kind,
      onclick: () => guard(() => post(`/api/playback/play-collection${qs({ kind, id })}`), `Playing ${kind}`),
    }));
  };

  const groups = data || {};
  for (const item of (groups.songs || [])) pushSong(item);
  for (const item of (groups.albums || [])) pushCollection(item, "album");
  for (const item of (groups.playlists || [])) pushCollection(item, "playlist");

  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "sub";
    empty.textContent = "No playable results found.";
    out.replaceChildren(empty);
    return;
  }
  out.replaceChildren(...rows);
}

async function loadPlaylists() {
  const data = await get(`/api/library/playlists${qs({ limit: 100 })}`);
  const items = Array.isArray(data?.items) ? data.items : (Array.isArray(data?.data) ? data.data : []);
  const out = $("playlists");
  if (!items.length) {
    out.replaceChildren(Object.assign(document.createElement("div"), { className: "sub", textContent: "No playlists found." }));
    return;
  }
  const rows = items.map((p) => {
    const id = p.id;
    const name = p.name || p.attributes?.name || id || "Playlist";
    const tracks = p.trackCount ?? p.attributes?.trackCount ?? p.tracks_count;
    return renderItemRow({
      title: name,
      subtitle: tracks != null ? `${tracks} tracks` : "Playlist",
      tag: "playlist",
      onclick: () => guard(async () => {
        await post("/api/playback/shuffle/on");
        await post(`/api/playback/play-collection${qs({ kind: "playlist", id })}`);
      }, `Playing playlist: ${name}`),
    });
  });
  out.replaceChildren(...rows);
}

async function armLatestUpdate() {
  $("update-msg").textContent = "Checking latest published version…";
  let target = null;
  try {
    const manifest = await fetch("/tidbyt-updates/bootstrap/manifest.json", { cache: "no-store" }).then(r => r.ok ? r.json() : null);
    target = manifest?.version || null;
  } catch {
    target = null;
  }

  const payload = target ? { device: "tidbyt-a2dp", version: String(target) } : { device: "tidbyt-a2dp" };
  const result = await guard(async () => {
    const res = await fetch(((settings.base || "") + "/api/device/tidbyt/update"), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(settings.key ? { "Authorization": `Bearer ${settings.key}` } : {}),
      },
      body: JSON.stringify(payload),
    });
    let body = {};
    try { body = await res.json(); } catch {}
    if (!res.ok) throw new Error(body?.error?.hint || body?.detail || `HTTP ${res.status}`);
    return body;
  }, "OTA arm request sent");

  if (result) {
    $("update-msg").textContent = `Armed ${result.armed || payload.device} for version ${result.version || target || "latest"}${target ? " (from manifest)" : ""}. Device checks in on its next OTA poll.`;
  }
}

function wireSettings() {
  const dlg = $("settings-dialog");
  $("settings-btn").addEventListener("click", () => {
    $("base-url").value = settings.base || "";
    $("api-key").value = settings.key || "";
    dlg.showModal();
  });
  $("save-settings").addEventListener("click", () => {
    settings.base = $("base-url").value || "";
    settings.key = $("api-key").value || "";
    dlg.close();
    location.reload();
  });
}

function wireControls() {
  $("refresh-btn").addEventListener("click", () => guard(async () => {
    await Promise.allSettled([refreshNowPlaying(), refreshDevice(), refreshVolume()]);
  }, "Refreshed"));

  $("toggle").addEventListener("click", () => guard(async () => {
    const playing = !!latestNowPlaying?.isPlaying;
    await post(`/api/playback/${playing ? "pause" : "play"}`);
    setTimeout(() => guard(refreshNowPlaying), 400);
  }));

  $("prev").addEventListener("click", () => guard(() => post("/api/playback/previous")));
  $("next").addEventListener("click", () => guard(() => post("/api/playback/next")));
  $("back").addEventListener("click", () => guard(() => post(`/api/playback/back${qs({ threshold_ms: 10000 })}`)));

  $("shuffle-on").addEventListener("click", () => guard(() => post("/api/playback/shuffle/on"), "Shuffle on"));
  $("shuffle-off").addEventListener("click", () => guard(() => post("/api/playback/shuffle/off"), "Shuffle off"));

  $("play-fav").addEventListener("click", () => guard(async () => {
    await post("/api/playback/shuffle/on");
    await post(`/api/playback/play-collection${qs({ kind: "playlist", id: FAVORITES_PLAYLIST })}`);
  }, "Playing Favorites (shuffled)"));

  $("play-heavy").addEventListener("click", () => guard(async () => {
    await post("/api/playback/shuffle/on");
    await post(`/api/playback/play-collection${qs({ kind: "playlist", id: HEAVY_ROTATION_PLAYLIST })}`);
  }, "Playing Heavy Rotation (shuffled)"));

  $("search-btn").addEventListener("click", () => guard(doSearch));
  $("search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => guard(doSearch), 300);
  });
  $("search").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      clearTimeout(searchTimer);
      guard(doSearch);
    }
  });

  $("load-playlists").addEventListener("click", () => guard(loadPlaylists));

  $("volume").addEventListener("input", () => {
    const v = Number($("volume").value) / 100;
    $("vol-label").textContent = `${Math.round(v * 100)}%`;
    clearTimeout(volTimer);
    volTimer = setTimeout(() => {
      volTimer = null;
      guard(async () => {
        lastVolume = v;
        await post(`/api/playback/volume/${v.toFixed(2)}`);
      });
    }, 180);
  });

  $("mute").addEventListener("click", () => guard(async () => {
    const current = Number($("volume").value) / 100;
    if (current > 0.001) {
      lastVolume = current;
      $("volume").value = "0";
      $("vol-label").textContent = "0%";
      await post("/api/playback/volume/0");
    } else {
      const restore = lastVolume > 0.001 ? lastVolume : 0.5;
      $("volume").value = String(Math.round(restore * 100));
      $("vol-label").textContent = `${Math.round(restore * 100)}%`;
      await post(`/api/playback/volume/${restore.toFixed(2)}`);
    }
  }));

  $("arm-update").addEventListener("click", () => guard(armLatestUpdate));
  $("open-status").addEventListener("click", () => { window.location.href = "/tidbyt-status"; });
}

async function boot() {
  wireSettings();
  wireControls();
  await Promise.allSettled([
    guard(refreshNowPlaying),
    guard(refreshDevice),
    guard(refreshVolume),
    guard(loadPlaylists),
  ]);

  setInterval(() => {
    refreshNowPlaying().catch(() => {});
  }, 3000);
  setInterval(() => {
    refreshDevice().catch(() => {});
  }, 10000);
}

boot().catch((e) => {
  console.error(e);
  toast(e?.message || "Failed to initialize Tidbyt control center", "err");
});
