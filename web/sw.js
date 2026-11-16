/* tonearm service worker (#90) — cache the SHELL, never the API.
   Network-first for NAVIGATIONS (a stale shell must never pin an old app —
   found live: an hours-old cached index served the pre-redesign UI through
   a funnel while the server was correct). Cache-first only for other
   same-origin GETs, which are versioned via ?v=N URLs anyway.
   Every /api request goes straight to the network (live data — a cached
   playhead or audio stream would be a lie). */
const CACHE = "tonearm-shell-v5";
const SHELL = ["/", "/app.js?v=4", "/styles.css?v=4", "/manifest.webmanifest?v=4"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE)
    .then((c) => c.addAll(SHELL))
    .then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(
      keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/metrics")) {
    return;                                   // NEVER the API — live data only
  }
  if (e.request.method !== "GET" || url.origin !== location.origin) return;

  // navigations: the network is the truth; the cache is only an offline
  // fallback. This is what stops an old shell from surviving a deploy.
  if (e.request.mode === "navigate") {
    e.respondWith((async () => {
      try {
        const fresh = await fetch(e.request);
        if (fresh.ok) {
          const c = await caches.open(CACHE);
          c.put("/", fresh.clone());
        }
        return fresh;
      } catch {
        return (await caches.match("/")) || Response.error();
      }
    })());
    return;
  }

  // other shell assets: cache-first, refresh in the background
  e.respondWith(caches.open(CACHE).then(async (c) => {
    const hit = await c.match(e.request);
    const net = fetch(e.request).then((r) => {
      if (r.ok) c.put(e.request, r.clone());
      return r;
    }).catch(() => hit);
    return hit || net;
  }));
});
