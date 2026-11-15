import datetime as dt
import logging
import random
import tempfile
import unittest
from pathlib import Path

from netradio import config, feeds, schedule

logging.disable(logging.CRITICAL)

T = dict(path="/srv/media/Music/The Louvin Brothers/Album/01 x.mp3", artist="The Louvin Brothers",
         genre="Country", yamnet={"banjo": 0.4, "singing": 0.02}, era="vintage")


class Rules(unittest.TestCase):
    def test_artist_match_normalised_and_substring(self):
        self.assertTrue(feeds.matches({"artists": ["Louvin Brothers"]}, **T))
        self.assertTrue(feeds.matches({"artists": ["louvin brothers"]}, **{**T, "artist": "Ralph Stanley & The Louvin Brothers"}))
        self.assertTrue(feeds.matches({"artists": ["Louvin Brothers"]}, **{**T, "artist": ""}))   # folder name still hits
        self.assertFalse(feeds.matches({"artists": ["Delmore Brothers"]}, **T))

    def test_genre_and_instruments(self):
        self.assertTrue(feeds.matches({"genres": ["country"]}, **T))
        self.assertFalse(feeds.matches({"genres": ["western swing"]}, **T))
        self.assertTrue(feeds.matches({"instruments": {"banjo": [0.3, None], "singing": [None, 0.05]}}, **T))
        self.assertFalse(feeds.matches({"instruments": {"banjo": [0.5]}}, **T))
        self.assertFalse(feeds.matches({"instruments": {"banjo": [0.3]}}, **{**T, "yamnet": None}))

    def test_clauses_combine_with_and(self):
        # old-time fiddle: the genre AND the instrument
        rule = {"genres": ["country"], "instruments": {"banjo": [0.3]}}
        self.assertTrue(feeds.matches(rule, **T))
        self.assertFalse(feeds.matches(rule, **{**T, "genre": "Rock"}))
        self.assertFalse(feeds.matches(rule, **{**T, "yamnet": {"banjo": 0.1}}))
        # artists OR genres inside the who/what clause
        self.assertTrue(feeds.matches({"artists": ["Nobody"], "genres": ["country"]}, **T))
        self.assertTrue(feeds.matches({"artists": ["Louvin Brothers"], "genres": ["polka"]}, **T))
        self.assertFalse(feeds.matches({"artists": ["Nobody"], "genres": ["polka"]}, **T))

    def test_era(self):
        self.assertFalse(feeds.matches({"all": True, "era": {"only": ["shellac"]}}, **T))
        self.assertTrue(feeds.matches({"all": True, "era": {"only": ["shellac"]}}, **{**T, "era": "shellac"}))
        self.assertFalse(feeds.matches({"all": True, "era": {"only": ["shellac"]}}, **{**T, "era": ""}))   # unmeasured
        self.assertFalse(feeds.matches({"all": True, "era": {"exclude": ["shellac"]}}, **{**T, "era": "shellac"}))
        self.assertTrue(feeds.matches({"all": True, "era": {"exclude": ["shellac"]}}, **{**T, "era": ""}))

    def test_validate(self):
        self.assertEqual(feeds.validate({"artists": ["a"]}), [])
        self.assertTrue(feeds.validate({}))
        self.assertTrue(feeds.validate({"instruments": {"banjo": "high"}}))


class Families(unittest.TestCase):
    def test_compatible(self):
        self.assertTrue(config.compatible(["country"], ["country", "folk"]))
        self.assertFalse(config.compatible(["rock"], ["bluegrass"]))
        self.assertTrue(config.compatible(["rock"], ["any"]))
        self.assertTrue(config.compatible(None, ["rock"]))

    def test_ids(self):
        self.assertEqual(config.new_id("Western Swing!", set()), "western-swing")
        self.assertEqual(config.new_id("Western Swing", {"western-swing"}), "western-swing-2")


