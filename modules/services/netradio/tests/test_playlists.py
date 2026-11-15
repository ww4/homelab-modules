import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from netradio import config, playlists as pl

logging.disable(logging.CRITICAL)


class GenreMatching(unittest.TestCase):
    def test_split_handles_separators_and_dashes(self):
        self.assertEqual(pl.split_genre("Folk/Rock"), ["folk", "rock"])
        self.assertEqual(pl.split_genre("Old-Time; Bluegrass"), ["old time", "bluegrass"])
        self.assertEqual(pl.split_genre(""), [])

    def test_singer_songwriter_is_one_genre(self):
        self.assertEqual(pl.split_genre("Singer/Songwriter; Folk"), ["singer songwriter", "folk"])

    def test_word_match_is_whole_word(self):
        self.assertTrue(pl.word_in("rock", ["folk rock"]))
        self.assertFalse(pl.word_in("rock", ["rockabilly"]))
        self.assertTrue(pl.word_in("r&b", ["soul and r&b"]))

    def test_holiday_by_name_catches_songs_not_places(self):
        for title in ["The Reindeer Boogie", "Santa's Big Parade", "Santa Claus Is Comin' To Town", "Sleigh Ride",
                      "Jingle Bell Rock", "Blue Christmas", "Frosty The Snowman", "Rudolph The Red-Nosed Reindeer"]:  # leak-scan-ok: a genre word, not a name
            self.assertTrue(pl.HOLIDAY_NAME.search(title), title)
        for title in ["'Longside The Santa Fe Trail", "Santa Rosa Serenade", "Santa Ana's Retreat", "Cold Frosty Morn",
                      "Santa Câfé", "Billie Holiday", "Snow Bird", "Carlos Santana"]:
            self.assertFalse(pl.HOLIDAY_NAME.search(title), title)

    def test_a_rock_opera_is_not_classical(self):
        """Genre words match on WORD BOUNDARIES, so "opera" matches inside
        "Rock Opera" — and Quadrophenia turned up on the classical station
        (2026-09-30: "I just heard the who play on classical radio").

        Dropping "opera" from the rule would cost the station every actual
        opera in the library, so the fix is to exclude rock outright. The
        boundary match itself is correct and stays: "folk rock" IS folk.
        """
        from netradio import feeds
        CLASSICAL = {"genres": ["classical", "baroque", "orchestral", "opera", "chamber"],
                     "exclude_genres": ["rock", "metal", "punk"]}

        def matches(tag):
            return feeds.matches(CLASSICAL, artist="", path="/m/A/B/01 x.mp3",
                                 genre=tag, yamnet=None, era="", folder_artist="", folder_album="")

        # the actual tag on the album that caused this
        self.assertFalse(matches("Rock; Rock Opera; Classic Rock; Hard Rock; Progressive Rock"),
                         "a rock opera is still landing on the classical station")
        for tag in ("Rock Opera", "Hard Rock", "Progressive Rock", "Punk", "Heavy Metal"):
            self.assertFalse(matches(tag), f"{tag} should not be classical")
        # and the exclusion must not take the operas with it
        for tag in ("Opera", "Classical; Opera", "Baroque", "Chamber Music", "Orchestral"):
            self.assertTrue(matches(tag), f"{tag} should still be classical")

    def test_feed_rule_shellac_default(self):
        self.assertEqual(pl.feed_rule({"rule": {"artists": ["A"]}})["era"], {"exclude": ["shellac"]})
        self.assertNotIn("era", pl.feed_rule({"rule": {"artists": ["A"]}, "shellac": True}))
        self.assertEqual(pl.feed_rule({"rule": {"artists": ["A"], "era": {"only": ["shellac"]}}})["era"], {"only": ["shellac"]})

    def test_families(self):
        self.assertEqual(pl.families_of(["bluegrass"]), ["bluegrass"])
        self.assertEqual(sorted(pl.families_of(["folk rock"])), ["folk", "rock"])
        self.assertEqual(pl.families_of(["other"]), [])


