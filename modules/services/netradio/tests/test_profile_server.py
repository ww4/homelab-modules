import json
import logging
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from netradio import profile, profile_server

logging.disable(logging.CRITICAL)

FACTS = {"duration": 12.0, "talk_frames": 0.0, "pitch_stable": 0.3, "head_talk_frames": 0.0,
         "head_pitch_stable": 0.3, "tail_talk_frames": 0.0, "tail_pitch_stable": 0.3,
         "bandwidth_hz": 6000.0, "stereo_corr": 1.0, "bitrate": 128, "codec": "mp3",
         "date": "", "artist": "A", "album": "B", "yamnet": {"banjo": 0.5}}


class FakePool:
    """Runs the worker function inline (no processes) with a stubbed analyse."""

    def apply(self, fn, args):
        with mock.patch.object(profile, "analyse", return_value=profile.Facts(**FACTS)), \
             mock.patch.object(profile, "read_title", return_value="Title"):
            return fn(*args)


class Server(unittest.TestCase):
    def setUp(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), profile_server.make_handler(FakePool()))
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.track = Path(self.tmp.name) / "01 Song.mp3"
        self.track.write_bytes(b"not really audio")

    def tearDown(self):
        self.srv.shutdown()
        self.tmp.cleanup()

    def test_health(self):
        with urllib.request.urlopen(self.url + "/health") as r:
            self.assertEqual(json.load(r)["profile_version"], profile.PROFILE_VERSION)

    def test_round_trip(self):
        facts, title = profile.analyse_remote(str(self.track), self.url)
        self.assertEqual(facts["bandwidth_hz"], 6000.0)
        self.assertEqual(facts["yamnet"], {"banjo": 0.5})
        self.assertEqual(title, "Title")

    def test_worker_prefers_remote_then_falls_back(self):
        profile._worker_init("/nonexistent/model.onnx", [self.url])
        track, facts, title = profile._worker_analyse(str(self.track))
        self.assertEqual(facts["bandwidth_hz"], 6000.0)      # came from the server
        # a dead remote: falls back to local analysis (stubbed here)
        profile._worker_init("/nonexistent/model.onnx", ["http://127.0.0.1:1"])
        with mock.patch.object(profile, "_load_local_model"), \
             mock.patch.object(profile, "analyse", return_value=profile.Facts(**{**FACTS, "bandwidth_hz": 1.0})), \
             mock.patch.object(profile, "read_title", return_value="Local"):
            track, facts, title = profile._worker_analyse(str(self.track))
        self.assertEqual((facts["bandwidth_hz"], title), (1.0, "Local"))

    def test_bad_requests(self):
        for path, code in (("/nope", 404),):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(self.url + path)
            self.assertEqual(cm.exception.code, code)
        req = urllib.request.Request(self.url + "/analyse", data=b"", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 413)


if __name__ == "__main__":
    unittest.main()
