"""The heart, and the merge with Jellyfin's favourites."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from netradio import jellyfin, ratings
from netradio.config import Config

A = "/srv/media/Music/Bill Monroe/Bluegrass 1950/03 Uncle Pen.mp3"
B = "/srv/media/Music/Hank Williams/Moanin/01 Lovesick.mp3"


class FakeJF:
    """Jellyfin with the network taken out — the two calls that matter are
    looking a path up and setting the flag."""

    def __init__(self, favourites=(), known=None, refuse=False):
        self.favs = [{"id": f"id-{i}", "path": p, "title": Path(p).stem, "artist": ""}
                     for i, p in enumerate(favourites)]
        self.known = set(known if known is not None else list(favourites) + [A, B])
        self.refuse = refuse
        self.set_calls: list[tuple[str, bool]] = []

    def favourites(self):
        return list(self.favs)

    def item_for_path(self, path):
        return f"id-{path}" if path in self.known else None

    def set_favourite(self, item_id, on):
        self.set_calls.append((item_id, on))
        return not self.refuse


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "stations.json").write_text("[]")
        self.cfg = Config(self.root)
        self.addCleanup(self.tmp.cleanup)

    def entry(self, path):
        return (self.cfg.ratings().get("tracks") or {}).get(path) or {}


class ToggleWithoutJellyfin(Base):
    """The button has to work for somebody who does not run Jellyfin at all."""

    def test_it_stores_locally_and_says_so(self):
        r = jellyfin.toggle(self.cfg, None, A, True, "Bill Monroe", "Uncle Pen")
        self.assertTrue(r["ok"])
        self.assertTrue(r["heart"])
        self.assertFalse(r["mirrored"])
        self.assertTrue(ratings.hearted(self.cfg.ratings(), A))

    def test_it_toggles_back_off(self):
        jellyfin.toggle(self.cfg, None, A, True)
        jellyfin.toggle(self.cfg, None, A, False)
        self.assertFalse(ratings.hearted(self.cfg.ratings(), A))

    def test_a_local_heart_is_not_marked_synced(self):
        """Which is what lets a later merge tell it apart from one REMOVED in
        Jellyfin — the distinction the whole design turns on."""
        jellyfin.toggle(self.cfg, None, A, True)
        self.assertFalse(self.entry(A).get("heart_synced"))


class ToggleWithJellyfin(Base):
    def test_it_mirrors_and_marks_synced(self):
        jf = FakeJF()
        r = jellyfin.toggle(self.cfg, jf, A, True)
        self.assertTrue(r["mirrored"])
        self.assertEqual(jf.set_calls, [(f"id-{A}", True)])
        self.assertTrue(self.entry(A).get("heart_synced"))

    def test_the_local_heart_survives_jellyfin_refusing(self):
        """A heart the listener pressed must not be lost because Jellyfin was
        unreachable; the next merge pushes it up."""
        jf = FakeJF(refuse=True)
        r = jellyfin.toggle(self.cfg, jf, A, True)
        self.assertTrue(r["heart"], "stored locally regardless")
        self.assertFalse(r["mirrored"])
        self.assertIn("next sync", r["message"])
        self.assertTrue(ratings.hearted(self.cfg.ratings(), A))
        self.assertFalse(self.entry(A).get("heart_synced"))

    def test_a_track_jellyfin_does_not_know_is_still_hearted_locally(self):
        jf = FakeJF(known=[])
        r = jellyfin.toggle(self.cfg, jf, A, True)
        self.assertTrue(ratings.hearted(self.cfg.ratings(), A))
        self.assertFalse(r["mirrored"])


class Merge(Base):
    def test_connecting_jellyfin_UNIONS_rather_than_either_side_erasing(self):
        """The case the owner asked about: hearts made locally while Jellyfin was
        not connected, and hearts already in Jellyfin. Both survive."""
        jellyfin.toggle(self.cfg, None, A, True)          # local only
        jf = FakeJF(favourites=[B])                        # Jellyfin has another
        counts = jellyfin.sync(self.cfg, jf)
        self.assertTrue(ratings.hearted(self.cfg.ratings(), A), "the local heart stayed")
        self.assertTrue(ratings.hearted(self.cfg.ratings(), B), "Jellyfin's came down")
        self.assertEqual(counts["pulled"], 1)
        self.assertEqual(counts["pushed"], 1)
        self.assertEqual(counts["cleared"], 0, "nothing may be erased on a first merge")
        self.assertIn((f"id-{A}", True), jf.set_calls, "the local one went up")

    def test_a_heart_removed_in_jellyfin_is_cleared_here(self):
        """Only once the two are in step — which `heart_synced` records."""
        jf = FakeJF(favourites=[A])
        jellyfin.sync(self.cfg, jf)                        # now in step
        self.assertTrue(self.entry(A).get("heart_synced"))
        counts = jellyfin.sync(self.cfg, FakeJF(favourites=[]))   # removed there
        self.assertFalse(ratings.hearted(self.cfg.ratings(), A))
        self.assertEqual(counts["cleared"], 1)

    def test_clearing_a_heart_adds_no_skip(self):
        jf = FakeJF(favourites=[A])
        jellyfin.sync(self.cfg, jf)
        jellyfin.sync(self.cfg, FakeJF(favourites=[]))
        self.assertEqual(ratings.skips(self.cfg.ratings(), A), 0)

    def test_a_second_merge_with_no_changes_does_nothing(self):
        jf = FakeJF(favourites=[A])
        jellyfin.sync(self.cfg, jf)
        counts = jellyfin.sync(self.cfg, FakeJF(favourites=[A]))
        self.assertEqual((counts["pulled"], counts["pushed"], counts["cleared"]), (0, 0, 0))

    def test_a_dry_run_changes_nothing(self):
        jellyfin.toggle(self.cfg, None, A, True)
        before = self.cfg.ratings()
        jf = FakeJF(favourites=[B])
        counts = jellyfin.sync(self.cfg, jf, dry_run=True)
        self.assertGreater(counts["pulled"] + counts["pushed"], 0)
        self.assertEqual(self.cfg.ratings(), before)
        self.assertEqual(jf.set_calls, [], "and must not touch Jellyfin either")

    def test_an_unreachable_jellyfin_leaves_the_local_hearts_alone(self):
        jellyfin.toggle(self.cfg, None, A, True)
        class Down(FakeJF):
            def favourites(self): raise OSError("connection refused")
        counts = jellyfin.sync(self.cfg, Down())
        self.assertTrue(ratings.hearted(self.cfg.ratings(), A))
        self.assertEqual(counts["remote"], 0)


class KeyReading(unittest.TestCase):
    def test_both_shapes_of_key_file_are_understood(self):
        with tempfile.TemporaryDirectory() as d:
            bare, env = Path(d) / "bare", Path(d) / "env"
            bare.write_text("abc123\n")
            env.write_text("# a comment\nJELLYFIN_API_KEY=abc123\n")
            self.assertEqual(jellyfin.read_key(key_file=bare), "abc123")
            self.assertEqual(jellyfin.read_key(key_file=env), "abc123")

    def test_a_missing_file_is_no_key_rather_than_a_crash(self):
        self.assertEqual(jellyfin.read_key(key_file=Path("/nonexistent")), "")


class NowPlaying(unittest.TestCase):
    """What is on a screen in the house.

    The radio can name its own track because it CHOSE it. A streaming box
    cannot be asked the same question — a Roku names the app and stops there —
    but Jellyfin is the server, so when the thing on the television came from
    the library it knows exactly (2026-10-09).

    The two cases below are the two the LIVE data corrected. Both would have
    looked right if they had been guessed.
    """

    def jf(self):
        return jellyfin.Jellyfin.__new__(jellyfin.Jellyfin)   # parsing only, no network

    def test_a_file_that_is_several_episodes(self):
        """`IndexNumberEnd` is set when one file holds a run of them — the
        episode playing when this was written was 40 THROUGH 42, and "S1E40"
        would have been wrong while looking perfectly plausible."""
        out = self.jf()._describe({
            "DeviceName": "Roku Express 4K  (3941X)", "Client": "Jellyfin Roku",
            "PlayState": {"PositionTicks": 9900000000, "IsPaused": False},
            "NowPlayingItem": {
                "Type": "Episode", "Name": "Little Bear the Magician",
                "SeriesName": "Little Bear", "SeriesId": "51910ca395bfc44fc7eb0c6a924bb38f",
                "Id": "6ddf97574d97bd5e813be645bb325171",
                "ParentIndexNumber": 1, "IndexNumber": 40, "IndexNumberEnd": 42,
                "ProductionYear": 1996, "RunTimeTicks": 14491010000},
        })
        self.assertEqual(out["head"], "Little Bear")
        self.assertEqual(out["episode"], "40\u201342")
        self.assertEqual(out["sub"], "S1E40\u201342 \u00b7 Little Bear the Magician")
        self.assertEqual((out["position"], out["duration"]), (990, 1449))
        self.assertFalse(out["paused"])
        # the SERIES poster, not the episode's own frame grab
        self.assertEqual(out["art_id"], "51910ca395bfc44fc7eb0c6a924bb38f")

    def test_a_single_episode_has_no_range(self):
        out = self.jf()._describe({"NowPlayingItem": {
            "Type": "Episode", "Name": "Pilot", "SeriesName": "A Show",
            "ParentIndexNumber": 2, "IndexNumber": 7, "Id": "a" * 32}})
        self.assertEqual(out["sub"], "S2E7 \u00b7 Pilot")

    def test_a_film(self):
        out = self.jf()._describe({"NowPlayingItem": {
            "Type": "Movie", "Name": "The Thin Man", "ProductionYear": 1934, "Id": "b" * 32}})
        self.assertEqual((out["head"], out["sub"]), ("The Thin Man", "1934"))

    def test_the_session_is_the_one_PLAYING_not_the_one_named_right(self):
        """Three sessions on this server shared the DeviceName "Roku Express
        4K (3941X)" — two left over from a fortnight before, one real. Choosing
        by device would have been a coin toss between them."""
        jf = self.jf()
        sessions = [
            {"DeviceName": "Roku Express 4K", "LastActivityDate": "2026-10-04T02:53:17"},
            {"DeviceName": "Roku Express 4K", "LastActivityDate": "2026-10-03T22:34:18"},
            {"DeviceName": "Roku Express 4K", "LastActivityDate": "2026-10-20T03:33:09",
             "NowPlayingItem": {"Type": "Movie", "Name": "The right one", "Id": "c" * 32}},
        ]
        with mock.patch.object(jellyfin.Jellyfin, "_req", return_value=sessions):
            self.assertEqual(jf.now_playing()["head"], "The right one")

    def test_nothing_playing_is_empty_not_an_error(self):
        jf = self.jf()
        with mock.patch.object(jellyfin.Jellyfin, "_req", return_value=[{"DeviceName": "idle"}]):
            self.assertEqual(jf.now_playing(), {})

    def test_a_media_server_that_is_down_is_not_this_page_failing(self):
        jf = self.jf()
        with mock.patch.object(jellyfin.Jellyfin, "_req", side_effect=OSError("refused")):
            self.assertEqual(jf.now_playing(), {})
