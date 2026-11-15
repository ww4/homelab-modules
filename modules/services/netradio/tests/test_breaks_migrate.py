import json
import logging
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from netradio import config, dj, migrate
from test_dj import FakeLS, FakeTTS

logging.disable(logging.CRITICAL)


class BreaksSpec(unittest.TestCase):
    def test_parsing(self):
        self.assertEqual(dj.breaks_spec(4), (4, 4))
        self.assertEqual(dj.breaks_spec("4"), (4, 4))
        self.assertEqual(dj.breaks_spec("3-5"), (3, 5))
        self.assertEqual(dj.breaks_spec(0), (0, 0))
        self.assertEqual(dj.breaks_spec(None), (3, 4))
        self.assertEqual(dj.breaks_spec("lots"), (3, 4))

    def test_zero_means_no_breaks_and_live_reload(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = config.Config(root / "config")
            station = {"mount": "x", "name": "X", "kind": "curated", "family": ["any"], "breaks_every": 0}
            cfg.seed({}, [station], [])
            (root / "x.m3u").write_text("#EXTM3U\n" + "".join(f"/m/A{i}/B/0{i} t{i}.mp3\n" for i in range(8)))
            ls, tts = FakeLS(), FakeTTS()
            prog = dj.Programme(station, cfg, root / "pools")
            with mock.patch.object(dj, "read_tags", side_effect=lambda p: dj.Track(p, Path(p).stem[3:], "A")):
                s = dj.StationDJ("x", "X", root / "x.m3u", root / "b", ls, tts, rng=random.Random(1),
                                 breaks_every=dj.breaks_spec(station["breaks_every"]), programme=prog)
                for _ in range(8):
                    s.fill(); ls.play()
                self.assertEqual(tts.texts, [])                       # never a break
                # The owner sets every 2 songs on the admin page: picked up without a restart
                import os, time
                cfg.save_stations([{**station, "breaks_every": 2}])
                os.utime(cfg.root / "stations.json", (time.time() + 5, time.time() + 5))
                for _ in range(8):
                    s.fill(); ls.play()
                self.assertGreaterEqual(len(tts.texts), 2)


class Migration(unittest.TestCase):
    def test_split_is_idempotent_and_moves_slots(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = config.Config(Path(d))
            cfg.seed({}, [{"mount": "country", "name": "C", "kind": "curated", "family": ["country"], "base": {}},
                         {"mount": "blues-jazz", "name": "Blues, Jazz & Soul", "kind": "curated", "family": ["blues-jazz"],
                          "base": {"genres": ["blues", "jazz"], "era": {"exclude": ["shellac"]}}, "breaks_every": 5},
                         {"mount": "gospel", "name": "G", "kind": "curated", "family": ["gospel"], "base": {}}],
                     [{"id": "s1", "station": "blues-jazz", "kind": "auto", "like": "artist", "start": "20:00"}])
            out = migrate.run(cfg)
            self.assertEqual(len(out), 7)
            self.assertTrue(any(s["mount"] == "rain" and s["kind"] == "fixed" for s in cfg.stations()))
            mounts = [s["mount"] for s in cfg.stations()]
            self.assertEqual(mounts, ["country", "blues", "jazz", "soul", "gospel", "rain", "rainymood", "celtic"])  # split in place, order kept; the new stations appended
            jazz = next(s for s in cfg.stations() if s["mount"] == "jazz")
            self.assertEqual(jazz["base"]["era"], {"exclude": ["shellac"]})
            self.assertEqual(jazz["base"]["fringe_genres"], migrate.FRINGE_GENRES["jazz"])   # yields to its neighbours
            self.assertIn("rock", next(s for s in cfg.stations() if s["mount"] == "country")["base"]["fringe_genres"])
            self.assertEqual(jazz["breaks_every"], 5)
            self.assertEqual(cfg.schedule()[0]["station"], "blues")
            self.assertTrue(list((Path(d) / "requests").glob("apply-*.json")))               # and an apply follows
            self.assertEqual(migrate.run(cfg), [])                                          # recorded: not again
            self.assertIn("split-blues-jazz-soul", cfg._read("migrations.json", {}))

    def test_existing_feeds_keep_shellac(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = config.Config(Path(d))
            cfg.seed({"a": {"title": "A", "status": "ready"}, "b": {"title": "B", "status": "ready", "shellac": False}}, [], [])
            migrate.run(cfg)
            self.assertTrue(cfg.feeds()["a"]["shellac"])
            self.assertFalse(cfg.feeds()["b"]["shellac"])     # an explicit choice is kept

    def test_nothing_to_do_on_a_fresh_seed(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = config.Config(Path(d))
            cfg.seed({}, [{"mount": "blues", "name": "Blues", "kind": "curated"}], [])
            self.assertEqual(migrate.run(cfg)[:2], ["split-blues-jazz-soul: nothing to do", "feeds-keep-shellac: nothing to do"])


if __name__ == "__main__":
    unittest.main()


class CountryExcludesBluegrass(unittest.TestCase):
    def test_country_base_gains_the_clause_once(self):
        import tempfile
        from pathlib import Path
        from netradio import migrate
        from netradio.config import Config
        with tempfile.TemporaryDirectory() as d:
            cfg = Config(Path(d))
            cfg.save_stations([{"mount": "country", "kind": "curated", "base": {"genres": ["country"]}},
                               {"mount": "bluegrass", "kind": "curated", "base": {"genres": ["bluegrass"]}}])
            self.assertIn("excludes bluegrass", migrate.country_excludes_bluegrass(cfg))
            self.assertIn("bluegrass", cfg.stations()[0]["base"]["exclude_genres"])
            self.assertNotIn("exclude_genres", cfg.stations()[1]["base"])
            self.assertEqual(migrate.country_excludes_bluegrass(cfg), "nothing to do")


class Celtic(unittest.TestCase):
    def test_celtic_takes_the_celtic_words_and_folk_yields_them(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = config.Config(Path(d))
            cfg.seed({}, [{"mount": "folk", "name": "Folk", "kind": "curated", "family": ["folk"],
                           "base": {"genres": ["folk", "celtic", "irish", "americana"],
                                    "era": {"exclude": ["shellac"]}}}], [])
            migrate.add_celtic_station(cfg)
            folk = next(s for s in cfg.stations() if s["mount"] == "folk")
            celtic = next(s for s in cfg.stations() if s["mount"] == "celtic")
            # Folk keeps its own words, loses the Celtic ones, and names the roster
            self.assertEqual(folk["base"]["genres"], ["folk", "americana"])
            self.assertIn("celtic", folk["base"]["exclude_genres"])
            self.assertIn("Clannad", folk["base"]["exclude_artists"])
            self.assertIn("celtic", celtic["base"]["genres"])
            self.assertIn("Clannad", celtic["base"]["artists"])
            # ordered right after Folk, and idempotent
            self.assertEqual([s["mount"] for s in cfg.stations()], ["folk", "celtic"])
            self.assertEqual(migrate.add_celtic_station(cfg), "nothing to do")

    def test_a_folk_tagged_celtic_act_lands_on_celtic_only(self):
        from netradio import feeds
        with tempfile.TemporaryDirectory() as d:
            cfg = config.Config(Path(d))
            cfg.seed({}, [{"mount": "folk", "name": "Folk", "kind": "curated", "family": ["folk"],
                           "base": {"genres": ["folk", "celtic", "irish"]}}], [])
            migrate.add_celtic_station(cfg)
            folk = next(s for s in cfg.stations() if s["mount"] == "folk")["base"]
            celtic = next(s for s in cfg.stations() if s["mount"] == "celtic")["base"]
            # Clannad ships tagged plain "Folk" — the reason a tag-only split fails
            track = dict(artist="Clannad", path="/m/Clannad/Magical Ring/01 x.mp3", genre="Folk", yamnet=None, era="")
            self.assertFalse(feeds.matches(folk, **track))     # excluded by name
            self.assertTrue(feeds.matches(celtic, **track))    # kept by the roster
            # and a plain folk record is untouched by either exclusion
            other = dict(artist="Nic Jones", path="/m/Nic Jones/Penguin Eggs/01 y.mp3", genre="Folk", yamnet=None, era="")
            self.assertTrue(feeds.matches(folk, **other))
