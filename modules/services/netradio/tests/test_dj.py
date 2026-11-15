import logging
import random
import tempfile
import re
import unittest
from pathlib import Path
from unittest import mock

from netradio import dj

logging.disable(logging.CRITICAL)


def T(title, artist, path=None):
    return dj.Track(path or f"/m/{artist}/{title}.mp3", title, artist)


class Compose(unittest.TestCase):
    def test_mentions_previous_and_next(self):
        prev = [T("Sweet Dixie", "Art Stamper"), T("Whoa Mule", "Adam Tanner"), T("Sugar Hill", "Art Stamper")]
        nxt = T("Double Banjo Blues", "Reno and Smiley")
        for seed in range(20):
            text = dj.compose(prev, nxt, "Bluegrass and Old-Time", random.Random(seed))
            for needle in ("Sugar Hill", "Whoa Mule", "Sweet Dixie", "Double Banjo Blues", "Reno and Smiley"):
                self.assertIn(needle, text, text)
            self.assertNotIn("{", text)
            self.assertNotIn("  ", text)

    def test_varies_wording(self):
        prev = [T("A", "X"), T("B", "Y"), T("C", "Z")]
        texts = {dj.compose(prev, T("D", "W"), "Rock", random.Random(s)) for s in range(40)}
        self.assertGreater(len(texts), 10)

    def test_first_break_with_one_or_no_previous(self):
        text = dj.compose([], T("D", "W"), "Rock", random.Random(3))
        self.assertIn("D", text)
        self.assertIn("W", text)
        text = dj.compose([T("A", "X")], T("D", "W"), "Rock", random.Random(3))
        self.assertIn("A", text)
        self.assertIn("D", text)

    def test_at_most_three_earlier_tracks_named(self):
        prev = [T(f"T{i}", f"A{i}") for i in range(8)]
        text = dj.compose(prev, T("N", "B"), "Folk", random.Random(1))
        self.assertIn("T7", text)                      # the one that just finished
        self.assertNotIn("T0", text)
        self.assertNotIn("T3", text)
        self.assertIn("T4", text)


