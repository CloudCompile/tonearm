// tonearm daily listening workspace: local library, recents, favorites,
// playlists, transport, lyrics, and the live queue share one persistent shell.
import { del, get, post, put } from "./api.js";
import { onState, currentState } from "./sse.js";
import { esc, fmt } from "./util.js";
import { VList } from "./vlist.js";
import * as lyrics from "./views/lyrics.js";
import { initListen } from "./views/listen.js";

const $ = (id) => document.getElementById(id);
const viewNames = { home: "Home", browse: "Library", favorites: "Favorites",
  history: "Recently played", playlist: "Playlist" };
let view = "home", activeTab = "songs", playlistId = null, playlistRows = [];
let favoriteIds = new Set(), libraryList, favoritesList, historyList, playlistList;
let playlistNavLoaded = false, toastTimer = null, mobileOpen = false;

initListen();

const escapeAttr = (v) => esc(v).replace(/'/g, "&#39;");
const artUrl = (s, size = 300) => (s?.artwork || s?.artworkUrl || "")
  .replace("{w}", String(size)).replace("{h}", String(size));
const rowSub = (r) => [r.artist, r.album].filter(Boolean).join(" · ");

function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2400);
}

async function safe(promise, success) {
  try { const result = await promise; if (success) success(result); return result; }
  catch (e) { toast(e.message || "That didn’t work. Try again."); throw e; }
}

function imageCss(src) {
  return src ? `background-image:url('${escapeAttr(src)}')` : "";
}

function iconUse(name) { return `<svg class="icon" aria-hidden="true"><use href="#i-${name}"/></svg>`; }

function mediaCard(item, source = "library") {
  const id = item.catalog_id || item.id || "";
  const type = item.type || item.kind || "songs";
  const song = source === "catalog" || type.includes("song") || type === "songs";
  return `<article class="media-card" data-action="play-item" data-id="${escapeAttr(id)}" data-kind="${song ? "song" : escapeAttr(type.replace(/^library-/, "").replace(/s$/, ""))}" data-source="${source}">
    <div class="media-art" style="${imageCss(artUrl(item))}">${artUrl(item) ? "" : `<svg class="icon"><use href="#i-note"/></svg>`}<button class="card-play" aria-label="Play ${escapeAttr(item.name || item.title)}"><svg class="icon"><use href="#i-play"/></svg></button></div>
    <div class="media-title">${esc(item.name || item.title || "Untitled")}</div><div class="media-subtitle">${esc(item.artist || item.artistName || (item.type || "").replace(/^library-/, ""))}</div></article>`;
}

function rowHtml(item, i) {
  const id = item.catalog_id || item.id || "";
  const liked = favoriteIds.has(String(id));
  const art = artUrl(item, 120);
  const badge = item.play_count != null ? `${item.play_count} plays` : "";
  const song = (item.type || "").includes("song") || item.catalog_id;
  return `${art ? `<img class="thumb" src="${esc(art)}" alt="" loading="lazy">` : `<span class="thumb thumb-empty"><svg class="icon"><use href="#i-note"/></svg></span>`}
    <div class="t"><b>${esc(item.name || item.title || "Untitled")}</b><span>${esc(rowSub(item) || item.artistName || "")}</span></div>
    ${badge ? `<span class="badge">${esc(badge)}</span>` : ""}
    <div class="row-actions">${song ? `<button class="row-action" data-action="favorite" data-id="${escapeAttr(id)}" title="${liked ? "Remove favorite" : "Add favorite"}">${iconUse("favorite")}</button><button class="row-action" data-action="play-next" data-id="${escapeAttr(id)}" title="Play next">${iconUse("plus")}</button><button class="row-action" data-action="play-later" data-id="${escapeAttr(id)}" title="Add to queue">${iconUse("add-queue")}</button>` : ""}</div>`;
}

function playCollection(kind, id) {
  return safe(post(`/api/playback/play-collection?kind=${encodeURIComponent(kind)}&id=${encodeURIComponent(id)}`), () => toast("Starting collection"));
}

function ensureList(which, element) {
  if (which === "libraryList" && !libraryList) libraryList = new VList(element, rowHtml);
  if (which === "favoritesList" && !favoritesList) favoritesList = new VList(element, rowHtml);
  if (which === "historyList" && !historyList) historyList = new VList(element, rowHtml);
  if (which === "playlistList" && !playlistList) playlistList = new VList(element, rowHtml);
  return ({ libraryList, favoritesList, historyList, playlistList })[which];
}

