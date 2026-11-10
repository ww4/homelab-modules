/* Service worker: read offline, and keep the app shell instant.
 *
 * ⚠️ THIS ORIGIN PREVIOUSLY RAN SILVERBULLET'S PWA. notes.example.com
 * served SilverBullet until 2026-11-09, so a phone that installed it still has
 * that service worker and its caches registered here. This worker therefore
 * claims control immediately and deletes every cache that is not ours —
 * otherwise the old worker keeps answering and serves a dead app's shell.
 */
var VERSION = "notes-v1";
var SHELL = VERSION + "-shell";
var DATA = VERSION + "-data";
var SHELL_URLS = ["/", "/manifest.webmanifest", "/icon.svg"];

self.addEventListener("install", function (ev) {
  ev.waitUntil(
    caches.open(SHELL).then(function (c) {
      return c.addAll(SHELL_URLS);
    }).then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (ev) {
  ev.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.map(function (k) {
        if (k !== SHELL && k !== DATA) return caches.delete(k);
      }));
    }).then(function () { return self.clients.claim(); })
  );
});

function networkFirst(req, cacheName) {
  return fetch(req).then(function (res) {
    if (res && res.ok) {
      var copy = res.clone();
      caches.open(cacheName).then(function (c) { c.put(req, copy); });
    }
    return res;
  }).catch(function (err) {
    return caches.match(req).then(function (hit) {
      if (hit) return hit;
      throw err;
    });
  });
}

self.addEventListener("fetch", function (ev) {
  var req = ev.request;
  if (req.method !== "GET") return;                 // writes are the outbox's job
  var url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // The listing and single notes: fresh when we can, last-known when we can't.
  if (url.pathname === "/api/notes" || url.pathname === "/api/note") {
    ev.respondWith(networkFirst(req, DATA));
    return;
  }
  // Never cache the health probe — a cached 200 would claim the box is up.
  if (url.pathname === "/healthz") return;

  // The shell: serve instantly from cache, refresh in the background.
  if (url.pathname === "/" || SHELL_URLS.indexOf(url.pathname) >= 0) {
    ev.respondWith(
      caches.match(req).then(function (hit) {
        var net = fetch(req).then(function (res) {
          if (res && res.ok) {
            var copy = res.clone();
            caches.open(SHELL).then(function (c) { c.put(req, copy); });
          }
          return res;
        }).catch(function () { return hit; });
        return hit || net;
      })
    );
  }
});
