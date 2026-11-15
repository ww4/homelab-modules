// Shared by both pages. Everything here is transport: the pages differ in what
// they show, not in how they ask for it, and these three were byte-identical
// in app.js and desktop.js until 2026-09-29.
//
// Loaded before the page script, so these are plain globals. There is no
// bundler here on purpose — the vhost is Tailscale-only and the pages are
// served as files.

async function getJSON(url) {
  try { const r = await fetch(url, { cache: "no-store" }); return r.ok ? await r.json() : null; } catch (e) { return null; }
}

async function call(method, url, body) {
  const r = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });
  let data = null; try { data = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error((data && (data.error || data.hint)) || `${method} ${url}: ${r.status}`);
  return data;
}

// Icecast's status JSON, reduced to {mount: {title, listeners}}. A single
// source comes back as an object rather than a one-element array.
function mountsOf(status) {
  let src = status && status.icestats && status.icestats.source;
  if (!src) return {};
  if (!Array.isArray(src)) src = [src];
  const out = {};
  for (const s of src) { const m = (s.listenurl || "").split("/").pop().replace(/\.mp3$/, ""); out[m] = { title: s.title || "", listeners: s.listeners | 0 }; }
  return out;
}