function hideLists() {
  for (const el of [$("list"), $("favorites-list"), $("history-list"), $("playlist-list")]) if (el) el.hidden = true;
}

function goHome() { showView("home"); }

function showView(name, { keepSearch = false } = {}) {
  view = name;
  document.querySelectorAll(".page-view").forEach((node) => { node.hidden = node.id !== `view-${name}`; });
  hideLists();
  const searchPanel = $("searchout");
  if (searchPanel) searchPanel.hidden = keepSearch ? false : true;
  const main = $("main");
  if (main) main.classList.toggle("focus-player", name !== "home");
  const title = $("view-title");
  if (title) title.textContent = name === "playlist" ? $("playlist-title")?.textContent || "Playlist" : viewNames[name] || "Home";
  document.querySelectorAll(".nav-item").forEach((n) => n.classList.toggle("active", n.dataset.view === name || (name === "playlist" && n.dataset.view === "browse")));
  document.title = `${$("view-title").textContent} — tonearm`;
  closeMobileNav();
}

function closeMobileNav() { $("sidebar").classList.remove("open"); mobileOpen = false; }

function loader(path, transform = (page) => page.items || []) {
  return async (offset, limit) => {
    const page = await get(path, { offset, limit });
    const rows = transform(page);
    return { items: rows, next_offset: page.next_offset ?? (rows.length === limit ? offset + rows.length : null) };
  };
}

async function loadLibrary(tab = activeTab) {
  activeTab = tab;
  $("tabs").dataset.tab = tab;
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
  $("list-count").textContent = tab === "queue" ? "Drag to reorder · changes apply to your player" : `Your ${tab}`;
  const sortCtl = $("sort-list");
  sortCtl.hidden = tab !== "songs";
  if (tab !== "songs") sortCtl.hidden = true;
  if (tab === "queue") { $("list").hidden = false; return renderQueueList(); }
  $("list").hidden = false;
  $("list").className = "list";
  const list = ensureList("libraryList", $("list"));
  if (tab === "songs" && sortMode === "recent") return loadRecentlyAdded(list);
  await list.setLoader(titleLoader(tab));
}

// "Recently added" is store-backed: the local walk's first_seen watermark,
// newest first. Apple's own listing is alphabetical — the label used to
// promise recency the API doesn't deliver (found live).
let sortMode = "recent";
function renderSortLabel() {
  $("sort-list").textContent = sortMode === "recent"
    ? "Sorted: recently added ⌄" : "Sorted: title A–Z ⌄";
}
function titleLoader(tab) {
  return loader(`/api/library/${tab}`, (page) => (page.items || []).map((x) => ({ ...x, name: x.name || x.title })));
}
async function loadRecentlyAdded(list) {
  renderSortLabel();
  try {
    const page = await get("/api/library/diff", { since: 0, what: "added", limit: 50 });
    const rows = (page.added?.items || []).map((x) => ({ ...x, id: x.catalog_id || x.library_id, type: "library-songs" }));
    if (!rows.length) throw Object.assign(new Error("walk"), { walk: true });
    await list.setLoader(async (offset) => {
      const p = await get("/api/library/diff", { since: 0, what: "added", limit: 50, offset });
      const items = (p.added?.items || []).map((x) => ({ ...x, id: x.catalog_id || x.library_id, type: "library-songs" }));
      return { items, next_offset: p.added?.next_offset ?? null };
    });
  } catch (e) {
    if (e?.walk) {
      toast("The local library walk hasn't indexed yet — showing A–Z");
      sortMode = "title";
      renderSortLabel();
      await list.setLoader(titleLoader("songs"));
    } else toast(e.message || "Could not load recently added");
  }
}
$("sort-list").onclick = () => {
  sortMode = sortMode === "recent" ? "title" : "recent";
  renderSortLabel();
  loadLibrary("songs");
};

