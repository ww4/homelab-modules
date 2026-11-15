"""Skips accumulate; the heart is a toggle. What that does to the odds."""

import random
import unittest
from collections import Counter

from netradio import ratings

P = "/srv/media/Music/Bill Monroe/Bluegrass 1950/03 Uncle Pen.mp3"
Q = "/srv/media/Music/Bill Monroe/Bluegrass 1950/04 Blue Moon.mp3"
OTHER = "/srv/media/Music/Hank Williams/Moanin/01 Lovesick.mp3"


class Skips(unittest.TestCase):
    def test_each_skip_halves_the_odds(self):
        st = {}
        for n, want in ((0, 1.0), (1, 0.5), (2, 0.25), (3, 0.125)):
            self.assertAlmostEqual(ratings.weight(st, P), want, msg=f"{n} skips")
            ratings.record_skip(st, P)

    def test_the_fourth_skip_retires_it_with_no_verdict(self):
        """Nothing has to be declared "never" for repeated skipping to stop a
        track coming back."""
        st = {}
        for _ in range(4):
            ratings.record_skip(st, P)
        self.assertEqual(ratings.weight(st, P), 0.0)

    def test_one_skip_is_not_a_life_sentence(self):
        st = {}
        ratings.record_skip(st, P)
        self.assertGreater(ratings.weight(st, P), 0.0)

    def test_a_skip_with_no_path_is_ignored_rather_than_stored_under_nothing(self):
        st = {}
        ratings.record_skip(st, "")
        self.assertEqual(st.get("tracks", {}), {})


class Heart(unittest.TestCase):
    """2026-09-27: "It's not cumulative, it's just a toggle. Heart tracks
    play more often. That's all." """

    def test_a_heart_cannot_be_run_up(self):
        st = {}
        ratings.set_heart(st, P, True)
        once = ratings.weight(st, P)
        for _ in range(20):
            ratings.set_heart(st, P, True)
        self.assertEqual(ratings.weight(st, P), once,
                         "pressing it twenty times must be the same as once")

    def test_it_is_worth_a_fixed_amount(self):
        st = {}
        ratings.set_heart(st, P, True)
        self.assertEqual(ratings.weight(st, P), ratings.HEART)

    def test_a_heart_outranks_even_enough_skips_to_retire_it(self):
        """Someone went and favourited this; it should not stay suppressed
        because they skipped it one distracted evening."""
        st = {}
        for _ in range(6):
            ratings.record_skip(st, P)
        self.assertEqual(ratings.weight(st, P), 0.0)
        ratings.set_heart(st, P, True)
        self.assertEqual(ratings.weight(st, P), ratings.HEART)

    def test_un_hearting_adds_no_skip(self):
        """"Not a favourite" is a long way from "play this less"."""
        st = {}
        ratings.set_heart(st, P, True)
        ratings.set_heart(st, P, False)
        self.assertEqual(ratings.skips(st, P), 0)
        self.assertAlmostEqual(ratings.weight(st, P), 1.0)

    def test_un_hearting_restores_whatever_the_skips_said(self):
        st = {}
        ratings.record_skip(st, P)
        ratings.set_heart(st, P, True)
        ratings.set_heart(st, P, False)
        self.assertAlmostEqual(ratings.weight(st, P), 0.5, msg="the skip is still on the record")


class Picking(unittest.TestCase):
    def test_a_hearted_track_comes_round_oftener(self):
        st = {}
        ratings.set_heart(st, P, True)
        rng = random.Random(7)
        got = Counter(ratings.pick(st, [P, Q, OTHER], rng) for _ in range(3000))
        self.assertGreater(got[P], got[Q] * 2, got)

    def test_but_it_does_not_take_the_station_over(self):
        """4x, not 40x — the others still get a real share."""
        st = {}
        ratings.set_heart(st, P, True)
        rng = random.Random(9)
        got = Counter(ratings.pick(st, [P, Q, OTHER], rng) for _ in range(3000))
        self.assertGreater(got[Q], 300, got)
        self.assertGreater(got[OTHER], 300, got)

    def test_a_retired_track_is_never_offered_while_others_remain(self):
        st = {}
        for _ in range(4):
            ratings.record_skip(st, P)
        rng = random.Random(11)
        got = Counter(ratings.pick(st, [P, OTHER], rng) for _ in range(500))
        self.assertEqual(got[P], 0, got)

    def test_a_station_skipped_to_nothing_plays_anyway_rather_than_going_silent(self):
        st = {}
        for p in (P, Q):
            for _ in range(5):
                ratings.record_skip(st, p)
        self.assertIn(ratings.pick(st, [P, Q], random.Random(3)), (P, Q))

    def test_an_empty_pool_picks_nothing(self):
        self.assertIsNone(ratings.pick({}, [], random.Random(1)))

    def test_with_no_opinions_every_track_is_equally_likely(self):
        rng = random.Random(5)
        got = Counter(ratings.pick({}, [P, Q, OTHER], rng) for _ in range(3000))
        for n in got.values():
            self.assertGreater(n, 800, got)
