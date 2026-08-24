/* Shell: stale-while-revalidate. Data: network-first.

   The shell used to be plain cache-first under a fixed cache name, which meant
   a phone that had installed the app once would serve that build forever --
   every later fix to app.js or app.css was invisible on the one device that
   mattered. Now the cached copy is still returned instantly (that is the point
   of installing it), but a fresh copy is fetched in the background and written
   over it, so the next launch is current. One launch behind, never stuck.

   Bump SHELL when you want to force an immediate purge rather than waiting for
   the next launch. */
const SHELL = "fw-shell-v2", DATA = "fw-data-v1";
const ASSETS = ["./", "./index.html", "./app.css", "./app.js", "./manifest.json",
                "./icon-192.png", "./icon-512.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(ASSETS))
    .then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(k => Promise.all(
    k.filter(x => x !== SHELL && x !== DATA).map(x => caches.delete(x))
  )).then(() => self.clients.claim()));
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;

  if (url.pathname.endsWith("data.json")) {
    e.respondWith(fetch(e.request).then(r => {
      const copy = r.clone();
      caches.open(DATA).then(c => c.put("data.json", copy));
      return r;
    }).catch(() => caches.match("data.json")));
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