let latestState = null, liveQueue = [];
function queueRow(t, i, compact = false) {
  const art = artUrl(t, 90);
  return `<div class="${compact ? "mini-queue-item" : "row queue-row"}" ${compact ? "" : `draggable="true" data-i="${i}" style="top:${i * 56}px"`}>
    ${compact ? "" : `<span class="handle">${iconUse("grip")}</span>`}<div class="mini-cover" style="${imageCss(art)}">${art ? "" : iconUse("note")}</div>
    <div class="${compact ? "mini-label" : "t"}"><b>${esc(t.title || t.name || t.id)}</b><small>${esc(t.artist || "")}</small></div><span class="${compact ? "mini-position" : "badge"}">${compact ? `#${i + 1}` : `#${i + 1}`}</span>
    ${compact ? "" : `<button class="row-action" data-action="play-next" data-id="${escapeAttr(t.id)}" title="Play next">${iconUse("plus")}</button>`}</div>`;
}

function renderMiniQueue() {
  const rest = liveQueue.filter((t) => !latestState || t.id !== latestState.id).slice(0, 4);
  $("mini-queue").innerHTML = rest.length ? rest.map((t, i) => queueRow(t, i, true)).join("") : `<div class="sidebar-empty">Nothing else queued</div>`;
}

async function refreshVolume() {
  try { const state = await get("/api/playback/volume"); $("vol").value = state.volume ?? 0.5; }
  catch { /* surfaced by key prompt / connection UI */ }
}

async function renderQueueList() {
  const el = $("list"); el.hidden = false; el.className = "list";
  el.innerHTML = `<div class="spacer" style="height:${liveQueue.length * 56}px"></div>` + liveQueue.map((t, i) => `<div class="row queue-row" draggable="true" data-i="${i}" style="top:${i * 56}px"><span class="handle"><svg class="icon"><use href="#i-grip"/></svg></span><div class="mini-cover" style="${imageCss(artUrl(t, 90))}">${artUrl(t) ? "" : `<svg class="icon"><use href="#i-note"/></svg>`}</div><div class="t"><b>${esc(t.title || t.id)}</b><span>${esc(t.artist || "")}</span></div><span class="badge">#${i + 1}</span><button class="row-action" data-action="play-next" data-id="${escapeAttr(t.id)}" title="Play next"><svg class="icon"><use href="#i-plus"/></svg></button></div>`).join("");
}

function noteState(s) {
  latestState = s;
  liveQueue = s.queue || liveQueue;
  lyrics.noteState(s); lyrics.onState(s);
  $("np-title").textContent = s.title || (s.isPlaying ? "Playing" : "Nothing playing");
  $("np-artist").textContent = s.title ? [s.artist, s.album].filter(Boolean).join(" · ") : "Choose something to listen to";
  const playIcon = s.isPlaying ? "i-pause" : "i-play";
  for (const id of ["play", "mobile-play", "home-play", "sheet-play"])
    $(id).innerHTML = `<svg class="icon"><use href="#${playIcon}"/></svg>`;
  $("np-fill").style.width = s.durationMs ? `${100 * (s.elapsedMs || 0) / s.durationMs}%` : "0%";
  $("mobile-player-fill").style.width = s.durationMs ? `${100 * (s.elapsedMs || 0) / s.durationMs}%` : "0%";
  $("np-elapsed").textContent = fmt(s.elapsedMs); $("np-dur").textContent = fmt(s.durationMs);
  $("vol").value = s.volume ?? $("vol").value;
  $("mute").innerHTML = iconUse("volume");
  const artwork = s.artworkUrl || "";
  $("art").className = `rail-art${artwork ? "" : " empty"}`; $("art").style.backgroundImage = artwork ? `url('${escapeAttr(artwork)}')` : "none";
  $("favorite-current").classList.toggle("active", favoriteIds.has(String(s.id)));
  $("favorite-current").innerHTML = iconUse("favorite");
  $("home-art").style.backgroundImage = artwork ? `url('${escapeAttr(artwork)}')` : "none";
  $("home-title").textContent = s.title || "Nothing playing yet";
  $("home-now").style.backgroundImage = artwork ? `linear-gradient(100deg,#202518ef,#202518b0),url('${escapeAttr(artwork)}')` : "";
  $("home-artist").textContent = s.title ? [s.artist, s.album].filter(Boolean).join(" · ") : "Choose a song from your library";
  $("mobile-title").textContent = s.title || "Nothing playing"; $("mobile-artist").textContent = s.artist || "Choose a song";
  $("sheet-title").textContent = s.title || "Nothing playing"; $("sheet-artist").textContent = [s.artist, s.album].filter(Boolean).join(" · ");
  $("sheet-fill").style.width = $("np-fill").style.width; $("sheet-elapsed").textContent = fmt(s.elapsedMs); $("sheet-duration").textContent = fmt(s.durationMs);
  $("sheet-art").style.backgroundImage = artwork ? `url('${escapeAttr(artwork)}')` : "none";
  $("mobile-mini-art").style.backgroundImage = artwork ? `url('${escapeAttr(artwork)}')` : "none";
  if (s.queue) liveQueue = s.queue;
  const stateKey = String(s.id || "");
  $("favorite-current").disabled = !stateKey;
  renderMiniQueue();
  if (activeTab === "queue" && view === "browse") renderQueueList();
}

