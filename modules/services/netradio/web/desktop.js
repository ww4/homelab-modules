// Library radio — the wide-screen page. Vue 3 global build, no bundler.
//
// Same-origin data, all of it written by the scanner or the DJ:
//   now/catalogue.json      the station list        now/stations.json   track counts
//   now/internet-radio.json the receiver's streams  now/tiles.json      cover art per station
//   now/<mount>.json        last played             now/<mount>-next.json  the DJ's queue
//   icecast-status          which mounts are up, their titles and listener counts
//   receiver/*              the receiver's JSON API (yamaha-ync-api via nginx)
//   speaker/*               this box's own sound card (netradio-speaker)
//   admin/api/*             feedback, hearts, search, resume
//
// Three targets, unchanged: "here" plays the stream in this browser, "room"
// drives the receiver by walking its NET RADIO menu, "local" drives this
// machine's sound card.
//
// Two surfaces: the WALL (every station and what it last played) and the
// STATION screen (one station's artwork, queue and actions), plus SOURCES for
// the receiver's own inputs and menus. The transport bar is common to all
// three and is a grid row, so nothing ever scrolls underneath it.
//
// This page was a Pico-CSS list of station names until 2026-09-28, and had
// never gained the heart, the artwork, the remote or resume that the phone
// page grew — a wide screen is redirected here automatically, so it lost them.

const { createApp } = Vue;

// Carrying the stream across the view switch.
//
// The phone remote and this page are separate documents. Following "Phone
// view" or "Desktop view" is a real navigation, the document is torn down, and
// the <audio> element goes with it — so the stream stops. The owner, reasonably:
// "it shouldn't need to drop the stream just to change CSS around" (2026-09-29).
// It isn't CSS, but it shouldn't cost him the stream either.
//
// So the page on the way out writes down what it was playing, and the page on
// the way in picks it up. The note is short-lived on purpose: a window this
// small covers a click from one view to the other (and a reload, which gets
// the same treatment for free) while a tab opened tomorrow starts silent.
const HANDOVER_MS = 90000;

