import json
import logging
import os
import tempfile
from unittest import mock
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from netradio import admin, compile as compile_, config, liq

logging.disable(logging.CRITICAL)


class AdminApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = config.Config(Path(self.tmp.name) / "config")
        self.cfg.seed(
            {"western-swing": {"title": "Western Swing", "description": "Bob Wills", "status": "ready", "count": 30,
                               "family": ["country"], "rule": {"artists": ["Bob Wills"]}}},
            [{"mount": "country", "name": "Classic Country", "kind": "curated", "family": ["country"], "base": {"genres": ["country"]}},
             {"mount": "rock", "name": "Rock", "kind": "curated", "family": ["rock"], "base": {"genres": ["rock"]}},
             {"mount": "western-swing", "name": "Western Swing", "kind": "specialty", "feed": "western-swing", "family": ["country"]}],
            [])
        self.cfg._write("artists.json", {"George Jones": {"tracks": 40, "families": ["country"], "slug": "george-jones"},
                                          "Boston": {"tracks": 30, "families": ["rock"], "slug": "boston"},
                                          "Tiny": {"tracks": 2, "families": ["country"], "slug": "tiny"}})
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), admin.make_handler(admin.Admin(self.cfg)))
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.tmp.cleanup()

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.url + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def requests(self, kind):
        return sorted(p.name for p in (self.cfg.root / "requests").glob(f"{kind}-*.json"))

    def test_state_and_options(self):
        code, st = self.call("GET", "/api/state")
        self.assertEqual(code, 200)
        self.assertTrue(st["feeds"]["western-swing"]["listenable"])
        self.assertEqual([s["mount"] for s in st["stations"]], ["country", "rock", "western-swing"])
        code, opt = self.call("GET", "/api/options?station=country")
        self.assertEqual([f["id"] for f in opt["feeds"]], ["western-swing"])
        self.assertEqual([a["name"] for a in opt["artists"]], ["George Jones"])     # not Boston, not Tiny
        code, opt = self.call("GET", "/api/options?station=rock")
        self.assertEqual(opt["feeds"], [])
        self.assertEqual([a["name"] for a in opt["artists"]], ["Boston"])

    def test_add_feed_requests_compile_and_makes_a_station(self):
        code, r = self.call("POST", "/api/feeds", {"title": "Brother Duets", "description": "Louvins, Delmores…"})
        self.assertEqual(code, 201)
        self.assertEqual(r["id"], "brother-duets")
        self.assertEqual(self.cfg.feeds()["brother-duets"]["status"], "pending")
        self.assertEqual(len(self.requests("compile")), 1)
        self.assertIn("brother-duets", [s["mount"] for s in self.cfg.stations()])
        code, _ = self.call("POST", "/api/feeds", {"title": ""})
        self.assertEqual(code, 400)

    def test_edit_description_recompiles_edit_rule_applies(self):
        code, r = self.call("PUT", "/api/feeds/western-swing", {"description": "more swing"})
        self.assertTrue(r["recompile"])
        self.assertEqual(self.cfg.feeds()["western-swing"]["status"], "pending")
        code, r = self.call("PUT", "/api/feeds/western-swing", {"rule": {"artists": ["Bob Wills", "Asleep at the Wheel"]}})
        self.assertEqual(code, 200)
        self.assertEqual(self.cfg.feeds()["western-swing"]["status"], "ready")
        self.assertEqual(len(self.requests("apply")), 1)
        code, r = self.call("PUT", "/api/feeds/western-swing", {"rule": {}})
        self.assertEqual(code, 400)
        code, r = self.call("PUT", "/api/feeds/western-swing", {"listenable": False})
        self.assertNotIn("western-swing", [s["mount"] for s in self.cfg.stations()])

    def test_station_excursion_is_a_percentage(self):
        code, r = self.call("PUT", "/api/stations/country", {"excursion": 20})
        self.assertEqual(code, 200, r)
        self.assertEqual(next(s for s in self.cfg.stations() if s["mount"] == "country")["excursion"], 0.2)
        code, r = self.call("PUT", "/api/stations/country", {"excursion": 80})
        self.assertEqual(code, 400, r)
        code, r = self.call("PUT", "/api/stations/country", {"base": {"genres": ["country"], "fringe_genres": "rock"}})
        self.assertEqual(code, 400, r)                                   # fringe_genres must be a list

    def test_schedule_validation(self):
        good = [{"station": "country", "name": "Swing Hour", "kind": "feed", "feed": "western-swing", "days": ["sat"], "start": "10:00", "minutes": 60},
                {"station": "country", "kind": "auto", "like": "artist", "days": "daily", "start": "20:00"}]
        code, r = self.call("PUT", "/api/schedule", good)
        self.assertEqual((code, r["count"]), (200, 2))
        ids = [s["id"] for s in self.cfg.schedule()]
        self.assertEqual(len(set(ids)), 2)
        for bad in ([{"station": "nope", "kind": "feed", "feed": "western-swing", "start": "10:00"}],
                    [{"station": "country", "kind": "feed", "feed": "missing", "start": "10:00"}],
                    [{"station": "country", "kind": "artist", "start": "10:00"}],
                    [{"station": "country", "kind": "feed", "feed": "western-swing", "start": "ten"}],
                    [{"station": "country", "kind": "feed", "feed": "western-swing", "start": "10:00", "minutes": 5}],
                    [{"station": "western-swing", "kind": "auto", "like": "feed", "start": "10:00"}]):
            code, r = self.call("PUT", "/api/schedule", bad)
            self.assertEqual(code, 400, r)

    def test_state_shows_todays_resolved_schedule(self):
        self.call("PUT", "/api/schedule", [{"station": "country", "kind": "auto", "like": "artist", "days": "daily", "start": "20:00"}])
        _, st = self.call("GET", "/api/state")
        self.assertEqual(st["today"]["country"][0]["value"], "George Jones")
        self.assertEqual(st["today"]["country"][0]["start"], "20:00")

    def test_delete_feed_cleans_up(self):
        self.call("PUT", "/api/schedule", [{"station": "country", "kind": "feed", "feed": "western-swing", "start": "10:00"}])
        code, _ = self.call("DELETE", "/api/feeds/western-swing")
        self.assertEqual(code, 200)
        self.assertEqual(self.cfg.feeds(), {})
        self.assertEqual(self.cfg.schedule(), [])
        self.assertNotIn("western-swing", [s["mount"] for s in self.cfg.stations()])