onState(noteState);

function transport(command) { return safe(post(`/api/playback/${command}`)); }
const playToggle = () => transport(latestState?.isPlaying ? "pause" : "play");
$("play").onclick = playToggle; $("home-play").onclick = playToggle; $("mobile-play").onclick = playToggle; $("sheet-play").onclick = playToggle;
$("next").onclick = $("home-next").onclick = $("sheet-next").onclick = () => transport("next");
$("prev").onclick = $("sheet-prev").onclick = () => safe(post("/api/playback/back"));
$("vol").onchange = (e) => safe(post(`/api/playback/volume/${e.target.value}`));
$("mute").onclick = () => { const vol = $("vol"); if (+vol.value) { vol.dataset.last = vol.value; vol.value = 0; } else vol.value = vol.dataset.last || ".7"; vol.onchange({ target: vol }); };
$("favorite-current").onclick = () => latestState?.id && toggleFavorite(latestState.id);

function playItem(id, kind = "song", source = "library") {
  if (!id) return;
  if (kind === "song") return safe(post(`/api/playback/play-song/${encodeURIComponent(id)}`), () => toast("Playing now"));
  return playCollection(kind, id);
}

$("favorite-current").onclick = () => {
  const id = latestState?.id;
  if (id) toggleFavorite(id);
};

async function toggleFavorite(id) {
  const key = String(id);
  try {
    if (favoriteIds.has(key)) { await del(`/api/write/favorites/${encodeURIComponent(key)}`); favoriteIds.delete(key); toast("Removed from favorites"); }
    else { await post(`/api/write/favorites/${encodeURIComponent(key)}`); favoriteIds.add(key); toast("Added to favorites"); }
    if (view === "favorites") loadFavorites();
  } catch (e) { toast(e.status === 501 ? "Favorites are disabled on this server" : e.message); }
}

async function queueAdd(id, next = false) {
  const endpoint = next ? "/api/playback/play-next" : "/api/playback/play-later";
  try { await post(`${endpoint}?catalog_id=${encodeURIComponent(id)}`); toast(next ? "Added to play next" : "Added to queue"); }
  catch (e) { toast(e.message); }
}

document.addEventListener("click", (e) => {
  const act = e.target.closest("[data-action]");
  if (act) {
    const id = act.dataset.id, action = act.dataset.action;
    if (action === "play-item" && !act.classList.contains("media-card")) playItem(id, act.dataset.kind, act.dataset.source);
    if (action === "favorite") toggleFavorite(id);
    if (action === "play-next") queueAdd(id, true);
    if (action === "play-later") queueAdd(id, false);
    if (!act.classList.contains("media-card")) { e.stopPropagation(); return; }
  }
  const card = e.target.closest(".media-card");
  if (e.target.closest(".card-play")) {
    const card = e.target.closest(".media-card");
    if (card) playItem(card.dataset.id, card.dataset.kind, card.dataset.source);
    return;
  }
  if (card && !e.target.closest("button")) playItem(card.dataset.id, card.dataset.kind, card.dataset.source);
  const row = e.target.closest("#list .row:not(.queue-row), #favorites-list .row, #history-list .row, #playlist-list .row");
  if (row && !e.target.closest("button")) {
    const parent = row.closest("#favorites-list") ? favoritesList
      : row.closest("#history-list") ? historyList
      : row.closest("#playlist-list") ? playlistList : libraryList;
    const item = parent?.rows[+row.dataset.i];
    if (item) playItem(item.catalog_id || item.id,
      (item.type || "").includes("song") || item.catalog_id ? "song" : activeTab.slice(0, -1), "library");
  }
  const go = e.target.closest("[data-go]"); if (go) navigate(go.dataset.go);
  const nav = e.target.closest("[data-view]"); if (nav) navigate(nav.dataset.view);
  const pl = e.target.closest("[data-playlist-id]"); if (pl) openPlaylist(pl.dataset.playlistId, pl.dataset.name || pl.textContent.trim());
});

