/* Shell cache-first so it opens instantly; data network-first so you get the
   newest committed reading when online and the last one when you're not. */
const SHELL = "fw-shell-v1", DATA = "fw-data-v1";
const ASSETS = ["./","./index.html","./app.css","./app.js","./manifest.json",
                "./icon-192.png","./icon-512.png"];
self.addEventListener("install", e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(ASSETS)).then(() => self.skipWaiting()));
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
  e.respondWith(caches.match(e.request, {ignoreSearch:true}).then(h => h || fetch(e.request)));
});