class Slots(unittest.TestCase):
    def setUp(self):
        self.slots = [
            {"id": "sat-swing", "station": "country", "name": "Western Swing Hour", "kind": "feed", "feed": "western-swing",
             "days": ["sat"], "start": "10:00", "minutes": 60},
            {"id": "ht", "station": "country", "name": "Honky Tonk Happy Hour", "kind": "feed", "feed": "honky-tonk",
             "days": "weekdays", "start": "17:00", "minutes": 60},
            {"id": "spot", "station": "country", "kind": "auto", "like": "artist", "days": "daily", "start": "20:00", "minutes": 60},
            {"id": "folk", "station": "folk", "name": "Old-Time Hour", "kind": "feed", "feed": "old-time", "days": "daily", "start": "09:00"},
        ]
        self.sat = dt.datetime(2026, 9, 19, 10, 30)   # a Saturday
        self.wed = dt.datetime(2026, 9, 16, 17, 15)

    def test_active_and_upcoming(self):
        self.assertEqual(schedule.active_slot("country", self.slots, self.sat)["id"], "sat-swing")
        self.assertIsNone(schedule.active_slot("country", self.slots, self.sat.replace(hour=12)))
        self.assertEqual(schedule.active_slot("country", self.slots, self.wed)["id"], "ht")
        up = schedule.upcoming("country", self.slots, self.wed.replace(hour=12))
        self.assertEqual([s["id"] for _, _, s in up], ["ht", "spot"])
        self.assertEqual([s["id"] for _, _, s in schedule.upcoming("country", self.slots, self.sat.replace(hour=9))], ["sat-swing", "spot"])

    def test_auto_resolves_once_per_day_and_avoids_recent(self):
        artists = {f"Artist {i}": {"tracks": 40, "families": ["country"]} for i in range(10)}
        artists["Rocker"] = {"tracks": 50, "families": ["rock"]}
        artists["Tiny"] = {"tracks": 12, "families": ["country"]}          # offered by hand, never auto-picked
        station = {"mount": "country", "family": ["country"]}
        picks = {}
        slot = self.slots[2]
        r1, changed = schedule.resolve(slot, self.wed.date(), picks, station=station, artists=artists, feeds={})
        self.assertTrue(changed)
        self.assertEqual(r1["kind"], "artist")
        self.assertIn(r1["artist"], artists)
        self.assertNotIn(r1["artist"], ("Rocker", "Tiny"))
        self.assertEqual(r1["name"], f"{r1['artist']} spotlight")
        r2, changed = schedule.resolve(slot, self.wed.date(), picks, station=station, artists=artists, feeds={})
        self.assertFalse(changed)
        self.assertEqual(r2["artist"], r1["artist"])                     # same day, same answer
        seen = {r1["artist"]}
        for d in range(1, 6):
            r, _ = schedule.resolve(slot, self.wed.date() + dt.timedelta(days=d), picks, station=station, artists=artists, feeds={})
            self.assertNotIn(r["artist"], seen)                          # a different one each day
            seen.add(r["artist"])

    def test_spotlight_score_wants_quintessential_and_plenty(self):
        score = schedule.spotlight_score
        jones = {"tracks": 120, "albums": 9, "families": ["country"], "share": {"country": 0.95}, "sound": {"country": 0.31}}
        untagged = {"tracks": 90, "albums": 4, "families": ["gospel"], "share": {}, "sound": {}}   # a feed gave it the family, nothing vouches
        delmores = {"tracks": 87, "albums": 4, "families": ["bluegrass", "country"], "share": {},
                    "feed_share": {"bluegrass": 1.0, "country": 1.0}, "sound": {}}                 # untagged, but the duets feed vouches
        martin = {"tracks": 151, "albums": 6, "families": ["bluegrass", "country"], "share": {"bluegrass": 0.99},
                  "feed_share": {"bluegrass": 0.96, "country": 0.96}, "sound": {"bluegrass": 0.14}}  # tagged bluegrass; a feed's country doesn't override
        murphey = {"tracks": 13, "albums": 1, "families": ["country", "folk"], "share": {"country": 1.0}, "sound": {"country": 0.2}}
        crossover = {"tracks": 60, "albums": 5, "families": ["rock", "country"], "share": {"rock": 0.6, "country": 0.4}, "sound": {"country": 0.1}}
        thin_tags = {"tracks": 60, "albums": 5, "families": ["country"], "share": {"country": 0.55}, "sound": {"country": 0.05}}
        self.assertEqual(score(murphey, ["country"]), 0.0)                # one album, too few tracks
        self.assertEqual(score(crossover, ["country"]), 0.0)              # mostly not country
        self.assertEqual(score(untagged, ["gospel"]), 0.0)                # no tags, no feed share: unknown
        self.assertGreater(score(delmores, ["country"]), 0.7)
        self.assertEqual(score(martin, ["country"]), 0.0)
        self.assertGreater(score(martin, ["bluegrass"]), 0.9)
        self.assertGreater(score(jones, ["country"]), score(thin_tags, ["country"]))
        self.assertGreater(score(jones, ["country"]), 0.9)
        self.assertGreater(score(crossover, ["rock"]), 0.0)
        # an inventory from before share/sound existed still ranks by depth
        old = {"tracks": 80, "families": ["country"]}
        self.assertGreater(score(old, ["country"]), 0.8)
        ranked = schedule.spotlight_ranked({"Jones": jones, "Murphey": murphey, "Thin": thin_tags, "ORB 2007": jones,
                                            "Various Artists": jones}, ["country"])
        self.assertEqual([a for a, _ in ranked], ["Jones", "Thin"])       # a year or a sampler is not an artist

    def test_auto_feed_pick_needs_ready_feeds(self):
        fds = {"western-swing": {"status": "ready", "count": 40, "family": ["country"]},
               "banjo": {"status": "ready", "count": 800, "family": ["bluegrass"]},
               "pending": {"status": "pending", "count": 0, "family": ["country"]}}
        slot = {"id": "x", "station": "country", "kind": "auto", "like": "feed", "days": "daily", "start": "13:00"}
        r, _ = schedule.resolve(slot, self.wed.date(), {}, station={"family": ["country"]}, artists={}, feeds=fds)
        self.assertEqual(r["feed"], "western-swing")
        self.assertEqual(r["name"], "western-swing")

    def test_promos_and_intros(self):
        rng = random.Random(1)
        up = schedule.upcoming("country", self.slots, self.wed.replace(hour=12))
        text = schedule.promo("Classic Country", up, rng)
        self.assertIn("5 PM", text)
        self.assertIn("Honky Tonk Happy Hour", text)
        spot = {"kind": "artist", "artist": "George Jones", "name": "George Jones spotlight"}
        t = schedule.promo("Classic Country", [(self.wed.replace(hour=20, minute=0), self.wed.replace(hour=21, minute=0), spot)], rng)
        self.assertIn("George Jones", t)
        self.assertIn("8 PM", t)
        self.assertIn("George Jones", schedule.intro("Classic Country", spot, rng))
        self.assertIn("Classic Country", schedule.intro("Classic Country", self.slots[0], rng))
        self.assertEqual(schedule.say_time(dt.datetime(2026, 1, 1, 0, 30)), "12:30 AM")
        self.assertEqual(schedule.say_time(dt.datetime(2026, 1, 1, 12, 0)), "12 PM")


