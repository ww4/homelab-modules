// Library radio — the remote (Vue 3 global build, no bundler). Same-origin data:
//   now/catalogue.json, now/internet-radio.json, now/stations.json, icecast-status
//   now/<mount>.json (last played), now/<mount>-next.json (the DJ's queue)
//   receiver/*      the receiver's JSON API (yamaha-ync-api via nginx)
//   admin/api/dj/*, admin/api/dislike, admin/api/search   listener feedback
// Two targets: "here" plays the stream in this browser; "room" drives the
// receiver — a station tile walks its NET RADIO menu, a Pandora tile its
// Pandora list, a preset tile the tuner.

const { createApp } = Vue;

// How long a note left for the other view stays good. See noteHandover().
const HANDOVER_MS = 90000;

// a stable colour per name, for tiles and the hero
function hue(name) { let h = 0; for (const c of name) h = (h * 31 + c.charCodeAt(0)) % 360; return h; }

createApp({
  // Everything the two pages agree on to the byte lives in shared.js.
  // Anything defined below overrides it — which is how the members that
  // genuinely differ stay different.
  mixins: [RadioShared],
  // The remote is shared markup, fetched the first time it is opened. The
  // wide page mounts the same component from the same file.
  components: {
    "remote-panel": remotePanel("remote-panel.html"),
    "ask-panel": askPanel("ask-panel.html"),
  },
  data() {
    let quality = "", target = "here", tab = "now", last = { here: "", room: null, local: "" };
    try { quality = localStorage.getItem("radio.quality") || ""; target = localStorage.getItem("radio.target") || "here"; tab = localStorage.getItem("radio.tab") || "now"; last = { ...last, ...JSON.parse(localStorage.getItem("radio.last") || "{}") }; } catch (e) {}
    return { tiles: {}, stations: [], quick: [], counts: {}, up: {}, histories: {}, nexts: {}, current: null, status: "", quality, scanning: false, last,
             blocked: "",      // a mount the browser refused to start on its own; tap the tile to take it
             paused: false,    // paused by the OS transport: still tuned, still holding the media session
             ctx: null, analyser: null, raf: 0, wide: window.innerWidth > 640,
             target, tab, receiver: { name: "", on: false, input: "", volume: 0, mute: false, error: "" }, inputs: [], presets: [],
             speaker: { playing: false, mount: "", volume: null, muted: false, error: "" }, speakerPoll: 0,
             pandora: JSON.parse((() => { try { return localStorage.getItem("radio.pandora") || "[]"; } catch (e) { return "[]"; } })()), menu: { lines: [], layer: 0, max_line: 0, current_line: 1, status: "", name: "" }, menuSource: "",
             site: { title: "Radio", localName: "These speakers", roomName: "Living room", localMount: "" },
             speakerDraft: 0, speakerDragging: false, hearts: {},
             remote: false, volumeDraft: 0, freqDraft: "", busy: "", toast: "", query: "", results: [], searchTimer: 0, roomPoll: 0, artFailed: "", artFailedAt: 0, tick: 0 };
  },
  computed: {
    nowTitle() { const m = this.current; return (this.up[m + this.quality] || this.up[m] || {}).title || ""; },
    nowLine2() {
      if (this.target === "local") return this.speaker.error || (this.speaker.playing ? `on ${this.site.localName}` : "stopped");
      if (this.nowKind === "ambient") return "on a loop";
      if (this.target === "here") { const n = this.listenersOf(this.current); return n ? `${n} listening` : ""; }
      if (this.nowKind === "tuner") return this.receiver.tuner.tuned ? (this.receiver.tuner.stereo ? "stereo" : "mono") : "no signal";
      const np = this.np; if (!np) return "";
      return this.nowKind === "pandora" ? [np.artist, np.album].filter(Boolean).join(" · ") : (np.artist || "");
    },
    // The station being heard, ambient beds included — `feedbackMount` excludes
    // them, because they have no DJ to talk to.
    heroMount() {
      return this.target === "local" ? (this.speaker.mount || "")
           : this.target === "here" ? (this.current || "")
           : (this.roomMount || "");
    },
    // What the now-playing hero shows: the track's own cover, else a picture
    // chosen for the station. An ambient bed has no track and so no cover, and
    // fell through to the monogram even though the station has a picture of its
    // own (2026-09-29). Only `art: true` tiles qualify — a mosaic would
    // put some other album on screen as though it were playing.
    heroArt() {
      if (this.artOk()) return this.art;
      const t = this.heroMount && this.tiles[this.heroMount];
      return t && t.art && (t.covers || []).length ? this.thumb(t.covers[0], 700) : "";
    },
    segment() { const seg = this.feedbackNext.segment; return seg && seg.name ? seg : null; },
    segmentLine() {
      const seg = this.segment; if (!seg) return "";
      return seg.kind === "artist" ? `${seg.name.replace(/ spotlight$/i, "")} spotlight` : seg.name;
    },
    heroStyle() { const h = hue(this.nowStation || "radio"); return { background: `linear-gradient(160deg, hsl(${h} 45% 34%), hsl(${(h + 40) % 360} 55% 18%))` }; },
    // --- nothing chosen yet: the play button starts what played last on this target
    idle() {
      if (this.target === "local") return !this.speaker.mount;
      if (this.target === "here") return !this.current;
      const np = this.np;
      return this.receiver.on && this.receiver.input === "NET RADIO" && !!np && np.playback === "Stop" && !np.station;
    },
    lastStation() {
      if (this.target === "local") return this.stations.find(s => s.mount === (this.last.local || this.site.localMount)) || null;
      if (this.target === "here") return this.stations.find(s => s.mount === this.last.here) || null;
      const l = this.last.room; if (!l) return null;
      for (const g of this.groups) { const s = g.stations.find(s => s.kind === l.kind && s.name === l.name); if (s) return s; }
      return l.kind ? l : null;   // not in today's lists (a Pandora station since removed, say): still worth a try
    },
  },
  watch: {
    target(t) {
      try { localStorage.setItem("radio.target", t); } catch (e) {}
      clearInterval(this.roomPoll); clearInterval(this.speakerPoll);
      if (t !== "room" && this.tab === "sources") this.tab = "now";
      if (t === "room") this.pollReceiver();
      if (t === "local") this.pollSpeaker();
    },
    tab(t) { try { localStorage.setItem("radio.tab", t); } catch (e) {} if (t === "sources") this.loadMenu(); },
    "receiver.volume"(v) { this.volumeDraft = v; },
    "receiver.input"(i) { if (i === "TUNER" && this.receiver.tuner) this.freqDraft = this.freqText; if (this.tab === "sources") this.loadMenu(); },
  },
  methods: {
    tileIcon(s) { return s.mount && this.tiles[s.mount] ? (this.tiles[s.mount].icon || "") : ""; },
    tileStyle(s) { const h = hue(s.name); return { background: `linear-gradient(160deg, hsl(${h} 40% 30%), hsl(${(h + 40) % 360} 50% 17%))` }; },
    label(it) { return it.kind === "break" ? "station break" : (it.artist ? `${it.artist} — ${it.title}` : it.title); },
    // here → the living room → this box's own speakers → back, skipping the
    // ones this install does not have (site.json says which exist)
    pickTarget() {
      const order = ["here"].concat(this.site.hasRoom ? ["room"] : [], this.site.hasLocal ? ["local"] : []);
      this.target = order[(order.indexOf(this.target) + 1) % order.length];
      this.say({ here: "playing on this phone", room: `controlling the ${this.receiver.name || "receiver"}`,
                 local: `controlling ${this.site.localName}` }[this.target]);
    },
    targetName() { return { here: "This phone", room: this.receiver.name || this.site.roomName, local: this.site.localName }[this.target]; },

    // ---- this box's own audio output ------------------------------------------
    async pollSpeaker() {
      const st = await getJSON("speaker/state");
      this.speaker = st ? { ...st, error: st.error || "" } : { ...this.speaker, error: "unreachable" };
      // the slider is a DRAFT: adopt the reported level only when the user is
      // not holding it, or the ten-second poll drags it back mid-gesture and
      // the control feels dead (2026-09-26)
      if (st && !this.speakerDragging) this.speakerDraft = st.volume ?? 0;
      clearInterval(this.speakerPoll);
      this.speakerPoll = setInterval(async () => {
        const s = await getJSON("speaker/state");
        if (s) { this.speaker = { ...s, error: s.error || "" }; if (!this.speakerDragging) this.speakerDraft = s.volume ?? this.speakerDraft; }
      }, 10000);
    },
    speakerSet(level) {
      this.speakerDraft = Math.max(0, Math.min(100, Math.round(level)));
      return this.speakerAction("", () => call("POST", "speaker/volume", { level: this.speakerDraft }));
    },
    speakerVolume(step) { return this.speakerSet((this.speaker.volume ?? this.speakerDraft) + step); },


    async refresh() {
      const [cat, quick, counts, ic] = await Promise.all([getJSON("now/catalogue.json"), getJSON("now/internet-radio.json"), getJSON("now/stations.json"), getJSON("icecast-status")]);
      if (cat) { this.stations = cat; this.scanning = false; } else if (!this.stations.length) this.scanning = true;
      if (quick) this.quick = quick;
      if (!this._tilesAt || Date.now() - this._tilesAt > 600000) { const t = await getJSON("now/tiles.json"); if (t) { this.tiles = t; this._tilesAt = Date.now(); } }
      if (counts) this.counts = counts;
      if (ic) this.up = mountsOf(ic);
      const m = this.feedbackMount;
      if (m) {
        const [h, n] = await Promise.all([getJSON(`now/${m}.json`), getJSON(`now/${m}-next.json`)]);
        if (h) this.histories[m] = h;
        if (n) this.nexts[m] = n;
      }
    },

    // ---- this phone ----------------------------------------------------------
    remember(s) {
      this.last = { ...this.last, [this.target]: this.target === "room" ? { kind: s.kind, name: s.name, mount: s.mount, number: s.number } : s.mount };
      try { localStorage.setItem("radio.last", JSON.stringify(this.last)); } catch (e) {}
    },
    resume() { const s = this.lastStation; if (s) this.playTarget(s); else this.tab = "stations"; },
    play(m) {
      const audio = this.$refs.audio;
      const s = this.stations.find(s => s.mount === m); if (s) this.remember(s);
      this.current = m; this.paused = false; this.status = "tuning…"; this._reTries = 0; clearTimeout(this._reTimer); this.tab = "now";
      audio.src = this.streamUrl(m);
      audio.play().then(() => { this.status = ""; this.blocked = ""; this.noteHandover(); this.$nextTick(() => this.startViz()); })
                  .catch(err => {
                    // A browser will not start audio by itself unless it has
                    // decided this site is one you play audio on. When it
                    // refuses, say so plainly and leave the button ready.
                    if (err && err.name === "NotAllowedError") {
                      this.blocked = m; this.current = null;
                      this.status = "press play to pick it back up";
                    } else {
                      this.status = `couldn't start (${err.message})`;
                    }
                  });
      this.refresh();
    },
    stop() { const a = this.$refs.audio; a.pause(); a.removeAttribute("src"); a.load(); this.current = null; this.paused = false; this.blocked = ""; this.forgetHandover(); clearTimeout(this._reTimer); this._reTries = 0; this.stopViz(); this.mediaSession(); },

    // The OS transport — the headset button, the lock screen, the keyboard's
    // play key — talks to whichever page holds the media session, and a page
    // only holds one while it has media loaded. `stop()` throws the src away,
    // which ends the session: the key then has nothing to come back to and
    // pressing play did nothing (2026-09-29). So the key PAUSES.
    pauseHere() {
      const a = this.$refs.audio;
      if (!a || !this.current || this.paused) return;
      a.pause();
      this.paused = true;
      clearTimeout(this._reTimer);      // a pause is not a dropped stream
      this.stopViz();
      this.mediaSession();
    },
    resumeHere() {
      if (this.current) { this.paused = false; return this.play(this.current); }
      const s = this.lastStation; if (s) this.playTarget(s);
    },

    playTarget(s) {
      this.tab = "now";              // always show the switch happen
      if (this.target === "local") { if (!s.mount) return; this.remember(s); return this.speakerPlay(s.mount); }
      if (this.target !== "room") return this.play(s.mount);
      this.remember(s);
      if (s.kind === "preset") return this.startOnReceiver("tuning…", async () => { if (this.receiver.input !== "TUNER") await call("POST", "receiver/input", { name: "TUNER" }); return call("POST", "receiver/tuner", { preset: s.number }); });
      if (s.kind === "pandora") return this.startOnReceiver(`starting ${s.name}…`, () => call("POST", "receiver/menu/path", { source: "Pandora", path: [s.name] }));
      const category = s.kind === "quick" ? "Internet Radio" : s.kind === "specialty" ? "Specialty" : "Curated";
      return this.startOnReceiver(`tuning to ${s.name}…`, () => call("POST", "receiver/menu/path", { path: ["My Stations", category, s.name] }));
    },

    // ---- the receiver --------------------------------------------------------
    async pollReceiver() {
      const st = await getJSON("receiver/status");
      if (!st) { this.receiver = { ...this.receiver, error: "unreachable" }; return; }
      this.receiver = { ...st, error: "" };
      // the station just became known (cold load): fetch its history now,
      // don't wait for the 10 s refresh — the cover comes from it
      const m = this.feedbackMount;
      if (m && !this.histories[m]) this.refresh();
      if (!this.inputs.length) this.inputs = (await getJSON("receiver/inputs")) || [];
      if (st.on && !this.presets.length) this.presets = (await getJSON("receiver/tuner/presets")) || [];
      if (st.on && st.input === "Pandora" && !this._pandoraFresh) { this._pandoraFresh = true; await this.loadPandora(); }
      clearInterval(this.roomPoll);
      this.roomPoll = setInterval(() => { if (this.target === "room" && !this.busy) this.pollReceiver(); }, 5000);
    },
    remoteSource() {
      const map = { "NET RADIO": "NET_RADIO", "Pandora": "Pandora", "Spotify": "Spotify",
                    "SERVER": "SERVER", "AirPlay": "AirPlay", "TUNER": "Tuner" };
      return map[this.receiver.input] || "";
    },
    band(b) { return this.receiverAction("", () => call("POST", "receiver/tuner", { band: b, frequency: b === "FM" ? 93.1 : 1300 })); },
    volumeStep(step) {
      if (this.target === "local") return this.speakerVolume(step);
      this.volumeDraft = Math.max(0, Math.min(this.receiver.volume_max || 100, this.volumeDraft + step));
      return this.setVolume(this.volumeDraft);
    },
    tuneTo() { const t = this.receiver.tuner; return this.receiverAction("tuning…", () => call("POST", "receiver/tuner", { band: t.band, frequency: parseFloat(this.freqDraft) })); },

    // ---- feedback on a library station ---------------------------------------
    nowTrack() { const h = this.feedbackHistory; return (h && h[0] && h[0].kind !== "break") ? h[0] : null; },


    // ---- keeping the stream alive across a restart -----------------------
    // Every deploy restarts Liquidsoap, which drops every listener. The page
    // used to set "stream error — try again" and stop, so a browser left
    // playing went quiet and somebody had to find the tab and press play
    // (2026-09-29). It reconnects itself now.
    //
    // Re-requesting the mount also WAKES it: nginx fires the wake on
    // /radio/<mount>.mp3, so a retry both restarts the encoder and reattaches
    // to it. Backs off 1, 2, 4, 8 s and then every 8 s, because the encoder is
    // genuinely absent for a few seconds and hammering it helps nobody.
    reconnect() {
      const m = this.current;
      if (!m) return;                       // stopped on purpose: stay stopped
      clearTimeout(this._reTimer);
      if (this._reTries > RECONNECT_GIVE_UP) {
        this.status = "stream lost — press play";
        return;
      }
      const audio = this.$refs.audio;
      audio.src = this.streamUrl(m);        // a fresh URL, so nothing is cached
      audio.play()
        .then(() => { this.status = ""; this._reTries = 0; this.$nextTick(() => this.startViz()); })
        .catch(() => this.scheduleReconnect());
    },

  },
  mounted() {
    const audio = this.$refs.audio;
    // "ended" is what a clean Liquidsoap shutdown looks like to the element;
    // "error" is what a mid-flight drop looks like. Both mean reconnect.
    audio.addEventListener("error", () => this.scheduleReconnect());
    audio.addEventListener("ended", () => this.scheduleReconnect());
    audio.addEventListener("waiting", () => { this.status = "buffering…"; });
    audio.addEventListener("playing", () => { this.status = ""; this._reTries = 0; });
    window.addEventListener("resize", () => { this.wide = window.innerWidth > 640; });
    // Leaving for the desktop view — or simply reloading. `pagehide` rather
    // than `unload`, which a bfcache-ing browser may never fire.
    window.addEventListener("pagehide", () => this.noteHandover());
    this.takeHandover();
    // who this box is, written at build time from the module's options
    getJSON("site.json").then(s => { if (s) { this.site = { ...this.site, ...s }; document.title = this.site.title; } });
    this.setMediaHandlers();
    this.refresh().then(() => { this.refreshHeart(); this.mediaSession(); });
    setInterval(() => { this.tick = Date.now(); this.refresh().then(() => { this.refreshHeart(); this.mediaSession(); }); }, 10000);
    if (this.target === "room") this.pollReceiver();
    if (this.target === "local") this.pollSpeaker();
    if (this.tab === "sources" && this.target !== "room") this.tab = "now";
    if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
  },
}).mount("#app");
