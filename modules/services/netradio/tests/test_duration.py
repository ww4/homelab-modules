import unittest
from unittest import mock
from types import SimpleNamespace

from netradio import profile


class Duration(unittest.TestCase):
    def run_with(self, ffprobe_out, mutagen_len=None, decoded=None):
        with mock.patch.object(profile.subprocess, "run", return_value=SimpleNamespace(stdout=ffprobe_out)), \
             mock.patch.dict("sys.modules", {"mutagen": SimpleNamespace(File=lambda p: SimpleNamespace(info=SimpleNamespace(length=mutagen_len)) if mutagen_len is not None else None)}), \
             mock.patch.object(profile, "decode", return_value=[0.0] * (decoded or 0)):
            return profile.duration_of("/m/x.mp3")

    def test_ffprobe_value(self):
        self.assertEqual(self.run_with("187.4\n"), 187.4)

    def test_na_falls_back_to_tags(self):
        self.assertEqual(self.run_with("N/A\n", mutagen_len=201.0), 201.0)

    def test_then_to_a_decode(self):
        self.assertEqual(self.run_with("N/A\n", mutagen_len=0.0, decoded=profile.SR * 3), 3.0)


if __name__ == "__main__":
    unittest.main()