document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => loadLibrary(b.dataset.tab)));
$("shuffle-library").onclick = async () => { try { const first = await get("/api/library/songs", { limit: 100 }); const rows = first.items || []; if (!rows.length) return toast("Your library is empty"); const random = rows[Math.floor(Math.random() * rows.length)]; await playItem(random.catalog_id || random.id); } catch (e) { toast(e.message); } };
$("play-favorites").onclick = async () => { try { if (!favoriteIds.size) await loadFavorites(true); const first = [...favoriteIds][0]; if (first) await playItem(first); else toast("No favorites yet"); } catch (e) { toast(e.message); } };
$("new-playlist").onclick = async () => { const name = prompt("Name your playlist"); if (!name?.trim()) return; try { const r = await post(`/api/write/playlists?name=${encodeURIComponent(name.trim())}`); toast("Playlist created"); playlistNavLoaded = false; await loadPlaylists(); if (r.playlist_id) openPlaylist(r.playlist_id, name); } catch (e) { toast(e.status === 501 ? "Playlist editing is disabled on this server" : e.message); } };
$("playlist-play").onclick = () => playlistId && playCollection("playlist", playlistId);
$("playlist-shuffle").onclick = () => { if (!playlistRows.length) return; const row = playlistRows[Math.floor(Math.random() * playlistRows.length)]; playItem(row.catalog_id || row.id); };
$("queue-view-all").onclick = () => { navigate("browse"); loadLibrary("queue"); };
$("rail-queue").onclick = () => { navigate("browse"); loadLibrary("queue"); };
$("home-lyrics").onclick = $("open-lyrics").onclick = () => lyrics.toggle();
$("profile-button").onclick = $("settings").onclick = () => { $("keyprompt").hidden = false; };
$("mobile-menu").onclick = () => { mobileOpen = !mobileOpen; $("sidebar").classList.toggle("open", mobileOpen); };
$("mobile-player-button").onclick = () => { $("mobile-sheet").hidden = false; };
$("sheet-close").onclick = () => { $("mobile-sheet").hidden = true; };
$("sheet-queue").onclick = () => { $("mobile-sheet").hidden = true; navigate("browse"); loadLibrary("queue"); };
$("sheet-lyrics").onclick = () => lyrics.toggle();
document.addEventListener("click", (e) => { if (mobileOpen && !e.target.closest(".sidebar") && !e.target.closest("#mobile-menu")) closeMobileNav(); });
$("sidebar").querySelector(".brand").addEventListener("click", (e) => { e.preventDefault(); navigate("home"); });
$("search-close").onclick = () => { $("searchout").hidden = true; $("q").value = ""; };
document.addEventListener("keydown", (e) => { if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); $("q").focus(); } });
$("sidebar").querySelector(".brand").onclick = (e) => { e.preventDefault(); goHome(); };

function navigate(name) { showView(name); if (name === "browse") loadLibrary(activeTab); if (name === "favorites") loadFavorites(); if (name === "history") loadHistory(); }

async function loadFavorites(force = false) {
  const list = ensureList("favoritesList", $("favorites-list"));
  $("favorites-list").hidden = false;
  try {
    const fav = await get("/api/favorites", force ? { fresh: 1 } : undefined);
    const rows = fav.tracks || [];
    favoriteIds = new Set(rows.map((r) => String(r.catalog_id || r.id)));
    $("favorites-count").textContent = `${fav.count ?? rows.length} songs you love`;
    await list.setLoader(async () => ({ items: rows, next_offset: null }));
  } catch (e) {
    $("favorites-count").textContent = e.status === 502 ? "Could not load favorites" : "Favorites aren’t available";
    $("favorites-list").innerHTML = `<div class="loading-card">${esc(e.message || "Favorites unavailable")}</div>`;
  }
}

async function loadHistory() {
  const list = ensureList("historyList", $("history-list"));
  $("history-list").hidden = false;
  try {
    let history;
    try {
      const page = await get("/api/history/recently-played", { limit: 100 });
      // a freshly-enabled store returns 200 with an EMPTY archive — fall
      // back to Apple's recent list until the local rows exist, otherwise
      // the page looks emptier than before the store was on
      if (!(page.items || []).length) throw Object.assign(new Error("empty"), { fallback: true });
      history = { ...page, items: (page.items || []).map((x) => ({ ...x,
        id: x.id, catalog_id: x.id, name: x.title, type: "songs" })) };
      $("history-note").textContent = "Your tonearm listening history";
    } catch (e) {
      if (!e?.fallback) throw e;
      const personal = await get("/api/personal/recently-played", { limit: 20 });
      history = { items: personal.items || [], next_offset: null };
      $("history-note").textContent = "Recent Apple Music history (the local archive fills as you play)";
    }
    const rows = history.items || [];
    await list.setLoader(async () => ({ items: rows, next_offset: null }));
  } catch (e) { $("history-list").innerHTML = `<div class="loading-card">${esc(e.message || "History unavailable")}</div>`; }
}

