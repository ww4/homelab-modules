import json
import logging
import tempfile
import unittest
from pathlib import Path

from netradio import pandora

logging.disable(logging.CRITICAL)


def status(track, station="Dinner Jazz Radio", album="That Old Feeling", feedback="---", artist="", input_="Pandora"):
    return {"input": input_, "on": True, "now_playing": {"station": station, "track": track, "artist": artist, "album": album, "feedback": feedback}}


class SplitTrack(unittest.TestCase):
    def test_pandora_folds_the_artist_into_the_title(self):
        self.assertEqual(pandora.split_track("The Man I Love by Zoot Sims Quartet", ""), ("The Man I Love", "Zoot Sims Quartet"))
        self.assertEqual(pandora.split_track("Stand by Me", "Ben E. King"), ("Stand by Me", "Ben E. King"))   # a real artist field wins
        self.assertEqual(pandora.split_track("Untitled", ""), ("Untitled", ""))


class Logging(unittest.TestCase):
    def test_a_track_is_written_when_the_next_starts_with_its_thumb(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "pandora.jsonl"
            lg = pandora.Logger("http://x", out)
            lg.observe(status("The Man I Love by Zoot Sims Quartet"), now=1000)
            lg.observe(status("The Man I Love by Zoot Sims Quartet", feedback="Thumb Up"), now=1100)
            self.assertFalse(out.exists())                                     # still playing
            lg.observe(status("Blue in Green by Miles Davis", album="Kind of Blue"), now=1200)
            lg.observe(status("Blue in Green by Miles Davis", album="Kind of Blue"), now=1210)
            lg.observe({"input": "NET RADIO", "on": True, "now_playing": {}}, now=1215)   # skipped after 15 s: not a listen
            recs = pandora.read_log(out)
            self.assertEqual(len(recs), 1)
            self.assertEqual((recs[0]["artist"], recs[0]["title"], recs[0]["feedback"], recs[0]["seconds"]),
                             ("Zoot Sims Quartet", "The Man I Love", "Thumb Up", 200))
            lg.observe(None, now=1300)                                          # the API down: nothing breaks
            summary = pandora.summarise(recs + [{"station": "Dinner Jazz Radio", "artist": "Miles Davis", "title": "So What", "feedback": ""}],
                                        {"Miles Davis"})
            arts = summary["Dinner Jazz Radio"]["artists"]
            self.assertEqual([a["name"] for a in arts], ["Zoot Sims Quartet", "Miles Davis"])   # thumbed first
            self.assertEqual([a["in_library"] for a in arts], [False, True])