class Cleaning(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(dj.clean("Whoa Mule (Live) [Remastered]"), "Whoa Mule")
        self.assertEqual(dj.clean('  "Sugar Hill" -- '), "Sugar Hill")
        self.assertEqual(dj.clean("()"), "untitled")

    def test_read_tags_falls_back_to_path(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "Art Stamper" / "Goodbye Girls" / "03 Hickory Jack.mp3"
            p.parent.mkdir(parents=True)
            p.write_bytes(b"not audio")
            t = dj.read_tags(str(p))
            self.assertEqual((t.title, t.artist), ("Hickory Jack", "Art Stamper"))

    def test_annotate_escapes(self):
        uri = dj.annotate({"title": 'Say "hi", now', "dj": "true"}, "/x/a b.wav")
        self.assertEqual(uri, 'annotate:title="Say \\"hi\\", now",dj="true":/x/a b.wav')


class FakeLS:
    """Liquidsoap's server, enough of it to tell the two SOURCES apart.

    A station is `fallback(track_sensitive=true, [q, pl])` — the DJ's queue and
    the station's plain shuffle — and either one can hold the air. A skip aimed
    at the QUEUE does nothing at all while the shuffle is playing, which is how
    a dead skip button shipped on 2026-09-29: the old fake had a single source,
    so a skip sent to the wrong one still counted as a skip.

    `on_air` is what a listener hears. `play()` ends it, as a track running out
    would, and the fallback re-selects: the queue if it has anything, else the
    shuffle.
    """

    def __init__(self, no_flush=False):
        self.pending = []
        self.pushed = []
        self.rid = 0
        self.no_flush = no_flush      # an older Liquidsoap that knows neither command
        self.shuffled = 0
        self.on_air = self._shuffle_track()   # nothing queued yet, so the shuffle covers
        self.heard = [self.on_air]            # everything that has held the air, in order

    def _shuffle_track(self):
        """The shuffle hands out a different track each time it is asked, so a
        skip that lands can be told from one that did nothing."""
        self.shuffled += 1
        return f"shuffle-{self.shuffled}"

    def _select(self):
        """What the fallback picks at a track boundary: the queue if it has
        anything, else the station's shuffle."""
        self.on_air = self.pending.pop(0) if self.pending else self._shuffle_track()
        self.heard.append(self.on_air)

    @property
    def shuffle_on_air(self):
        return isinstance(self.on_air, str) and self.on_air.startswith("shuffle")

    def command(self, cmd):
        if cmd.startswith("q_x.push "):
            self.rid += 1
            uri = cmd[len("q_x.push "):]
            self.pending.append(self.rid)
            self.pushed.append(uri)
            return str(self.rid)
        if cmd == "q_x.queue":
            return " ".join(str(r) for r in self.pending)
        if cmd.startswith("q_x.ignore "):
            # Liquidsoap 2.4 has no such command; the real server answers this
            # way and the DJ must never depend on it again (2026-09-25)
            return "ERROR: unknown command, type \"help\" to get a list of commands."
        if cmd == "q_x.flush_and_skip":
            # Liquidsoap's own: empties the queue and skips THE QUEUE's track.
            # Silent whenever the shuffle is the source on air.
            if self.no_flush:
                return "ERROR: unknown command, type \"help\" to get a list of commands."
            self.flushed = getattr(self, "flushed", 0) + 1
            self.pending = []
            if not self.shuffle_on_air:
                self.skipped = getattr(self, "skipped", 0) + 1
                self._select()
            return "Done"
        if cmd == "src_x.flush":
            # ours: drop what is queued, leave the air alone
            if self.no_flush:
                return "ERROR: unknown command, type \"help\" to get a list of commands."
            self.flushed = getattr(self, "flushed", 0) + 1
            self.pending = []
            return "Done"
        if cmd == "src_x.skip":
            # ours: end whatever the FALLBACK selected, queue or shuffle
            self.skipped = getattr(self, "skipped", 0) + 1
            self._select()
            return "Done"
        raise AssertionError(cmd)

    def play(self):
        """A track runs out and the fallback re-selects."""
        self._select()


class FakeTTS:
    def __init__(self, fail=False):
        self.texts = []
        self.fail = fail

    def render(self, text):
        self.texts.append(text)
        if self.fail:
            raise RuntimeError("kokoro down")
        return b"RIFF" + text.encode()


class StationDJTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.pl = root / "x.m3u"
        lines = ["#EXTM3U"] + [f"/m/Artist{i}/Album/0{i} Song{i}.mp3" for i in range(12)]
        self.pl.write_text("\n".join(lines) + "\n")
        self.ls = FakeLS()
        self.tts = FakeTTS()
        self.dj = dj.StationDJ("x", "Test Station", self.pl, root / "dj" / "x", self.ls, self.tts,
                               rng=random.Random(7), breaks_every=(3, 3))
        self.read_tags = mock.patch.object(dj, "read_tags", side_effect=lambda p: dj.Track(p, Path(p).stem[3:], Path(p).parent.parent.name))
        self.read_tags.start()

    def tearDown(self):
        self.read_tags.stop()
        self.tmp.cleanup()

    def test_keeps_two_ahead_and_breaks_every_three(self):
        self.dj.fill()
        self.assertEqual(len(self.ls.pending), 2)
        # play items one by one; a break rides in front of its track, so the
        # queue holds two tracks plus at most one break
        for _ in range(12):
            self.ls.play()
            self.dj.fill()
            self.assertIn(len(self.ls.pending), (2, 3))
        kinds = ["break" if u.startswith("annotate:") else "track" for u in self.ls.pushed]
        # first three are tracks, then a break, then three tracks, ...
        self.assertEqual(kinds[:8], ["track", "track", "track", "break", "track", "track", "track", "break"])
        self.assertEqual(kinds.count("break"), 3)

    def test_break_names_the_three_previous_and_the_next(self):
        for _ in range(6):
            self.dj.fill()
            self.ls.play()
        text = self.tts.texts[0]
        pushed_tracks = [u for u in self.ls.pushed if not u.startswith("annotate:")]
        # split off the track number rather than slicing a fixed 3 chars: the
        # fixture numbers past 9, so stem[3:] left a leading space on "010 Song11"
        names = [Path(u).stem.split(" ", 1)[-1].strip() for u in pushed_tracks]
        # Match on WORD BOUNDARIES: the fixture's names are Song0..Song11, so a
        # plain substring test reports "Song1" as present whenever "Song11" is.
        # That produced a false failure the moment weighted picking changed which
        # tracks a seeded run chooses (2026-09-27) — the break was correct.
        def named(n):
            return re.search(rf"\b{re.escape(n)}\b", text) is not None
        for n in names[:3]:
            self.assertTrue(named(n), f"{n} should be named in: {text}")
        self.assertTrue(named(names[3]), f"{names[3]} (queued after the break) in: {text}")
        self.assertFalse(named(names[4]), f"{names[4]} should NOT be named in: {text}")
        break_uri = [u for u in self.ls.pushed if u.startswith("annotate:")][0]
        self.assertIn('liq_amplify="1.8"', break_uri)
        self.assertIn('title="Station break"', break_uri)
        self.assertTrue(break_uri.endswith("break-000001.wav"))
        self.assertTrue((self.dj.out_dir / "break-000001.wav").exists())

    def test_tts_failure_skips_break_and_keeps_music_going(self):
        self.tts.fail = True
        for _ in range(8):
            self.dj.fill()
            self.ls.play()
        self.assertFalse(any(u.startswith("annotate:") for u in self.ls.pushed))
        self.assertGreaterEqual(len(self.ls.pushed), 8)
        self.assertGreaterEqual(len(self.tts.texts), 2)  # it kept trying at each break point

    def test_no_repeat_until_pool_exhausted(self):
        for _ in range(12):
            self.dj.fill()
            self.ls.play()
        tracks = [u for u in self.ls.pushed if not u.startswith("annotate:")]
        self.assertEqual(len(tracks), len(set(tracks)))

    def test_empty_playlist_queues_nothing(self):
        self.pl.write_text("#EXTM3U\n")
        self.dj.fill()
        self.assertEqual(self.ls.pushed, [])

    def test_next_json_for_the_page(self):
        import json
        now = Path(self.tmp.name) / "now"
        self.dj.now_dir = now
        self.dj.fill()                                   # two tracks queued
        d = json.loads((now / "x-next.json").read_text())
        self.assertEqual(len(d["next"]), 2)
        self.assertEqual(d["next"][0]["kind"], "track")
        self.assertIn("artist", d["planned"])
        self.dj.until_break = 0
        self.ls.play(); self.dj.fill()                   # a break rides in front of the next track
        d = json.loads((now / "x-next.json").read_text())
        self.assertEqual([e["kind"] for e in d["next"]], ["track", "break", "track"])
        self.assertTrue(d["last_break"])

    def test_old_breaks_are_pruned(self):
        self.dj.breaks_every = (1, 1)
        self.dj.until_break = 0
        for _ in range(10):
            self.dj.fill()
            self.ls.play()
        self.assertLessEqual(len(list(self.dj.out_dir.glob("break-*.wav"))), dj.KEEP_BREAKS)


if __name__ == "__main__":
    unittest.main()


class TheSameSongDoesNotFollowItself(unittest.TestCase):
    """2026-11-02: "I'm listening to the western swing station, and it
    just plays Hot Rod Lincoln over and over again."

    Liquidsoap's own log had the queue preparing ONE file three times, as RIDs
    75, 93 and 288 — pushes from three different fill cycles. Two things let
    that happen, and both are guarded here.
    """

    HRL_A = "/srv/media/Music/Asleep At The Wheel/Unknown Album/00 Hot Rod Lincoln.mp3"
    HRL_B = "/srv/media/Music/Asleep At The Wheel/Unknown Album/Hot Rod Lincoln.mp3"
    OTHER = "/srv/media/Music/Asleep At The Wheel/Collision Course/04 One O'Clock Jump.mp3"

    def test_two_rips_of_one_song_are_one_song(self):
        """The real pair out of the library: the same recording twice in one
        folder, differing only by a leading track number. A no-repeat list
        keyed on the path sees two unrelated tracks."""
        self.assertEqual(dj.song_key(self.HRL_A), dj.song_key(self.HRL_B))
        self.assertNotEqual(dj.song_key(self.HRL_A), dj.song_key(self.OTHER))

    def test_a_different_artist_with_the_same_title_is_a_different_song(self):
        """Commander Cody's Hot Rod Lincoln is not Asleep At The Wheel's, and
        a station is allowed to play both."""
        other_artist = "/srv/media/Music/Commander Cody/Lost in the Ozone/03 Hot Rod Lincoln.mp3"
        self.assertNotEqual(dj.song_key(self.HRL_A), dj.song_key(other_artist))

    def _station(self, tracks):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        pl = root / "x.m3u"
        pl.write_text("\n".join(["#EXTM3U"] + tracks) + "\n")
        return dj.StationDJ("x", "Test", pl, root / "dj" / "x", FakeLS(), FakeTTS(),
                            rng=random.Random(3), breaks_every=(99, 99)), root

    def test_the_no_repeat_list_survives_a_rebuild(self):
        """The station object is rebuilt whenever the station comes back on
        air. The list used to live only in memory, so every rebuild forgot
        what had just been queued — and the Liquidsoap queue is drained only
        while somebody is listening, so the forgotten track was often still
        sitting in it."""
        tracks = [f"/m/Artist/Album/{i:02d} Song{i}.mp3" for i in range(20)]
        station, root = self._station(tracks)
        with mock.patch.object(dj, "read_tags", side_effect=lambda p: dj.Track(p, "t", "a")):
            first = [station.choose().path for _ in range(5)]
        self.assertEqual(len(set(first)), 5, "it repeated within one lifetime")

        # same playlist, same directory: a fresh object, as a restart makes
        rebuilt = dj.StationDJ("x", "Test", root / "x.m3u", root / "dj" / "x",
                               FakeLS(), FakeTTS(), rng=random.Random(3), breaks_every=(99, 99))
        with mock.patch.object(dj, "read_tags", side_effect=lambda p: dj.Track(p, "t", "a")):
            after = [rebuilt.choose().path for _ in range(5)]
        self.assertFalse(set(first) & set(after),
                         "the rebuilt station replayed what the old one had just queued")

    def test_the_window_does_not_swallow_a_small_station(self):
        """NO_REPEAT is 300. On a station of 20 tracks a 300-deep memory
        excludes every candidate, the `or playable` fallback takes over, and
        the guard quietly does nothing — which looks exactly like working."""
        station, _ = self._station([f"/m/A/B/{i:02d} S{i}.mp3" for i in range(20)])
        with mock.patch.object(dj, "read_tags", side_effect=lambda p: dj.Track(p, "t", "a")):
            station.choose()
        self.assertLessEqual(station.recent.maxlen, 10,
                             "the window is wider than the station, so it guards nothing")
        self.assertGreaterEqual(station.recent.maxlen, 1)


class Feedback(unittest.TestCase):
    """The admin's inbox files: skip, and a request that jumps the queue."""
    setUp = StationDJTests.setUp
    tearDown = StationDJTests.tearDown

    def test_skip_and_request(self):
        import json
        self.dj.fill()
        pending_before = list(self.ls.pending)
        self.assertEqual(len(pending_before), 2)
        self.tracks = ["/m/Artist3/Album/03 Song3.mp3"]
        inbox = Path(self.tmp.name) / "dj" / "inbox"        # where the admin API writes: <dj>/inbox, not <dj>/<mount>/inbox
        self.assertEqual(self.dj.inbox, inbox)
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "x-1.json").write_text(json.dumps({"action": "skip"}))
        (inbox / "x-2.json").write_text(json.dumps({"action": "request", "path": self.tracks[0]}))
        self.dj.fill()
        self.assertEqual(self.ls.skipped, 1)
        self.assertFalse(list(inbox.glob("x-*.json")))                     # consumed
        # A SKIP drops what was queued behind it, because those items carry a
        # break describing a running order that is not going to happen now
        # (2026-09-29). Liquidsoap cannot remove one item, so the queue goes
        # and fill() writes it again in the same pass — see the next test for
        # the request case, where nothing is dropped.
        self.assertEqual(self.ls.flushed, 1)
        for rid in pending_before:
            self.assertNotIn(rid, self.ls.pending)
        self.assertIn("Song3", " ".join(self.tts.texts))                   # announced on air by title
        self.assertTrue(any(self.tracks[0] in u for u in self.ls.pushed[-4:]))
        self.assertTrue(any(e.get("request") for e in self.dj.pushed))

    def test_several_requests_share_one_announcement_and_keep_their_order(self):
        import json
        self.dj.fill()
        inbox = Path(self.tmp.name) / "dj" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        for i, song in enumerate(("First", "Second", "Third")):
            (inbox / f"x-{1000000000000 + i}-0000.json").write_text(
                json.dumps({"action": "request", "path": f"/m/AAA/Album/0{i} {song}.mp3"}))
        before = len(self.tts.texts)
        self.dj.handle_inbox()
        # ONE break for the batch, naming all three and both askers
        breaks = self.tts.texts[before:]
        self.assertEqual(len(breaks), 1, breaks)
        for name in ("First", "Second", "Third"):
            self.assertIn(name, breaks[0])
        # and the three tracks queued behind it, in the order they were asked
        reqs = [e for e in self.dj.pushed if e.get("request")]
        self.assertEqual([r["title"] for r in reqs], ["First", "Second", "Third"])

    def test_the_dj_never_names_a_requester(self):
        # there is no sign-in anywhere in this system: the DJ cannot know who
        # asked, and an unauthenticated name field would be anyone's to set
        import inspect
        from netradio import admin as admin_mod
        self.assertNotIn("who", inspect.getsource(admin_mod.Admin.request))
        text = dj.request_break([dj.Track("/p", "Song", "Artist")], random.Random(1))
        self.assertNotIn("for ", text)

    def test_the_dj_never_calls_a_command_liquidsoap_lacks(self):
        import json
        self.dj.fill()
        inbox = Path(self.tmp.name) / "dj" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "x-9.json").write_text(json.dumps({"action": "request", "path": "/m/AAA/Album/01 X.mp3"}))
        seen = []
        real = self.ls.command
        self.ls.command = lambda c: seen.append(c) or real(c)
        self.dj.handle_inbox()
        self.assertFalse([c for c in seen if ".ignore" in c],
                         "q.ignore does not exist in Liquidsoap 2.4 — see play_requests")

    def test_skip_waits_while_liquidsoap_is_down(self):
        """A press while Liquidsoap's socket is gone (a deploy) is kept for
        the next pass — unless it has gone stale, when it is dropped rather
        than fired minutes later at whatever is playing then."""
        import json
        import os
        import time
        inbox = Path(self.tmp.name) / "dj" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        real = self.ls.command
        self.ls.command = lambda cmd: (_ for _ in ()).throw(FileNotFoundError("no socket"))
        fresh = inbox / "x-1.json"
        fresh.write_text(json.dumps({"action": "skip"}))
        self.dj.handle_inbox()
        self.assertTrue(fresh.exists())                                    # kept for retry
        stale = inbox / "x-0.json"
        stale.write_text(json.dumps({"action": "skip"}))
        old = time.time() - 600
        os.utime(stale, (old, old))
        self.dj.handle_inbox()
        self.assertFalse(stale.exists())                                   # too old: dropped
        self.assertTrue(fresh.exists())
        self.ls.command = real
        self.dj.handle_inbox()
        self.dj.finish_skip()          # fill() does this; the skip is taken after the rewrite
        self.assertEqual(self.ls.skipped, 1)                               # fired once the socket is back
        self.assertFalse(fresh.exists())

    def test_skip_refused_is_logged_not_swallowed(self):
        import json
        inbox = Path(self.tmp.name) / "dj" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        self.ls.command = lambda cmd: 'ERROR: unknown command, type "help" to get a list of commands.'
        (inbox / "x-1.json").write_text(json.dumps({"action": "skip"}))
        logging.disable(logging.NOTSET)          # the module silences logging; this test reads it
        try:
            with self.assertLogs("netradio.dj", level="ERROR") as cm:
                self.dj.handle_inbox()
                self.dj.finish_skip()
        finally:
            logging.disable(logging.CRITICAL)
        self.assertTrue(any("skip refused" in line for line in cm.output))
        self.assertFalse(list(inbox.glob("x-*.json")))


