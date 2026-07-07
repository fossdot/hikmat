/* Hikmat PWA service worker — offline-first app shell + content.
   Scope: /assets/hikmat/. Bump CACHE to ship an update to installed PWAs. */
const CACHE = "hikmat-pwa-v7";   // v7: Reply-to-the-Email activity
const BASE = "/assets/hikmat/";
const SHELL = [
  BASE + "game.html",
  BASE + "curriculum.json",          // full 74-lesson offline baseline (survives localStorage eviction)
  BASE + "manifest.webmanifest",
  BASE + "icons/icon-192.png",
  BASE + "icons/icon-512.png",
  BASE + "icons/icon-512-maskable.png",
  BASE + "icons/icon-180.png",
];
// read APIs cached (network-first) so the game keeps full content offline even if localStorage is wiped
const CACHED_API = ["hikmat.api.get_courses", "hikmat.api.get_structure", "hikmat.api.get_settings"];

self.addEventListener("install", (e) => {
  // tolerate a missing asset; do NOT unconditionally skipWaiting — the page asks us to activate
  // (see 'message') so an update never reloads a child mid-activity.
  e.waitUntil(caches.open(CACHE).then((c) => Promise.allSettled(SHELL.map((u) => c.add(u)))));
});

self.addEventListener("message", (e) => { if (e.data === "SKIP_WAITING") self.skipWaiting(); });

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

function networkFirst(req) {
  return fetch(req)
    .then((res) => { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(req, copy)); return res; })
    .catch(() => caches.match(req));
}

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;                       // never touch POSTs (login, submit_attempt)
  const url = new URL(req.url);

  if (url.pathname.startsWith("/api/method/")) {
    // cache only the content read endpoints; other API GETs (roster etc.) go straight to the network
    if (CACHED_API.some((m) => url.pathname.indexOf(m) !== -1)) e.respondWith(networkFirst(req));
    return;
  }
  if (!url.pathname.startsWith(BASE)) return;             // only manage this app's own static files

  const isDoc = req.mode === "navigate" || url.pathname.endsWith("game.html");
  if (isDoc) {
    e.respondWith(networkFirst(req).then((r) => r || caches.match(BASE + "game.html")));
  } else {
    e.respondWith(caches.match(req).then((r) => r || fetch(req).then((res) => {
      const copy = res.clone(); caches.open(CACHE).then((c) => c.put(req, copy)); return res;
    })));
  }
});