createApp({
  // Everything the two pages agree on to the byte lives in shared.js.
  // Anything defined below overrides it — which is how the members that
  // genuinely differ stay different.
  mixins: [RadioShared],
  // The admin is a component of this page, not a page of its own. Reaching
  // it is a view change, so nothing is navigated and the <audio> element —
  // and the stream in it — is untouched (2026-09-29). The markup is
  // fetched the first time it is opened and not before.
  components: {
    "admin-panel": adminPanel("admin/panel.html", "admin/"),
    // the same remote the phone mounts, from the same file
    "remote-panel": remotePanel("remote-panel.html"),
    "ask-panel": askPanel("ask-panel.html"),
  },
  data() {
    let quality = "", target = "here", view = "wall", openMount = "", hereVol = 100, bedVol = 35, bedOn = false, notify = false;
    try {
      hereVol = Math.max(0, Math.min(100, parseInt(localStorage.getItem("radio.volume"), 10) || 100));
      bedVol = Math.max(0, Math.min(100, parseInt(localStorage.getItem("radio.bedVolume"), 10) || 35));
      bedOn = localStorage.getItem("radio.bed") === "1";
      quality = localStorage.getItem("radio.quality") || "";
      notify = localStorage.getItem("radio.notify") === "1";
      target = localStorage.getItem("radio.target") || "here";
      view = localStorage.getItem("radio.desktopView") || "wall";
      openMount = localStorage.getItem("radio.openMount") || "";
    } catch (e) {}
    if (view === "sources") view = "wall";          // needs a live receiver; never land on it
    return {
      view, qtab: "played", openMount, target, quality,
      stations: [], quick: [], counts: {}, up: {}, tiles: {}, histories: {}, nexts: {}, hearts: {},
      current: null, status: "", scanning: false, busy: "", toast: "", tick: 0,
      blocked: "",              // a mount the browser refused to start on its own; the play button takes it
      paused: false,            // paused by the OS transport: still tuned, and still holding the media session
      query: "", results: [], searchTimer: 0,
      site: { title: "Radio", localName: "These speakers", roomName: "Living room", localMount: "", hasLocal: false, hasRoom: true, hasRoku: false, rokuName: "Roku", bedMount: "", tvInput: "" },
      receiver: { name: "", on: false, input: "", volume: 0, volume_max: 100, mute: false, error: "" },
      inputs: [], presets: [], menu: { lines: [], layer: 0, max_line: 0, current_line: 1, status: "", name: "" }, menuSource: "",
      // The receiver's Pandora stations. Walking its menu to list them takes a
      // few seconds, so the last answer is kept and shown straight away.
      pandora: JSON.parse((() => { try { return localStorage.getItem("radio.pandora") || "[]"; } catch (e) { return "[]"; } })()),
      speaker: { playing: false, mount: "", volume: null, muted: false, error: "" },
      remote: false, volDraft: 0, volDragging: false,
      // This browser's own output. The receiver and the sound card have a
      // volume; the tab had none, so the only way down was the OS mixer
      // (2026-09-29). Kept in localStorage so a reload is not a
      // surprise at full volume.
      hereVol, hereMuted: false,
      // The rain bed: a SECOND stream, mixed here rather than at the station.
      // Mixing it into the broadcast would put rain under everyone who tuned
      // in — including a receiver that cannot turn it off — and one level
      // cannot suit both a soft track and a loud one (2026-09-29).
      bedOn, bedVol,
      roomPoll: 0, speakerPoll: 0, ctx: null, analyser: null, raf: 0,
      // Whether each output has actually answered yet. The rail shows all
      // of them at once, and until 2026-09-29 it rendered the ones that
      // were not selected straight from these defaults — so a speaker that
      // was playing read "stopped" until you clicked it. A status nobody
      // asked for is a guess, and it should not look like a fact.
      speakerSeen: false, receiverSeen: false,
      // covers that 404'd, by path. Plenty of tracks have no embedded picture
      // and no folder cover, and an <img> that fails renders the browser's
      // broken-image glyph — worse than the coloured monogram it should fall
      // back to. Retried after 30 s so a slow art service is not written off.
      badArt: {},
      // matches index.html's own wide-screen test, so the two pages agree
      // about what counts as a phone
      narrow: !window.matchMedia("(min-width: 900px)").matches,
      drawer: false,            // the rail, as an off-canvas sheet on a narrow screen
      // Windows shows no per-track toast of its own — the media session gives
      // it transport controls, not a notice. The owner wanted the same thing the
      // phone does: "it would just be handy to see what came on the radio"
      // (2026-09-29). Off until asked for, because a permission prompt nobody
      // invited is rude, and the browser only grants one from a gesture anyway.
      notify,
      notifyState: (typeof Notification !== "undefined" ? Notification.permission : "unsupported"),
    };
  },

  computed: {
    // ---- the rail's zone list -------------------------------------------
    zones() {
      const z = [{ id: "here", name: "This browser", live: !!this.current,
                   sub: this.current ? `playing ${this.currentStation.name}` : (this.status || "idle"), dead: false }];
      // Named for the EQUIPMENT, with the room underneath. "Living room" is
      // where it is, not what it is, and a list of places cannot tell you
      // which box a button is about to talk to: "the Living Room device should
      // be the name of the receiver, like the Roku Express 4K+ label"
      // (2026-10-21). The model comes from the receiver itself.
      if (this.site.hasRoom) z.push({
        id: "room", name: this.receiverLabel(),
        live: this.receiverSeen && this.receiver.on && this.playbackState === "Play",
        sub: !this.receiverSeen ? "checking…"
           : this.receiver.error ? this.receiver.error
           : this.where(this.receiver.on ? this.receiver.input : "standby"),
        dead: this.receiver.error === "unreachable" });
      if (this.site.hasLocal) z.push({
        id: "local", name: this.site.localName, live: this.speakerSeen && !!this.speaker.playing,
        sub: !this.speakerSeen ? "checking…"
           : this.speaker.error || (this.speaker.playing ? "playing" : "stopped"),
        dead: this.speaker.error === "unreachable" });
      return z;
    },
    targetName() { return (this.zones.find(z => z.id === this.target) || {}).name || ""; },


    // ---- what is PLAYING on the chosen target ----------------------------
    playbackState() { return ((this.receiver.now_playing || {}).playback) || ""; },
    // the actions on the station screen act on the PLAYING track, so they are
    // only offered when the station being LOOKED at is the one being heard
    canActHere() { return this.canDJ && this.openMount === this.feedbackMount; },
    anythingPlaying() {
      return this.target === "room" ? (this.receiver.on && this.playbackState === "Play")
           : this.target === "local" ? !!this.speaker.playing
           : (!!this.current && !this.paused);
    },
    barSub() {
      const bits = [];
      if (this.nowStation) bits.push(this.nowStation);
      if (this.anythingPlaying && this.playingMount && this.isUp(this.playingMount)) bits.push("on air");
      const n = this.playingMount ? this.listenersOf(this.playingMount) : 0;
      if (n) bits.push(`${n} listening`);
      if (!bits.length) bits.push(this.targetName);
      return bits.join(" · ");
    },
    // What a track notice is ABOUT. Derived rather than watched directly, so a
    // refresh that returns the same track is not a change — the history is
    // re-fetched every ten seconds and would otherwise notify on every poll.
    noticeKey() {
      const t = this.playingTrack;
      return this.anythingPlaying && t && t.title ? `${t.artist}\u0000${t.title}` : "";
    },
    barCovers() {
      const t = this.playingTrack;
      const own = this.goodArt(t && t.path ? [t.path] : []);
      if (own.length) return own;
      const m = this.playingMount;
      return this.goodArt(m && this.tiles[m] ? (this.tiles[m].covers || []) : []);
    },

    // ---- what is being LOOKED at (the station screen) ---------------------
    openStationObj() { return this.stations.find(s => s.mount === this.openMount) || { name: "", mount: "" }; },
    openStationName() { return this.openStationObj.name; },
    viewTrack() {
      const h = this.histories[this.openMount] || [];
      return (h[0] && h[0].kind !== "break") ? h[0] : null;
    },
    viewTitle() {
      const t = this.viewTrack;
      if (t) return t.title;
      return (this.up[this.openMount] || this.up[this.openMount + "-lo"] || {}).title || "";
    },
    viewArtist() { const t = this.viewTrack; return t ? (t.artist || "") : ""; },
    // the track's own cover if it has one, else the station's mosaic
    heroCovers() {
      const t = this.viewTrack;
      const own = this.goodArt(t && t.path ? [t.path] : []);
      if (own.length) return own;
      return this.goodArt(this.openMount && this.tiles[this.openMount] ? (this.tiles[this.openMount].covers || []) : []);
    },
    // The right column follows the station being LOOKED at, not the one being
    // heard: opening a station you are not listening to and being shown some
    // other station's queue is simply wrong.
    viewHistory() { return this.histories[this.openMount] || []; },
    viewNext() { return this.nexts[this.openMount] || {}; },
    // Keyed to the STATION, not the track: on a radio the station is the
    // long-lived identity, so its colour holds steady while songs change
    // inside it (Plexamp's UltraBlur, applied to the thing that persists).
    washStyle() {
      const c = this.openMount && this.tiles[this.openMount] ? (this.tiles[this.openMount].covers || []) : [];
      return c.length ? { backgroundImage: `url("${this.thumb(c[0], 500)}")` } : {};
    },
    techLine() {
      const bits = [];
      if (this.target === "here") bits.push(this.quality === "-lo" ? "MP3 96 kbps" : "MP3 192 kbps");
      const t = this.openMount === this.feedbackMount ? this.playingTrack : this.viewTrack;
      if (t && t.at) {
        const secs = Math.max(0, Math.floor(this.tickNow / 1000 - t.at));
        if (secs < 3600) bits.push(`${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")} into this track`);
      }
      return bits.join(" · ");
    },

    // ---- the transport bar's volume, whichever target is chosen ----------
    volumeMax() { return this.target === "room" ? (this.receiver.volume_max || 100) : 100; },
    isMuted() {
      return this.target === "room" ? !!this.receiver.mute
           : this.target === "local" ? !!this.speaker.muted
           : this.hereMuted;
    },
  },

  watch: {
    target(t) {
      try { localStorage.setItem("radio.target", t); } catch (e) {}
      clearInterval(this.roomPoll); clearInterval(this.speakerPoll);
      if (t !== "room" && this.view === "sources") this.view = "wall";
      if (t === "room") this.pollReceiver();
      if (t === "local") this.pollSpeaker();
      this.syncVolume();
    },
    view(v) { try { localStorage.setItem("radio.desktopView", v); } catch (e) {} },
    noticeKey(now) { if (now) this.showTrackNotice(); },
    openMount(m) { try { localStorage.setItem("radio.openMount", m); } catch (e) {} if (m) this.refresh(); },
    "receiver.volume"() { this.syncVolume(); },
    "receiver.input"() { if (this.view === "sources") this.loadMenu(); },
    "speaker.volume"() { this.syncVolume(); },
  },

  methods: {
    // Hide just the tile that failed and remember the path. Hiding keeps a
    // four-up mosaic four-up: dropping the cover from the list instead would
    // re-lay the whole card out as a single image the moment one 404'd.
    imgError(path, ev) {
      if (ev && ev.target) ev.target.style.visibility = "hidden";
      this.badArt = { ...this.badArt, [path]: Date.now() };
    },
    goodArt(paths) {
      const now = this.tickNow;
      return (paths || []).filter(p => !this.badArt[p] || now - this.badArt[p] > 30000);
    },
    // In a two-line list row the artist has its own line, so the headline is
    // the title alone, with the artist beneath it. A break's own
    // title is the useful part ("Krüger Brothers spotlight"), not the word
    // "break", so it keeps it.
    rowTitle(it) { return it.kind === "break" ? (it.title || "station break") : it.title; },
    rowSub(it) { return it.kind === "break" ? "station break" : (it.artist || ""); },
    shortCount(m) { const c = this.counts[m]; return c ? (c.tracks >= 1000 ? (c.tracks / 1000).toFixed(1).replace(/\.0$/, "") + "k" : String(c.tracks)) : ""; },
    // what this station last played, for its card on the wall
    lastOf(m) { const h = this.histories[m]; return (h && h[0] && h[0].kind !== "break") ? h[0] : null; },
    focusSearch() { const el = document.querySelector(".search input"); if (el) el.focus(); },

    openStation(s) {
      // an entry with no mount of our own (an internet stream in the
      // receiver's menu) has no station screen to show: just tune it
      if (!s.mount) return this.playTarget(s);
      this.openMount = s.mount;
      this.view = "station";
      this.drawer = false;              // on a phone the sheet is in the way once you have chosen
    },

    // ---- loading ---------------------------------------------------------
    async refresh() {
      const [cat, quick, counts, ic] = await Promise.all([
        getJSON("now/catalogue.json"), getJSON("now/internet-radio.json"),
        getJSON("now/stations.json"), getJSON("icecast-status")]);
      if (cat) { this.stations = cat; this.scanning = false; } else if (!this.stations.length) this.scanning = true;
      if (quick) this.quick = quick;
      if (counts) this.counts = counts;
      if (ic) this.up = mountsOf(ic);
      // the cover manifest changes only when the library is rescanned
      if (!this._tilesAt || Date.now() - this._tilesAt > 600000) {
        const t = await getJSON("now/tiles.json");
        if (t) { this.tiles = t; this._tilesAt = Date.now(); }
      }
      // the station being heard and the one being looked at, every time
      const want = new Set([this.feedbackMount, this.openMount].filter(Boolean));
      for (const m of want) {
        const [h, n] = await Promise.all([getJSON(`now/${m}.json`), getJSON(`now/${m}-next.json`)]);
        if (h) this.histories[m] = h;
        if (n) this.nexts[m] = n;
      }
      // …and every station's last track, for the wall. One small file each and
      // they are static on the same nginx, but 28 of them is not something to
      // do on the 10 s tick, so it runs at most once a minute.
      if (this.view === "wall" && (!this._wallAt || Date.now() - this._wallAt > 60000)) {
        this._wallAt = Date.now();
        const mounts = this.stations.map(s => s.mount).filter(m => m && !want.has(m));
        const got = await Promise.all(mounts.map(m => getJSON(`now/${m}.json`)));
        const merged = { ...this.histories };
        mounts.forEach((m, i) => { if (got[i]) merged[m] = got[i]; });
        this.histories = merged;
      }
    },

    // ---- playing ---------------------------------------------------------
    play(m) {
      const audio = this.$refs.audio;
      this.current = m; this.paused = false; this.status = "tuning…"; this._reTries = 0; clearTimeout(this._reTimer);
      audio.src = this.streamUrl(m);
      this.applyHereVolume();
      this.$nextTick(() => this.applyBed());
      audio.play().then(() => { this.status = ""; this.blocked = ""; this.noteHandover(); this.$nextTick(() => this.startViz()); })
                  .catch(err => {
                    // A browser will not start audio by itself unless it has
                    // decided this site is one you play audio on. When it
                    // refuses, say so plainly and leave the button ready —
                    // never a dead page that looks like it should be playing.
                    if (err && err.name === "NotAllowedError") {
                      this.blocked = m; this.current = null;
                      this.status = "press play to pick it back up";
                    } else {
                      this.status = `couldn't start (${err.message})`;
                    }
                  });
      this.refresh();
    },
    stop() { const a = this.$refs.audio; a.pause(); a.removeAttribute("src"); a.load(); this.current = null; this.blocked = ""; this.forgetHandover(); clearTimeout(this._reTimer); this._reTries = 0; this.stopViz(); this.applyBed(); this.mediaSession(); },

    playTarget(s) {
      if (this.target === "room") return this.playOnReceiver(s);
      if (this.target === "local") return s.mount ? this.speakerPlay(s.mount) : undefined;
      return s.mount ? this.play(s.mount) : undefined;
    },
    // the bar's play button with nothing playing: start the station being
    // looked at, else this box's default, else the first in the list
    resumePlay() {
      // What the listener was actually hearing comes first — paused by the
      // media key, then a mount the browser refused to start for us.
      if (this.paused && this.current && this.target === "here") return this.resumeHere();
      if (this.blocked && this.target === "here") { const m = this.blocked; this.blocked = ""; return this.play(m); }
      const s = this.openStationObj.mount ? this.openStationObj
              : this.stations.find(x => x.mount === this.site.localMount) || this.stations[0];
      if (s) return this.playTarget(s);
    },
    playOnReceiver(s) {
      if (s.kind === "preset")
        return this.startOnReceiver("tuning…", async () => {
          if (this.receiver.input !== "TUNER") await call("POST", "receiver/input", { name: "TUNER" });
          return call("POST", "receiver/tuner", { preset: s.number });
        });
      if (s.kind === "pandora")
        return this.startOnReceiver(`starting ${s.name}…`,
          () => call("POST", "receiver/menu/path", { source: "Pandora", path: [s.name] }));
      const category = s.kind === "quick" ? "Internet Radio" : s.kind === "specialty" ? "Specialty" : "Curated";
      return this.startOnReceiver(`tuning the ${this.receiver.name || "receiver"} to ${s.name}…`,
        () => call("POST", "receiver/menu/path", { path: ["My Stations", category, s.name] }));
    },

    // ---- volume, for whichever target is chosen --------------------------
    syncVolume() {
      if (this.volDragging) return;          // never drag the slider out from under a finger
      this.volDraft = this.target === "room" ? Math.round(this.receiver.volume || 0)
                    : this.target === "local" ? (this.speaker.volume ?? 0)
                    : this.hereVol;
    },
    setLevel(level) {
      if (this.target === "room") return this.setVolume(level);
      if (this.target === "local") return this.speakerSet(level);
      return this.hereSet(level);
    },
    toggleMute() {
      if (this.target === "room") return this.mute(!this.receiver.mute);
      if (this.target === "local") return this.speakerMute(!this.speaker.muted);
      this.hereMuted = !this.hereMuted;
      this.applyHereVolume();
    },
    // ---- this browser's own volume ---------------------------------------
    hereSet(level) {
      this.hereVol = Math.max(0, Math.min(100, Math.round(level)));
      this.volDraft = this.hereVol;
      if (this.hereVol > 0) this.hereMuted = false;   // moving the slider up means unmute
      try { localStorage.setItem("radio.volume", String(this.hereVol)); } catch (e) {}
      this.applyHereVolume();
    },
    applyHereVolume() {
      const a = this.$refs.audio;
      if (!a) return;
      // HTMLMediaElement.volume is 0..1 and is NOT affected by the OS mixer,
      // so this is the tab's own level rather than the machine's.
      a.volume = this.hereMuted ? 0 : this.hereVol / 100;
      a.muted = this.hereMuted;
      this.applyBed();          // the bed is this browser's sound as well
    },

    // ---- this box's own sound card ---------------------------------------
    async pollSpeaker() {
      const st = await getJSON("speaker/state");
      this.speaker = st ? { ...st, error: st.error || "" } : { ...this.speaker, error: "unreachable" };
      this.speakerSeen = true;
      this.syncVolume();
      clearInterval(this.speakerPoll);
      this.speakerPoll = setInterval(async () => {
        if (this.target !== "local" || this.busy) return;
        const s2 = await getJSON("speaker/state");
        if (s2) { this.speaker = { ...s2, error: s2.error || "" }; this.syncVolume(); }
      }, 5000);
    },
    speakerSet(level) {
      this.volDraft = Math.max(0, Math.min(100, Math.round(level)));
      return this.speakerAction("", () => call("POST", "speaker/volume", { level: this.volDraft }));
    },
    speakerVolume(step) { return this.speakerSet((this.speaker.volume ?? this.volDraft) + step); },

    // ---- the outputs nobody has selected ---------------------------------
    // The rail lists every output at once, so every one of them needs a status,
    // not just the one being driven. The selected target keeps its own 5 s poll;
    // these ride the 10 s tick, which is plenty for "is it on".
    // Read-only: status endpoints only, nothing here changes what anything is
    // doing.
    async refreshZones() {
      if (this.busy) return;
      const jobs = [];
      if (this.site.hasLocal && this.target !== "local") jobs.push((async () => {
        const st = await getJSON("speaker/state");
        this.speaker = st ? { ...st, error: st.error || "" } : { ...this.speaker, error: "unreachable" };
        this.speakerSeen = true;
      })());
      if (this.site.hasRoom && this.target !== "room") jobs.push((async () => {
        const st = await getJSON("receiver/status");
        this.receiver = st ? { ...st, error: "" } : { ...this.receiver, error: "unreachable" };
        this.receiverSeen = true;
      })());
      await Promise.all(jobs);
    },

    // ---- the receiver ----------------------------------------------------
    // The television, as a row among the outputs. NOT a target: you cannot
    // send a station to it, so it is a line of status rather than a button —
    // clicking it would promise something that does not exist.
    tvZone() {
      const w = this.watching;
      if (w.playing) {
        const bits = [w.head, w.sub].filter(Boolean);
        return { name: this.rokuLabel() || "Television", sub: bits.join(" \u00b7 "), live: !w.paused };
      }
      if (this.site.hasRoku && this.roku.awake && this.roku.app)
        return { name: this.rokuLabel() || "Television", sub: this.roku.app, live: true };
      return null;
    },
    watchProgress() {
      const w = this.watching;
      if (!w.playing || !w.duration) return 0;
      return Math.max(0, Math.min(100, Math.round((w.position / w.duration) * 100)));
    },
    async pollReceiver() {
      const st = await getJSON("receiver/status");
      this.receiverSeen = true;
      if (!st) { this.receiver = { ...this.receiver, error: "unreachable" }; return; }
      this.receiver = { ...st, error: "" };
      this.syncVolume();
      if (!this.inputs.length) this.inputs = (await getJSON("receiver/inputs")) || [];
      if (st.on && !this.presets.length) this.presets = (await getJSON("receiver/tuner/presets")) || [];
      // Listing the Pandora stations means walking the receiver's menu, so it
      // is done once, the first time the receiver is actually ON Pandora.
      if (st.on && st.input === "Pandora" && !this._pandoraFresh) { this._pandoraFresh = true; await this.loadPandora(); }
      clearInterval(this.roomPoll);
      this.roomPoll = setInterval(() => { if (this.target === "room" && !this.busy) this.pollReceiver(); }, 5000);
    },
    volumeStep(step) {
      if (this.target === "local") return this.speakerVolume(step);
      this.volDraft = Math.max(0, Math.min(this.volumeMax, this.volDraft + step));
      return this.setVolume(this.volDraft);
    },





    // ---- telling you what came on ----------------------------------------
    // Windows gives a web page no per-track toast of its own: the media session
    // buys transport controls, not a notice. The Notification API is the thing
    // that does it, and it needs permission, which a browser grants only from a
    // gesture — so this hangs off the switch rather than off page load.
    async askNotify() {
      if (typeof Notification === "undefined") { this.say("This browser has no notifications."); return; }
      if (this.notify) { this.notify = false; this.saveNotify(); return; }
      if (Notification.permission === "denied") {
        this.notifyState = "denied";
        this.say("Notifications are blocked for this site — allow them in the browser's site settings.");
        return;
      }
      let state = Notification.permission;
      if (state !== "granted") {
        try { state = await Notification.requestPermission(); } catch (e) { state = "denied"; }
      }
      this.notifyState = state;
      this.notify = state === "granted";
      this.saveNotify();
      if (!this.notify) this.say("Notifications were not allowed.");
    },
    saveNotify() { try { localStorage.setItem("radio.notify", this.notify ? "1" : "0"); } catch (e) {} },
    showTrackNotice() {
      if (!this.notify || typeof Notification === "undefined" || Notification.permission !== "granted") return;
      if (!this._noticeReady) return;      // not for whatever was already playing when the page opened
      const t = this.playingTrack;
      if (!t || !t.title) return;
      try {
        // ONE notice at a time: the tag replaces the previous one instead of
        // stacking a tower of them through an evening's listening. It is a
        // notice, not an alert, so it makes no sound and steals no focus.
        const n = new Notification(t.title, {
          body: [t.artist, this.nowStation].filter(Boolean).join(" · "),
          icon: this.artOk() ? new URL(this.thumb(t.path, 192), location.href).href : undefined,
          tag: "netradio-track",
          renotify: false,
          silent: true,
        });
        n.onclick = () => { try { window.focus(); } catch (e) {} n.close(); };
      } catch (e) { /* a browser that refuses is not worth a broken page */ }
    },

    // ---- the rain bed ----------------------------------------------------
    // A second <audio> on its own mount, with its own level. It follows the
    // music: it plays while something is playing in this browser and stops
    // when that stops, so it never becomes rain on its own by accident.
    // Requesting the mount also wakes its encoder, the same as any other.
    bedToggle() {
      this.bedOn = !this.bedOn;
      try { localStorage.setItem("radio.bed", this.bedOn ? "1" : "0"); } catch (e) {}
      this.applyBed();
    },
    bedSet(level) {
      this.bedVol = Math.max(0, Math.min(100, Math.round(level)));
      try { localStorage.setItem("radio.bedVolume", String(this.bedVol)); } catch (e) {}
      this.applyBed();
    },
    applyBed() {
      const b = this.$refs.bed;
      if (!b || !this.site.bedMount) return;
      const wanted = this.bedOn && this.target === "here" && !!this.current && !this.paused;
      // Mute is this browser going quiet, so it has to reach the bed too. It
      // muted only the music, and the rain carried on over the silence
      // (2026-09-29). The bed keeps its own LEVEL — that is the slider's
      // job — and mute rides on top of it.
      b.volume = this.hereMuted ? 0 : this.bedVol / 100;
      b.muted = this.hereMuted;
      if (wanted) {
        if (!b.src) b.src = `radio/${this.site.bedMount}.mp3?t=${Date.now()}`;
        if (b.paused) b.play().catch(() => {});      // a wake can take a moment; the retry below covers it
      } else if (b.src) {
        b.pause(); b.removeAttribute("src"); b.load();
      }
    },

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
      this.applyHereVolume();
      this.$nextTick(() => this.applyBed());
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
    const bed = this.$refs.bed;
    if (bed) {
      const again = () => { if (this.bedOn && this.current) setTimeout(() => { bed.removeAttribute("src"); this.applyBed(); }, 2000); };
      bed.addEventListener("error", again);
      bed.addEventListener("ended", again);
    }
    audio.addEventListener("waiting", () => { this.status = "buffering…"; });
    audio.addEventListener("playing", () => { this.status = ""; this._reTries = 0; });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") this.remote = false; });
    // Leaving for the other view — or simply reloading. Write down what was
    // playing so the page that comes next can pick it up. `pagehide` rather
    // than `unload`, which a bfcache-ing browser may never fire.
    window.addEventListener("pagehide", () => this.noteHandover());
    this.takeHandover();
    window.addEventListener("resize", () => { this.narrow = !window.matchMedia("(min-width: 900px)").matches; });
    // who this box is, written at build time from the module's options
    getJSON("site.json").then(s => {
      if (s) { this.site = { ...this.site, ...s }; document.title = this.site.title; }
      this.applyBed();
      this.refreshZones();          // site.json is what says which outputs exist
    });
    // Whatever is already on when the page opens is not news; only a CHANGE
    // after we have settled is worth a notice.
    setTimeout(() => { this._noticeReady = true; }, 6000);
    // A permission revoked in site settings while the page was open leaves the
    // switch on and lying, so re-read what the browser actually thinks.
    if (typeof Notification !== "undefined") {
      this.notifyState = Notification.permission;
      if (this.notify && Notification.permission !== "granted") { this.notify = false; this.saveNotify(); }
    }
    this.applyHereVolume();
    this.setMediaHandlers();
    this.refresh().then(() => { this.refreshHeart(); this.mediaSession(); });
    this.pollWatching();
    setInterval(() => { this.tick = Date.now(); this.pollWatching(); this.refreshZones(); this.refresh().then(() => { this.refreshHeart(); this.mediaSession(); }); }, 10000);
    if (this.target === "room") this.pollReceiver();
    if (this.target === "local") this.pollSpeaker();
    if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
  },
}).mount("#app");