class Excursion(unittest.TestCase):
    """The fringe list beside the playlist: a small share of base picks, none
    while a segment is on, none when the station sets excursion 0."""
    setUp = StationDJTests.setUp
    tearDown = StationDJTests.tearDown

    def test_fringe_share(self):
        (Path(self.tmp.name) / "x-fringe.m3u").write_text("#EXTM3U\n/m/Fringe/Album/01 Edge.mp3\n")
        self.dj.rng = random.Random(3)
        picks = [self.dj.choose().path for _ in range(300)]
        fringe = sum("Fringe" in p for p in picks)
        self.assertEqual(len(self.dj.fringe), 1)
        self.assertTrue(15 <= fringe <= 50, fringe)            # ~10% of 300
        self.dj.excursion = 0.0
        self.assertFalse(any("Fringe" in self.dj.choose().path for _ in range(100)))

    def test_no_fringe_file_means_no_excursion(self):
        self.dj.load_playlist()
        self.assertEqual(self.dj.fringe, [])
        self.assertFalse(any("Fringe" in self.dj.choose().path for _ in range(50)))


class BreakFilesSurviveARestart(unittest.TestCase):
    """The DJ renders a break to break-NNNNNN.wav, queues that path, and prunes
    older ones. Liquidsoap only opens the file when the break's turn comes,
    minutes later — so anything that removes it in between loses the break
    silently: Liquidsoap logs `Nonexistent file or ill-formed URI` and plays on,
    while the DJ's own log still says it queued a break.

    2026-09-28: the counter restarted at 0 with every DJ restart, so the first
    break was written as break-000001.wav; the prune kept "the newest
    KEEP_BREAKS by NAME"; and 000001 sorted FIRST among the previous run's
    leftovers, so the file just written was the one deleted. Two deploys that
    evening cost two breaks on the station being listened to.
    """

    def _dj(self, out_dir, breaks_made):
        """A DJ with the counter it would really have. `breaks_made=0` is a DJ
        that has just restarted — the case that broke."""
        d = dj.StationDJ.__new__(dj.StationDJ)
        d.mount, d.name = "modern-old-time", "Modern Old-time"
        d.out_dir = out_dir
        d.voice_gain = "1.8"
        d.breaks_made = breaks_made
        d.tts = mock.Mock()
        d.tts.render.return_value = b"RIFFfake-wav"
        return d

    @staticmethod
    def _leftovers(out, lo=2, hi=6):
        for n in range(lo, hi):
            (out / f"break-{n:06d}.wav").write_bytes(b"old")

    def test_a_restarted_dj_does_not_delete_the_break_it_just_wrote(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t)
            self._leftovers(out)                 # break-000002 … break-000005
            d = self._dj(out, breaks_made=0)     # …and a counter back at zero

            uri = d.render_break("Here is the news.")
            self.assertIsNotNone(uri)
            path = Path(uri[uri.rindex(":") + 1:])          # annotate:…:<path>
            self.assertTrue(path.exists(),
                            f"the DJ deleted the break it had just written ({path.name}); "
                            f"Liquidsoap skips it and the station goes quiet")

    def test_the_counter_resumes_from_what_is_on_disk(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t)
            self._leftovers(out)
            self.assertEqual(dj._highest_break(out), 5)
            d = self._dj(out, breaks_made=dj._highest_break(out))
            uri = d.render_break("Hello.")
            self.assertTrue(Path(uri[uri.rindex(":") + 1:]).name.endswith("000006.wav"),
                            "numbering restarted and would overwrite a file still on disk")

    def test_an_empty_directory_starts_at_one(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(dj._highest_break(Path(t)), 0)

    def test_the_directory_stays_bounded(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t)
            d = self._dj(out, breaks_made=0)
            for i in range(10):
                d.render_break(f"Break {i}.")
            self.assertLessEqual(len(list(out.glob("break-*.wav"))), dj.KEEP_BREAKS,
                                 "the prune stopped bounding the directory")


class HowItReadsOutASet(unittest.TestCase):
    """Consecutive tracks by one artist are gathered rather than repeating the
    name, and a record is sometimes placed in time (2026-09-29):
    "Uncle Pen, Molly and Tenbrooks and Wheel Hoss from Bill Monroe", and
    "Blue Night by Hot Rize, from their 1985 album Traditional Ties".
    """

    def t(self, title, artist, album="", year=""):
        return dj.Track(f"/m/{artist}/{title}.mp3", title, artist, album, year)

    def test_a_run_by_one_artist_names_them_once(self):
        run = [self.t("Uncle Pen", "Bill Monroe"), self.t("Wheel Hoss", "Bill Monroe"),
               self.t("Molly and Tenbrooks", "Bill Monroe")]
        said = dj.say_tracks(run, random.Random(3))
        self.assertEqual(said.count("Bill Monroe"), 1, f"the name is repeated: {said}")
        for title in ("Uncle Pen", "Wheel Hoss", "Molly and Tenbrooks"):
            self.assertIn(title, said)

    def test_only_CONSECUTIVE_tracks_are_gathered(self):
        """The order is what was played. Grouping a non-adjacent artist would
        reorder the set and make the sentence untrue."""
        mixed = [self.t("A", "Monroe"), self.t("B", "Flatt"), self.t("C", "Monroe")]
        said = dj.say_tracks(mixed, random.Random(3))
        self.assertEqual(said.count("Monroe"), 2, f"non-adjacent tracks were gathered: {said}")

    def test_different_artists_are_still_named_separately(self):
        two = [self.t("A", "Monroe"), self.t("B", "Flatt")]
        said = dj.say_tracks(two, random.Random(3))
        self.assertIn("Monroe", said)
        self.assertIn("Flatt", said)

    def test_a_record_can_be_placed_in_time(self):
        t = self.t("Blue Night", "Hot Rize", "Traditional Ties", "1985")
        saids = {dj.say_track(t, random.Random(s), place=True) for s in range(40)}
        self.assertTrue(any("1985" in x for x in saids), "never mentions the year")
        self.assertTrue(any("Traditional Ties" in x for x in saids), "never mentions the album")

    def test_it_never_invents_a_year_it_does_not_have(self):
        bare = self.t("Song", "Artist")           # no album, no year in the tags
        for seed in range(30):
            said = dj.say_track(bare, random.Random(seed), place=True)
            self.assertNotIn("album", said, f"placed a track with no album: {said}")
            self.assertNotRegex(said, r"\b(18|19|20)\d\d\b", f"invented a year: {said}")

    def test_it_does_not_say_the_year_twice_when_the_album_carries_it(self):
        t = self.t("Song", "Artist", "Bluegrass 1959", "1959")
        for seed in range(40):
            said = dj.say_track(t, random.Random(seed), place=True)
            self.assertLessEqual(said.count("1959"), 1, f"said the year twice: {said}")

    def test_a_break_gathers_the_run_it_just_played(self):
        prev = [self.t("Wheel Hoss", "Bill Monroe"), self.t("Uncle Pen", "Bill Monroe")]
        nxt = self.t("Blue Night", "Hot Rize")
        text = dj.compose(prev, nxt, "Bluegrass", random.Random(5))
        self.assertEqual(text.count("Bill Monroe"), 1, f"repeated the name on air: {text}")


class SkipRewritesTheSpot(unittest.TestCase):
    """A break is written when its track is queued and names what came before
    and what comes next. Skip or "never" a song — "never" sends a skip too —
    and everything queued is describing a running order that will not happen,
    so the DJ announces a track the listener has just banned (
    2026-09-29). Liquidsoap 2.4 cannot remove one queued item, so the queue is
    dropped and fill() writes it again.
    """

    def test_a_request_on_its_own_does_not_clear_the_queue(self):
        """The older lesson, still true: a request joins the BACK of the queue.
        Only a skip clears it."""
        ls = FakeLS()
        ls.command("q_x.push annotate:a=1:/m/A/Al/01 One.mp3")
        before = list(ls.pending)
        ls.command("q_x.push annotate:a=1:/m/A/Al/02 Two.mp3")
        self.assertEqual(ls.pending[:1], before)
        self.assertEqual(getattr(ls, "flushed", 0), 0)

    def _dj(self, ls):
        d = dj.StationDJ.__new__(dj.StationDJ)
        d.mount, d.ls = "x", ls
        d.pushed, d.since_break, d.until_break = [{"a": 1}], [1], 3
        d.skip_when_filled = False
        return d

    def test_the_skip_takes_the_track_off_even_when_the_shuffle_is_playing(self):
        """THE regression (2026-09-29 15:24). After a deploy the queue is
        empty, so the fallback's shuffle holds the air — and the shuffle honours
        none of the listener's skips or nevers. Four presses against a Bowie
        track on Ambient did nothing and all 2:23 of it played, because the skip
        was being sent to the queue instead of to the fallback."""
        ls = FakeLS()
        self.assertTrue(ls.shuffle_on_air, "the fixture is not set up as a restart")
        was = ls.on_air
        d = self._dj(ls)
        d.skip_and_rewrite()
        d.finish_skip()
        self.assertNotEqual(ls.on_air, was, "the button did nothing: the same track kept playing")
        self.assertEqual(ls.skipped, 1, "skipped more than once")

    def test_the_queue_is_dropped_before_the_refill_and_skipped_after(self):
        """The order is the point. Flush first so the stale break goes; refill so
        there is a fresh one; skip LAST so the fallback has something of the DJ's
        to take over with, instead of dropping into the shuffle for a track."""
        ls = FakeLS()
        ls.command("q_x.push annotate:a=1:/m/A/Al/01 One.mp3")
        ls.play()                                   # the queue is on air now
        ls.command("q_x.push annotate:a=1:/m/A/Al/02 Stale.mp3")
        d = self._dj(ls)
        d.skip_and_rewrite()
        self.assertEqual(ls.pending, [], "the stale running order survived the flush")
        self.assertEqual(getattr(ls, "skipped", 0), 0, "skipped before the queue was rewritten")
        self.assertEqual(d.pushed, [], "the break state was not reset for a rewrite")
        ls.command("q_x.push annotate:a=1:/m/A/Al/03 Fresh.mp3")   # what fill() writes
        d.finish_skip()
        self.assertEqual(ls.skipped, 1)
        self.assertFalse(ls.shuffle_on_air, "dropped into the shuffle instead of the rewritten queue")

    def test_four_presses_in_a_row_are_one_skip(self):
        """The owner pressed it four times. Each press rewrites the queue; only one
        track comes off the air."""
        ls = FakeLS()
        d = self._dj(ls)
        for _ in range(4):
            d.skip_and_rewrite()
        d.finish_skip()
        self.assertEqual(ls.skipped, 1)

    def test_it_still_skips_when_liquidsoap_cannot_flush(self):
        """A stale break is a poor thing; a skip button that does nothing is
        worse."""
        d = self._dj(FakeLS(no_flush=True))
        d.skip_and_rewrite()
        d.finish_skip()
        self.assertEqual(d.ls.skipped, 1, "the skip never happened")
        self.assertEqual(d.pushed, [{"a": 1}], "state was cleared although nothing was flushed")