async function loadPlaylists() {
  try {
    const response = await get("/api/library/playlists", { limit: 100 });
    const rows = response.items || [];
    const playablePlaylists = rows.filter((p) => p.id && !p.type?.includes("M3U_STREAM"));
    $("playlist-nav").innerHTML = playablePlaylists.length ? playablePlaylists.map((p) => `<button class="playlist-link" data-playlist-id="${escapeAttr(p.id)}" data-name="${escapeAttr(p.name)}">${iconUse("note")}${esc(p.name)}</button>`).join("") : `<div class="sidebar-empty">No playlists yet</div>`;
    playlistNavLoaded = true;
  } catch (e) { $("playlist-nav").innerHTML = `<div class="sidebar-empty">${esc(e.status === 401 ? "Connect your server to browse" : "Playlists unavailable")}</div>`; }
}

async function openPlaylist(id, name) {
  playlistId = id; showView("playlist");
  $("playlist-title").textContent = name; $("view-title").textContent = name;
  const list = ensureList("playlistList", $("playlist-list"));
  $("playlist-list").hidden = false;
  try {
    const response = await get(`/api/library/playlists/${encodeURIComponent(id)}/tracks`);
    playlistRows = (response.tracks || []).map((x) => ({ ...x, id: x.library_id || x.id,
      catalog_id: x.catalog_id || x.id, name: x.name || x.title, type: "library-songs" }));
    $("playlist-subtitle").textContent = `${playlistRows.length} songs`;
    await list.setLoader(async () => ({ items: playlistRows, next_offset: null }));
  } catch (e) { $("playlist-list").innerHTML = `<div class="loading-card">${esc(e.message || "Could not open playlist")}</div>`; }
}

function renderRecent(rows, target, source = "library") {
  $(target).innerHTML = rows.length ? rows.slice(0, 5).map((x) => mediaCard(x, source)).join("") : `<div class="loading-card">No songs to show yet</div>`;
}

async function loadHome() {
  try { const page = await get("/api/library/recently-added", { limit: 10 }); renderRecent(page.items || [], "recent-grid"); }
  catch (e) { $("recent-grid").innerHTML = `<div class="loading-card">${esc(e.message || "Your library is unavailable")}</div>`; }
  try {
    let items;
    let page = null;
    try { page = await get("/api/history/recently-played", { limit: 5 }); } catch { /* store off or empty */ }
    if (page && (page.items || []).length) items = page.items;
    else items = (await get("/api/personal/recently-played", { limit: 5 })).items || [];
    renderRecent(items, "history-grid");
  } catch { $("history-grid").innerHTML = `<div class="loading-card">Listening history appears as you play</div>`; }
}

// Search is intentionally one unified surface; library matches are playable,
// catalog matches can be played or queued.
  const searchBox = $("q"); let searchTimer, searchSequence = 0;
