import logging
import unittest

from netradio import rules

logging.disable(logging.CRITICAL)


def f(bw, corr, bitrate=320, codec="mp3"):
    return {"bandwidth_hz": bw, "stereo_corr": corr, "bitrate": bitrate, "codec": codec}


class Era(unittest.TestCase):
    """The 35 measured tracks (docstring in profile.py), by bucket."""

    def test_shellac(self):
        for bw, corr in [(5200, 1.0), (5400, 1.0), (7300, 1.0), (5800, 1.0), (5700, 0.99),   # Carter, Rodgers, Monroe Bros
                         (7500, 1.0), (5500, 1.0), (4700, 1.0), (5600, 1.0)]:                 # Hank Williams, early Stanleys, Boggs '27
            self.assertEqual(rules.era_of(f(bw, corr)), "shellac", (bw, corr))

    def test_vintage(self):
        for bw, corr in [(12000, 1.0), (10900, 1.0), (8300, 1.0), (11600, 1.0), (10500, 1.0), (12900, 1.0),  # Stanleys, F&S, Hank, Boggs 60s
                         (10300, 0.80),                                                                      # tape-era stereo (Flatt & Scruggs)
                         (14400, 1.0)]:                                                                      # 60s George Jones, mono
            self.assertEqual(rules.era_of(f(bw, corr)), "vintage", (bw, corr))

    def test_hifi(self):
        for bw, corr in [(17800, 0.87), (13300, 0.86),   # 60s stereo Nashville sounds modern; only an honest date demotes it
                         (15400, 1.0), (16000, 0.89), (20500, 0.85), (18800, 0.60), (15000, 0.99),
                         (12629, 0.89), (12188, 0.86), (13620, 0.90), (11822, 0.91)]:   # modern VBR MP3s (Sutton, Skaggs)
            self.assertEqual(rules.era_of(f(bw, corr)), "hifi", (bw, corr))

    def test_codec_limited_stereo_is_not_vintage(self):
        # a 96 kbps rip of a 2003 album lowpasses at ~11 kHz
        self.assertEqual(rules.era_of(f(10400, 0.96, bitrate=96)), "hifi")
        # but the same band at a bitrate that could carry more is tape
        self.assertEqual(rules.era_of(f(10400, 0.96, bitrate=320)), "vintage")
        # the known miss: a modern 149 kbps VBR file at 10.4 kHz stays vintage (override it)
        self.assertEqual(rules.era_of(f(10422, 0.96, bitrate=149)), "vintage")

    def test_honest_old_date_demotes_clean_stereo(self):
        self.assertEqual(rules.era_of({**f(17800, 0.87), "date": "1965-03-01"}), "vintage")
        self.assertEqual(rules.era_of({**f(17800, 0.87), "date": "1998"}), "hifi")     # a reissue date changes nothing
        self.assertEqual(rules.era_of({**f(5200, 1.0), "date": "1998"}), "shellac")   # nor does it promote

    def test_lossless_and_unmeasured(self):
        self.assertEqual(rules.era_of(f(19000, 0.7, bitrate=900, codec="flac")), "hifi")
        self.assertEqual(rules.era_of({}), "")
        self.assertEqual(rules.era_of(f(0, 1.0)), "")

    def test_verdict_and_override(self):
        v = rules.evaluate("/m/x.mp3", {"talk_frames": 0.0, "pitch_stable": 0.5, **f(6000, 1.0)})
        self.assertEqual(v.era, "shellac")
        self.assertFalse(v.talk)
        out = rules.apply_overrides({"/m/x.mp3": v}, {"/m/x.mp3": "vintage"})
        self.assertEqual(out["/m/x.mp3"].era, "vintage")
        self.assertFalse(out["/m/x.mp3"].talk)
        out = rules.apply_overrides({"/m/x.mp3": v}, {"/m/x.mp3": "talk"})
        self.assertTrue(out["/m/x.mp3"].talk)
        self.assertEqual(out["/m/x.mp3"].era, "shellac")   # a talk override keeps the era


if __name__ == "__main__":
    unittest.main()
