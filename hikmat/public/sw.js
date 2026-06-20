/* Hikmat PWA service worker — offline-first app shell.
   Scope: /assets/hikmat/ (covers game.html). Bump CACHE to ship an update. */
const CACHE = "hikmat-pwa-v1";
const BASE = "/assets/hikmat/";
const SHELL = [
  BASE + "game.html",
  BASE + "manifest.webmanifest",
  BASE + "icons/icon-192.png",
  BASE + "icons/icon-512.png",
  BASE + "icons/icon-512-maskable.png",
  BASE + "icons/icon-180.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE)
      .then((c) => Promise.allSettled(SHELL.map((u) => c.add(u))))  // tolerate a missing asset
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;                      // never cache POSTs (login, submit_attempt)
  const url = new URL(req.url);
  if (url.pathname.startsWith("/api/")) return;          // API hits the network; the game falls back to localStorage offline
  if (!url.pathname.startsWith(BASE)) return;            // only manage this app's own static files

  const isDoc = req.mode === "navigate" || url.pathname.endsWith("game.html");
  if (isDoc) {
    // network-first so the game updates when online, cache fallback when offline
    e.respondWith(
      fetch(req)
        .then((res) => { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(req, copy)); return res; })
        .catch(() => caches.match(req).then((r) => r || caches.match(BASE + "game.html")))
    );
  } else {
    // cache-first for icons/manifest/etc.
    e.respondWith(
      caches.match(req).then((r) => r || fetch(req).then((res) => {
        const copy = res.clone(); caches.open(CACHE).then((c) => c.put(req, copy)); return res;
      }))
    );
  }
});