searchBox.addEventListener("input", () => { clearTimeout(searchTimer); const term = searchBox.value.trim(); searchSequence++; if (!term) { $("searchout").hidden = true; return; } $("searchout").hidden = false; $("search-scope").textContent = "Searching your library and Apple Music…"; searchTimer = setTimeout(() => search(term, searchSequence), 240); });
searchBox.addEventListener("keydown", (e) => { if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); searchBox.focus(); } if (e.key === "Escape") { $("searchout").hidden = true; searchBox.value = ""; searchBox.blur(); } });
async function search(term, sequence) {
  const list = $("search-list"); let rows = [];
  try {
    const [libResult, catResult] = await Promise.allSettled([
      get("/api/library/search", { term, limit: 15 }),
      get("/api/catalog/search", { term, types: "songs", limit: 12 }),
    ]);
    if (sequence !== searchSequence || term !== searchBox.value.trim()) return;
    if (libResult.status === "fulfilled") for (const [kind, group] of Object.entries(libResult.value)) for (const item of group) rows.push({ ...item, _kind: "song", _source: "Your library", _catalog: item.catalog_id || item.id });
    if (catResult.status === "fulfilled") for (const item of catResult.value.songs || []) rows.push({ ...item, _kind: "song", _source: "Apple Music", _catalog: item.id });
    if (!rows.length && libResult.status === "rejected" && catResult.status === "rejected") throw libResult.reason;
    $("search-scope").textContent = rows.length ? `${rows.length} results · library first, Apple Music after` : "No results found";
    list.innerHTML = rows.map((r) => `<div class="row search-row" data-action="play-item" data-id="${escapeAttr(r._catalog)}" data-kind="song" data-source="${r._source === "Your library" ? "library" : "catalog"}"><span class="thumb thumb-empty">${iconUse("note")}</span><div class="t"><b>${esc(r.name || r.title)}</b><span>${esc(r.artist || "")}</span></div><span class="badge">${esc(r._source)}</span><div class="row-actions"><button class="row-action" data-action="play-next" data-id="${escapeAttr(r._catalog)}" title="Play next">${iconUse("plus")}</button><button class="row-action" data-action="play-later" data-id="${escapeAttr(r._catalog)}" title="Add to queue">${iconUse("add-queue")}</button></div></div>`).join("");
  } catch (e) {
    if (sequence !== searchSequence || term !== searchBox.value.trim()) return;
    $("search-scope").textContent = `Search failed: ${e.message || e}`;
  }
}

// Queue rows use whole-list replacement exactly as the API contract requires.
let dragIndex = -1;
$("list").addEventListener("dragstart", (e) => { const row = e.target.closest(".queue-row"); if (!row) return; dragIndex = +row.dataset.i; row.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; });
$("list").addEventListener("dragover", (e) => { if (!e.target.closest(".queue-row")) return; e.preventDefault(); });
$("list").addEventListener("drop", async (e) => { e.preventDefault(); const row = e.target.closest(".queue-row"); if (!row || dragIndex < 0) return; const to = +row.dataset.i; if (to === dragIndex) return; const ids = liveQueue.map((x) => x.id); const [moved] = ids.splice(dragIndex, 1); ids.splice(to, 0, moved); try { await put("/api/playback/queue", { ids }); toast("Queue updated"); } catch (err) { toast(err.message); } dragIndex = -1; });
$("list").addEventListener("dragend", () => { dragIndex = -1; document.querySelectorAll(".dragging").forEach((el) => el.classList.remove("dragging")); });

$("lyrClose").onclick = () => { $("lyr").hidden = true; };
$("offMinus").onclick = () => lyrics.adjustOffset(-100);
$("offPlus").onclick = () => lyrics.adjustOffset(100);

$("keysave").onclick = () => { localStorage.setItem("tonearm_key", $("key").value.trim()); $("keyprompt").hidden = true; location.reload(); };
window.addEventListener("unhandledrejection", (e) => { if (e.reason?.status === 401 || e.reason?.status === 403) $("keyprompt").hidden = false; });

$("today-date").textContent = new Intl.DateTimeFormat(undefined, { weekday: "long", month: "long", day: "numeric" }).format(new Date());
showView("home"); loadHome(); loadPlaylists(); refreshVolume();
(async () => { try { const s = await get("/api/playback/now-playing"); if (s && !latestState) noteState(s); } catch (e) { if (e.status === 401) $("keyprompt").hidden = false; } })();

// ---- build-freshness self-heal -------------------------------------------
// A long-lived tab or an installed PWA window can sit on a stale cached
// shell for hours (found live: an old window kept the pre-redesign UI while
// the server was correct). The bundle carries the build stamp it was built
// with; poll build.json and reload when it differs — no manual clearing.
const BUILD = typeof __BUILD__ !== "undefined" ? __BUILD__ : "";
if (BUILD) {
  const checkBuild = async () => {
    try {
      const r = await fetch("/build.json", { cache: "no-store" });
      if (!r.ok) return;
      const j = await r.json();
      if (j.built && j.built !== BUILD) location.reload();
    } catch { /* offline or mid-restart — next tick */ }
  };
  setInterval(checkBuild, 60_000);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") checkBuild();
  });
  checkBuild();
}

// ---- service worker: register, and force an update check every load ------
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").then((reg) => {
    reg.update().catch(() => {});   // never wait for the browser's 24h check
  }).catch(() => {});
}

export { navigate };
