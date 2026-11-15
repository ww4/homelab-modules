// Service worker: makes the page installable and keeps the shell (page,
// script, styles, icon) available offline. Live data (now/*, receiver/*,
// admin/api/*, the streams) is never cached — always the network.
const SHELL = "radio-shell-v6";
const FILES = ["./", "index.html", "app.js", "remote.css", "vendor/vue.global.prod.js", "icon.svg", "manifest.webmanifest"];
self.addEventListener("install", e => { e.waitUntil(caches.open(SHELL).then(c => c.addAll(FILES)).then(() => self.skipWaiting())); });
self.addEventListener("activate", e => { e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== SHELL).map(k => caches.delete(k)))).then(() => self.clients.claim())); });
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (/\/(now|receiver|admin|radio|icecast-status)/.test(url.pathname)) return;   // live: network only
  e.respondWith(fetch(e.request).then(r => { const copy = r.clone(); caches.open(SHELL).then(c => c.put(e.request, copy)); return r; })
                                .catch(() => caches.match(e.request)));
});
