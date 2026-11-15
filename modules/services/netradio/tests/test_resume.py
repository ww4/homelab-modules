"""`netradio resume` — the guardrails, and the memory it depends on."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from netradio import playlists, resume

MENU = playlists.ycast_yaml(
    [{"name": "Classic Country", "mount": "country", "kind": "curated"},
     {"name": "Rainy Mood", "mount": "rainymood", "kind": "fixed"},
     {"name": "Banjo Instrumentals", "mount": "banjo-instrumentals", "kind": "specialty"}],
    "http://radioyamaha.vtuner.com/radio", [{"name": "NPR News", "url": "http://npr/live.mp3"}])


def playing(station="Classic Country", **over):
    st = {"on": True, "input": "NET RADIO", "now_playing": {"playback": "Play", "station": station}}
    st.update(over)
    return st


class Memory(unittest.TestCase):
    def test_a_playing_library_station_is_remembered_with_its_mount(self):
        r = resume.observe(playing(), MENU)
        self.assertEqual(r["last_library"],
                         {"station": "Classic Country", "mount": "country",
                          "category": "Curated", "at": r["at"]})

    def test_the_menu_file_decides_the_category_not_the_station_kind(self):
        # Rainy Mood is a `fixed` station that the PAGE lists under Specialty,
        # while the receiver's menu has it under Curated. Deriving the category
        # from kind would send the receiver down the wrong branch.
        r = resume.observe(playing("Rainy Mood"), MENU)
        self.assertEqual(r["last_library"]["category"], "Curated")
        r = resume.observe(playing("Banjo Instrumentals"), MENU)
        self.assertEqual(r["last_library"]["category"], "Specialty")

    def test_the_memory_is_STICKY_when_it_stops(self):
        """The whole point: a stopped receiver reports no station, so if the
        record were overwritten from the live state there would be nothing left
        to resume to."""
        first = resume.observe(playing(), MENU)
        later = resume.observe({"on": True, "input": "NET RADIO",
                                "now_playing": {"playback": "Stop", "station": ""}}, MENU, first)
        self.assertEqual(later["last_library"], first["last_library"])
        self.assertEqual(later["playback"], "Stop")

    def test_an_unreachable_receiver_does_not_erase_the_memory(self):
        first = resume.observe(playing(), MENU)
        later = resume.observe(None, MENU, first)
        self.assertEqual(later["last_library"], first["last_library"])
        self.assertFalse(later["reachable"])

    def test_something_that_is_not_a_library_station_is_not_remembered(self):
        r = resume.observe(playing("Some Internet Station"), MENU)
        self.assertIsNone(r["last_library"])

    def test_round_trips_through_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            resume.save(Path(d), resume.observe(playing(), MENU))
            self.assertEqual(resume.load(Path(d))["last_library"]["mount"], "country")

    def test_a_missing_or_corrupt_file_reads_as_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(resume.load(Path(d)), {})
            (Path(d) / resume.STATE).write_text("{not json")
            self.assertEqual(resume.load(Path(d)), {})


class Guardrails(unittest.TestCase):
    """It is somebody's living room. Each of these is a refusal, and each says
    why — a box that quietly drives a stereo is worse than one that does not."""

    REMEMBERED = {"station": "Classic Country", "mount": "country", "category": "Curated"}

    def test_it_acts_only_when_stopped_on_the_right_input_while_powered_on(self):
        self.assertEqual(resume.why_not({"on": True, "input": "NET RADIO",
                                         "now_playing": {"playback": "Stop"}}, self.REMEMBERED), "")

    def test_it_never_powers_a_receiver_on(self):
        self.assertIn("standby", resume.why_not({"on": False}, self.REMEMBERED))

    def test_it_never_switches_inputs(self):
        why = resume.why_not({"on": True, "input": "CD", "now_playing": {}}, self.REMEMBERED)
        self.assertIn("CD", why)

    def test_it_never_interrupts_something_already_playing(self):
        self.assertIn("already playing", resume.why_not(playing(), self.REMEMBERED))

    def test_force_overrides_only_the_already_playing_check(self):
        self.assertEqual(resume.why_not(playing(), self.REMEMBERED, force=True), "")
        # …and not the others
        self.assertIn("standby", resume.why_not({"on": False}, self.REMEMBERED, force=True))

    def test_with_nothing_remembered_it_does_nothing(self):
        self.assertIn("nothing to resume", resume.why_not(playing(), None))

    def test_an_unreachable_receiver_is_not_an_occasion_to_guess(self):
        self.assertIn("did not answer", resume.why_not(None, self.REMEMBERED))


class Acting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        resume.save(self.dir, resume.observe(playing(), MENU))
        self.addCleanup(self.tmp.cleanup)

    def test_it_wakes_the_encoder_BEFORE_telling_the_receiver_to_play(self):
        """The encoders are on demand. A receiver that asks half a second too
        early gets a 404 from Icecast and stops again — the same failure by
        another road."""
        order = []
        with mock.patch.object(resume, "_get", side_effect=lambda url, **k: (
                order.append("wake" if "/wake" in url else "status"),
                {"on": True, "input": "NET RADIO", "now_playing": {"playback": "Stop"}})[1]), \
             mock.patch.object(resume, "_post", side_effect=lambda url, body, **k: order.append(("play", body))):
            msg = resume.resume("http://r", self.dir, wake_url="http://w")
        self.assertEqual(msg, "resumed Classic Country")
        self.assertEqual(order[0], "status")
        self.assertIn("wake", order)
        self.assertLess(order.index("wake"), [i for i, o in enumerate(order) if isinstance(o, tuple)][0])

    def test_the_menu_path_is_the_one_the_receiver_understands(self):
        sent = []
        with mock.patch.object(resume, "_get", return_value={"on": True, "input": "NET RADIO",
                                                            "now_playing": {"playback": "Stop"}}), \
             mock.patch.object(resume, "_post", side_effect=lambda url, body, **k: sent.append((url, body))):
            resume.resume("http://r", self.dir)
        self.assertEqual(sent[0][0], "http://r/menu/path")
        self.assertEqual(sent[0][1], {"path": ["My Stations", "Curated", "Classic Country"]})

    def test_a_refusal_sends_nothing_at_all(self):
        sent = []
        with mock.patch.object(resume, "_get", return_value={"on": False}), \
             mock.patch.object(resume, "_post", side_effect=lambda *a, **k: sent.append(a)):
            msg = resume.resume("http://r", self.dir)
        self.assertIn("standby", msg)
        self.assertEqual(sent, [], "a refusal must not touch the receiver")

    def test_it_never_sends_volume_or_mute(self):
        sent = []
        with mock.patch.object(resume, "_get", return_value={"on": True, "input": "NET RADIO",
                                                            "now_playing": {"playback": "Stop"}}), \
             mock.patch.object(resume, "_post", side_effect=lambda url, body, **k: sent.append(url)):
            resume.resume("http://r", self.dir)
        self.assertTrue(all("volume" not in u and "mute" not in u and "power" not in u for u in sent), sent)


if __name__ == "__main__":
    unittest.main()
