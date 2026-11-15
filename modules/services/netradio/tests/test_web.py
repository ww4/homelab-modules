"""The web pages are plain files served as-is, so nothing type-checks them.
These are the cheap structural checks that catch the mistakes that actually
happened.
"""

import os
import re
import unittest
from pathlib import Path

# web/ is deliberately NOT part of the Python package's source (editing a page
# must not rehash the package and restart every unit), so inside that build
# there is nothing here to check and these tests skip. They are run for real by
# the `netradio-web` derivation, which has the pages and sets NETRADIO_WEB —
# and that derivation is nginx's document root, so it gates every deploy.
WEB = Path(os.environ.get("NETRADIO_WEB") or (Path(__file__).resolve().parent.parent / "web"))
PAGES = ["index.html", "desktop.html"]
SCRIPTS = ["app.js", "desktop.js"]
# Everything nginx serves as a page. The admin page is not part of the
# player-parity checks — it is a different thing — but it can 404 on a missing
# stylesheet exactly like the others, so the packaging check covers it too.
SERVED_PAGES = PAGES + ["admin/index.html"]
# Template/script pairs for the scope checks. The admin's markup lives in its
# own file now (it is a component both the player and /admin/ mount, not a page)
# and can drift from its script exactly the way the others can.
TEMPLATE_PAIRS = list(zip(PAGES, SCRIPTS)) + [("admin/panel.html", "admin/admin.js")]


def setUpModule():
    if not (WEB / "index.html").exists():
        raise unittest.SkipTest(f"no web/ at {WEB} — these run in the netradio-web build")


