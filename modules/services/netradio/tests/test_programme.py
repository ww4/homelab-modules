import datetime as dt
import json
import logging
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from netradio import config, dj
from test_dj import FakeLS, FakeTTS

logging.disable(logging.CRITICAL)


class ProgrammeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.cfg = config.Config(d / "config")
        self.station = {"mount": "country", "name": "Classic Country", "kind": "curated", "family": ["country"]}
        self.cfg.seed(
            {"western-swing": {"title": "Western Swing", "status": "ready", "count": 20, "family": ["country"], "rule": {}}},
            [self.station],
            [{"id": "swing", "station": "country", "name": "Western Swing Hour", "kind": "feed", "feed": "western-swing",
              "days": "daily", "start": "10:00", "minutes": 60},
             {"id": "spot", "station": "country", "kind": "auto", "like": "artist", "days": "daily", "start": "20:00", "minutes": 60}])
        self.cfg._write("artists.json", {"George Jones": {"tracks": 40, "families": ["country"], "slug": "george-jones"},
                                          "Boston": {"tracks": 30, "families": ["rock"], "slug": "boston"}})
        pools = d / "pools"
        (pools / "feeds").mkdir(parents=True)
        (pools / "artists").mkdir(parents=True)
        (pools / "feeds" / "western-swing.m3u").write_text("#EXTM3U\n" + "".join(f"/m/Bob Wills/A/0{i} s{i}.mp3\n" for i in range(6)))
        (pools / "artists" / "george-jones.m3u").write_text("#EXTM3U\n" + "".join(f"/m/George Jones/A/0{i} g{i}.mp3\n" for i in range(6)))
        base = d / "playlists"
        base.mkdir()
        (base / "country.m3u").write_text("#EXTM3U\n" + "".join(f"/m/Hank Williams/A/0{i} h{i}.mp3\n" for i in range(6)))
        self.prog = dj.Programme(self.station, self.cfg, pools, lastfm_key="")
        self.ls = FakeLS()
        self.tts = FakeTTS()
        self.dj = dj.StationDJ("country", "Classic Country", base / "country.m3u", d / "b", self.ls, self.tts,
                               rng=random.Random(3), breaks_every=(50, 50), programme=self.prog, now_dir=d / "now")
        self.read_tags = mock.patch.object(dj, "read_tags", side_effect=lambda p: dj.Track(p, Path(p).stem[3:], Path(p).parent.parent.name))
        self.read_tags.start()
        # FakeLS speaks for q_x only; teach it this mount
        self.ls.command = self._cmd
        self.rid = 0

    def _cmd(self, cmd):
        if cmd.startswith("q_country.push "):
            self.rid += 1
            self.ls.pending.append(self.rid)
            self.ls.pushed.append(cmd[len("q_country.push "):])
            return str(self.rid)
        if cmd == "q_country.queue":
            return " ".join(str(r) for r in self.ls.pending)
        raise AssertionError(cmd)

    def tearDown(self):
        self.read_tags.stop()
        self.tmp.cleanup()

    def artists_in(self, uris):
        return {Path(u.split(":")[-1]).parent.parent.name for u in uris if not u.startswith("annotate:")}

    def test_base_outside_segments(self):
        self.prog.clock = lambda: dt.datetime(2026, 9, 16, 12, 0)
        self.dj.fill()
        self.assertEqual(self.artists_in(self.ls.pushed), {"Hank Williams"})
        self.assertIsNone(self.dj.segment)

    def test_segment_switch_queues_intro_and_draws_from_the_feed(self):
        self.prog.clock = lambda: dt.datetime(2026, 9, 16, 10, 5)
        self.dj.fill()
        self.assertTrue(self.ls.pushed[0].startswith("annotate:"))                 # the intro break first
        self.assertIn("Western Swing Hour", self.tts.texts[0])
        self.assertIn("Classic Country", self.tts.texts[0])
        self.assertEqual(self.artists_in(self.ls.pushed), {"Bob Wills"})
        nxt = json.loads((Path(self.tmp.name) / "now" / "country-next.json").read_text())
        self.assertEqual(nxt["segment"], {"name": "Western Swing Hour", "kind": "feed"})
        # the hour ends: back to the base, no second intro
        self.prog.clock = lambda: dt.datetime(2026, 9, 16, 11, 5)
        for _ in range(3):
            self.ls.play(); self.dj.fill()
        self.assertIn("Hank Williams", self.artists_in(self.ls.pushed))
        self.assertEqual(len(self.tts.texts), 1)

    def test_auto_spotlight_picks_a_compatible_artist_and_says_so(self):
        self.prog.clock = lambda: dt.datetime(2026, 9, 16, 20, 10)
        self.dj.fill()
        self.assertIn("George Jones", self.tts.texts[0])                            # not Boston (rock)
        self.assertEqual(self.artists_in(self.ls.pushed), {"George Jones"})
        picks = self.cfg.picks()
        self.assertEqual(picks["spot"][0]["value"], "George Jones")

    def test_promo_in_every_other_break(self):
        self.prog.clock = lambda: dt.datetime(2026, 9, 16, 12, 0)
        self.dj.breaks_every = (1, 1)
        self.dj.until_break = 0
        for _ in range(6):
            self.dj.fill(); self.ls.play()
        promos = [t for t in self.tts.texts if "8 PM" in t]
        self.assertTrue(promos, self.tts.texts)
        self.assertTrue(any("George Jones" in t or "spotlight" in t for t in promos))
        self.assertLess(len(promos), len(self.tts.texts))                          # not every break


if __name__ == "__main__":
    unittest.main()