class Build(unittest.TestCase):
    """A fake library on disk with stubbed tags; exercises walk → build end to end."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.root = d / "Music"
        self.files = {   # relative path: (genre, artist)
            "The Louvin Brothers/A/01 a.mp3": ("Country", "The Louvin Brothers"),
            "The Louvin Brothers/A/02 b.mp3": ("Other", "The Louvin Brothers"),
            "Bob Wills/B/01 c.mp3": ("Western Swing", "Bob Wills"),
            "Boston/C/01 d.mp3": ("Rock", "Boston"),
            "Hal Leonard/D/01 e.mp3": ("Instructional", "Hal Leonard"),
            "Talker/E/01 f.mp3": ("Country", "Talker"),
            "Byrds/F/01 g.mp3": ("Folk Rock; Country", "Byrds"),          # country by a later tag only: fringe
            "Dan Gellert/G/01 h.mp3": ("Country", "Dan Gellert"),        # tagged Country, but the old-time feed holds him
            "Dan Gellert/G/02 i.mp3": ("Country", "Dan Gellert"),
            "X/cover.jpg": ("", ""),
        }
        for rel in self.files:
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")
        self.cfg = config.Config(d / "config")
        self.cfg.seed(
            {"brother-duets": {"title": "Brother Duets", "status": "ready", "family": ["bluegrass", "country"],
                               "shellac": True, "rule": {"artists": ["Louvin Brothers"]}},
             "western-swing": {"title": "Western Swing", "status": "ready", "family": ["country"],
                               "rule": {"genres": ["western swing"]}},
             "old-time": {"title": "Old-Time", "status": "ready", "family": ["bluegrass"],
                          "rule": {"artists": ["Dan Gellert"]}},
             "draft": {"title": "Draft", "status": "pending"}},
            [{"mount": "all", "name": "Everything", "kind": "curated", "family": ["any"], "base": {"all": True}},
             {"mount": "country", "name": "Classic Country", "kind": "curated", "family": ["country"],
              "base": {"genres": ["country", "western swing"], "era": {"exclude": ["shellac"]},
                       "exclude_genres": ["bluegrass", "old time"], "fringe_genres": ["rock"]}},
             {"mount": "brother-duets", "name": "Brother Duets", "kind": "specialty", "feed": "brother-duets"}],
            [])
        self.out = d / "playlists"
        self.pools = d / "pools"

    def tearDown(self):
        self.tmp.cleanup()

    def fake_tags(self, path):
        return self.files[str(Path(path).relative_to(self.root))]

    def test_walk_and_build(self):
        cache = pl.TagCache(Path(self.tmp.name) / "cache.json")
        with mock.patch.object(pl, "read_tags", side_effect=self.fake_tags):
            tracks, counts = pl.walk([self.root], cache)
        self.assertEqual(counts["files"], 9)
        self.assertEqual(counts["excluded"], 1)                       # the lesson
        self.assertEqual(len(tracks), 8)
        talk = {str(self.root / "Talker/E/01 f.mp3")}
        n = pl.build(tracks, self.cfg, self.out, self.pools, talk=talk,
                     summary=Path(self.tmp.name) / "summary.json", ycast=Path(self.tmp.name) / "stations.yml",
                     public_base="http://h/radio", internet_radio=[{"name": "NPR", "url": "http://npr"}],
                     web_base="https://r/radio")
        # country's core: the "Other"-tagged Louvin track is not country by tag; the
        # Byrds are country by a later tag only (fringe); Dan Gellert says Country
        # but the old-time feed vouches for him (out — the exclusion's intent)
        self.assertEqual(n, {"all": 7, "country": 2, "brother-duets": 2})
        self.assertEqual((self.out / "country-fringe.m3u").read_text().count(".mp3"), 1)
        self.assertIn("Byrds", (self.out / "country-fringe.m3u").read_text())
        self.assertNotIn("Gellert", (self.out / "country.m3u").read_text() + (self.out / "country-fringe.m3u").read_text())
        self.assertEqual((self.out / "all-fringe.m3u").read_text().count(".mp3"), 0)
        self.assertEqual((self.pools / "feeds" / "western-swing.m3u").read_text().count(".mp3"), 1)
        self.assertFalse((self.pools / "feeds" / "draft.m3u").exists())
        feeds = self.cfg.feeds()
        self.assertEqual((feeds["brother-duets"]["count"], feeds["western-swing"]["count"], feeds["draft"]["count"]), (2, 1, 0))
        artists = self.cfg.artists()
        self.assertEqual(artists["The Louvin Brothers"]["tracks"], 2)
        self.assertEqual(artists["The Louvin Brothers"]["families"], ["bluegrass", "country"])   # tag + inherited from the feed
        self.assertEqual(artists["Boston"]["families"], ["rock"])
        self.assertNotIn("Talker", artists)                           # talk tracks are not an artist pool
        self.assertTrue((self.pools / "artists" / "bob-wills.m3u").exists())
        yml = (Path(self.tmp.name) / "stations.yml").read_text()
        self.assertIn('Curated:\n  "Everything": "http://h/radio/all.mp3"', yml)
        self.assertIn('Specialty:\n  "Brother Duets": "http://h/radio/brother-duets.mp3"', yml)
        self.assertIn('"NPR": "http://npr"', yml)
        self.assertEqual(json.loads((Path(self.tmp.name) / "summary.json").read_text())["country"], {"tracks": 2, "fringe": 1})
        cat = json.loads((Path(self.tmp.name) / "catalogue.json").read_text())
        self.assertEqual([c["mount"] for c in cat], ["all", "country", "brother-duets"])
        self.assertIn("https://r/radio/brother-duets-lo.mp3", (Path(self.tmp.name) / "stations-lo.m3u").read_text())
        self.assertIn("NumberOfEntries=3", (Path(self.tmp.name) / "stations.pls").read_text())

    def test_cache_skips_unchanged_files_and_ignores_old_format(self):
        cache_path = Path(self.tmp.name) / "cache.json"
        cache_path.write_text(json.dumps({"/old/style.mp3": [1, 2, "genre"]}))   # v1 cache: ignored
        with mock.patch.object(pl, "read_tags", side_effect=self.fake_tags) as rt:
            cache = pl.TagCache(cache_path)
            pl.walk([self.root], cache)
            cache.save()
            self.assertEqual(rt.call_count, 9)
            cache = pl.TagCache(cache_path)
            pl.walk([self.root], cache)
            self.assertEqual(rt.call_count, 9)
            self.assertEqual((cache.hits, cache.misses), (9, 0))

    def test_unreadable_dir_is_skipped_not_fatal(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory modes")
        locked = self.root / "Locked"
        locked.mkdir()
        (locked / "x.mp3").write_bytes(b"x")
        locked.chmod(0o000)
        try:
            with mock.patch.object(pl, "read_tags", side_effect=self.fake_tags):
                _, counts = pl.walk([self.root], pl.TagCache(Path(self.tmp.name) / "c.json"))
            self.assertEqual(counts["unreadable_dirs"], 1)
        finally:
            locked.chmod(0o700)


if __name__ == "__main__":
    unittest.main()


class Tiles(unittest.TestCase):
    def test_covers_are_unique_across_stations_and_all_gets_a_glyph(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            def track(artist, album, n):
                p = root / artist / album / f"{n:02d} x.mp3"; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(b"\x00"); return str(p)
            # five artists with one album each, all with a cover file; A shared by two stations
            paths = {a: track(a, "Album", 1) for a in "ABCDE"}
            for a in "ABCDE":
                (root / a / "Album" / "cover.jpg").write_bytes(b"jpg")
            artist_of = {p: a for a, p in paths.items()}
            stations = [{"mount": "all", "kind": "curated", "base": {"all": True}},
                        {"mount": "x", "kind": "curated", "base": {"genres": ["x"]}},
                        {"mount": "y", "kind": "curated", "base": {"genres": ["y"]}}]
            pools = {"all": list(paths.values()), "x": [paths[a] for a in "ABCD"], "y": [paths[a] for a in "AE"]}
            from netradio import playlists
            with mock.patch.object(playlists, "has_art", return_value=True):
                tiles = playlists.station_tiles(stations, pools, {}, artist_of)
            self.assertEqual(tiles["all"], {"icon": "radio", "covers": []})
            self.assertEqual(len(tiles["y"]["covers"]), 2)                     # the smaller station picks first: A and E
            self.assertEqual(len(tiles["x"]["covers"]), 3)                     # so x gets B, C, D — a hero, not a mosaic — rather than reuse A
            used = [Path(p).parent for t in tiles.values() for p in t["covers"]]
            self.assertEqual(len(used), len(set(used)))                        # no album folder on two tiles


class ArtistScope(unittest.TestCase):
    """`Artist :: album words` — an artist admitted for part of their work."""

    def hit(self, rule, artist, path):
        from netradio import feeds
        return feeds.artist_hit(rule, artist, path)

    def test_only_the_named_albums_count(self):
        rule = ["Bear McCreary :: Outlander"]
        self.assertTrue(self.hit(rule, "Bear McCreary", "/srv/media/Music/Bear McCreary/Outlander Vol 1/01 x.mp3"))
        self.assertFalse(self.hit(rule, "Bear McCreary", "/srv/media/Music/Bear McCreary/The Singularity/01 y.mp3"))
        self.assertFalse(self.hit(rule, "Bear McCreary", "/srv/media/Music/Bear McCreary/Ekleipsis/01 z.mp3"))

    def test_an_unscoped_entry_still_takes_the_whole_artist(self):
        rule = ["Clannad"]
        for album in ("Magical Ring", "Anam"):
            self.assertTrue(self.hit(rule, "Clannad", f"/srv/media/Music/Clannad/{album}/01 a.mp3"))

    def test_a_scoped_entry_does_not_match_a_different_artist(self):
        self.assertFalse(self.hit(["Bear McCreary :: Outlander"], "Raya Yarbrough",
                                  "/srv/media/Music/Raya Yarbrough/Outlander Songs/01 q.mp3"))

    def test_the_scope_works_under_a_root_of_ANY_depth(self):
        # The old code read the artist at parts[4] and the album at parts[5],
        # absolute indices that only lined up under the FIRST library root. A
        # second root of a different depth (Lidarr's
        # /srv/media/arr/media/music) made the artist read "media" and the
        # album "music", so this scope could never match an imported album —
        # silently, and only for part of the library (2026-09-26).
        rule = ["Bear McCreary :: Outlander"]
        for root in ("/srv/media/Music", "/srv/media/arr/media/music", "/srv/music", "/m"):
            self.assertTrue(self.hit(rule, "Bear McCreary", f"{root}/Bear McCreary/Outlander Vol 1/01 x.mp3"),
                            f"should match under {root}")
            self.assertFalse(self.hit(rule, "Bear McCreary", f"{root}/Bear McCreary/The Singularity/01 y.mp3"),
                             f"should NOT match the sci-fi score under {root}")

    def test_the_scanner_resolves_the_folders_against_the_root_it_walked(self):
        # and the values it passes in beat any guess made from the path
        from netradio import feeds
        self.assertTrue(feeds.artist_hit(["Clannad"], "", "/anything/at/all/x.mp3",
                                         folder_artist="Clannad", folder_album="Magical Ring"))
        self.assertFalse(feeds.artist_hit(["Clannad :: Anam"], "", "/anything/at/all/x.mp3",
                                          folder_artist="Clannad", folder_album="Magical Ring"))


class YcastMenuRoundTrip(unittest.TestCase):
    """The menu file is now READ as well as written — it is the receiver's own
    truth about where a station sits in its menu. Reader and writer live beside
    each other; this keeps them honest."""

    def test_what_is_written_can_be_read_back(self):
        from netradio import playlists as pl
        stations = [{"name": 'A "quoted" name', "mount": "quoted", "kind": "curated"},
                    {"name": "Soul & R&B", "mount": "soul", "kind": "curated"},
                    {"name": "A Cappella & Lined-Out Singing", "mount": "a-cappella", "kind": "specialty"}]
        text = pl.ycast_yaml(stations, "http://host/radio", [{"name": "NPR", "url": "http://npr/x.mp3"}])
        got = pl.parse_ycast_yaml(text)
        self.assertEqual([(c, n) for c, n, _ in got],
                         [("Curated", 'A "quoted" name'), ("Curated", "Soul & R&B"),
                          ("Specialty", "A Cappella & Lined-Out Singing"), ("Internet Radio", "NPR")])
        self.assertEqual(pl.menu_entry_for_mount(text, "soul"), ("Curated", "Soul & R&B"))
        self.assertIsNone(pl.menu_entry_for_mount(text, "nosuch"))

    def test_a_mount_that_is_a_suffix_of_another_does_not_collide(self):
        from netradio import playlists as pl
        text = pl.ycast_yaml([{"name": "Country", "mount": "country", "kind": "curated"},
                              {"name": "Hits", "mount": "90s-country", "kind": "curated"}],
                             "http://host/radio", [])
        self.assertEqual(pl.menu_entry_for_mount(text, "country"), ("Curated", "Country"))
        self.assertEqual(pl.menu_entry_for_mount(text, "90s-country"), ("Curated", "Hits"))


class MenuCodecFilter(unittest.TestCase):
    """A device is offered only what it can decode.

    2026-09-27: the codecs belong to the receiver's own declaration, and
    they decide what shows up in Internet Radio. The reason is concrete — Hank FM
    and Froggy both serve HE-AACv2 at 32 kbps, and the R-N301 decodes AAC-LC,
    which is a different profile. Offering them would give the listener a menu
    entry that plays silence.
    """

    STATIONS = [{"name": "Classic Country", "mount": "country", "kind": "curated"}]
    PICKS = [{"name": "WMMT", "url": "http://x/r.mp3", "codec": "mp3"},
             {"name": "Hank FM 105.5", "url": "http://y/WLXO", "codec": "he-aac"},
             {"name": "WETS", "url": "http://z/live-1", "codec": "mp3"}]

    def names(self, codecs):
        from netradio import playlists as pl
        text = pl.ycast_yaml(self.STATIONS, "http://b/radio", self.PICKS, codecs)
        return [n for _, n, _ in pl.parse_ycast_yaml(text)]

    def test_a_receiver_that_only_does_mp3_is_not_shown_the_aac_one(self):
        got = self.names({"mp3"})
        self.assertIn("WMMT", got)
        self.assertIn("WETS", got)
        self.assertNotIn("Hank FM 105.5", got)

    def test_aac_lc_does_NOT_admit_he_aac(self):
        """The distinction the whole option exists for."""
        self.assertNotIn("Hank FM 105.5", self.names({"mp3", "wma", "aac-lc"}))

    def test_a_device_that_declares_he_aac_gets_it(self):
        self.assertIn("Hank FM 105.5", self.names({"mp3", "he-aac"}))

    def test_no_declaration_filters_nothing(self):
        self.assertIn("Hank FM 105.5", self.names(None))

    def test_the_librarys_own_stations_are_never_filtered_out(self):
        """They are ours and they are MP3; a codec list must not hide them."""
        for codecs in ({"mp3"}, {"wma"}, set()):
            self.assertIn("Classic Country", self.names(codecs), f"codecs={codecs}")

    def test_a_pick_with_no_codec_is_treated_as_mp3(self):
        from netradio import playlists as pl
        picks = [{"name": "Unlabelled", "url": "http://x/r.mp3"}]
        text = pl.ycast_yaml(self.STATIONS, "http://b/radio", picks, {"mp3"})
        self.assertIn("Unlabelled", [n for _, n, _ in pl.parse_ycast_yaml(text)])


class HasArtNeedsReadability(unittest.TestCase):
    """`has_art` gates what goes into tiles.json, and `admin/api/art` has to be
    able to serve everything it lists. A cover that exists but cannot be opened
    breaks that promise: the manifest names it, the endpoint 404s, and the
    station's tile comes up a square short with nothing to say why.

    Real: 46 covers fetched by an agent on 2026-09-18 were written mode 0600
    (owner-only) into a library the netradio user reads as a group member.
    `Path.exists()` needs only directory traversal, so it returned True for
    every one of them for ten days.
    """

    def test_a_cover_that_cannot_be_read_does_not_count(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            track = folder / "01 song.mp3"
            track.write_bytes(b"not really audio")
            cover = folder / "cover.jpg"
            cover.write_bytes(b"\xff\xd8\xff\xe0 jpeg-ish")

            self.assertTrue(pl.has_art(str(track)), "a readable cover should count")

            # No need to restore the mode: the temp DIRECTORY is writable, so an
            # unreadable file in it still deletes. An addCleanup would fire after
            # the directory is already gone.
            cover.chmod(0o000)
            if os.access(cover, os.R_OK):
                self.skipTest("running as a user that bypasses file modes (root)")
            self.assertFalse(pl.has_art(str(track)),
                             "an unreadable cover was counted as art — tiles.json will name a "
                             "path admin/api/art cannot serve")