def module_functions(js: str) -> set[str]:
    """Functions declared at module scope — visible inside the script, and
    NOT inside a Vue template expression."""
    return set(re.findall(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", js, re.M))


def template_calls(html: str) -> set[str]:
    """Identifiers invoked inside Vue binding attributes and {{ }}."""
    exprs = re.findall(r'(?:@[\w.:-]+|v-if|v-else-if|v-model[\w.]*|:[\w-]+)\s*=\s*"([^"]*)"', html)
    exprs += re.findall(r"\{\{(.*?)\}\}", html, re.S)
    names: set[str] = set()
    for e in exprs:
        # Only BARE identifiers: `foo(` resolves on the component, while
        # `x.some(` is a method on some other object and says nothing about
        # the component's own surface. Missing this made `Array.some` look
        # like an undefined method.
        names |= set(re.findall(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(", e))
    return names


class TemplateScope(unittest.TestCase):
    def test_no_template_calls_a_module_scope_function(self):
        """Vue evaluates a template expression against the component, so a
        module-scope helper is simply not there — and the failure is a runtime
        "call is not a function" in the browser, which no build step catches.

        This happened: the local-speaker volume slider was written as
            @change="speakerAction('', () => call('POST', 'speaker/volume', …))"
        and `call` is a module function in app.js. Mute worked (a real method),
        volume silently did nothing but throw in the console, and it read as
        "the volume doesn't work" (2026-09-26).
        """
        for page, script in TEMPLATE_PAIRS:
            html = (WEB / page).read_text()
            js = (WEB / script).read_text()
            # radio.js too: the shared fetch helpers are module scope like any
            # other, and a template calling one fails exactly the same way.
            shared = (WEB / "radio.js").read_text() if (WEB / "radio.js").exists() else ""
            bad = template_calls(html) & (module_functions(js) | module_functions(shared))
            self.assertEqual(bad, set(),
                             f"{page} calls module-scope {sorted(bad)} from {script} — "
                             f"move it onto the component as a method")

    def test_every_method_a_template_calls_actually_exists(self):
        """The mirror of the above: a template naming a method that was renamed
        away fails the same silent way."""
        for page, script in TEMPLATE_PAIRS:
            html = (WEB / page).read_text()
            js = (WEB / script).read_text()
            # method shorthand `name(...)  {`, `name: function`, `name: (…) =>`
            defined = set(re.findall(r"^\s{4}(?:async\s+)?([A-Za-z_$][\w$]*)\s*\(", js, re.M))
            defined |= set(re.findall(r"^\s{4}([A-Za-z_$][\w$]*)\s*:\s*(?:async\s*)?(?:function|\()", js, re.M))
            defined |= set(re.findall(r"\b([A-Za-z_$][\w$]*)\s*\(", js))   # anything called in the script too
            # …and what the page mixes in: a member that moved to shared.js is
            # still on the component, and the template is still right to call it.
            sh = WEB / "shared.js"
            if sh.exists() and script in ("app.js", "desktop.js"):
                shared = sh.read_text()
                defined |= set(re.findall(r"^\s{4}(?:async\s+)?([A-Za-z_$][\w$]*)\s*\(", shared, re.M))
            # JS builtins and Vue helpers a template may legitimately use
            allowed = {"Math", "Number", "String", "Boolean", "Object", "Array", "JSON", "Date",
                       "parseInt", "parseFloat", "isNaN", "encodeURIComponent", "$event"}
            missing = template_calls(html) - defined - allowed
            self.assertEqual(missing, set(),
                             f"{page} calls {sorted(missing)}, which {script} does not define")


class VueDirectiveChains(unittest.TestCase):
    """A `v-else-if` / `v-else` must follow a `v-if` sibling.

    Orphan one and Vue does not fail politely at that element — it fails to
    compile the WHOLE root template, and the page renders as raw {{ mustaches }}
    with nothing in the console but a URL. Adding the admin view put a
    `v-else-if` after the chain's closing `v-else` and blanked the desktop page
    entirely; every test here passed (2026-09-29). Nothing type-checks these
    files, so this is the check that would have caught it.
    """

    def chains(self, html: str):
        """(line, tag, directive, previous sibling's directive) per branch
        directive, tracking siblings at each depth."""
        from html.parser import HTMLParser
        VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
                "link", "meta", "param", "source", "track", "wbr"}
        found = []

        class P(HTMLParser):
            def __init__(self):
                super().__init__(convert_charrefs=True)
                self.stack = [[]]          # per depth: the directive of each element seen

            def branch(self, attrs):
                for k, _ in attrs:
                    if k in ("v-if", "v-else-if", "v-else"):
                        return k
                return None

            def handle_starttag(self, tag, attrs):
                d = self.branch(attrs)
                if d in ("v-else-if", "v-else"):
                    prev = next((x for x in reversed(self.stack[-1]) if x is not None), "NOTHING") \
                        if self.stack[-1] else "NOTHING"
                    # only the IMMEDIATELY preceding element counts
                    prev = self.stack[-1][-1] if self.stack[-1] else "NOTHING"
                    found.append((self.getpos()[0], tag, d, prev))
                self.stack[-1].append(d)
                if tag not in VOID:
                    self.stack.append([])

            def handle_endtag(self, tag):
                if tag not in VOID and len(self.stack) > 1:
                    self.stack.pop()

        P().feed(html)
        return found

    def test_every_else_follows_an_if(self):
        for page in SERVED_PAGES + ["admin/panel.html"]:
            html = (WEB / page).read_text()
            for line, tag, directive, prev in self.chains(html):
                self.assertIn(prev, ("v-if", "v-else-if"),
                              f"{page}:{line} <{tag} {directive}> follows an element with "
                              f"{prev!r} — an orphan branch, and Vue then fails to compile "
                              f"the whole template, not just this element")


class AdminIsAViewNotAPage(unittest.TestCase):
    """The admin used to be a third document, so opening it was a navigation:
    the player's <audio> element was torn down and the music stopped. The owner,
    after the view handover: "the handover stops and restarts the stream. What
    I'm asking, is can we write the admin into the same page so the music
    doesn't need to stop?" (2026-09-29). So the player mounts it as a component
    and nothing is navigated.
    """

    def test_the_player_does_not_navigate_to_the_admin(self):
        html = (WEB / "desktop.html").read_text()
        self.assertNotIn('href="admin/"', html,
                         "the player still LINKS to the admin, which tears the page down "
                         "and stops the stream — it has to be a view change")
        self.assertIn("<admin-panel", html, "the player does not mount the admin at all")

    def test_one_definition_serves_both_hosts(self):
        """The player and /admin/ mount the same component from the same markup.
        Two copies would drift, and the standalone page is what the phone's
        gear icon opens."""
        js = (WEB / "admin" / "admin.js").read_text()
        self.assertIn("window.AdminPanel", js, "the admin is not exposed as a component")
        self.assertIn("panel.html", (WEB / "desktop.html").read_text() + js,
                      "nothing fetches the shared markup")
        solo = (WEB / "admin" / "index.html").read_text()
        self.assertIn("<admin-panel", solo, "/admin/ no longer mounts the shared component")
        self.assertNotIn("createApp({\n  components: { \"tag-input\"", solo,
                         "/admin/ still carries its own copy of the app")

    def test_the_admin_css_cannot_restyle_the_player(self):
        """They share a document now. Both stylesheets had a .card, a .head, a
        .body, a .row and a .tag; unscoped, the admin's would silently repaint
        the wall."""
        css = (WEB / "admin" / "admin.css").read_text()
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)      # comments are not selectors
        rules = re.findall(r"([^{}]+)\{", css)
        loose = []
        for sel in rules:
            for part in sel.split(","):
                part = part.strip()
                if not part or part.startswith("@") or part.startswith("%"):
                    continue
                # the standalone page's own chrome, and bare element defaults
                # for that page, are allowed to sit outside the scope
                if part.startswith(".adminpanel") or part.startswith(".solobar") or part == "body":
                    continue
                loose.append(part)
        self.assertEqual(loose, [],
                         f"admin.css rules outside .adminpanel would reach the player: {loose}")


class SpeakerTargetParity(unittest.TestCase):
    """The two pages drift, and always the same way: something is built on the
    phone and the desktop never gets it. index.html redirects a wide screen to
    desktop.html automatically, so whatever is missing there is missing for
    anyone on a computer — silently, because both pages still work.

    This began as one check for one omission (the local speaker) and caught
    nothing after it: by 2026-09-28 the desktop had also never gained the
    heart, station artwork, the receiver remote or resume. One check per
    capability, named for what a listener loses without it.
    """

    def script(self, name: str) -> str:
        """A page's EFFECTIVE surface: its own script plus what it mixes in.

        The members the two pages agreed on to the byte moved to shared.js and
        both pages now `mixins: [RadioShared]`. A parity check that read only
        app.js or only desktop.js would report a feature missing the moment it
        stopped being duplicated — which is exactly backwards (2026-09-29)."""
        own = (WEB / name).read_text()
        shared = (WEB / "shared.js")
        return own + ("\n" + shared.read_text() if shared.exists() else "")

    def test_the_desktop_page_knows_about_the_local_speaker(self):
        self.assertIn("speaker", self.script("desktop.js"),
                      "desktop.js has no speaker support: a wide screen gets redirected here "
                      "and loses the local-speaker target entirely")

    def test_both_pages_offer_the_heart(self):
        """A toggle, mirrored to Jellyfin. It reached the phone on 2026-09-27;
        the desktop kept the "Never" / "Less of them" pair it replaced."""
        for page, script in zip(PAGES, SCRIPTS):
            js = self.script(script)
            self.assertIn("toggleHeart", js, f"{script} cannot set the heart")
            self.assertIn("admin/api/heart", js, f"{script} never reads the heart back")
            self.assertIn("hearted", (WEB / page).read_text(),
                          f"{page} never shows whether the track is hearted")

    def test_both_pages_show_station_artwork(self):
        """now/tiles.json and admin/api/art are served for both pages; the
        desktop ignored them and drew a list of names."""
        for script in SCRIPTS:
            js = self.script(script)
            self.assertIn("now/tiles.json", js, f"{script} never loads the cover manifest")
            self.assertIn("admin/api/art", js, f"{script} never asks for a cover")

    def test_both_pages_tell_the_OS_what_is_playing(self):
        """Without MediaSession the phone's notification is a bare pause button
        over the page title — no track, no artist, no cover (2026-09-28).
        Skip is wired to `nexttrack`; there is no heart action to wire, and no
        amount of wanting one changes MediaSessionAction's closed list."""
        for script in SCRIPTS:
            js = self.script(script)
            self.assertIn("MediaMetadata", js, f"{script} never tells the OS what is playing")
            self.assertIn('setActionHandler', js, f"{script} sets no media control handlers")
            self.assertIn('"nexttrack"', js, f"{script} does not offer skip on the notification")

    def test_the_now_screen_falls_back_to_the_station_picture(self):
        """An ambient bed has no track, so no track cover — and the phone's hero
        showed a monogram even though the station has a picture of its own
        (2026-09-29). The desktop had this via heroCovers; the phone did
        not, which is the usual direction of drift reversed."""
        self.assertIn("heroArt", self.script("app.js"),
                      "app.js has no station-picture fallback for the hero")
        self.assertIn("heroArt", (WEB / "index.html").read_text(),
                      "index.html's hero does not use it")

    def test_the_rail_does_not_report_a_status_it_never_fetched(self):
        """The desktop rail lists every output at once. Only the SELECTED one
        was ever polled, so the others rendered straight from the component's
        defaults — a speaker that was playing read "stopped" until you clicked
        it (2026-09-29). A status nobody asked for is a guess, and it
        must not look like a fact.
        """
        js = self.script("desktop.js")
        self.assertIn("refreshZones", js,
                      "desktop.js never polls the outputs that are not selected")
        for flag in ("speakerSeen", "receiverSeen"):
            self.assertIn(flag, js, f"desktop.js cannot tell whether {flag} is known or assumed")
        self.assertIn('"checking…"', js,
                      "the rail asserts a state before the first answer instead of saying so")
        # and it has to actually run: on load, and on the tick
        self.assertIn("this.refreshZones();", js, "refreshZones is defined but never called")

    def test_both_pages_reconnect_a_dropped_stream(self):
        """Every deploy restarts Liquidsoap and drops every listener. Both pages
        used to set "stream error — try again" and stop, so a browser left
        playing went quiet until somebody found the tab (2026-09-29).
        Re-requesting the mount also wakes the encoder, so the retry both
        restarts it and reattaches."""
        for script in SCRIPTS:
            js = self.script(script)
            self.assertIn("scheduleReconnect", js, f"{script} gives up on a dropped stream")
            self.assertIn('addEventListener("ended"', js,
                          f"{script} ignores a clean shutdown, which is what a Liquidsoap restart looks like")
            self.assertIn("RECONNECT_GIVE_UP", js, f"{script} would retry for ever")

    def test_switching_views_carries_the_stream_over(self):
        """The two pages are separate documents, so the view links are real
        navigations: the document is torn down and the <audio> element with it,
        and the stream stops. The owner: "it shouldn't need to drop the stream just
        to change CSS around" (2026-09-29). Both ends have to play their part —
        the page leaving writes the note, the page arriving takes it — so this
        checks both, in both files."""
        for script in SCRIPTS:
            js = self.script(script)
            self.assertIn("noteHandover", js, f"{script} leaves nothing behind when it goes")
            self.assertIn("takeHandover", js, f"{script} ignores what the other view left")
            self.assertIn("radio.handover", js, f"{script} does not use the shared key")
            self.assertIn("HANDOVER_MS", js,
                          f"{script} would resume a stream from an arbitrarily old visit")
            self.assertIn('addEventListener("pagehide"', js,
                          f"{script} never writes the note — unload is not fired by a bfcache-ing browser")
            self.assertIn("this.takeHandover();", js, f"{script} defines takeHandover but never calls it")
            # An autoplay refusal must leave the page honest rather than
            # showing a transport that looks like it is playing.
            self.assertIn("NotAllowedError", js,
                          f"{script} treats a refused autoplay as an ordinary error")
            self.assertIn("blocked", js, f"{script} forgets what it was refused, so nothing can resume it")

    def test_the_browser_is_an_output_with_a_volume_like_the_others(self):
        """The receiver and the sound card had a level; the tab had none, so the
        only way down was the OS mixer (2026-09-29)."""
        js = self.script("desktop.js")
        self.assertIn("applyHereVolume", js, "desktop.js cannot set the tab's own volume")
        self.assertIn("radio.volume", js, "the level is forgotten on reload")
        html = (WEB / "desktop.html").read_text()
        self.assertNotIn('class="vol" v-if="target !== \'here\'"', html,
                         "the volume control is still hidden for this browser")

    def test_the_rain_bed_is_mixed_at_the_listener(self):
        """Some music sits well over rain. Mixing it into the STATION would put
        rain under everyone who tuned in — including a receiver that cannot turn
        it off — and one level cannot suit both a soft track and a loud one
        (2026-09-29). So it is a second stream with its own level, and
        the page must not be able to broadcast it."""
        js = self.script("desktop.js")
        html = (WEB / "desktop.html").read_text()
        self.assertIn("applyBed", js, "desktop.js cannot play a bed")
        self.assertIn("radio.bedVolume", js, "the bed has no level of its own")
        self.assertIn('ref="bed"', html, "there is no second audio element to mix")
        self.assertIn("site.bedMount", html,
                      "the panel is shown whether or not a bed mount is configured")

    def test_mute_silences_the_rain_as_well_as_the_music(self):
        """Mute is this browser going quiet. It reached only the <audio> with the
        music in it, so the rain bed carried on playing over the silence
        (2026-09-29). The bed keeps its own LEVEL — that is what its
        slider is for — and mute rides on top of it."""
        js = self.script("desktop.js")
        self.assertIn("this.hereMuted ? 0 : this.bedVol", js,
                      "the bed's level ignores mute")
        self.assertIn("b.muted = this.hereMuted", js, "the bed element is never muted")
        # and mute has to REACH it: setting the flag is no good if nothing applies it
        self.assertRegex(js, r"a\.muted = this\.hereMuted;\s*\n\s*this\.applyBed\(\);",
                         "applyHereVolume does not push the change to the bed")

    def test_the_os_transport_pauses_rather_than_stops(self):
        """The keyboard's play/pause key, the lock screen and a headset button
        all talk to whichever page holds the media session — and a page only
        holds one while it has media LOADED. `stop()` throws the src away, which
        ends the session, so the key had nothing to come back to and pressing
        play did nothing (2026-09-29).

        So the key pauses: the element stays loaded, the session stays ours, and
        the state we report has to be the truth or the OS shows the wrong icon
        and sends the wrong action back.
        """
        for script in SCRIPTS:
            js = self.script(script)
            self.assertIn("pauseHere", js, f"{script} has no pause that keeps the session")
            self.assertIn('set("pause", () => this.pauseHere())', js,
                          f"{script} still hands the pause key to stop(), which ends the session")
            self.assertIn('set("play", () => this.resumeHere())', js,
                          f"{script} does not resume what was paused")
            self.assertNotIn('ms.playbackState = "playing";', js,
                             f"{script} reports 'playing' unconditionally — it is wrong while paused")
            self.assertIn('this.paused ? "paused" : "playing"', js,
                          f"{script} never reports the paused state to the OS")

    def test_the_desktop_page_can_drive_and_recover_the_receiver(self):
        """Without resume, a receiver left stopped by a deploy stays stopped
        until somebody notices it went quiet (2026-09-27)."""
        js = self.script("desktop.js")
        self.assertIn("receiver/menu/cursor", js, "desktop.js cannot drive the receiver's menus")
        self.assertIn("admin/api/resume", js, "desktop.js cannot put the receiver back")


class OnePageAtEveryWidth(unittest.TestCase):
    """Narrowing the window must re-lay the page out where it stands.

    The player had no width media query at all — every rule assumed three
    columns — so a narrow screen got a banner telling it to go to the phone
    page instead, and crossing the threshold meant a navigation, a fresh
    document and a dropped stream. The owner: "switch the viewport to mobile but
    keep the same page logic and don't reload anything. Just redo the CSS"
    (2026-09-29).
    """

    def test_the_player_has_a_narrow_layout(self):
        css = (WEB / "desktop.css").read_text()
        widths = re.findall(r"@media[^{]*max-width:\s*(\d+)px", css)
        self.assertTrue(widths, "desktop.css still has no width breakpoint — "
                                "a narrow screen gets the three-column layout")
        self.assertIn(".app.sheet .rail", css,
                      "the rail has no narrow form, so it would sit on top of the content")

    def test_nothing_sends_you_to_another_page_when_it_gets_narrow(self):
        html = (WEB / "desktop.html").read_text()
        js = (WEB / "desktop.js").read_text()
        # the ELEMENT, not the word: the comment explaining its removal says
        # "escape" too, and a substring test failed on that
        self.assertNotRegex(html, r'class="escape"',
                            "the 'go to the phone view' banner is still there — the page "
                            "should lay itself out instead of sending you away")
        # a resize handler may re-measure, but it must never navigate
        for m in re.finditer(r'addEventListener\("resize",(.{0,160})', js, re.S):
            self.assertNotRegex(m.group(1), r"location\s*\.\s*(replace|assign|href)",
                                "resizing navigates, which drops the stream")


class ThePlayButtonNamesWhereItPlays(unittest.TestCase):
    """The station screen had ONE button. It read "Play here" and started the
    station on whatever output was selected — its own tooltip said "Play on
    <target>", which is the tell that the label and the behaviour disagreed.

    2026-11-02: "The button literally says 'Play here' under the station
    track. 'Here' is usually interpreted as 'in my browser'." From a phone
    outside the house that press took the living room off a film. Naming the
    destination on the button removes the ambiguity rather than catching it,
    which is why this is the real fix and the warning is only the net.
    """

    def test_there_are_two_buttons_and_each_says_where(self):
        html = (WEB / "desktop.html").read_text()
        acts = html[html.index('<div class="acts" v-else-if="openStationObj.mount">'):]
        acts = acts[: acts.index("</div>")]
        self.assertIn("playHere(openStationObj)", acts, "no button that means THIS BROWSER")
        self.assertIn("playTarget(openStationObj)", acts, "no button that means the chosen output")
        self.assertIn("Play on {{ targetName }}", acts,
                      "the second button does not name the output it will use")

    def test_play_here_cannot_reach_another_output(self):
        """Whatever is selected, "here" is this browser — and the selector is
        moved to match, so the button and the state can never disagree again."""
        js = (WEB / "shared.js").read_text()
        body = js[js.index("    playHere(s) {"):]
        body = body[: body.index("\n    },")]
        self.assertIn('this.target = "here"', body, "playHere leaves the output selector alone")
        self.assertIn("this.play(", body, "playHere does not play in this browser")
        self.assertNotIn("playTarget", body, "playHere can still reach another output")

    def test_the_unlabelled_card_button_names_its_destination_too(self):
        """It is an icon with no words, so the tooltip is the only thing that
        can say where it is about to play."""
        html = (WEB / "desktop.html").read_text()
        self.assertIn("'Play on ' + targetName", html,
                      "the card's play button still says only 'Play'")


class AskingHappensInThePage(unittest.TestCase):
    """2026-11-02: "that should be an in-page modal not a browser modal,
    those are ugly." """

    def test_nothing_calls_window_confirm(self):
        # Comments stripped: the code EXPLAINS what it replaced, and naming it
        # in that explanation is not calling it. The uncommented version of
        # this failed on its own rationale — twice now, in this file.
        for f in ("shared.js", "app.js", "desktop.js"):
            js = re.sub(r"//.*", "", (WEB / f).read_text())
            self.assertNotIn("window.confirm", js, f"{f} still uses the browser dialog")

    def test_the_dialog_is_one_component_both_pages_mount(self):
        """Same reason the remote is one component: two copies drift, and last
        time the phone ended up missing a feature entirely."""
        for page in PAGES:
            html = (WEB / page).read_text()
            self.assertIn("<ask-panel", html, f"{page} does not mount the dialog")
        for script in SCRIPTS:
            js = (WEB / script).read_text()
            self.assertIn('"ask-panel": askPanel(', js, f"{script} never registers it")
        self.assertTrue((WEB / "ask-panel.html").exists())
        self.assertTrue((WEB / "dialog.css").exists())

    def test_the_question_resolves_whichever_way_it_is_answered(self):
        """A promise that is never resolved is a station that never starts and
        a page with no way back."""
        js = (WEB / "shared.js").read_text()
        body = js[js.index("    askAnswer(yes) {"):]
        body = body[: body.index("\n    },")]
        self.assertIn("this.ask = null", body, "the dialog is never dismissed")
        self.assertIn("a.resolve(", body, "the caller is left waiting forever")

    def test_the_gate_waits_for_the_answer(self):
        """`confirmInterrupt` returns a promise now. Unawaited it is truthy,
        and the station would start while the question was still on screen."""
        js = (WEB / "shared.js").read_text()
        body = js[js.index("    async startOnReceiver("):]
        body = body[: body.index("\n    },")]
        self.assertIn("await this.confirmInterrupt(", body,
                      "the answer is not awaited — a promise object is always truthy")


class AnOpenTabLearnsItIsStale(unittest.TestCase):
    """This page is built never to reload — that is what keeps the stream alive
    across a view change — so a deploy is invisible to a tab already open, and
    a change that shipped correctly looks like it did not. It cost two rounds
    of "merged, tested, still broken" on 2026-11-02 against a page that was
    four minutes older than the fix.
    """

    def test_the_page_assets_are_stamped(self):
        nix = (Path(os.environ["NETRADIO_NIX"]).read_text()
               if os.environ.get("NETRADIO_NIX") else "")
        if not nix:
            self.skipTest("set NETRADIO_NIX")
        self.assertIn("build = builtins.baseNameOf", nix,
                      "site.json carries no build id, so nothing can notice a change")

    def test_the_page_compares_it_and_offers_a_reload(self):
        js = (WEB / "shared.js").read_text()
        body = js[js.index("    async checkBuild() {"):]
        body = body[: body.index("\n    },")]
        self.assertIn("site.json?b=", body, "the check is cacheable, so it may never see a change")
        self.assertIn("this.updateReady = true", body, "nothing is raised when the build changes")
        self.assertNotIn("location.reload", body,
                         "it reloads by itself — that drops the stream without asking")

    def test_the_reload_keeps_the_stream(self):
        js = (WEB / "shared.js").read_text()
        body = js[js.index("    reloadForUpdate() {"):]
        body = body[: body.index("\n    },")]
        self.assertIn("noteHandover", body, "reloading would drop what is playing")


class DoNotTakeTheRoomFromTheTelevision(unittest.TestCase):
    """Starting a station on the receiver takes it off whatever it was playing.
    Usually that is another station. When the receiver is on the television's
    input it is somebody's film — and the station button is reachable from a
    phone that is not in the house: "Interrupted the movie audio when I wasn't
    even at home... oops!" (2026-11-02).

    The cause was not a mis-click. The output selector is sticky, so the press
    that starts a station HERE and the press that takes the room away from the
    television are the same press, and nothing told them apart.
    """

    def test_every_station_start_asks_first(self):
        """Both pages, and both of the shared ones. A path that still calls
        receiverAction directly is a way to cut the sound off without asking."""
        for script in ("app.js", "desktop.js"):
            js = (WEB / script).read_text()
            body = js[js.index("playTarget(s) {"):]
            body = body[: body.index("\n    },")]
            self.assertNotIn("this.receiverAction(", body,
                             f"{script} starts a station on the receiver without asking")
            self.assertIn("this.startOnReceiver(", js, f"{script} never uses the guard")
        shared = (WEB / "shared.js").read_text()
        for name in ("resumeRoom", "tunerPreset"):
            seg = shared[shared.index(f"    {name}("):][:400]
            self.assertIn("this.startOnReceiver(", seg,
                          f"{name} puts the room back on a station without asking")

    def test_volume_and_mute_stay_immediate(self):
        """A confirmation on every press is a dialog people learn to dismiss
        without reading, which would cost us the one that matters. These are
        deliberate acts on a remote and take nothing from anybody."""
        shared = (WEB / "shared.js").read_text()
        for name in ("mute", "power", "setVolume", "playback"):
            seg = shared[shared.index(f"    {name}("):][:300]
            self.assertNotIn("startOnReceiver", seg,
                             f"{name} now prompts — the gate has spread to the controls")

    def test_the_television_input_is_configured_not_hardcoded(self):
        """OPTICAL is this house's wiring, not a fact about receivers."""
        shared = (WEB / "shared.js").read_text()
        self.assertIn("async tvInTheWay()", shared, "there is no guard to check")
        body = shared[shared.index("async tvInTheWay()"):]
        body = body[: body.index("\n    },")]
        self.assertIn("this.site.tvInput", body, "the TV input is not read from config")
        self.assertNotIn('"OPTICAL"', body, "OPTICAL is written into the page")

    def test_an_unreachable_box_is_not_a_clear_road(self):
        """The receiver being on the television's input is the stronger of the
        two signals. A diagnostic we could not read is no reason to decide the
        room is free — so the gate holds, and only a box that positively says
        it is idle opens it."""
        shared = (WEB / "shared.js").read_text()
        self.assertIn("async tvInTheWay()", shared, "there is no guard to check")
        body = shared[shared.index("async tvInTheWay()"):]
        body = body[: body.index("\n    },")]
        self.assertIn("if (!p.playing) return null;", body,
                      "nothing lets an idle television through without a prompt")
        self.assertRegex(body, r"if \(p && !p\.error\)",
                         "an unreachable Roku is treated as an answer")

    def test_the_module_ships_the_setting(self):
        nix = (Path(os.environ["NETRADIO_NIX"]).read_text()
               if os.environ.get("NETRADIO_NIX") else "")
        if not nix:
            self.skipTest("set NETRADIO_NIX")
        self.assertIn("tvInput", nix, "site.json never tells the page which input is the television")


class RemoteIsOnePanel(unittest.TestCase):
    """The remote is ONE component, mounted by both pages.

    It was two: a receiver-only sheet in index.html and a second copy in
    desktop.html that had grown the Roku half. Nothing said they had to match,
    so they did not — the phone had no Roku at all, which is the one place a
    remote is actually used: "The phone should have the roku, obviously. What
    am I sitting on the couch with" (2026-10-21).

    A third copy would have been the quick answer and the wrong one. These
    guard the structural fix instead.
    """

    def test_both_pages_mount_it(self):
        for page in PAGES:
            html = (WEB / page).read_text()
            self.assertIn("<remote-panel", html, f"{page} does not mount the remote")
            self.assertIn(':app="this"', html, f"{page} mounts it without handing it the page")
        for script in SCRIPTS:
            js = (WEB / script).read_text()
            self.assertIn('"remote-panel": remotePanel(', js,
                          f"{script} never registers the component it mounts")

    def test_neither_page_keeps_remote_markup_of_its_own(self):
        """The failure this whole change is about: a second copy that nobody
        remembers to update."""
        for page in PAGES:
            html = (WEB / page).read_text()
            self.assertNotIn('class="remote-wrap"', html,
                             f"{page} has grown its own remote again")
            self.assertNotIn("rokuKey(", html, f"{page} drives the Roku outside the panel")

    def test_the_styles_are_not_duplicated_back_into_the_page_sheets(self):
        """Same failure, one layer down: the two copies of this sheet differed
        only in how big a finger is, which is a viewport query, not a page."""
        self.assertTrue((WEB / "remote-panel.css").exists(), "the shared sheet is gone")
        for sheet in ("desktop.css", "remote.css"):
            css = (WEB / sheet).read_text()
            for rule in (".remote-wrap", ".dpad ", ".r-pair", ".rapp"):
                self.assertNotIn(f"\n{rule}", css,
                                 f"{sheet} styles {rule.strip()} again — it belongs to the panel")

    def test_everything_the_panel_reaches_for_actually_exists(self):
        """TemplateScope cannot see these: it only reads BARE identifiers, and
        every call here goes through `app.`, which that check deliberately
        skips. So a renamed method would be invisible to it and silent in the
        browser — the exact failure mode TemplateScope exists to catch."""
        html = (WEB / "remote-panel.html").read_text()
        # strip comments: the markup explains deleted buttons by name
        html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
        used = set(re.findall(r"\bapp\.([A-Za-z_$][\w$]*)", html))
        defined = set()
        for script in ("shared.js", *SCRIPTS):
            js = (WEB / script).read_text()
            defined |= set(re.findall(r"^\s{2,8}(?:async\s+)?([A-Za-z_$][\w$]*)\s*[({:]", js, re.M))
            # data keys share a line — `rokuPoll: 0, rokuText: "", rxOpen: false`
            # — and a line-anchored pattern sees only the first of them.
            defined |= set(re.findall(r"[\s,{]([A-Za-z_$][\w$]*)\s*:", js))
        missing = used - defined
        self.assertEqual(missing, set(),
                         f"the remote calls {sorted(missing)}, which no page or shared.js defines")

    def test_it_is_shipped(self):
        """Fetched at runtime, so a missing file is a remote that never opens
        and no build error anywhere."""
        nix = (Path(os.environ["NETRADIO_NIX"]).read_text()
               if os.environ.get("NETRADIO_NIX") else "")
        if not nix:
            self.skipTest("set NETRADIO_NIX")
        for f in ("remote-panel.html", "remote-panel.css"):
            self.assertIn(f, nix, f"{f} is never copied into the document root")


class RokuRemote(unittest.TestCase):
    """A second remote in the same modal.

    The owner put a Roku on the television, whose optical out feeds the R-N301 — so
    the amplifier owns the volume and the Roku owns navigation (2026-10-03).
    """

    def test_the_page_can_drive_a_roku(self):
        js = (WEB / "shared.js").read_text()
        html = (WEB / "remote-panel.html").read_text()
        self.assertIn("rokuKey", js, "no way to press a Roku key")
        self.assertIn("roku/status", js, "the page never asks what the Roku is doing")
        self.assertIn("rokuSend", js, "no text entry — the worst part of any streaming box")
        self.assertIn('@click="app.rokuKey(\'Home\')"', html, "no Home button")
        self.assertIn('v-model="app.rokuText"', html, "no field to type into")

    def test_volume_is_never_sent_to_the_roku(self):
        """Its volume keys emit CEC/IR at the television, and the television is
        not in the audio path — the sound leaves it by optical to the receiver.
        A volume button here would look right and do nothing."""
        js = (WEB / "shared.js").read_text()
        html = (WEB / "remote-panel.html").read_text()
        for key in ("VolumeUp", "VolumeDown", "VolumeMute"):
            self.assertNotIn(f"app.rokuKey('{key}')", html, f"{key} offered on the Roku remote")
            self.assertNotIn(f'roku/key/{key}', js, f"{key} sent to the Roku")

    def test_the_remote_is_hidden_when_there_is_no_roku(self):
        html = (WEB / "remote-panel.html").read_text()
        self.assertIn("site.hasRoku", html,
                      "the Roku keys show whether or not one is configured")

    def test_one_remote_rather_than_two_behind_tabs(self):
        """The receiver owns the volume and the Roku owns the picture, so
        turning down the show you are watching used to mean switching panes:
        "maybe the real prize here would be a combined remote, that can control
        the useful aspects of both" (2026-10-21)."""
        html = (WEB / "remote-panel.html").read_text()
        js = (WEB / "shared.js").read_text()
        self.assertNotIn("remoteTab", html, "the remote still splits into tabs")
        self.assertNotIn("remoteTab", js, "the tab state outlived the tabs")
        # The two halves have to be in one pane, not merely both present.
        pair = html[html.index('class="r-pair"'):html.index('class="r-pair"') + 1600]
        self.assertIn("app.rokuKey('Select')", pair, "the d-pad is not in the combined pane")
        self.assertIn("app.volumeStep(", pair, "the volume is not beside the d-pad")

    def test_the_buttons_this_hardware_cannot_use_are_gone(self):
        """Both were dead ends on THIS equipment (2026-10-21):

        Find remote beeps a paired Roku remote, and the physical remote in this
        house is an infrared knock-off the box has never heard of — "Find the
        remote is useless to me". Wake sent PowerOn when Home already wakes it.
        """
        # Comments stripped first: the markup EXPLAINS why these are gone, and
        # naming a key in that explanation is not shipping a button for it.
        # The uncommented version of this failed on its own rationale.
        html = re.sub(r"<!--.*?-->", "", (WEB / "remote-panel.html").read_text(), flags=re.S)
        self.assertNotIn("FindRemote", html, "Find-the-remote is back, and still beeps nothing")
        self.assertNotIn("PowerOn", html, "a Wake button is back; Home already does it")

    def test_quick_launch_comes_from_the_box_not_from_this_file(self):
        """The owner: "one for Jellyfin or Disney Plus etc that goes straight to the
        app, like the real remote has". The box lists what is installed, so a
        channel added tomorrow appears without anybody editing a page — and a
        channel that was uninstalled does not offer a button that 404s."""
        html = (WEB / "remote-panel.html").read_text()
        js = (WEB / "shared.js").read_text()
        self.assertIn("roku/apps", js, "nothing asks the box which apps it has")
        self.assertIn('v-for="a in app.rokuApps"', html, "the app row is not built from the list")
        self.assertIn("app.rokuLaunch(a)", html, "the app buttons launch nothing")
        for hardcoded in ("592369", "291097", "Disney Plus"):
            self.assertNotIn(hardcoded, html,
                             f"{hardcoded!r} is written into the page instead of read from the box")

    def test_opening_the_remote_before_site_json_lands_still_polls(self):
        """`hasRoku` is false until site.json arrives; `hasRoom` defaults TRUE,
        so the Remote button is on screen before the page knows there is a Roku
        at all. Opening it in that window left the Roku half empty for good —
        no apps, no model, a header reading "asleep" against a box that was
        playing. Caught in a headless render on 2026-10-21, not by any test
        here: the two tabs used to cover it, because changing tab polled again.
        """
        js = (WEB / "shared.js").read_text()
        self.assertIn('"site.hasRoku"(', js,
                      "nothing re-polls when the page learns there is a Roku")
        # And the gate has to live in the methods, or the late call is the one
        # that fires against a site with no Roku at all.
        self.assertIn("if (!this.site.hasRoku) return;", method_body(js, "pollRoku"),
                      "pollRoku asks a device the install does not have")

    def test_a_launch_failure_is_caught_like_every_other_press(self):
        """`call` throws; see RemoteErrorsReachSomebody for the bug this is."""
        js = (WEB / "shared.js").read_text()
        body = method_body(js, "rokuLaunch")
        self.assertIn("catch", body, "a refused launch is an unhandled rejection")
        self.assertIn("this.say(", body, "a refused launch says nothing to anybody")


class DevicesAreNamedForWhatTheyAre(unittest.TestCase):
    """The owner: "I want the labels to be explicit, the 'Living Room' device should
    be the name of the receiver, like the Roku Express 4K+ label" (2026-10-21).

    A room is where a box is, not what it is, and a list of places cannot tell
    you which box a button is about to talk to. Both devices report a model
    over their own API — the receiver answers "R-N301" — so the label is read
    from the equipment rather than configured twice.
    """

    def test_the_label_is_the_model_with_the_configured_name_as_fallback(self):
        js = (WEB / "shared.js").read_text()
        for name, field in (("receiverLabel", "receiver.model"), ("rokuLabel", "roku.model")):
            self.assertIn(f"{name}() {{", js, f"there is no {name}() to label anything with")
            body = js[js.index(f"{name}() {{"):]
            body = body[: body.index("\n")]
            self.assertIn(field, body, f"{name} ignores the model the device reports")
            self.assertIn("||", body, f"{name} has no fallback for a device that has not answered")

    def test_the_model_survives_the_poll(self):
        """Read off /status and kept. The poll REPLACES this object rather than
        merging into it, so a field left out of the assignment is silently
        blanked on the first refresh."""
        js = (WEB / "shared.js").read_text()
        body = method_body(js, "pollRoku")
        self.assertIn("model: s.model", body, "the Roku's model is dropped on every poll")

    def test_the_room_is_still_said_underneath(self):
        js = (WEB / "desktop.js").read_text()
        self.assertIn("this.where(", js, "the device list stopped saying which room it is in")

    def test_the_module_proxies_it_under_the_pages_own_vhost(self):
        """Loopback service, same access gate as the page — not a second port
        anyone has to think about."""
        nix = (Path(os.environ["NETRADIO_NIX"]).read_text()
               if os.environ.get("NETRADIO_NIX") else "")
        if not nix:
            self.skipTest("set NETRADIO_NIX")
        self.assertIn('"/roku/"', nix, "nothing proxies the Roku API")
        self.assertIn("hasRoku", nix, "site.json never tells the page there is one")


def method_body(js: str, name: str) -> str:
    """One method's source, sliced to the NEXT member at the same indent.

    A fixed-size window is not a method body: a 900-character slice from
    `rokuKey` ran past its end into `rokuSend` and found THAT method's `catch`,
    so the guard below passed against the very bug it was written for
    (2026-10-19). Same family as every other measurement that reported a pass
    by looking at the wrong region.
    """
    if f"async {name}(" not in js:
        raise AssertionError(f"there is no async {name}() in the script")
    start = js.index(f"async {name}(")
    rest = js[start:]
    nxt = re.search(r"\n    (?:async\s+)?[A-Za-z_$][\w$]*\s*\(", rest[1:])
    return rest[: nxt.start() + 1] if nxt else rest


class RemoteErrorsReachSomebody(unittest.TestCase):
    """`call()` THROWS on a non-2xx; it does not return an error object.

    The Roku remote was written the other way round — `const r = await call(…);
    if (r && r.error) …` — so every failure became an unhandled rejection and
    no message appeared anywhere. The box was refusing every key with a 403 in
    Limited mode and the page showed nothing at all: "none of the buttons are
    working in the remote" (2026-10-19).
    """

    def test_every_call_that_can_fail_is_caught(self):
        js = (WEB / "shared.js").read_text()
        for name in ("rokuKey", "rokuSend"):
            body = method_body(js, name)
            self.assertIn("catch", body,
                          f"{name} lets a rejected call() go nowhere — the failure is silent")
            self.assertIn("this.say(", body, f"{name} catches but tells nobody")

    def test_the_page_does_not_test_a_returned_error_field(self):
        """The tell for the old shape. `call` either returns the body or
        throws, so `r.error` is a branch that can never be taken."""
        js = (WEB / "shared.js").read_text()
        # Scoped to the methods in question: a bare search for "r.error" also
        # matches inside `receiver.error` and `this.roku.error`, which are
        # ordinary state and nothing to do with this.
        for name in ("rokuKey", "rokuSend"):
            body = method_body(js, name)
            self.assertNotRegex(body, r"\br\s*&&\s*r\.error",
                                f"{name} branches on a returned .error — call() throws "
                                f"instead, so that branch can never be taken")

    def test_limited_mode_is_shown_before_a_button_is_pressed(self):
        """Nothing here can clear it: it is a setting on the television. Said
        once, up front, rather than one toast per refused press."""
        js = (WEB / "shared.js").read_text()
        html = (WEB / "remote-panel.html").read_text()
        self.assertIn("limited: !!s.limited", js, "status carries it but the page drops it")
        self.assertIn('v-if="app.roku.limited"', html, "nothing warns before the first press")
        self.assertIn("Control by mobile apps", html, "the warning does not say where the setting is")


class WhatIsOnTheTelevision(unittest.TestCase):
    """The owner: "when we're watching jellyfin we do have more visibility into
    what's playing. Could we merge that in there" (2026-10-09). The Roku can
    only ever name the app; Jellyfin is the server and knows the item."""

    def test_the_page_asks_and_shows_it(self):
        js = (WEB / "shared.js").read_text()
        desk = (WEB / "desktop.html").read_text()
        self.assertIn("admin/api/watching", js, "nothing asks what is playing")
        # It lives among the outputs, not beside the history: "it would make
        # more sense than having it in recently played songs list" (2026-10-19)
        self.assertIn("tvZone()", desk, "the television is not in the device list")
        self.assertIn('class="zone info"', desk,
                      "it renders as a selectable zone — a station cannot be sent to a television")

    def test_the_names_and_the_clock_come_from_different_places(self):
        """Two sources with complementary blind spots, verified live on
        2026-10-21: the Roku's Jellyfin session reports
        `SupportsRemoteControl: false` and `CanSeek: false` and no supported
        commands, so Jellyfin can be ASKED nothing — but it still knows the
        item is "Little Bear · S1E4–6 · Birthday Soup". /query/media-player
        knows `position 135636 ms` of `duration 1447781 ms` and calls it
        "Jellyfin". Neither is a progress bar with a title on it alone.

        So the merge is the feature, and the clock must prefer the BOX:
        Jellyfin's position is the last progress report a client sent and can
        be ten seconds stale.
        """
        js = (WEB / "shared.js").read_text()
        self.assertIn("roku/player", js, "nothing asks the box how far through it is")
        body = js[js.index("tvNow() {"):]
        body = body[: body.index("\n    },")]
        self.assertIn("w.playing ? w.head", body, "the names are not taken from the media server")
        self.assertIn("p.duration ? p.position", body,
                      "the position does not prefer the box's own clock")
        self.assertIn("p.live", body, "a live channel still gets a progress bar")

    def test_it_rides_an_existing_poll_rather_than_a_timer_of_its_own(self):
        """A timer of its own would poll a media server every few seconds for a
        panel most evenings will not show. The wide page takes it on the ten-
        second tick, for the television's row in the device list; the phone has
        no such row, so it comes along with the Roku poll while the remote is
        open — otherwise the phone shows a progress bar with no title on it."""
        desk = (WEB / "desktop.js").read_text()
        shared = (WEB / "shared.js").read_text()
        self.assertRegex(desk, r"setInterval\(\(\) => \{[^}]*pollWatching\(\)",
                         "the wide page stopped taking it on the tick")
        self.assertIn("this.pollWatching()", method_body(shared, "pollRoku"),
                      "the phone never asks what is playing, so its remote has no title")


class TrackNotices(unittest.TestCase):
    """A notice when the track changes. Windows gives a web page no per-track
    toast of its own — the media session buys transport controls, not a notice
    — and the owner wanted what the phone does: "it would just be handy to see what
    came on the radio" (2026-09-29)."""

    def test_it_is_off_until_asked_for_and_asked_from_a_gesture(self):
        js = (WEB / "desktop.js").read_text()
        html = (WEB / "desktop.html").read_text()
        self.assertIn("Notification.requestPermission", js, "nothing ever asks for permission")
        self.assertIn('@click="askNotify"', html,
                      "permission is not requested from a click — a browser refuses otherwise, "
                      "and an uninvited prompt is rude")
        self.assertNotRegex(js, r"mounted\(\)[\s\S]{0,800}requestPermission",
                            "permission is requested on load rather than on request")
        self.assertIn('localStorage.setItem("radio.notify"', js, "the choice is forgotten on reload")

    def test_one_notice_at_a_time_and_none_for_what_was_already_playing(self):
        js = (WEB / "desktop.js").read_text()
        self.assertIn('tag: "netradio-track"', js,
                      "without a tag an evening's listening stacks a tower of notices")
        self.assertIn("_noticeReady", js,
                      "the track already playing when the page opens would notify on load")
        self.assertIn("noticeKey", js,
                      "watching the track object directly re-fires on every poll that "
                      "returns the same track")


class SharedHalf(unittest.TestCase):
    """The members the two pages agreed on to the byte live in one file.

    app.js and desktop.js grew side by side: 82 method names in common, and the
    SpeakerTargetParity class above exists only to catch the drift. Every change
    this month had to be written twice. The 36 byte-identical members moved to
    shared.js, which both pages mix in; Vue gives a component's own options
    priority, so the 45 that genuinely differ keep their own version and win
    (2026-09-29).
    """

    def members(self, path):
        src = (WEB / path).read_text()
        kw = {"if", "for", "while", "switch", "catch", "return", "typeof",
              "function", "do", "else", "try", "new", "await", "delete", "in", "of", "case"}
        return {m.group(1) for m in re.finditer(r"^\s{4}(?:async\s+)?([A-Za-z_$][\w$]*)\s*\(", src, re.M)
                if m.group(1) not in kw}

    def test_both_pages_mix_in_the_shared_half(self):
        self.assertTrue((WEB / "shared.js").exists(), "shared.js is gone")
        for page, script in zip(PAGES, SCRIPTS):
            js = (WEB / script).read_text()
            self.assertIn("mixins: [RadioShared]", js, f"{script} does not mix in the shared half")
            html = (WEB / page).read_text()
            self.assertIn('src="shared.js"', html, f"{page} never loads shared.js")
            # order matters: the page's script reads RadioShared at definition time
            self.assertLess(html.index("shared.js"), html.index(f'src="{script}"'),
                            f"{page} loads {script} before shared.js, so RadioShared is undefined")

    def test_an_override_has_to_actually_differ(self):
        """A page may override a shared member — that is how the ones that
        genuinely differ stay different. But an override with the SAME body is
        the duplication coming back, and nothing else would catch it."""
        import re as _re
        shared_src = (WEB / "shared.js").read_text()

        def bodies(src):
            kw = {"if", "for", "while", "switch", "catch", "return", "typeof", "function",
                  "do", "else", "try", "new", "await", "delete", "in", "of", "case"}
            starts = [(m.start(), m.group(1)) for m in
                      _re.finditer(r"^\s{4}(?:async\s+)?([A-Za-z_$][\w$]*)\s*\(", src, _re.M)
                      if m.group(1) not in kw]
            out = {}
            for i, (pos, name) in enumerate(starts):
                end = starts[i + 1][0] if i + 1 < len(starts) else len(src)
                out[name] = _re.sub(r"\s+", " ", _re.sub(r"//[^\n]*", "", src[pos:end])).strip()
            return out

        sh = bodies(shared_src)
        for script in SCRIPTS:
            own = bodies((WEB / script).read_text())
            same = [n for n in set(sh) & set(own) if sh[n] == own[n]]
            self.assertEqual(same, [],
                             f"{script} redefines {sorted(same)} identically to shared.js — "
                             f"that is the duplication returning, delete the copy")


class Packaging(unittest.TestCase):
    """Every asset a page asks the browser for must be one the derivation
    copies. Nothing else catches this: an uncopied stylesheet is a 404 at
    runtime, and the page then renders unstyled but otherwise working — which
    reads as a CSS bug rather than a missing file.
    """

    def test_every_local_asset_a_page_references_is_installed(self):
        nix_path = os.environ.get("NETRADIO_NIX")
        if not nix_path or not Path(nix_path).exists():
            self.skipTest("no NETRADIO_NIX — this runs for real in the netradio-web build")
        nix = Path(nix_path).read_text()
        for page in SERVED_PAGES:
            html = (WEB / page).read_text()
            for ref in re.findall(r'(?:href|src)\s*=\s*"([^"]+)"', html):
                if ref.startswith(("http://", "https://", "#", "data:", "/")):
                    continue
                # resolved against the PAGE's directory: the admin page reaches
                # its shared files with ../, and a naive strip would look for
                # them in the wrong place and silently skip the check
                target = ((WEB / page).parent / ref.split("?")[0]).resolve()
                if not target.is_file() or not target.is_relative_to(WEB.resolve()):
                    continue          # written at build time (site.json) or by the scanner (now/…)
                name = target.name
                # A WHOLE path component, not a substring. `assertIn("radio.js", nix)`
                # passed against "internet-radio.json", so a genuinely missing
                # file looked installed (found reviewing this, 2026-09-29).
                if not re.search(rf"(?<![\w.-]){re.escape(name)}(?!\w)", nix):
                    self.fail(f"{page} loads {name}, which default.nix never copies into $out — "
                              f"it 404s on the deployed page")


if __name__ == "__main__":
    unittest.main()
