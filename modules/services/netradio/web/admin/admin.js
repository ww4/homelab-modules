// Library radio — the admin, as a COMPONENT rather than a page of its own.
//
// It used to be a third document (admin/index.html + its own createApp), which
// meant reaching it was a navigation: the player's <audio> element was torn
// down and the music stopped. The view handover added in #377 restarted the
// stream on arrival, and the owner was right that that is not the same thing —
// "the handover stops and restarts the stream. What I'm asking, is can we
// write the admin into the same page so the music doesn't need to stop?"
// (2026-09-29). So: no navigation at all. The desktop player renders this as a
// view beside the wall, and nothing touches the audio element.
//
// Two hosts mount it, from one definition:
//
//   /          the player, <admin-panel base="admin/">
//   /admin/    on its own, for a bookmark and the phone's ⚙ link
//
// The markup lives in panel.html and is fetched at runtime — the pages have no
// build step, and a 250-line template inside a JavaScript string would be
// worse than the problem it solved. `base` is what tells the component where
// the API is from where it happens to be mounted.

const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];

// A tag field: chips + an input with suggestions (datalist). Enter or comma
// adds; only suggested values are accepted when suggestions are given.
const TagInput = {
  props: { modelValue: { type: Array, default: () => [] }, suggestions: { type: Array, default: () => [] }, placeholder: { type: String, default: "add…" } },
  emits: ["update:modelValue"],
  data() { return { text: "", id: "dl" + Math.random().toString(36).slice(2) }; },
  methods: {
    add() {
      const v = this.text.trim().replace(/,$/, "").toLowerCase();
      this.text = "";
      if (!v || this.modelValue.includes(v)) return;
      if (this.suggestions.length && !this.suggestions.includes(v)) return;
      this.$emit("update:modelValue", [...this.modelValue, v]);
    },
    remove(v) { this.$emit("update:modelValue", this.modelValue.filter(x => x !== v)); },
    key(e) { if (e.key === "Enter" || e.key === ",") { e.preventDefault(); this.add(); } else if (e.key === "Backspace" && !this.text && this.modelValue.length) this.remove(this.modelValue[this.modelValue.length - 1]); },
  },
  template: `<span class="tags">
    <span v-for="t in modelValue" :key="t" class="tag">{{ t }}<button type="button" @click="remove(t)" title="remove" aria-label="remove">✕</button></span>
    <input :list="id" v-model="text" :placeholder="placeholder" @keydown="key" @change="add" @blur="add">
    <datalist :id="id"><option v-for="s in suggestions.filter(s => !modelValue.includes(s))" :key="s" :value="s"></option></datalist>
  </span>`,
};

