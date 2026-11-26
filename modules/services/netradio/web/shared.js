// Library radio — what the two pages agree on, to the byte.
//
// app.js (the phone) and desktop.js (the wide player) grew side by side and
// drifted: 82 method names in common, and a `SpeakerTargetParity` test class
// that exists only to catch that drift. Every change this month — reconnect,
// the view handover, mute reaching the rain bed, the OS transport — had to be
// written twice, in two files, and reviewed twice.
//
// This is the half of that duplication with NO judgement in it: members whose
// bodies are byte-identical in both files today. They move here and both pages
// mix this in. Vue's merge gives a component's OWN options priority, so a page
// that genuinely differs simply keeps its version and wins — which is why only
// the identical ones are here, and why moving them cannot change behaviour.
//
// The 45 members that differ stay where they are, deliberately. Some of those
// differences are real features rather than drift (the phone's playTarget
// drives tuner presets and Pandora stations; the desktop's does neither), and
// reconciling them is a decision per member, not a refactor.
//
// Loads after radio.js — whose getJSON/call/mountsOf these use — and before
// the page's own script.

// ~5 minutes of trying at the 8 s ceiling. A deploy takes about a minute;
// beyond this the station is probably gone rather than restarting.
const RECONNECT_GIVE_UP = 40;

window.RadioShared = {
  // The remote's own state. It lives here rather than on either page because
  // the remote is now ONE component that both of them mount — the owner, on the
  // phone page not having the Roku half: "The phone should have the roku,
  // obviously. What am I sitting on the couch with" (2026-10-21).
  //
  // Vue merges a mixin's data() with the page's, and the PAGE wins on a
  // conflict, so a page that wants a different default keeps its own.
  data() {
    return {
      // The streaming box on the television. Navigation only: its sound leaves
      // the TV by optical to the receiver, so the Roku's own volume keys emit
      // CEC at a television that is not in that path (2026-10-03).
      roku: { name: "", model: "", app: "", power: "", awake: false, limited: false, error: "" },
      // The box's own clock. Jellyfin names what is playing, but only when the
      // library is the source; this works for Disney+ and Prime as well, and
      // reports the exact position rather than the last progress report a
      // client happened to send.
      rokuPlayer: { state: "", playing: false, app: "", position: 0, duration: 0, live: false },
      rokuApps: [],              // quick launch, from the box's own app list
      rokuPoll: 0, rokuText: "", rxOpen: false,
      // What is on a screen in the house. The radio can name its own track
      // because it chose it; a streaming box cannot be asked — but Jellyfin is
      // the server, so when the thing on the television came from the library
      // it knows exactly (2026-10-09).
      watching: { playing: false },
      // The in-page question. window.confirm() worked, but it is browser
      // chrome: "that should be an in-page modal not a browser modal, those
      // are ugly" (2026-11-02). One at a time, by construction.
      ask: null,
      // A deploy happened while this tab was open. Not an error, so not a
      // modal — see ask-panel.html.
      updateReady: false,
      // Each registered device's own state, keyed by id: a second PC with its
      // own speakers, a bridge in another room. The registry itself lives in
      // site.json, so a device added there needs no page code — which is the
      // point of there being a registry.
      //
      // Both pages carry this, not just the wide one, because both read
      // `radio.target` from the same localStorage key: choose a device on the
      // desktop and the phone opens already pointed at it. Before this, a
      // target the phone did not recognise fell through to "here" and played
      // in the browser while the button still named the device.
      devState: {},
    };
  },

  mounted() {
    // An open tab keeps the JavaScript it loaded, and this page is built never
    // to reload: that is what keeps the stream alive across a view change. So
    // a deploy is invisible here, and a change that shipped correctly looks
    // like it did not — "I just merged that, and tested, and it still didn't
    // gate", against a page four minutes older than the fix (2026-11-02).
    this.checkBuild();
    this._buildPoll = setInterval(() => this.checkBuild(), 60000);
  },
  unmounted() { clearInterval(this._buildPoll); },

  watch: {
    remote(open) {
      if (!open) { clearInterval(this.rokuPoll); return; }
      // One remote, not two behind tabs. The volume lives on the receiver and
      // the picture lives on the Roku, and switching panes to turn down a show
      // you are already watching is the wrong shape for the thing people
      // actually do with it (2026-10-21).
      //
      // The receiver's OWN world — inputs, FM, presets, Pandora, its menu — is
      // folded away, because none of it is part of watching television. With
      // no Roku to combine it with, it is the whole remote and opens.
      this.rxOpen = !this.site.hasRoku;
      this.pollRoku(); this.loadRokuApps();
    },
    // site.json arrives AFTER the first paint, and `hasRoku` defaults to false
    // until it does — while `hasRoom` defaults to true, so the Remote button is
    // already on screen. Opening the remote in that window used to leave the
    // Roku half permanently empty: no apps, no model, "asleep" against a box
    // that was playing. The tabs hid this, because changing tab polled again;
    // one pane has no second chance, so the second chance is here (2026-10-21).
    "site.hasRoku"(yes) {
      this.rxOpen = !yes;
      if (yes && this.remote) { this.pollRoku(); this.loadRokuApps(); }
    },
  },

  computed: {
    art() {
      // sized, not the original: covers in this library run to 3 MB, and the
      // hero is 340 CSS px. Asking for the full file made the one image on the
      // page the biggest thing on it.
      const t = this.playingTrack;
      if (t && t.path) return `admin/api/art?path=${encodeURIComponent(t.path)}&size=700`;
      // Pandora and the streaming inputs hand the receiver a cover of their
      // own. Proxied unless it is already an absolute URL, because some of
      // them are served off the receiver's own address.
      const np = this.np;
      if (!np || !np.album_art_url) return "";
      return /^https?:/.test(np.album_art_url) ? np.album_art_url
           : `receiver/art?url=${encodeURIComponent(np.album_art_url)}`;
    },
    canDJ() { return !!this.feedbackMount; },
    // a fixed station has no DJ: nothing to skip to, request or rate
    feedbackMount() { const m = this.playingMount; return m && !this.isFixed(m) ? m : null; },
    feedbackNext() { return this.nexts[this.feedbackMount] || {}; },
    freqText() {
      const t = this.receiver.tuner; if (!t) return "";
      return t.band === "FM" ? (t.fm.val / 100).toFixed(1) : String(t.am.val);
    },
    groups() {
      // a fixed station (Rain, Rainy Mood) lists as a specialty: it is a loop,
      // not a curated genre programme
      const curated = this.stations.filter(s => !["specialty", "fixed"].includes(s.kind));
      const specialty = this.stations.filter(s => ["specialty", "fixed"].includes(s.kind));
      const g = [{ kind: "curated", title: "Curated", stations: curated }];
      if (specialty.length) g.push({ kind: "specialty", title: "Specialty", stations: specialty });
      // the internet streams live in the receiver's own menu, so they are only
      // reachable when the receiver is the target
      if (this.target === "room") {
        if (this.quick.length)
          g.push({ kind: "quick", title: "Internet Radio", stations: this.quick.map(q => ({ mount: "", name: q.name, kind: "quick" })) });
        // The receiver's own: its Pandora stations and its tuner presets. The
        // phone has offered both all along; this page simply never listed them.
        if (this.pandora.length)
          g.push({ kind: "pandora", title: "Pandora", stations: this.pandora.map(n => ({ mount: "", name: n, kind: "pandora" })) });
        if (this.presets.length)
          g.push({ kind: "preset", title: "Radio presets",
                   stations: this.presets.map(x => ({ mount: "", name: x.text.replace(/\s+/g, " ").trim(), kind: "preset", number: x.number })) });
      }
      return g;
    },
    hearted() { const t = this.playingTrack; return !!t && !!this.hearts[t.path]; },
    nowKind() {
      if (this.targetDevice) { const m = this.dev(this.target).mount; return !m ? "" : this.isFixed(m) ? "ambient" : "library"; }
      if (this.target === "local") return !this.speaker.mount ? "" : this.isFixed(this.speaker.mount) ? "ambient" : "library";
      if (this.target === "here") return !this.current ? "" : this.isFixed(this.current) ? "ambient" : "library";
      if (!this.receiver.on) return "";
      if (this.receiver.input === "TUNER") return "tuner";
      if (this.receiver.input === "Pandora") return "pandora";
      if (this.roomMount) return "library";
      if (["NET RADIO", "Spotify", "SERVER", "AirPlay"].includes(this.receiver.input)) return "stream";
      return "";
    },
    nowLine1() {
      if (this.target === "room") {
        const np = this.receiver.now_playing || {};
        return [np.artist, np.track].filter(Boolean).join(" — ") || np.station || "";
      }
      const t = this.playingTrack;
      if (t) return t.artist ? `${t.artist} — ${t.title}` : t.title;
      const m = this.playingMount;
      return m ? ((this.up[m] || this.up[m + "-lo"] || {}).title || "") : "";
    },
    nowStation() {
      const m = this.playingMount;
      if (m) return (this.stations.find(s => s.mount === m) || {}).name || "";
      if (this.target === "room" && this.receiver.on) {
        const np = this.receiver.now_playing || {};
        return np.station || this.receiver.input || "";
      }
      return "";
    },
    // What the RECEIVER is doing, and what kind of thing it is. This page only
    // ever knew about library mounts, so a receiver sitting on its tuner or on
    // Pandora showed as nothing at all (2026-09-29).
    np() { return (this.target === "room" && this.receiver.now_playing) || null; },
    roomMount() {
      const np = this.receiver.now_playing;
      if (!this.receiver.on || this.receiver.input !== "NET RADIO" || !np || !np.station) return null;
      const s = this.stations.find(s => s.name === np.station);
      return s ? s.mount : null;
    },
    // WHAT IS PLAYING, asked once. Both pages had their own spelling of
    // this — the phone inlined it in four places — and that divergence was
    // what kept ten otherwise-identical members apart (2026-09-29).
    // ---- the device registry ---------------------------------------------
    // Every device netradio knows about, in its declared order, read straight
    // from site.json. A device is anything that can be told to play a station
    // over the four verbs; the page deliberately does not know what KIND of
    // thing one is, so adding a box is a registry entry and not a new branch.
    devices() { return this.site.devices || []; },
    // The registered device the chosen target names, or null when the target is
    // this browser, the receiver or the server's own card — those three predate
    // the registry and keep their own code paths for now.
    targetDevice() { return this.devices.find(d => d.id === this.target) || null; },
    playingMount() {
      return this.targetDevice ? (this.dev(this.target).mount || null)
           : this.target === "room" ? this.roomMount
           : this.target === "local" ? (this.speaker.mount || null)
           : this.current;
    },
    playingTrack() {
      const h = this.feedbackHistory;
      return (h && h[0] && h[0].kind !== "break") ? h[0] : null;
    },
    tickNow() { return this.tick || Date.now(); },
    stationFacts() {
      const out = [];
      const c = this.counts[this.openMount];
      if (c) {
        out.push({ head: `${c.tracks.toLocaleString()} tracks`, sub: "in this station's own pool" });
        if (c.fringe) out.push({ head: `+ ${c.fringe.toLocaleString()} fringe`, sub: "neighbouring genres, now and then" });
      }
      const seg = this.feedbackNext.segment;
      if (seg && seg.name) out.push({ head: seg.name, sub: "the segment on air now" });
      return out;
    },
    currentStation() { return this.stations.find(s => s.mount === this.current) || { name: "", mount: "" }; },
    feedbackHistory() { return this.histories[this.feedbackMount] || []; },
    feedbackStation() { return this.stations.find(s => s.mount === this.feedbackMount) || { name: "" }; },
    isFixed() { return (m) => (this.stations.find(s => s.mount === m) || {}).kind === "fixed"; },
  },

  methods: {
    async pollWatching() {
      // Rides the ten-second tick rather than a timer of its own. Answers
      // {playing:false} when Jellyfin is not configured, so there is nothing
      // to switch on and nothing to configure twice.
      const w = await getJSON("admin/api/watching");
      if (w) this.watching = w;
    },
    // What is on the screen, for the remote's own header. Two sources with
    // complementary blind spots, and the rule is: whoever knows, answers.
    //
    //   Jellyfin knows the NAMES \u2014 "Little Bear \u00b7 S1E4\u20136 \u00b7 Birthday Soup" \u2014
    //   and only for what came out of the library.
    //   The Roku knows the CLOCK, for anything at all, to the second.
    //
    // The Roku's Jellyfin app reports SupportsRemoteControl: false and
    // CanSeek: false, so there is no asking it to skip or seek \u2014 the clock is
    // all it will give up, and it is the half that was missing (2026-10-21).
    tvNow() {
      const w = this.watching, p = this.rokuPlayer;
      const head = w.playing ? w.head : (p.app || this.roku.app || "");
      const sub = w.playing ? w.sub : (p.live ? "live" : "");
      // Prefer the box's clock: Jellyfin's position is the last progress report
      // a client sent, which can be ten seconds stale.
      const dur = p.duration || (w.playing ? w.duration : 0);
      const pos = p.duration ? p.position : (w.playing ? w.position : 0);
      if (!head && !dur) return null;
      return { head, sub, pos, dur: p.live ? 0 : dur,
               playing: p.state ? p.playing : (w.playing && !w.paused),
               pct: dur && !p.live ? Math.max(0, Math.min(100, Math.round(pos / dur * 100))) : 0 };
    },
    clock(secs) {
      if (!secs && secs !== 0) return "";
      const m = Math.floor(secs / 60), s2 = secs % 60;
      return `${m}:${String(s2).padStart(2, "0")}`;
    },
    // ---- labels -----------------------------------------------------------
    //
    // What a thing IS, from the thing itself. Both boxes report a model over
    // their own API — the receiver answers "R-N301", the Roku "Roku Express
    // 4K+" — and the configured name is the fallback for a device that has not
    // answered yet, or has been given a name by hand.
    receiverLabel() { return this.receiver.model || this.receiver.name || this.site.roomName; },
    rokuLabel() { return this.roku.model || this.roku.name || this.site.rokuName; },
    where(suffix) {
      // The room is still worth saying; it is just not the headline.
      const room = this.site.roomName;
      return [room, suffix].filter(Boolean).join(" · ");
    },
    remoteSub() {
      // The header says the ROOM, so this line says which boxes are in it and
      // what state each is in — the two facts that decide whether a button
      // will do anything.
      const bits = [];
      if (this.site.hasRoku)
        bits.push(this.roku.error ? `${this.rokuLabel()} ${this.roku.error}`
                : this.roku.awake ? (this.roku.app || this.rokuLabel())
                : (this.roku.power || "asleep").toLowerCase());
      if (this.site.hasRoom)
        bits.push(this.receiver.error ? `${this.receiverLabel()} ${this.receiver.error}`
                : this.receiver.on ? this.receiver.input : "receiver in standby");
      return bits.join(" · ");
    },

    // ---- the Roku ---------------------------------------------------------
    async pollRoku() {
      if (!this.site.hasRoku) return;
      // Two calls, one round trip. /status carries device-info, which barely
      // changes; /player is the second hand and is asked for separately so a
      // five-second poll is not re-reading a serial number.
      // The media server comes along for the ride. The wide page asks for it
      // on its ten-second tick anyway, for the television's row in the device
      // list; the phone has no such row and would otherwise show a progress
      // bar with no title on it.
      const [s, p] = await Promise.all([getJSON("roku/status"), getJSON("roku/player"),
                                        this.pollWatching()]);
      if (!s || s.error) {
        this.roku = { ...this.roku, error: (s && s.error) || "unreachable" };
      } else {
        this.roku = { name: s.name || this.site.rokuName, model: s.model || "",
                      app: (s.app && s.app.name) || "",
                      power: s.power || "", awake: !!s.awake,
                      limited: !!s.limited, error: "" };
      }
      if (p && !p.error) this.rokuPlayer = p;
      clearInterval(this.rokuPoll);
      // Only while the remote is open. Nothing else on the page shows this,
      // and a box asleep in another room is not worth a request every five
      // seconds.
      if (this.remote)
        this.rokuPoll = setInterval(() => { if (this.remote) this.pollRoku(); }, 5000);
    },
    async loadRokuApps() {
      // The box's own list, so a newly installed app appears here without
      // anybody editing a page. Fetched once per visit: it changes when
      // somebody installs a channel, which is not every five seconds.
      if (!this.site.hasRoku || this.rokuApps.length) return;
      const r = await getJSON("roku/apps");
      // `appl` is a real app. The list also carries `tvin` (an HDMI port) and
      // `menu` entries, and launching those from here does nothing useful on a
      // box whose television is fed by one cable.
      this.rokuApps = ((r && r.apps) || []).filter(a => a.type === "appl");
    },
    async rokuLaunch(app) {
      try {
        await call("POST", `roku/launch/${encodeURIComponent(app.id)}`);
        this.say(`opening ${app.name}…`);
        this.roku.limited = false;
        // It takes a moment to come up, and asking immediately reports the app
        // that is on its way out.
        setTimeout(() => { if (this.remote) this.pollRoku(); }, 1500);
      } catch (e) {
        this.say(e.message);
        if (/Limited mode/i.test(e.message)) this.roku.limited = true;
      }
    },
    async rokuKey(key, action) {
      const q = action && action !== "keypress" ? `?action=${action}` : "";
      // `call` THROWS on a non-2xx — it does not hand back an error object.
      // Written the other way round (as this was), every failure became an
      // unhandled rejection and NO message appeared: the Roku was refusing
      // every key with a 403 and the page showed nothing whatever, which is
      // how it arrived as "none of the buttons are working" (2026-10-19).
      try {
        await call("POST", `roku/key/${key}${q}`);
        this.roku.limited = false;
        if (!action || action === "keypress") this.pollRoku();
      } catch (e) {
        this.say(e.message);
        if (/Limited mode/i.test(e.message)) this.roku.limited = true;
      }
    },
    async rokuSend() {
      const text = this.rokuText.trim();
      if (!text) return;
      try {
        const r = await call("POST", "roku/type", { text });
        this.rokuText = "";
        this.say(`sent ${(r && r.sent) || 0} character(s)`);
      } catch (e) {
        this.say(e.message);
        if (/Limited mode/i.test(e.message)) this.roku.limited = true;
      }
    },
    // a cover that failed is retried after 30 s, not written off until the song changes
    artOk() { return !!this.art && (this.artFailed !== this.art || this.tickNow - this.artFailedAt > 30000); },
    // ---- the tuner -------------------------------------------------------
    band(b) {
      const t = this.receiver.tuner || {};
      const f = b === "FM" ? ((t.fm && t.fm.val) || 9310) / 100 : ((t.am && t.am.val) || 1300);
      return this.receiverAction("", () => call("POST", "receiver/tuner", { band: b, frequency: f }));
    },
    // offered only when there is something to fix: on, on net radio, stopped
    canResume() {
      if (this.target !== "room") return false;
      const np = this.receiver.now_playing || {};
      return this.receiver.on && this.receiver.input === "NET RADIO" && np.playback !== "Play";
    },
    cursor(action) {
      const src = this.sourceOf(this.receiver.input);
      if (!src || src === "Tuner") return;
      return this.receiverAction("", async () => { this.menu = await call("POST", "receiver/menu/cursor", { source: src, action }); });
    },
    async dislike(scope) {
      const t = this.playingTrack;
      if (!t || !this.feedbackMount) { this.say("nothing to rate yet"); return; }
      try {
        await call("POST", "admin/api/dislike", { scope, path: t.path, artist: t.artist, title: t.title, mount: this.feedbackMount });
        this.say(scope === "artist" ? `less ${t.artist} from now on` : `never again: ${t.title}`);
        setTimeout(() => this.refresh(), 2500);
      } catch (e) { this.say(e.message); }
    },
    isPlaying(s) {
      if (this.target === "room") {
        const np = this.np;
        if (s.kind === "preset") return this.receiver.input === "TUNER" && !!this.receiver.tuner
                                      && String(this.receiver.tuner.preset) === String(s.number);
        if (s.kind === "pandora") return this.receiver.input === "Pandora" && !!np && np.station === s.name;
        return this.receiver.on && !!np && np.station === s.name;
      }
      if (this.targetDevice) return !!s.mount && this.dev(this.target).mount === s.mount;
      if (this.target === "local") return !!s.mount && this.speaker.mount === s.mount;
      return !!s.mount && this.current === s.mount;
    },
    // ---- the OS's own media controls -------------------------------------
    // Desktop browsers put the same metadata on the system media keys and the
    // notification shade. Same closed action list as on the phone: skip maps
    // to `nexttrack`, and there is no heart action to map one to.
    mediaSession() {
      if (!("mediaSession" in navigator)) return;
      const ms = navigator.mediaSession;
      if (this.target !== "here" || !this.current) { ms.metadata = null; ms.playbackState = "none"; return; }
      const playing = this.paused ? "paused" : "playing";
      const t = this.playingTrack;
      const cover = this.artOk() ? new URL(this.art, location.href).href : "";
      const title = (t && t.title) || this.nowLine1 || this.nowStation || "Library radio";
      const key = [title, t && t.artist, cover].join("\u0000");
      if (key === this._msKey) { ms.playbackState = playing; return; }
      this._msKey = key;
      try {
        ms.metadata = new MediaMetadata({
          title,
          artist: (t && t.artist) || "",
          album: this.nowStation || this.site.title,
          artwork: cover ? [96, 192, 384, 512].map(px => ({ src: cover, sizes: `${px}x${px}`, type: "image/jpeg" })) : [],
        });
        ms.playbackState = playing;
      } catch (e) { /* older browsers manage without */ }
    },
    // asked once per track, then cached
    async refreshHeart() {
      const t = this.playingTrack;
      if (!t || t.path in this.hearts) return;
      const r = await getJSON(`admin/api/heart?path=${encodeURIComponent(t.path)}`);
      if (r) this.hearts = { ...this.hearts, [t.path]: !!r.heart };
    },
    remotePage(down) {
      const src = this.sourceOf(this.receiver.input);
      if (!src || src === "Tuner") return;
      return this.receiverAction("", async () => { this.menu = await call("POST", "receiver/menu/page", { source: src, down }); });
    },
    async request(r) {
      if (!this.feedbackMount) { this.say("start a library station first"); return; }
      try {
        await call("POST", `admin/api/dj/${this.feedbackMount}/request`, { path: r.path });
        this.say(`next on ${this.feedbackStation.name}: ${r.title}`);
        this.query = ""; this.results = [];
        setTimeout(() => this.refresh(), 3000);
      } catch (e) { this.say(e.message); }
    },
    // The receiver stops dead when an encoder restarts and does not come back
    // by itself; netradio-resume does this after Liquidsoap starts, and this is
    // the same thing on a finger for when it stopped some other way.
    resumeRoom() {
      return this.startOnReceiver("resuming…", async () => {
        const r = await call("POST", "admin/api/resume", {});
        this.say((r && r.message) || "asked the receiver to resume");
        return null;
      });
    },
    say(msg) { this.toast = msg; clearTimeout(this._toastT); this._toastT = setTimeout(() => { this.toast = ""; }, 4000); },
    shownVolume() { return this.target === "local" ? (this.speaker.volume ?? 0) : Math.round(this.receiver.volume || 0); },
    // ---- feedback --------------------------------------------------------
    // Skip and "less of this" are ONE press: the station moves on and the track
    // loses ground. Four skips and it stops coming back, without anything
    // having to be declared "never" (2026-09-27).
    async skip() {
      if (!this.feedbackMount) return;
      const t = this.playingTrack;
      try {
        if (!t) { await call("POST", `admin/api/dj/${this.feedbackMount}/skip`); this.say("skipping…"); }
        else {
          const r = await call("POST", "admin/api/feedback",
                               { kind: "skip", path: t.path, artist: t.artist, title: t.title, mount: this.feedbackMount });
          this.say(r && r.out_of_rotation ? `skipped — that's enough of ${t.title}` : `skipping — less of ${t.title}`);
        }
        setTimeout(() => this.refresh(), 2500);
      } catch (e) { this.say(e.message); }
    },
    sourceOf(input) { return ({ "NET RADIO": "NET_RADIO", Pandora: "Pandora", SERVER: "SERVER", Spotify: "Spotify", AirPlay: "AirPlay", TUNER: "Tuner" })[input] || ""; },
    async speakerAction(label, fn) {
      this.busy = label;
      try { const st = await fn(); if (st) this.speaker = { ...st, error: st.error || "" }; }
      catch (e) { this.say(e.message); }
      finally { this.busy = ""; }
    },

    // ---- a registered device, over the four verbs ------------------------
    // One shape for every device, which is what makes the registry worth
    // having: the page posts play/stop/volume/mute (and whatever extras the
    // device declares) to a path built from its id, and the device's answer IS
    // its new state — so one round trip both acts and refreshes.
    dev(id) {
      return this.devState[id] ||
        { seen: false, playing: false, mount: "", volume: null, muted: false, target: "", outputs: [], bed: null, error: "" };
    },
    // The module writes `base` into site.json. The fallback matches it rather
    // than the device's own nginx location: the call goes through the admin API
    // so the bearer token can be added server-side.
    devBase(id) { return (this.devices.find(d => d.id === id) || {}).base || `admin/api/devices/${id}`; },
    // `state` is the contract. A device already speaking a different dialect
    // declares its own path instead of being rewritten to match — yamaha-ync
    // answers `status`.
    devStatePath(id) { return (this.devices.find(d => d.id === id) || {}).statePath || "state"; },
    // An extra is offered only where the device says it has it. A rain bed
    // needs an output that can share a sink, which an exclusive ALSA device
    // cannot, and not every box has a second output to move to. Asking anyway
    // earns a 400 — the device correctly saying no — but the control should not
    // have been there to press.
    devCan(cap, id) {
      const d = this.devices.find(x => x.id === (id || this.target));
      return !!d && (d.capabilities || []).includes(cap);
    },
    devPut(id, st, fallback) {
      this.devState = { ...this.devState,
                        [id]: { ...(st || this.dev(id)), seen: true, error: (st && st.error) || fallback || "" } };
    },
    async deviceAction(label, fn) {
      const id = this.target;          // read once: the target can change mid-flight
      this.busy = label;
      try { this.devPut(id, await fn(id)); }
      catch (e) { this.devPut(id, null, e.message); this.say(e.message); }
      finally { this.busy = ""; }
    },
    devicePlay(mount) { return this.deviceAction("starting…", id => call("POST", `${this.devBase(id)}/play`, { mount })); },
    deviceStop() { return this.deviceAction("stopping…", id => call("POST", `${this.devBase(id)}/stop`, {})); },
    deviceSet(level) { return this.deviceAction("", id => call("POST", `${this.devBase(id)}/volume`, { level })); },
    deviceMute(on) { return this.deviceAction("", id => call("POST", `${this.devBase(id)}/mute`, { on })); },
    deviceBed(patch) { return this.deviceAction("", id => call("POST", `${this.devBase(id)}/bed`, patch)); },
    deviceOutput(t) { return this.deviceAction("moving…", id => call("POST", `${this.devBase(id)}/output`, { target: t })); },
    // getJSON swallows the failure and answers null, so an unreachable device
    // is a known state rather than a thrown error.
    async pollDevice(id) {
      const st = await getJSON(`${this.devBase(id)}/${this.devStatePath(id)}`);
      this.devPut(id, st, st ? "" : "unreachable");
    },
    stopTarget() {
      if (this.targetDevice) return this.deviceStop();
      if (this.target === "room") return this.receiverAction("stopping…", () => call("POST", "receiver/playback", { action: "Stop" }));
      if (this.target === "local") return this.speakerAction("stopping…", () => call("POST", "speaker/stop", {}));
      return this.stop();
    },
    stopViz() { cancelAnimationFrame(this.raf); },
    takeHandover() {
      let h = null;
      try {
        h = JSON.parse(localStorage.getItem("radio.handover") || "null");
        localStorage.removeItem("radio.handover");     // one-shot; pagehide re-arms it
      } catch (e) {}
      if (!h || !h.mount || !(Date.now() - (h.at || 0) < HANDOVER_MS)) return;
      if (this.target !== "here") return;              // the speakers never stopped
      this.play(h.mount);
    },
    // A toggle, not a tally: stored locally always, and mirrored to Jellyfin
    // when one is configured.
    async toggleHeart() {
      const t = this.playingTrack;
      if (!t) { this.say("nothing to rate yet"); return; }
      const want = !this.hearted;
      try {
        const r = await call("POST", "admin/api/feedback",
                             { kind: "heart", on: want, path: t.path, artist: t.artist, title: t.title });
        this.hearts = { ...this.hearts, [t.path]: !!(r && r.heart) };
        this.say((r && r.message) || (r && r.heart ? "hearted" : "heart removed"));
      } catch (e) { this.say(e.message); }
    },
    tuneStep(dir) {
      const t = this.receiver.tuner; if (!t) return;
      const fm = t.band === "FM";
      const f = fm ? Math.round((t.fm.val / 100 + dir * 0.2) * 10) / 10 : t.am.val + dir * 10;
      return this.receiverAction("", () => call("POST", "receiver/tuner", { band: t.band, frequency: f }));
    },
    async loadPandora() {
      try {
        let m = await call("POST", "receiver/menu/cursor", { source: "Pandora", action: "Return to Home" });
        const names = [];
        for (let i = 0; i < 6 && m; i++) {
          for (const l of m.lines) if (l.attribute !== "Unselectable" && l.text !== "Shuffle") names.push(l.text);
          if (m.current_line + 8 > m.max_line) break;
          m = await call("POST", "receiver/menu/page", { source: "Pandora", down: true });
        }
        this.pandora = names;
        try { localStorage.setItem("radio.pandora", JSON.stringify(names)); } catch (e) {}
      } catch (e) {}
    },
    artError() { this.artFailed = this.art; this.artFailedAt = Date.now(); },
    countOf(m) { const c = this.counts[m]; return c ? `${c.tracks.toLocaleString()} tracks${c.fringe ? " +" + c.fringe.toLocaleString() + " fringe" : ""}` : ""; },
    feedback(up) { return this.receiverAction("", async () => { await call("POST", "receiver/feedback", { thumbs_up: up, source: "Pandora" }); this.say(up ? "thumbs up" : "thumbs down"); }); },
    forgetHandover() { try { localStorage.removeItem("radio.handover"); } catch (e) {} },
    initials(name) { return (name || "").split(/[\s&]+/).filter(Boolean).slice(0, 2).map(w => w[0].toUpperCase()).join(""); },
    isUp(m) { return !!m && !!(this.up[m] || this.up[m + "-lo"]); },
    listenersOf(m) { return ((this.up[m] || {}).listeners | 0) + ((this.up[m + "-lo"] || {}).listeners | 0); },
    async loadMenu() {
      this.menuSource = ["SERVER", "Pandora", "NET RADIO"].includes(this.receiver.input) ? this.sourceOf(this.receiver.input) : "";
      if (!this.menuSource) return;
      const m = await getJSON(`receiver/menu?source=${this.menuSource}`);
      if (m) this.menu = m;
    },
    menuBack() { return this.receiverAction("", async () => { this.menu = await call("POST", "receiver/menu/cursor", { source: this.menuSource, action: "Return" }); }); },
    menuPage(down) { return this.receiverAction("", async () => { this.menu = await call("POST", "receiver/menu/page", { source: this.menuSource, down }); }); },
    menuSelect(line) { return this.receiverAction("", async () => { this.menu = await call("POST", "receiver/menu/select", { source: this.menuSource, line }); }); },
    mute(on) { return this.receiverAction("", () => call("POST", "receiver/mute", { on })); },
    // ---- the view switch -------------------------------------------------
    noteHandover() {
      try {
        if (this.current && this.target === "here")
          localStorage.setItem("radio.handover", JSON.stringify({ mount: this.current, at: Date.now() }));
        else localStorage.removeItem("radio.handover");
      } catch (e) {}
    },
    // ---- the remote, and the receiver's own menus ------------------------
    openRemote() {
      this.remote = true;
      if (this.target !== "room") this.target = "room";
      this.pollReceiver();
      if (!this.inputs.length) getJSON("receiver/inputs").then(i => { if (i) this.inputs = i; });
      if (!this.presets.length) getJSON("receiver/tuner/presets").then(p => { if (p) this.presets = p; });
    },
    playback(action) { return this.receiverAction("", () => call("POST", "receiver/playback", { action })); },
    power(on) { return this.receiverAction(on ? "powering on…" : "standby…", () => call("POST", "receiver/power", { on })); },
    presetStep(up) { return this.receiverAction("", () => call("POST", "receiver/tuner", { preset: up ? "Up" : "Down" })); },
    // ---- asking, in the page ---------------------------------------------
    askConfirm(opts) {
      return new Promise(resolve => {
        this.ask = { title: "", body: "", confirm: "Go ahead", cancel: "Cancel", ...opts, resolve };
      });
    },
    askAnswer(yes) {
      const a = this.ask;
      this.ask = null;                 // cleared FIRST: resolving can open another
      if (a) a.resolve(!!yes);
    },

    // ---- is this page out of date? ---------------------------------------
    async checkBuild() {
      // site.json carries the content hash of the page assets. Cache-busted
      // because the whole point is to notice a change on the server.
      const s = await getJSON("site.json?b=" + Date.now());
      const id = s && s.build;
      if (!id) return;                 // an install without the stamp: say nothing
      if (!this._build) { this._build = id; return; }
      if (id !== this._build) this.updateReady = true;
    },
    reloadForUpdate() {
      // Through the handover, so the reload costs the listener nothing: the
      // page on the way out writes down what it was playing and the page on
      // the way in picks it up.
      try { this.noteHandover(); } catch (e) {}
      location.reload();
    },

    // ---- starting a station ----------------------------------------------
    // HERE means this browser. Always — not whatever output happens to be
    // selected. The single button used to say "Play here" and follow the
    // selector, which is how a film got interrupted from outside the house
    // (2026-11-02).
    playHere(s) {
      if (!s || !s.mount) return;
      // The selector follows the press rather than the other way round: the
      // button that says "here" now also MAKES here the output, so the two
      // can never disagree again.
      this.target = "here";
      return this.play(s.mount);
    },

    // ---- taking the room off the television ------------------------------
    //
    // Starting a station on the receiver takes it off whatever it was playing.
    // Usually that is another station and nobody minds. When the receiver is
    // on the television's input it is somebody's film, and the station button
    // is reachable from a phone that is not in the house: "Interrupted the
    // movie audio when I wasn't even at home... oops!" (2026-11-02).
    //
    // The cause was not a mis-click. The output selector is sticky, so the
    // press that starts a station HERE and the press that takes the room away
    // from the television are the same press, and nothing distinguished them.
    //
    // This is on the START paths only. Volume, mute and power stay immediate:
    // they are deliberate acts on a remote, and a confirmation on every one of
    // them is a dialog people learn to dismiss without reading — which would
    // cost us this one too.
    async tvInTheWay() {
      const tv = this.site.tvInput;
      if (!tv || !this.receiver.on) return null;
      if (String(this.receiver.input || "").toUpperCase() !== String(tv).toUpperCase()) return null;

      // On that input, assume the television is in use unless the box says
      // otherwise. An unreachable Roku is not a clear road: the receiver being
      // on the television's input is the stronger of the two signals, and a
      // diagnostic we could not read is no reason to decide the room is free.
      let what = "";
      const p = await getJSON("roku/player");
      if (p && !p.error) {
        if (!p.playing) return null;          // positively idle — nothing to take
        what = p.app || "";
      }
      // The box knows the app, the media server knows the title. Only asked
      // for on the rare path where we are about to interrupt something.
      await this.pollWatching();
      const w = this.watching;
      if (w && w.playing) what = [w.head, w.sub].filter(Boolean).join(" · ") || what;
      return { what };
    },
    async confirmInterrupt(tv) {
      const room = this.receiverLabel();
      const line = tv.what
        ? `${room} is on ${this.receiver.input} and the television is playing ${tv.what}.`
        : `${room} is on ${this.receiver.input} — the television's own sound.`;
      const yes = await this.askConfirm({
        title: "Interrupt the television?",
        body: `${line} Starting a station there will cut it off.`,
        confirm: "Interrupt it",
        cancel: "Leave it alone",
      });
      if (!yes) this.say("left the television alone");
      return yes;
    },
    // Same as receiverAction, but for the acts that CHANGE WHAT THE ROOM IS
    // PLAYING rather than how loud it is.
    async startOnReceiver(label, fn) {
      const tv = await this.tvInTheWay();
      if (tv && !(await this.confirmInterrupt(tv))) return;
      return this.receiverAction(label, fn);
    },
    async receiverAction(label, fn) {
      this.busy = label;
      try { const r = await fn(); if (r && "on" in r) this.receiver = { ...this.receiver, ...r }; await this.pollReceiver(); await this.refresh(); }
      catch (e) { this.say(e.message); }
      finally { this.busy = ""; }
    },
    retune() { try { localStorage.setItem("radio.quality", this.quality); } catch (e) {} if (this.current) this.play(this.current); },
    scheduleReconnect() {
      if (!this.current) return;
      clearTimeout(this._reTimer);
      this._reTries = (this._reTries || 0) + 1;
      if (this._reTries > RECONNECT_GIVE_UP) { this.status = "stream lost — press play"; return; }
      const wait = Math.min(1000 * 2 ** (this._reTries - 1), 8000);
      this.status = "reconnecting…";
      this._reTimer = setTimeout(() => this.reconnect(), wait);
    },
    searchDebounced() {
      clearTimeout(this.searchTimer);
      this.searchTimer = setTimeout(async () => {
        this.results = this.query.trim().length > 1 ? ((await getJSON(`admin/api/search?q=${encodeURIComponent(this.query)}`)) || []) : [];
      }, 250);
    },
    seek(up) { return this.receiverAction(up ? "seeking up…" : "seeking down…", () => call("POST", "receiver/tuner/seek", { up })); },
    selectInput(name) { return this.receiverAction(`switching to ${name}…`, () => call("POST", "receiver/input", { name })); },
    setMediaHandlers() {
      if (!("mediaSession" in navigator)) return;
      const set = (action, fn) => { try { navigator.mediaSession.setActionHandler(action, fn); } catch (e) {} };
      set("play", () => this.resumeHere());
      set("pause", () => this.pauseHere());   // pause, NOT stop: stop ends the session
      set("stop", () => this.stop());
      set("nexttrack", () => this.skip());
      set("previoustrack", null);
    },
    setVolume(level) { return this.receiverAction("", () => call("POST", "receiver/volume", { level })); },
    speakerMute(on) { return this.speakerAction("", () => call("POST", "speaker/mute", { on })); },
    speakerPlay(mount) { return this.speakerAction("starting…", () => call("POST", "speaker/play", { mount })); },
    // ---- the spectrum in the bar (this browser only) ---------------------
    startViz() {
      const audio = this.$refs.audio, canvas = this.$refs.viz;
      if (!canvas) return;
      if (!this.ctx) {
        const AC = window.AudioContext || window.webkitAudioContext; if (!AC) return;
        this.ctx = new AC();
        const src = this.ctx.createMediaElementSource(audio);
        this.analyser = this.ctx.createAnalyser();
        this.analyser.fftSize = 256; this.analyser.smoothingTimeConstant = 0.82;
        src.connect(this.analyser); this.analyser.connect(this.ctx.destination);
      }
      if (this.ctx.state === "suspended") this.ctx.resume();
      cancelAnimationFrame(this.raf);
      const g = canvas.getContext("2d"), data = new Uint8Array(this.analyser.frequencyBinCount);
      const draw = () => {
        this.raf = requestAnimationFrame(draw);
        const c = this.$refs.viz; if (!c) return;
        this.analyser.getByteFrequencyData(data);
        const W = c.width, H = c.height, bars = 40, step = Math.floor(data.length * 0.75 / bars);
        g.clearRect(0, 0, W, H); g.fillStyle = "#d9743f";
        for (let i = 0; i < bars; i++) {
          let v = 0; for (let j = 0; j < step; j++) v = Math.max(v, data[i * step + j]);
          const h = (v / 255) * H, w = W / bars;
          g.globalAlpha = 0.35 + 0.65 * (v / 255);
          g.fillRect(i * w + 1, H - h, w - 2, h);
        }
        g.globalAlpha = 1;
      };
      draw();
    },
    streamUrl(m) { return `radio/${m}${this.quality}.mp3?t=${Date.now()}`; },
    // ---- small helpers the template uses ---------------------------------
    thumb(path, size = 200) { return `admin/api/art?path=${encodeURIComponent(path)}&size=${size}`; },
    // NOT filtered through goodArt: see imgError. The hero and the bar, which
    // show ONE image, do filter — there a failure has to fall through to the
    // station's monogram rather than leave a blank square.
    tileCovers(s) { return s.mount && this.tiles[s.mount] ? (this.tiles[s.mount].covers || []) : []; },
    tunerPreset(n) { return this.startOnReceiver("tuning…", () => call("POST", "receiver/tuner", { preset: n })); },
  },
};

// The remote, as one component both pages mount. Markup fetched at runtime,
// the way admin/panel.html is — the page that opens it is handed in whole as
// `app`, so the state and the methods stay where they already live rather
// than being threaded through a dozen props and events.
//
// Passing the parent to a child is unusual, and the alternative here is worse:
// this template touches ~20 members of the page, and every one of them would
// otherwise need a prop in and an event back out, which is a lot of machinery
// to keep two files from drifting when a mixin and one template already do it.
window.askPanel = function (where) {
  return Vue.defineAsyncComponent(async () => ({
    props: { app: { type: Object, required: true } },
    template: await fetch(where).then(r => r.text()),
  }));
};

window.remotePanel = function (where) {
  return Vue.defineAsyncComponent(async () => ({
    props: { app: { type: Object, required: true } },
    template: await fetch(where).then(r => r.text()),
  }));
};
