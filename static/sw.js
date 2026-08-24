/* Shell: stale-while-revalidate. API: network-first.

   The shell used to be plain cache-first under a fixed cache name, which meant
   a device that had installed the app once would serve that build forever --
   every later fix to app.js or app.css was invisible on the one device that
   mattered. Now the cached copy is still returned instantly (that is the point
   of installing it), but a fresh copy is fetched in the background and written
   over it, so the next launch is current. One launch behind, never stuck.

   Bump SHELL when you want to force an immediate purge rather than waiting for
   the next launch. */
const SHELL = "farewatch-shell-v2";
const DATA  = "farewatch-data-v1";
const ASSETS = ["/", "/index.html", "/app.css", "/app.js",
                "/manifest.json", "/icon-192.png", "/icon-512.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(ASSETS))
    .then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(
    keys.filter(k => k !== SHELL && k !== DATA).map(k => caches.delete(k))
  )).then(() => self.clients.claim()));
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;

  if (url.pathname === "/api/status") {
    e.respondWith(
      fetch(e.request).then(res => {
        const copy = res.clone();
        caches.open(DATA).then(c => c.put(e.request, copy));
        return res;
      }).catch(() => caches.match(e.request))
    );
    return;
  }

  e.respondWith(caches.match(e.request, { ignoreSearch: true }).then(hit => {
    const fresh = fetch(e.request).then(res => {
      if (res && res.ok) {
        const copy = res.clone();
        caches.open(SHELL).then(c => c.put(e.request, copy));
      }
      return res;
    }).catch(() => hit);
    return hit || fresh;
  }));
});