window.AdminPanel = {
  components: { "tag-input": TagInput },
  // Where the admin API is, relative to the page doing the mounting: "" from
  // /admin/, "admin/" from the player at /.
  props: { base: { type: String, default: "" } },
  data() {
    return { state: { feeds: {}, stations: [], schedule: [], artists: {}, families: [], today: {}, pending_requests: [] },
             options: {}, tab: "feeds", openId: null, edit: {}, add: { title: "", description: "", family: [], listenable: true, shellac: false },
             slots: {}, days: DAYS, flash: "", flashErr: false, scheduleMsg: "", answers: {},
             sel: null,
             pandora: { stations: {}, plays: 0 } };
  },
  computed: {
    // The tab strip says how much of each there is, so you can see there is
    // nothing on a tab without opening it.
    tabs() {
      return [{ id: "feeds", title: "Feeds", count: Object.keys(this.state.feeds).length || "" },
              { id: "stations", title: "Stations", count: this.state.stations.length || "" },
              { id: "pandora", title: "Pandora", count: Object.keys(this.pandora.stations || {}).length || "" }];
    },
    feedList() { return Object.entries(this.state.feeds).sort((a, b) => a[1].title.localeCompare(b[1].title)); },
    curated() { return this.state.stations.filter(s => s.kind !== "specialty"); },
    specialty() { return this.state.stations.filter(s => s.kind === "specialty"); },
    selected() { return this.state.stations.find(s => s.mount === this.sel) || null; },
  },
  methods: {
    async api(method, path, body) {
      const r = await fetch(`${this.base}api${path}`, { method, headers: { "Content-Type": "application/json" },
                                                       body: body === undefined ? undefined : JSON.stringify(body) });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || `${r.status}`);
      return j;
    },
    say(msg, err) { this.flash = msg; this.flashErr = !!err; clearTimeout(this._t); this._t = setTimeout(() => this.flash = "", 7000); },
    async load() {
      this.state = await this.api("GET", "/state");
      try { this.pandora = await this.api("GET", "/pandora"); } catch (e) {}
      this.slots = {};
      for (const s of this.state.schedule) {
        const custom = Array.isArray(s.days);
        (this.slots[s.station] ||= []).push({ ...s, _k: s.id || Math.random(), dayspec: custom ? "custom" : (s.days || "daily"),
                                               customDays: custom ? [...s.days] : [], minutes: s.minutes || 60, like: s.like || "artist" });
      }
    },
    // -- feeds
    openFeed(id) { const f = this.state.feeds[id]; this.edit = { title: f.title, description: f.description, family: [...(f.family || [])], listenable: !!f.listenable, shellac: !!f.shellac, rule: JSON.stringify(f.rule || {}, null, 1), _rule0: JSON.stringify(f.rule || {}, null, 1) }; },
    async saveFeed(id) {
      try {
        const body = { title: this.edit.title, description: this.edit.description, family: this.edit.family, listenable: this.edit.listenable, shellac: this.edit.shellac };
        if (this.edit.rule.trim() !== this.edit._rule0.trim()) body.rule = JSON.parse(this.edit.rule);
        const r = await this.api("PUT", `/feeds/${id}`, body);
        this.say(r.recompile ? "Saved — the description changed, so the rule will be rebuilt." : "Saved; apply requested.");
        await this.load(); this.openFeed(id);
      } catch (e) { this.say(e.message, true); }
    },
    async showAnswer(id) {
      if (this.answers[id]) { delete this.answers[id]; return; }
      try { const r = await this.api("GET", `/feeds/${id}/answer`); this.answers[id] = r.answer || "(no answer kept yet — rebuild the rule once)"; }
      catch (e) { this.say(e.message, true); }
    },
    async compileFeed(id) { try { await this.api("POST", `/feeds/${id}/compile`); this.say("Rebuilding the rule from the description — a minute or two."); await this.load(); } catch (e) { this.say(e.message, true); } },
    async deleteFeed(id) {
      if (!confirm(`Delete feed "${this.state.feeds[id].title}"?`)) return;
      try { await this.api("DELETE", `/feeds/${id}`); this.openId = null; this.say("Feed removed; apply requested."); await this.load(); } catch (e) { this.say(e.message, true); }
    },
    async addFeed() {
      try {
        const r = await this.api("POST", "/feeds", this.add);
        this.say(`Feed added as ${r.id}; the agent is building its rule.`);
        this.add = { title: "", description: "", family: [], listenable: true, shellac: false }; this.openId = r.id;
        await this.load(); this.openFeed(r.id);
      } catch (e) { this.say(e.message, true); }
    },
    // -- stations (with their schedule)
    async select(st) {
      this.sel = st.mount;
      this.edit = { name: st.name, family: [...(st.family || [])], base: JSON.stringify(st.base || {}, null, 1),
                    breaks_every: st.breaks_every === undefined || st.breaks_every === null ? "" : String(st.breaks_every),
                    excursion: typeof st.excursion === "number" ? String(Math.round(st.excursion * 100)) : "" };
      if (st.kind !== "specialty" && !this.options[st.mount])
        this.options[st.mount] = await this.api("GET", `/options?station=${encodeURIComponent(st.mount)}`);
    },
    slotsOf(mount) { return this.slots[mount] ||= []; },
    slotCount(mount) { return this.slotsOf(mount).length; },
    addSlot(mount) {
      const o = this.options[mount] || { feeds: [], artists: [] };
      this.slotsOf(mount).push({ _k: Math.random(), station: mount, name: "", kind: "feed", feed: (o.feeds[0] || {}).id, artist: (o.artists[0] || {}).name,
                                 like: "artist", dayspec: "daily", customDays: [], start: "20:00", minutes: 60 });
    },
    removeSlot(mount, i) { this.slotsOf(mount).splice(i, 1); },
    allSlots() {
      const out = [];
      for (const [mount, list] of Object.entries(this.slots)) {
        for (const s of list) {
          const slot = { id: s.id, station: mount, name: s.name, kind: s.kind, days: s.dayspec === "custom" ? s.customDays : s.dayspec, start: s.start, minutes: s.minutes };
          if (s.kind === "feed") slot.feed = s.feed;
          if (s.kind === "artist") slot.artist = s.artist;
          if (s.kind === "auto") slot.like = s.like;
          out.push(slot);
        }
      }
      return out;
    },
    async saveStation() {
      const st = this.selected;
      try {
        const body = { name: this.edit.name };
        if (this.edit.breaks_every !== "") body.breaks_every = /^\d+-\d+$/.test(this.edit.breaks_every) ? this.edit.breaks_every : Number(this.edit.breaks_every);
        if (st.kind !== "specialty") {
          body.family = this.edit.family;
          if (this.edit.excursion !== "") body.excursion = Number(this.edit.excursion);
          body.base = JSON.parse(this.edit.base || "{}");
          const r = await this.api("PUT", "/schedule", this.allSlots());
          this.scheduleMsg = `schedule: ${r.count} slot(s)`;
        }
        await this.api("PUT", `/stations/${st.mount}`, body);
        this.say("Saved; apply requested."); this.options = {};
        await this.load(); await this.select(this.selected);
      } catch (e) { this.say(e.message, true); }
    },
    async apply() { try { await this.api("POST", "/apply"); this.say("Apply requested: rescan, and a Liquidsoap restart if the station list changed."); await this.load(); } catch (e) { this.say(e.message, true); } },
  },
  watch: {
    openId(id) { if (id && this.tab === "feeds" && this.state.feeds[id]) this.openFeed(id); },
  },
  mounted() {
    this.load().then(() => { if (this.curated.length) this.select(this.curated[0]); }).catch(e => this.say(e.message, true));
    // Don't clobber an open edit. The interval is cleared on unmount now that
    // this can be closed without the page going away with it.
    this._poll = setInterval(() => { if (!this.openId && this.tab === "feeds") this.load().catch(() => {}); }, 30000);
  },
  unmounted() { clearInterval(this._poll); clearTimeout(this._t); },
};

// Fetch panel.html and hand back the finished component. `where` is the path to
// the markup from the mounting page; `base` the path to the API.
window.adminPanel = function (where, base) {
  return Vue.defineAsyncComponent(async () => ({
    ...window.AdminPanel,
    props: { base: { type: String, default: base } },
    template: await fetch(where).then(r => r.text()),
  }));
};
