import json
import logging
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from netradio import dj, profile, rules

logging.disable(logging.CRITICAL)


def feats(talk=0.0, stable=0.5, head=(0.0, 0.5), tail=(0.0, 0.5), dur=180.0):
    from dataclasses import asdict
    return asdict(profile.Facts(dur, talk, stable, head[0], head[1], tail[0], tail[1]))


def decide(f, title=""):
    return rules.evaluate("/m/x.mp3", f, title)


class Decide(unittest.TestCase):
    """The measured points from the labelled set (docstring in profile.py)."""

    def test_talk_tracks(self):
        for talk, stable in [(0.88, 0.00), (0.54, 0.00), (0.85, 0.00), (0.79, 0.01), (0.53, 0.07), (0.83, 0.00), (0.98, 0.10)]:
            self.assertTrue(decide(feats(talk, stable)).talk, (talk, stable))

    def test_songs_including_a_cappella(self):
        for talk, stable in [(0.00, 0.19), (0.48, 0.32), (0.02, 0.24), (0.10, 0.11), (0.00, 0.22), (0.22, 0.04), (0.10, 0.29)]:
            self.assertFalse(decide(feats(talk, stable)).talk, (talk, stable))

    def test_both_signals_must_agree(self):
        self.assertFalse(decide(feats(0.9, 0.5)).talk)   # speech-like but holding notes
        self.assertFalse(decide(feats(0.1, 0.0)).talk)   # glides but the model hears music

    def test_edges_only_when_the_track_is_music(self):
        v = decide(feats(0.05, 0.3, head=(0.9, 0.0), tail=(0.8, 0.02)))
        self.assertEqual((v.talk, v.head_talk, v.tail_talk), (False, True, True))
        v = decide(feats(0.9, 0.0, head=(0.9, 0.0), tail=(0.9, 0.0)))
        self.assertEqual((v.talk, v.head_talk, v.tail_talk), (True, False, False))

    def test_reason_mentions_title_hint(self):
        self.assertIn("title agrees", decide(feats(0.9, 0.0), "Band Intro").reason)
        self.assertNotIn("title", decide(feats(0.9, 0.0), "Cripple Creek").reason)


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.pj = self.dir / "profile.json"
        self.pj.write_text(json.dumps({
            "/m/a.mp3": {"v": 1, "title": "Intro", **feats(0.9, 0.0, head=(0.9, 0.0), tail=(0.9, 0.0), dur=30)},
            "/m/b.mp3": {"v": 1, "title": "Song", **feats(0.0, 0.3, tail=(0.8, 0.02), dur=200)},
            "/m/c.mp3": {"v": 1, "title": "Song2", **feats(0.0, 0.3, head=(0.8, 0.02), dur=200)},
        }))
        self.ov = self.dir / "overrides.json"
        self.ov.write_text(json.dumps({"/m/a.mp3": "music", "/m/b.mp3": "talk", "/m/zzz.mp3": "talk", "/m/c.mp3": "bogus"}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_verdicts_and_overrides(self):
        v = profile.Profile.load_verdicts(self.pj)
        self.assertTrue(v["/m/a.mp3"].talk)
        self.assertTrue(v["/m/b.mp3"].tail_talk)
        v = profile.Profile.load_verdicts(self.pj, self.ov)
        self.assertFalse(v["/m/a.mp3"].talk)           # override wins
        self.assertTrue(v["/m/b.mp3"].talk)
        self.assertTrue(v["/m/b.mp3"].tail_talk)       # edge hints survive an override
        self.assertTrue(v["/m/zzz.mp3"].talk)          # override for an unprofiled path
        self.assertTrue(v["/m/c.mp3"].head_talk)       # a bogus override value is ignored

    def test_missing_files_are_empty_not_fatal(self):
        self.assertEqual(profile.Profile.load_verdicts(self.dir / "nope.json"), {})
        self.assertEqual(profile.load_overrides(self.dir / "nope.json"), {})

    def test_cache_by_size_and_mtime(self):
        p = profile.Profile(self.pj)
        f = self.dir / "x.mp3"
        f.write_bytes(b"xx")
        st = f.stat()
        self.assertIsNone(p.current(str(f), st))
        p.data[str(f)] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "v": profile.PROFILE_VERSION}
        self.assertIsNotNone(p.current(str(f), st))
        p.data[str(f)]["v"] = profile.PROFILE_VERSION - 1   # measured by an older profiler
        self.assertIsNone(p.current(str(f), st))
        p.data[str(f)]["v"] = profile.PROFILE_VERSION
        f.write_bytes(b"xxx")
        self.assertIsNone(p.current(str(f), f.stat()))

    def test_report_lists_flagged(self):
        p = profile.Profile(self.pj)
        rep = self.dir / "report.txt"
        profile.write_report(p, rep, {"/m/b.mp3": "talk"})
        text = rep.read_text()
        self.assertIn("TALK      30s  'Intro'", text)             # by the rules
        self.assertIn("/m/b.mp3 (override)", text)                 # by hand
        self.assertIn("head ", text)


class DJUsesProfile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.m3u = d / "x.m3u"
        self.tracks = [f"/m/Art{i}/Alb/0{i} T{i}.mp3" for i in range(6)]
        self.m3u.write_text("#EXTM3U\n" + "\n".join(self.tracks) + "\n")
        self.pj = d / "profile.json"
        self.pj.write_text(json.dumps({
            self.tracks[0]: feats(0.9, 0.0),
            self.tracks[1]: feats(tail=(0.9, 0.0)),
            self.tracks[2]: feats(head=(0.9, 0.0)),
        }))
        self.read_tags = mock.patch.object(dj, "read_tags", side_effect=lambda p: dj.Track(p, Path(p).stem[3:], Path(p).parent.parent.name))
        self.read_tags.start()

    def tearDown(self):
        self.read_tags.stop()
        self.tmp.cleanup()

    def make(self, seed):
        from test_dj import FakeLS, FakeTTS
        ls = FakeLS()
        d = dj.StationDJ("x", "X", self.m3u, Path(self.tmp.name) / "b", ls, FakeTTS(),
                         rng=random.Random(seed), breaks_every=(50, 50), profile=self.pj)
        return d, ls

    def test_talk_track_never_queued(self):
        for seed in range(5):
            d, ls = self.make(seed)
            for _ in range(5):
                d.fill()
                ls.play()
            self.assertFalse(any(self.tracks[0] in u for u in ls.pushed))

    def test_transition_hints(self):
        d, ls = self.make(1)
        # queue everything, then inspect the URIs that were produced
        for _ in range(6):
            d.fill()
            ls.play()
        by_track = {}
        for u in ls.pushed:
            for t in self.tracks:
                if u.endswith(t):
                    by_track[t] = u
        self.assertIn('liq_cross_duration="0.5"', by_track[self.tracks[1]])   # ends in chatter
        self.assertIn('liq_fade_out="0"', by_track[self.tracks[1]])
        self.assertIn('liq_fade_in="0"', by_track[self.tracks[2]])            # starts with chatter
        self.assertEqual(by_track[self.tracks[4]], self.tracks[4])                # plain song: a plain path
        # whichever track precedes the head-talk one got a short exit
        order = [t for u in ls.pushed for t in self.tracks if u.endswith(t)]
        before = order[order.index(self.tracks[2]) - 1]
        self.assertIn('liq_cross_duration="0.5"', by_track[before])


if __name__ == "__main__":
    unittest.main()


class Loudness(unittest.TestCase):
    def test_gain_brings_a_track_to_target_without_clipping(self):
        g = rules.gain_for
        self.assertEqual(g({"loudness_lufs": -16.0, "true_peak_db": -3.0}), 0.0)
        self.assertEqual(g({"loudness_lufs": -20.0, "true_peak_db": -8.0}), 4.0)       # a quiet old record comes up
        self.assertEqual(g({"loudness_lufs": -9.0, "true_peak_db": -0.1}), -7.0)       # a loud master comes down
        self.assertEqual(g({"loudness_lufs": -24.0, "true_peak_db": -3.0}), 2.0)       # capped by the peak ceiling
        self.assertEqual(g({"loudness_lufs": -40.0, "true_peak_db": -30.0}), 15.0)     # and by the gain limit
        self.assertIsNone(g({}))                                                        # unmeasured: play as is
        self.assertIsNone(g({"loudness_lufs": -70.0}))                                  # silence: don't
        v = rules.evaluate("x.mp3", {"loudness_lufs": -20.0, "true_peak_db": -8.0})
        self.assertEqual(v.gain_db, 4.0)

    def test_summary_parses_ffmpeg_ebur128(self):
        summary = ("[Parsed_ebur128_0 @ 0x1] Summary:\n\n  Integrated loudness:\n    I:         -15.1 LUFS\n"
                   "    Threshold: -25.4 LUFS\n\n  Loudness range:\n    LRA:         6.3 LU\n\n  True peak:\n"
                   "    Peak:       -2.2 dBFS\n")
        got = {m.group(1): m.group(2) for m in profile.LOUDNESS_RE.finditer(summary)}
        self.assertEqual(got, {"I": "-15.1", "LRA": "6.3", "Peak": "-2.2"})