class Compile(unittest.TestCase):
    def test_prompt_and_apply(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = config.Config(Path(d))
            cfg.seed({"ws": {"title": "Western Swing", "description": "Bob Wills style", "status": "pending"}}, [], [])
            cfg._write("artists.json", {"Bob Wills": {"tracks": 5, "families": ["country"], "slug": "bob-wills"}})
            prompt = compile_.build_prompt(cfg, "ws", ["country", "western swing"])
            self.assertIn("Bob Wills (5 tracks; country)", prompt)
            self.assertIn("Bob Wills style", prompt)
            answer = 'Sure, here you go:\n{"rule": {"artists": ["Bob Wills"], "genres": ["western swing"]}, "family": ["country"], "note": "thin"}\n'
            self.assertEqual(compile_.apply_result(cfg, "ws", compile_.parse_result(answer)), [])
            f = cfg.feeds()["ws"]
            self.assertEqual((f["status"], f["family"], f["rule"]["genres"]), ("ready", ["country"], ["western swing"]))
            self.assertTrue(list((Path(d) / "requests").glob("apply-*.json")))
            errs = compile_.apply_result(cfg, "ws", {"rule": {}, "family": ["country"]})
            self.assertTrue(errs)
            self.assertEqual(cfg.feeds()["ws"]["status"], "failed")
            with self.assertRaises(ValueError):
                compile_.parse_result("no json here")
            # prose with braces before the object, a code fence, a bare rule
            r = compile_.parse_result('Notes {thin} first.\n```json\n{"rule": {"artists": ["A"]}, "family": ["country"]}\n```')
            self.assertEqual(r["rule"]["artists"], ["A"])
            r = compile_.parse_result('{"artists": ["A"], "genres": ["x"]}')
            self.assertEqual(r["rule"]["genres"], ["x"])
            r = compile_.parse_result('{"note": "x"} then {"rule": {"all": true}}')
            self.assertTrue(r["rule"]["all"])


class Liq(unittest.TestCase):
    def test_render(self):
        text = liq.render([{"mount": "country", "name": "Classic Country"}, {"mount": "ws", "name": "Western Swing"}],
                          socket="/run/x.sock", playlists="/p", now_dir="/n", port=8020)
        self.assertIn('station("country", "Classic Country")', text)
        self.assertIn('station("ws", "Western Swing")', text)
        self.assertIn('port=8020', text)
        self.assertIn('playlists = "/p"', text)
        self.assertEqual(text.count("{{"), 0)   # the format braces were resolved


if __name__ == "__main__":
    unittest.main()


class CompileParse(unittest.TestCase):
    def test_truncated_answer_keeps_family_and_note(self):
        from netradio.compile import parse_result
        cut = '{"rule": {"artists": ["Shania Twain", "George Strait"]}, "family": ["country"], "note": "Built from what is owned."'
        r = parse_result(cut)
        self.assertEqual(r["rule"]["artists"], ["Shania Twain", "George Strait"])
        self.assertEqual(r["family"], ["country"])
        self.assertEqual(r["note"], "Built from what is owned.")
        r = parse_result('```json\n{"rule": {"genres": ["western swing"]}, "family": ["country"], "note": "x"}\n```')
        self.assertEqual(r["family"], ["country"])


class Feedback(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory(); root = Path(self.tmp.name)
        (root / "config").mkdir(); (root / "dj").mkdir(); (root / "pl").mkdir()
        (root / "pl" / "library.m3u").write_text("#EXTM3U\n/srv/media/Music/Bill Monroe/Bluegrass 1950/03 Uncle Pen.mp3\n/srv/media/Music/The Who/Tommy/01 Overture.mp3\n")
        self.admin = admin.Admin(config.Config(root / "config"), root / "dj", root / "pl")
        self.root = root

    def tearDown(self):
        self.tmp.cleanup()

    def test_skip_and_request_land_in_the_inbox(self):
        self.admin.skip("bluegrass")
        self.admin.request("bluegrass", {"path": "/srv/media/Music/Bill Monroe/Bluegrass 1950/03 Uncle Pen.mp3", "who": "listener"})
        files = sorted((self.root / "dj" / "inbox").glob("bluegrass-*.json"))
        self.assertEqual([json.loads(f.read_text())["action"] for f in files], ["skip", "request"])
        with self.assertRaises(ValueError):
            self.admin.request("bluegrass", {"path": "/nowhere.mp3"})

    def test_search_matches_every_word(self):
        hits = self.admin.search("monroe pen")
        self.assertEqual([h["title"] for h in hits], ["Uncle Pen"])
        self.assertEqual(hits[0]["artist"], "Bill Monroe")
        self.assertEqual(self.admin.search("who overture")[0]["title"], "Overture")
        self.assertEqual(self.admin.search(""), [])

    def test_dislike_track_and_artist(self):
        r = self.admin.dislike({"path": "/srv/media/Music/The Who/Tommy/01 Overture.mp3", "scope": "track", "title": "Overture", "mount": "rock"})
        self.assertEqual((r["tracks"], r["artists"]), (1, 0))
        self.assertTrue(list((self.root / "dj" / "inbox").glob("rock-*.json")))      # a disliked track is skipped now
        self.admin.dislike({"scope": "artist", "artist": "The Who"})
        d = self.admin.cfg.dislikes()
        self.assertIn("the who", d["artists"]) and self.assertIn("/srv/media/Music/The Who/Tommy/01 Overture.mp3", d["tracks"])
        self.admin.undislike({"scope": "artist", "key": "the who"})
        self.assertNotIn("the who", self.admin.cfg.dislikes()["artists"])


class Art(unittest.TestCase):
    def test_folder_cover_and_no_art(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); (root / "config").mkdir(); (root / "pl").mkdir()
            alb = root / "Music" / "A" / "B"; alb.mkdir(parents=True)
            (alb / "01 x.mp3").write_bytes(b"\x00" * 64); (alb / "cover.jpg").write_bytes(b"\xff\xd8jpeg")
            (root / "pl" / "library.m3u").write_text(f"#EXTM3U\n{alb / '01 x.mp3'}\n{alb / '02 y.mp3'}\n")
            a = admin.Admin(config.Config(root / "config"), None, root / "pl")
            data, mime = a.art(str(alb / "01 x.mp3"))
            self.assertEqual((data, mime), (b"\xff\xd8jpeg", "image/jpeg"))
            with self.assertRaises(ValueError):
                a.art("/nowhere.mp3")


class Watching(unittest.TestCase):
    """Asking Jellyfin what is on a screen in the house."""

    def admin(self, **kw):
        import tempfile
        d = tempfile.mkdtemp()
        (Path(d) / "config").mkdir(exist_ok=True)
        return admin.Admin(config.Config(Path(d) / "config"), None, None, **kw)

    def test_reading_does_not_require_permission_to_write_favourites(self):
        """`jellyfin_hearts` gates WRITING into somebody's library. Asking what
        is on the television writes nothing, so it must not be gated on it —
        the artwork path already works this way for the same reason."""
        a = self.admin(jellyfin_url="http://jf:8096", jellyfin_key="k", jellyfin_hearts=False)
        with mock.patch.object(admin.jellyfin.Jellyfin, "now_playing",
                               return_value={"playing": True, "head": "A Show"}):
            self.assertEqual(a.watching()["head"], "A Show")

    def test_no_jellyfin_configured_is_simply_nothing_playing(self):
        self.assertEqual(self.admin().watching(), {"playing": False})

    def test_a_server_that_throws_does_not_take_the_page_down(self):
        a = self.admin(jellyfin_url="http://jf:8096", jellyfin_key="k")
        with mock.patch.object(admin.jellyfin.Jellyfin, "now_playing", side_effect=OSError("boom")):
            self.assertEqual(a.watching(), {"playing": False})

    def test_the_poster_proxy_only_accepts_a_real_item_id(self):
        """The id is pasted into a URL this process then fetches, so anything
        that is not a Jellyfin id is refused before it gets there."""
        from netradio.conventions import is_item_id
        self.assertTrue(is_item_id("51910ca395bfc44fc7eb0c6a924bb38f"))
        for bad in ("", "../../etc/passwd", "51910CA395BFC44FC7EB0C6A924BB38F",
                    "51910ca395bfc44fc7eb0c6a924bb38", "x" * 32, "a/b"):
            self.assertFalse(is_item_id(bad), bad)
        a = self.admin(jellyfin_url="http://jf:8096", jellyfin_key="k")
        self.assertIsNone(a.watching_art("../../secret"))


class SidecarArt(unittest.TestCase):
    """A picture named after one track, beside it.

    A folder cover is an album's sleeve and right for an album. It is wrong for
    a folder that is not an album: Music For Programming is 107 separate mixes
    in one directory with a different picture for each and no sleeve to share
    (2026-09-29).
    """

    def _lib(self, d):
        root = Path(d); (root / "config").mkdir(); (root / "pl").mkdir()
        alb = root / "Music" / "Various Artists" / "Music for Programming"
        alb.mkdir(parents=True)
        for n in ("mfp_1-datassette", "mfp_2-sunjammer"):
            (alb / f"{n}.mp3").write_bytes(b"\x00" * 64)
        (alb / "cover.jpg").write_bytes(b"\xff\xd8shared")
        (root / "pl" / "library.m3u").write_text(
            "#EXTM3U\n" + "\n".join(str(alb / f"{n}.mp3") for n in ("mfp_1-datassette", "mfp_2-sunjammer")) + "\n")
        return admin.Admin(config.Config(root / "config"), None, root / "pl"), alb

    def test_a_picture_named_after_the_track_beats_the_folder_cover(self):
        with tempfile.TemporaryDirectory() as d:
            a, alb = self._lib(d)
            (alb / "mfp_1-datassette.jpg").write_bytes(b"\xff\xd8episode one")
            self.assertEqual(a.art(str(alb / "mfp_1-datassette.mp3"))[0], b"\xff\xd8episode one")
            # its neighbour, which has none, still gets the shared sleeve
            self.assertEqual(a.art(str(alb / "mfp_2-sunjammer.mp3"))[0], b"\xff\xd8shared")

    def test_png_and_webp_are_served_as_themselves(self):
        with tempfile.TemporaryDirectory() as d:
            a, alb = self._lib(d)
            (alb / "mfp_1-datassette.png").write_bytes(b"\x89PNG one")
            self.assertEqual(a.art(str(alb / "mfp_1-datassette.mp3"))[1], "image/png")
            (alb / "mfp_2-sunjammer.webp").write_bytes(b"RIFF....WEBP")
            self.assertEqual(a.art(str(alb / "mfp_2-sunjammer.mp3"))[1], "image/webp")

    def test_a_sidecar_that_cannot_be_read_is_not_a_picture(self):
        """exists() says yes to a file the service user cannot open, and the
        endpoint then throws instead of falling through to the cover
        (2026-09-28, 46 agent-written 0600 covers)."""
        with tempfile.TemporaryDirectory() as d:
            a, alb = self._lib(d)
            bad = alb / "mfp_1-datassette.jpg"
            bad.write_bytes(b"\xff\xd8unreadable")
            bad.chmod(0o000)
            if os.access(bad, os.R_OK):
                self.skipTest("running as root: permissions prove nothing")
            self.assertEqual(a.art(str(alb / "mfp_1-datassette.mp3"))[0], b"\xff\xd8shared")

    def test_adding_one_does_not_wait_for_the_thumbnail_cache_to_expire(self):
        """The whole point is adding pictures to a library that already has a
        thumbnail cached for every track in it."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            a, alb = self._lib(d)
            a.thumb_cache = root / "thumbs"
            track = str(alb / "mfp_1-datassette.mp3")
            before = admin.art_stamp(track)
            self.assertEqual(a.cached_art(track, 0)[0], b"\xff\xd8shared")
            (alb / "mfp_1-datassette.jpg").write_bytes(b"\xff\xd8episode one")
            self.assertNotEqual(admin.art_stamp(track), before,
                                "art_stamp ignores the sidecar, so the stale thumbnail would stand")
            self.assertEqual(a.cached_art(track, 0)[0], b"\xff\xd8episode one")


class ThumbnailCache(unittest.TestCase):
    """Resized covers are memoised on disk. A miss costs a full mutagen read of
    the track (some in the owner's library are 29 MB) plus a Pillow decode; a hit is
    one read(). A cold desktop wall asks for ~150 of them, which is what made it
    take ~17 s to paint on a LAN (2026-09-28).
    """

    def _admin(self, root: Path, cache: Path | None):
        alb = root / "Music" / "A" / "B"
        if not alb.exists():
            alb.mkdir(parents=True)
            (alb / "01 x.mp3").write_bytes(b"\x00" * 64)
            (alb / "cover.jpg").write_bytes(b"\xff\xd8jpeg")
            (root / "config").mkdir(exist_ok=True)
            (root / "pl").mkdir(exist_ok=True)
            (root / "pl" / "library.m3u").write_text(f"#EXTM3U\n{alb / '01 x.mp3'}\n{alb / '02 no-art.mp3'}\n")
        return admin.Admin(config.Config(root / "config"), None, root / "pl", thumb_cache=cache), alb

    def test_a_second_request_is_served_from_disk(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); cache = root / "thumbs"
            a, alb = self._admin(root, cache)
            track = str(alb / "01 x.mp3")

            first = a.cached_art(track, 0)
            self.assertIsNotNone(first)
            entries = [f for f in cache.rglob("*") if f.is_file()]
            self.assertEqual(len(entries), 1, "nothing was written to the cache")

            # make the SOURCE unreadable: a real hit must not touch it again
            # no addCleanup: it would fire after the temp directory is gone.
            # An unreadable file in a writable directory still deletes.
            (alb / "cover.jpg").chmod(0o000)
            second = a.cached_art(track, 0)
            self.assertEqual(second, first, "the cached entry was not used")

    def test_a_new_cover_invalidates_the_entry(self):
        """Keying on the track's mtime alone would serve the old thumbnail
        forever after someone drops a new cover.jpg beside it."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); cache = root / "thumbs"
            a, alb = self._admin(root, cache)
            track = str(alb / "01 x.mp3")
            self.assertEqual(a.cached_art(track, 0)[0], b"\xff\xd8jpeg")

            (alb / "cover.jpg").write_bytes(b"\xff\xd8NEWJPEG")
            self.assertEqual(a.cached_art(track, 0)[0], b"\xff\xd8NEWJPEG",
                             "a replaced cover still served the stale thumbnail")

    def test_a_track_with_no_art_is_remembered_as_such(self):
        """The expensive answer is 'no artwork': mutagen read the whole file to
        find nothing. Without a negative entry that repeats on every load."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); cache = root / "thumbs"
            a, alb = self._admin(root, cache)
            bare = alb.parent / "C"; bare.mkdir()
            track = bare / "02 no-art.mp3"
            track.write_bytes(b"\x00" * 64)
            (root / "pl" / "library.m3u").write_text(f"#EXTM3U\n{track}\n")

            self.assertIsNone(a.cached_art(str(track), 0))
            misses = [f for f in cache.rglob("*.none") if f.is_file()]
            self.assertEqual(len(misses), 1, "the miss was not remembered")
            self.assertIsNone(a.cached_art(str(track), 0))

    def test_without_a_cache_directory_it_still_works(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            a, alb = self._admin(root, None)
            self.assertEqual(a.cached_art(str(alb / "01 x.mp3"), 0)[0], b"\xff\xd8jpeg")

    def test_the_library_membership_set_is_not_rebuilt_per_call(self):
        """`art` asked "is this a library track?" as `path not in set(...)`,
        rebuilding a 22,857-entry set on every request."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            a, alb = self._admin(root, None)
            self.assertTrue(a.in_library(str(alb / "01 x.mp3")))
            self.assertFalse(a.in_library("/nowhere.mp3"))
            self.assertIs(a._library_set, a._library_set, "the set should be held, not recomputed")


class StationArtwork(unittest.TestCase):
    """A station with no artists to draw a mosaic from — an ambient rain bed, or
    "Everything", which is the whole library rather than four acts — had only a
    monogram. A picture at <art_dir>/<mount>.jpg becomes its tile, and because
    the path goes into the tile's `covers` it travels through the existing art
    endpoint and the pages unchanged (2026-09-28).
    """

    def test_a_station_picture_is_served_though_it_is_not_a_library_track(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); (root / "config").mkdir(); (root / "pl").mkdir()
            art = root / "art"; art.mkdir()
            pic = art / "rain.jpg"; pic.write_bytes(b"\xff\xd8rain")
            (root / "pl" / "library.m3u").write_text("#EXTM3U\n/mnt/music/a.mp3\n")
            a = admin.Admin(config.Config(root / "config"), None, root / "pl", station_art=art)
            self.assertEqual(a.art(str(pic)), (b"\xff\xd8rain", "image/jpeg"))

    def test_a_picture_installed_as_a_symlink_is_served(self):
        """How the module ACTUALLY installs them: a tmpfiles `L+` link into the
        Nix store. The first version of this check called Path.resolve(), which
        follows the link out to /nix/store — so the picture was no longer "under"
        the art directory and every station tile came back 400. The test that
        was supposed to cover this used a plain file, which is not how it is
        deployed (2026-09-28).
        """
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); (root / "config").mkdir(); (root / "pl").mkdir()
            art = root / "art"; art.mkdir()
            store = root / "store"; store.mkdir()
            real = store / "abcdef-rain.jpg"; real.write_bytes(b"\xff\xd8rain")
            link = art / "rain.jpg"; link.symlink_to(real)
            (root / "pl" / "library.m3u").write_text("#EXTM3U\n/mnt/music/a.mp3\n")
            a = admin.Admin(config.Config(root / "config"), None, root / "pl", station_art=art)
            self.assertEqual(a.art(str(link)), (b"\xff\xd8rain", "image/jpeg"))

    def test_the_art_endpoint_is_still_not_a_file_browser(self):
        """`art` refuses anything that is neither a library track nor station
        artwork — the station-art directory must not become a way to read the
        rest of the disk."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); (root / "config").mkdir(); (root / "pl").mkdir()
            art = root / "art"; art.mkdir()
            secret = root / "secret.txt"; secret.write_bytes(b"not yours")
            (root / "pl" / "library.m3u").write_text("#EXTM3U\n/mnt/music/a.mp3\n")
            a = admin.Admin(config.Config(root / "config"), None, root / "pl", station_art=art)
            with self.assertRaises(ValueError):
                a.art(str(secret))
            with self.assertRaises(ValueError):
                a.art(str(art / ".." / "secret.txt"))      # no escaping by traversal

    def test_station_art_wins_over_the_mosaic_and_the_icon(self):
        from netradio import playlists as pl
        with tempfile.TemporaryDirectory() as d:
            art = Path(d); (art / "rain.jpg").write_bytes(b"\xff\xd8x")
            stations = [{"mount": "rain", "name": "Rain", "kind": "fixed"}]
            tiles = pl.station_tiles(stations, {}, {}, {}, art_dir=art)
            self.assertEqual(tiles["rain"]["covers"], [str(art / "rain.jpg")])
            self.assertNotIn("icon", tiles["rain"])

    def test_a_station_picture_is_marked_as_one(self):
        """`art: true` distinguishes a picture chosen FOR the station from a
        mosaic of its albums. The phone's now-playing hero stands in with the
        former when a track has no cover of its own; standing in with a mosaic
        would put some other album on screen as though it were playing."""
        from netradio import playlists as pl
        with tempfile.TemporaryDirectory() as d:
            art = Path(d); (art / "rain.jpg").write_bytes(b"\xff\xd8x")
            tiles = pl.station_tiles([{"mount": "rain", "name": "Rain", "kind": "fixed"}],
                                     {}, {}, {}, art_dir=art)
            self.assertIs(tiles["rain"]["art"], True)

    def test_a_mosaic_is_not_marked_as_station_art(self):
        from netradio import playlists as pl
        with tempfile.TemporaryDirectory() as d:
            tiles = pl.station_tiles([{"mount": "folk", "name": "Folk", "kind": "curated"}],
                                     {"folk": []}, {}, {}, art_dir=Path(d))
            self.assertNotIn("art", tiles["folk"])

    def test_without_a_picture_a_fixed_station_still_gets_its_icon(self):
        from netradio import playlists as pl
        with tempfile.TemporaryDirectory() as d:
            stations = [{"mount": "rain", "name": "Rain", "kind": "fixed"}]
            tiles = pl.station_tiles(stations, {}, {}, {}, art_dir=Path(d))
            self.assertEqual(tiles["rain"], {"icon": "rain", "covers": []})


class JellyfinArtistArtwork(unittest.TestCase):
    """When an album has no sleeve, Jellyfin often still knows what the artist
    looks like. 502 of 1,668 artists had a picture when this was added, taking
    cover coverage from 79% to 84% — and landing on the ones that come round
    often (Bill Monroe, the Stanley Brothers, the Louvins).
    """

    def _admin(self, root, **kw):
        (root / "config").mkdir(exist_ok=True); (root / "pl").mkdir(exist_ok=True)
        alb = root / "Music" / "Bill Monroe" / "Bluegrass 1959"
        alb.mkdir(parents=True, exist_ok=True)
        track = alb / "01 x.mp3"; track.write_bytes(b"\x00" * 64)     # no art of any kind
        (root / "pl" / "library.m3u").write_text(f"#EXTM3U\n{track}\n")
        return admin.Admin(config.Config(root / "config"), None, root / "pl", **kw), track

    def test_the_artists_picture_stands_in_for_a_missing_sleeve(self):
        with tempfile.TemporaryDirectory() as d:
            a, track = self._admin(Path(d), jellyfin_url="http://jf", jellyfin_key="k")
            a._jf_artists, a._jf_artists_at = {"bill monroe": "ITEM1"}, 9e18
            with mock.patch.object(admin.urllib.request, "urlopen") as u:
                u.return_value.__enter__.return_value.read.return_value = b"\xff\xd8jf"
                u.return_value.__enter__.return_value.headers = {"Content-Type": "image/jpeg"}
                self.assertEqual(a.art(str(track)), (b"\xff\xd8jf", "image/jpeg"))

    def test_names_match_across_case_and_accents(self):
        self.assertEqual(admin._fold("Bill Monroe"), admin._fold("bill  monroe".replace("  ", " ")))
        self.assertEqual(admin._fold("Beyoncé"), admin._fold("BEYONCÉ"))

    def test_no_jellyfin_means_no_art_rather_than_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            a, track = self._admin(Path(d))
            self.assertIsNone(a.art(str(track)))

    def test_credentials_alone_do_not_start_mirroring_hearts(self):
        """Connecting Jellyfin for pictures must not quietly begin writing
        favourites into somebody's library."""
        with tempfile.TemporaryDirectory() as d:
            a, _ = self._admin(Path(d), jellyfin_url="http://jf", jellyfin_key="k")
            self.assertIsNone(a._jellyfin(), "artwork credentials enabled heart mirroring")
            b, _ = self._admin(Path(d), jellyfin_url="http://jf", jellyfin_key="k", jellyfin_hearts=True)
            self.assertIsNotNone(b._jellyfin())


class RequestIsNotAnArtEndpoint(unittest.TestCase):
    """`request()` and `art()` both begin "is this a library track?", and an
    edit meant for one landed in the other twice on 2026-09-28 — once shipping
    in #361, once caught here. They are not interchangeable: request() queues a
    song, art() returns bytes.
    """

    def test_request_still_refuses_a_station_picture(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); (root / "config").mkdir(); (root / "pl").mkdir()
            art = root / "art"; art.mkdir()
            pic = art / "rain.jpg"; pic.write_bytes(b"\xff\xd8rain")
            (root / "pl" / "library.m3u").write_text("#EXTM3U\n/mnt/music/a.mp3\n")
            a = admin.Admin(config.Config(root / "config"), root / "dj", root / "pl", station_art=art)
            with self.assertRaises(ValueError):
                a.request("rain", {"path": str(pic)})
