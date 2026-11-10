/* SilverBullet's service worker URL, replaced by a self-destructing one.
 *
 * notes.example.com served SilverBullet's PWA until 2026-11-09. Its
 * worker registered at THIS path with scope "/" and is still installed in
 * every browser that ever opened the old app. It intercepts navigations and
 * answers them from its own cache, whose assets are now gone — which is why
 * a plain refresh gives a blank page with the SilverBullet favicon while
 * shift-refresh (which bypasses the worker) shows the real app.
 *
 * That also makes the problem self-sustaining: the dead shell never runs the
 * new app's JavaScript, so the new app can never register a worker of its own
 * to take over. The only thing the browser still fetches directly, bypassing
 * the worker entirely, is the worker SCRIPT — so the fix has to live here.
 *
 * Serving a 404 here is NOT reliable: browsers differ on whether a failed
 * update drops the registration. A valid worker that unregisters itself is
 * deterministic.
 */
self.addEventListener("install", function () { self.skipWaiting(); });

self.addEventListener("activate", function (ev) {
  ev.waitUntil((async function () {
    const keys = await caches.keys();
    await Promise.all(keys.map(function (k) { return caches.delete(k); }));
    await self.registration.unregister();
    // Reload whatever this worker was poisoning, now that it is gone.
    const clients = await self.clients.matchAll({ type: "window" });
    clients.forEach(function (c) { c.navigate(c.url); });
  })());
});

// Until activate finishes, pass everything straight to the network so a
// navigation in flight gets the real app rather than a dead cache entry.
self.addEventListener("fetch", function (ev) {
  ev.respondWith(fetch(ev.request));
});