class Store(unittest.TestCase):
    def test_seed_once_and_requests(self):
        with tempfile.TemporaryDirectory() as d:
            c = config.Config(Path(d))
            self.assertEqual(c.seed({"a": {}}, [{"mount": "x"}], []), ["feeds.json", "stations.json", "schedule.json"])
            c.save_feeds({"b": {}})
            self.assertEqual(c.seed({"a": {}}, None, None), [])          # never overwrites
            self.assertEqual(c.feeds(), {"b": {}})
            p = c.request("apply", {"why": "test"})
            self.assertTrue(p.exists())
            self.assertTrue(p.name.startswith("apply-"))


if __name__ == "__main__":
    unittest.main()


class ExcludeGenres(unittest.TestCase):
    def test_negative_clause_keeps_bluegrass_out_of_country(self):
        from netradio import feeds as fr
        rule = {"genres": ["country"], "exclude_genres": ["bluegrass", "old time"]}
        kw = dict(artist="Bill Monroe", path="/srv/media/Music/Bill Monroe/x/1.mp3", yamnet=None, era="hifi")
        self.assertFalse(fr.matches(rule, genre="Country; Bluegrass", **kw))
        self.assertTrue(fr.matches(rule, genre="Country; Honky Tonk", **kw))
        self.assertFalse(fr.matches({"all": True, "exclude_genres": ["old time"]}, genre="Old Time", **kw))
        self.assertEqual(fr.validate({"genres": ["x"], "exclude_genres": "bluegrass"}), ["exclude_genres must be a list of strings"])


class SpotlightFitsTheStation(unittest.TestCase):
    def test_station_pool_counts_decide(self):
        score = schedule.spotlight_score
        martin = {"tracks": 151, "albums": 6, "families": ["bluegrass", "country"], "share": {"bluegrass": 1.0, "country": 1.0},
                  "sound": {}, "stations": {"bluegrass": 151, "country": 0}}
        carters = {"tracks": 256, "albums": 12, "families": ["country", "bluegrass"], "share": {"country": 1.0}, "sound": {},
                   "eras": {"shellac": 154, "vintage": 93, "hifi": 9}, "stations": {"country": 20, "scratchy": 154, "bluegrass": 102}}
        self.assertEqual(score(martin, ["country"], mount="country"), 0.0)          # its base plays none of him
        self.assertGreater(score(martin, ["bluegrass"], mount="bluegrass"), 0.8)
        self.assertEqual(score(carters, ["country"], mount="country"), 0.0)        # 20 playable < the bar
        self.assertGreater(score(carters, ["any"], mount="scratchy"), 0.5)         # the shellac station wants them
        # without station counts the era rule still narrows depth
        self.assertEqual(schedule.playable_count(carters, {"exclude": ["shellac"]}), 102)
        self.assertEqual(schedule.playable_count(carters, {"only": ["shellac"]}), 154)
