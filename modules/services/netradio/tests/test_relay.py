"""Relays: an outside station re-encoded on demand for a device that cannot
decode the original.

2026-09-28: "Put them in quick picks, a codec doesn't make them special."
So a relay is invisible as a category — the listener sees one Internet Radio
list, and a station the receiver cannot decode simply points at our own MP3
mount instead of the origin.
"""

import json
import unittest

from netradio import liq, playlists, wake

STATIONS = [{"mount": "country", "name": "Classic Country", "kind": "curated"}]
RELAYS = [{"mount": "ir-hank-fm-105-5", "name": "Hank FM 105.5", "url": "http://ice9/WLXO"},
          {"mount": "ir-froggy-104-9", "name": "Froggy 104.9", "url": "http://ice9/WFKY"}]


class Script(unittest.TestCase):
    def render(self, relays=RELAYS):
        return liq.render(STATIONS, relays=relays, socket="/s", playlists="/p",
                          now_dir="/n", port=8020)

    def test_a_relay_is_emitted_for_each_outside_station(self):
        out = self.render()
        for r in RELAYS:
            self.assertIn(f'relay("{r["mount"]}", "{r["name"]}", "{r["url"]}")', out)

    def test_the_stations_are_still_there(self):
        self.assertIn('station("country", "Classic Country")', self.render())

    def test_no_relays_changes_nothing(self):
        self.assertEqual(self.render([]), self.render(None))
        # the `def relay` lives in the header either way — it is the CALLS that
        # should be absent, which is what a line starting with relay( is
        calls = [l for l in self.render([]).splitlines() if l.startswith("relay(")]
        self.assertEqual(calls, [])

    def test_a_relay_starts_STOPPED_like_every_other_mount(self):
        """The encoders are on demand — a relay that ran all the time would be
        transcoding somebody else's stream around the clock for nobody."""
        self.assertIn("start=false", liq.HEADER)

    def test_quoting_survives_a_name_with_a_quote_in_it(self):
        out = liq.render(STATIONS, relays=[{"mount": "ir-x", "name": 'He said "hi"',
                                            "url": "http://x"}],
                         socket="/s", playlists="/p", now_dir="/n", port=8020)
        self.assertIn(json.dumps('He said "hi"'), out)


class Wakeable(unittest.TestCase):
    def test_a_relay_can_be_woken_like_a_station(self):
        mounts = wake.all_mounts(STATIONS, RELAYS)
        for r in RELAYS:
            self.assertIn(r["mount"], mounts)

    def test_a_relay_has_no_quality_variants(self):
        """One upstream; re-encoding it twice pays twice for the same thing."""
        mounts = wake.all_mounts(STATIONS, RELAYS)
        self.assertNotIn("ir-hank-fm-105-5-lo", mounts)
        self.assertIn("country-lo", mounts, "…while a library station still has its own")

    def test_stations_alone_still_work(self):
        self.assertEqual(wake.all_mounts(STATIONS), wake.all_mounts(STATIONS, []))


class Menu(unittest.TestCase):
    IR = [{"name": "WMMT", "url": "http://x/r.mp3", "codec": "mp3"},
          {"name": "Hank FM 105.5", "url": "http://y/WLXO", "codec": "he-aac",
           "relay": "ir-hank-fm-105-5"},
          {"name": "Orphan", "url": "http://z/q", "codec": "he-aac"}]

    def entries(self, codecs):
        text = playlists.ycast_yaml(STATIONS, "http://vt/radio", self.IR, codecs)
        return {n: u for c, n, u in playlists.parse_ycast_yaml(text) if c == "Internet Radio"}

    def test_an_undecodable_station_points_at_its_relay_instead_of_vanishing(self):
        got = self.entries({"mp3"})
        self.assertEqual(got["Hank FM 105.5"], "http://vt/radio/ir-hank-fm-105-5.mp3")

    def test_a_decodable_one_keeps_the_original_url(self):
        """No point paying to re-encode something the device can already play."""
        self.assertEqual(self.entries({"mp3"})["WMMT"], "http://x/r.mp3")
        self.assertEqual(self.entries({"mp3", "he-aac"})["Hank FM 105.5"], "http://y/WLXO")

    def test_one_with_no_relay_is_still_dropped(self):
        self.assertNotIn("Orphan", self.entries({"mp3"}))
        self.assertIn("Orphan", self.entries({"mp3", "he-aac"}))

    def test_the_category_is_Internet_Radio(self):
        text = playlists.ycast_yaml(STATIONS, "http://vt/radio", self.IR, {"mp3"})
        self.assertIn("Internet Radio", [c for c, _, _ in playlists.parse_ycast_yaml(text)])
        self.assertNotIn("Quick Picks", text)
