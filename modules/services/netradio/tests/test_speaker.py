import json
import logging
import sys
import tempfile
import time
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from netradio import ambient, speaker

logging.disable(logging.CRITICAL)


class _FakeAlsa(speaker.AlsaOutput):
    """The REAL AlsaOutput with only the amixer fork replaced. The little
    model of the card lives on the FakeMixer that owns it, so the tests go on
    poking `m.level` and `m.calls` exactly as they always have."""

    def __init__(self, owner: "FakeMixer"):
        self.owner = owner
        super().__init__("0", control="Master")

    def _run(self, *args: str) -> str:
        return self.owner._run(*args)


class FakeMixer(speaker.Mixer):
    """The REAL Mixer with only amixer replaced — every method under test is
    the shipped one.

    It used to override state/set/mute with its own little model, and the
    interface drifted: `choose()` and `target` were added to Mixer, the
    handler started calling them, and this fixture kept passing while /state
    returned a 500 on the live box. Stub the process boundary, nothing above
    it (see the `test-the-real-wiring` note). That boundary is now
    AlsaOutput._run rather than Mixer._run, which is the only thing the
    output split changed here."""

    def __init__(self, level: int = 35):
        self.calls: list[tuple[str, ...]] = []
        self.level, self.muted = level, False
        self.STEP_MS = 1                          # a step per ms, so a ramp is
        self.fade_in_ms, self.fade_out_ms = 30, 12   # 30 and 12 steps, ~42 ms total
        super().__init__(output=_FakeAlsa(self))

    def _run(self, *args: str) -> str:
        self.calls.append(args)
        if args[0] == "scontrols":
            return "Simple mixer control 'Master',0\n"
        if args[0] == "sget":
            return f"  Front Left: Playback 87 [{self.level}%] [{'off' if self.muted else 'on'}]\n"
        if args[0] == "sset":
            for a in args[2:]:
                if a.endswith("%"):
                    self.level = max(0, min(100, int(a[:-1])))
                elif a == "mute":
                    self.muted = True
                elif a == "unmute":
                    self.muted = False
        return ""

    def levels(self) -> list[int]:
        """The sequence of levels written, for checking a fade happened."""
        out = []
        for a in self.calls:
            if a[0] == "sset":
                out += [int(x[:-1]) for x in a[2:] if x.endswith("%")]
        return out

    def switches(self) -> list[str]:
        return [x for a in self.calls if a[0] == "sset" for x in a[2:] if x in ("mute", "unmute")]


class MixerParsing(unittest.TestCase):
    def test_picks_master_and_reads_level(self):
        # Parsing amixer's output is the AlsaOutput's job now, so this tests
        # it where it lives rather than through the Mixer.
        out = speaker.AlsaOutput("0")
        with mock.patch.object(speaker.AlsaOutput, "_run", return_value="Simple mixer control 'PCM',0\nSimple mixer control 'Master',0\n"):
            self.assertEqual(out._find(), "Master")        # Master wins over PCM
        out._control = "Master"
        with mock.patch.object(speaker.AlsaOutput, "_run", return_value="  Front Left: Playback 87 [68%] [-12.00dB] [on]\n"):
            self.assertEqual(out.read_state(), (68, False))
        with mock.patch.object(speaker.AlsaOutput, "_run", return_value="  Front Left: Playback 0 [0%] [off]\n"):
            self.assertEqual(out.read_state(), (0, True))

    def test_a_missing_amixer_is_not_fatal(self):
        m = speaker.Mixer("99", amixer="/nonexistent/amixer")
        self.assertEqual(m.control, "")                    # no control found, no exception
        self.assertEqual(m.state(), (None, False))
        m.set(50); m.mute(True)                            # and these do nothing rather than raise


class VolumeCeilingAndWatchdog(unittest.TestCase):
    """The level has twice been found at 100% with nothing in the journal that
    put it there — 2026-09-26, and again around 03:14 on 2026-10-01, which woke
    The owner out of a dead sleep. Neither cause was ever found: no restart, no
    deploy, no API call, no card reset, nothing else on the box driving amixer.

    So the guard cannot depend on knowing the writer. It notices that the mixer
    no longer reads what this service set, says so, and puts it back.
    """

    def test_the_check_interval_bounds_how_long_the_room_is_loud(self):
        """This number is not a sampling rate. The watchdog cannot prevent the
        rise, only end it, so the interval IS the worst case a sleeping person
        is exposed to. The third occurrence (10:47 p.m., 2026-10-22) was caught
        and corrected in 120 ms — after sounding for up to ten seconds, which
        at that hour was the entire harm."""
        self.assertLessEqual(speaker.Mixer.WATCH_S, 3.0,
                             "a rise can sound for this many seconds before anything stops it")

    def test_an_unexplained_rise_is_pulled_back_and_logged(self):
        m = FakeMixer(level=70)
        m.target = 70
        m.level = 100                      # something outside the service, as on both nights
        logging.disable(logging.NOTSET)
        try:
            with self.assertLogs("netradio.speaker", level="ERROR") as cm:
                self.assertTrue(m.reconcile())
        finally:
            logging.disable(logging.CRITICAL)
        self.assertEqual(m.level, 70, "the level was not brought back down")
        self.assertTrue(any("100" in l and "70" in l for l in cm.output),
                        f"the log does not say what it found and what it expected: {cm.output}")

    def test_a_level_someone_lowered_by_hand_is_left_alone(self):
        """Only the rise wakes people up. Turning the card down by hand is
        nobody's emergency, and fighting it would be rude."""
        m = FakeMixer(level=70)
        m.target = 70
        m.level = 40
        self.assertFalse(m.reconcile())
        self.assertEqual(m.level, 40)

    def test_a_level_within_tolerance_is_not_fought(self):
        m = FakeMixer(level=70)
        m.target = 70
        m.level = 70 + m.DRIFT
        self.assertFalse(m.reconcile())

    def test_while_muted_anything_audible_is_a_rise(self):
        m = FakeMixer(level=0)
        m.target, m._muted = 70, True
        m.level = 55
        self.assertTrue(m.reconcile())
        self.assertEqual(m.level, 0, "a muted speaker was left making sound")

    def test_the_ceiling_clamps_every_write(self):
        """`maxVolume` is the ceiling that was added after the first incident
        and never bit, because its default is 100. It has to hold against a
        direct set, not only against the UI."""
        m = FakeMixer(level=30)
        m.cap = 70
        m.set(100, "test")
        self.assertEqual(m.level, 70)
        m.choose(95)
        self.assertLessEqual(max(m.levels()), 70, "a ramp climbed past the ceiling")


class Api(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # a stand-in player that just stays alive: no shell, no PATH lookup —
        # the build sandbox has neither (2026-09-23, this test broke the deploy)
        fake = Path(self.tmp.name) / "player"
        fake.write_text(f"#!{sys.executable}\nimport time; time.sleep(30)\n")
        fake.chmod(0o755)
        self.player = speaker.Player("http://127.0.0.1:8020", str(fake))
        self.mixer = FakeMixer()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), speaker.make_handler(self.player, self.mixer))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.player.stop(); self.srv.shutdown(); self.tmp.cleanup()

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.url + path, method=method,
                                     data=json.dumps(body or {}).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_play_stop_volume_and_a_bad_mount(self):
        code, st = self.call("GET", "/state")
        self.assertEqual((code, st["playing"], st["volume"]), (200, False, 35))
        code, st = self.call("POST", "/play", {"mount": "rain"})
        self.assertEqual((code, st["playing"], st["mount"]), (200, True, "rain"))
        code, st = self.call("POST", "/play", {"mount": "bluegrass"})   # switching kills the old player
        self.assertEqual(st["mount"], "bluegrass")
        self.assertEqual(self.call("POST", "/volume", {"level": 60})[1]["volume"], 60)
        self.assertEqual(self.call("POST", "/volume", {"step": -10})[1]["volume"], 50)
        self.assertTrue(self.call("POST", "/mute", {"on": True})[1]["muted"])
        for bad in ("../evil", "rain; rm -rf /", "", "Rain"):
            code, body = self.call("POST", "/play", {"mount": bad})
            self.assertEqual(code, 400, bad)
        self.assertEqual(self.call("POST", "/stop")[1]["playing"], False)

    def test_a_dead_player_is_restarted_while_a_mount_is_selected(self):
        self.player.play("rain")
        self.player.proc.kill(); self.player.proc.wait()
        self.assertFalse(self.player.alive())
        stop = threading.Event()
        t = threading.Thread(target=self.player.watch, args=(stop, 0.2), daemon=True)
        t.start()
        for _ in range(40):
            if self.player.alive(): break
            threading.Event().wait(0.2)
        stop.set()
        self.assertTrue(self.player.alive())
        self.assertEqual(self.player.mount, "rain")


class Ambient(unittest.TestCase):
    def test_playlist_lists_whatever_is_in_the_directory(self):
        with tempfile.TemporaryDirectory() as d:
            root, pl = Path(d) / "ambient", Path(d) / "rain.m3u"
            (root / "rain").mkdir(parents=True)
            for name in ("a.flac", "b.mp3", "notes.txt", "cover.jpg"):
                (root / "rain" / name).write_bytes(b"x")
            with mock.patch.object(ambient, "fetch", return_value=True) as f:
                n = ambient.build("rain", root, pl)
            self.assertTrue(f.called)                       # the fetch was attempted
            self.assertEqual(n, 2)                          # only the audio is listed
            lines = [l for l in pl.read_text().splitlines() if not l.startswith("#")]
            self.assertTrue(all(l.endswith((".flac", ".mp3")) for l in lines))
            self.assertIn("aporee", (root / "rain" / "LICENCES.txt").read_text())   # provenance is written beside the audio


def _fake_fetch(bed, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)   # the real fetch makes src/
    dest.write_bytes(b"x")
    return True


class BedCheck(unittest.TestCase):
    """The guard that keeps a slated sound-effects cut off the station."""

    def run_check(self, stderr):
        with mock.patch.object(ambient.subprocess, "run",
                               return_value=mock.Mock(stderr=stderr, stdout="")):
            return ambient.slate_or_gap(Path("/x/bed.flac"))

    def test_a_slate_is_rejected(self):
        # the real shape of GOLD TAPE G46-04: announcer to 1.5 s, gap, then rain
        why = self.run_check("lavfi.silence_start=1.506\nlavfi.silence_end=2.865\n")
        self.assertIn("slated", why)

    def test_a_long_internal_silence_is_rejected(self):
        why = self.run_check("lavfi.silence_start=6.0\nlavfi.silence_end=11.9\n")
        self.assertIn("silent", why)

    def test_continuous_rain_passes(self):
        self.assertEqual(self.run_check(""), "")
        self.assertEqual(self.run_check("lavfi.silence_start=25.0\nlavfi.silence_end=25.4\n"), "")

    def test_the_check_failing_does_not_drop_the_file(self):
        with mock.patch.object(ambient.subprocess, "run", side_effect=OSError("no ffmpeg")):
            self.assertEqual(ambient.slate_or_gap(Path("/x/bed.flac")), "")

    def test_a_bed_no_longer_wanted_is_removed_but_a_dropped_in_file_is_kept(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "ambient"; (root / "rain").mkdir(parents=True)
            (root / "rain" / "old.mp3").write_bytes(b"x")       # fetched last time
            (root / "rain" / "mine.mp3").write_bytes(b"x")      # the owner's own
            (root / "rain" / ".fetched.json").write_text('["old.mp3"]')
            with mock.patch.object(ambient, "SETS", {"rain": [ambient.Bed("http://x/new.mp3", "new.mp3", "CC0")]}), \
                 mock.patch.object(ambient, "fetch", side_effect=lambda bed, dest: dest.write_bytes(b"x") or True), \
                 mock.patch.object(ambient, "slate_or_gap", return_value=""):
                ambient.build("rain", root, Path(d) / "rain.m3u")
            self.assertFalse((root / "rain" / "old.mp3").exists())
            self.assertTrue((root / "rain" / "mine.mp3").exists())
            self.assertTrue((root / "rain" / "new.mp3").exists())

    def test_a_file_that_merely_begins_quiet_is_not_a_slate(self):
        # the beach recording: silence from 0.0 — nothing before it to be a slate
        self.assertEqual(self.run_check("lavfi.silence_start=0\nlavfi.silence_end=1.653\n"), "")

    def test_a_retired_bed_is_removed_even_with_no_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "ambient"; (root / "rain").mkdir(parents=True)
            (root / "rain" / "G46-04-Distant Storm.flac").write_bytes(b"x")   # shipped by an older version
            (root / "rain" / "mine.mp3").write_bytes(b"x")
            with mock.patch.object(ambient, "SETS", {"rain": [ambient.Bed("http://x/new.mp3", "new.mp3", "CC0")]}), \
                 mock.patch.object(ambient, "fetch", side_effect=lambda bed, dest: dest.write_bytes(b"x") or True), \
                 mock.patch.object(ambient, "slate_or_gap", return_value=""):
                ambient.build("rain", root, Path(d) / "rain.m3u")
            self.assertFalse((root / "rain" / "G46-04-Distant Storm.flac").exists())
            self.assertTrue((root / "rain" / "mine.mp3").exists())

    def test_a_looped_bed_plays_the_loop_not_the_raw_download(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "ambient"
            bed = ambient.Bed("http://x/src.m4a", "src.m4a", "personal copy",
                              loop=ambient.Loop(head=1.5, tail=35.0, xfade=12.0))
            self.assertEqual(ambient.played_name(bed), "src-loop.flac")
            with mock.patch.object(ambient, "SETS", {"rainymood": [bed]}), \
                 mock.patch.object(ambient, "fetch", side_effect=_fake_fetch), \
                 mock.patch.object(ambient, "make_loop",
                                   side_effect=lambda src, dest, spec: dest.write_bytes(b"x") or True) as ml, \
                 mock.patch.object(ambient, "slate_or_gap", return_value=""):
                n = ambient.build("rainymood", root, Path(d) / "rainymood.m3u")
            self.assertEqual(n, 1)
            # the raw download is parked in src/ so it can never reach the playlist
            self.assertTrue((root / "rainymood" / "src" / "src.m4a").exists())
            self.assertEqual(ml.call_args[0][2].xfade, 12.0)
            played = [l for l in (Path(d) / "rainymood.m3u").read_text().splitlines() if not l.startswith("#")]
            self.assertEqual([Path(x).name for x in played], ["src-loop.flac"])

    def test_a_recording_too_short_for_its_trims_is_not_looped(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "short.m4a"; src.write_bytes(b"x")
            with mock.patch.object(ambient, "duration", return_value=20.0):
                self.assertFalse(ambient.make_loop(src, Path(d) / "out.flac", ambient.Loop(1.5, 35.0, 12.0)))
            self.assertFalse((Path(d) / "out.flac").exists())

    def test_the_fetch_identifies_itself(self):
        # Python's default agent is 403'd by some CDNs; that emptied the
        # Rainy Mood playlist on its first deploy (2026-09-24)
        seen = []

        class FakeResp:
            headers = {"Content-Length": "3"}
            status = 200
            def __init__(self): self._left = [b"abc"]
            def read(self, *a): return self._left.pop() if self._left else b""
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=None):
            seen.append(req.get_header("User-agent"))
            return FakeResp()

        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(ambient.urllib.request, "urlopen", side_effect=fake_urlopen):
            ambient.fetch(ambient.Bed("http://x/a.mp3", "a.mp3", "CC0"), Path(d) / "a.mp3")
        self.assertTrue(seen and all(u and "urllib" not in u.lower() for u in seen), seen)

    def test_a_server_refusing_HEAD_is_still_downloaded(self):
        calls = []

        class FakeResp:
            headers = {}
            def __init__(self): self._left = [b"abc"]
            def read(self, *a): return self._left.pop() if self._left else b""
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=None):
            calls.append(req.get_method())
            if req.get_method() == "HEAD":
                raise OSError("405")
            return FakeResp()

        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(ambient.urllib.request, "urlopen", side_effect=fake_urlopen):
            ok = ambient.fetch(ambient.Bed("http://x/a.mp3", "a.mp3", "CC0"), Path(d) / "a.mp3")
        self.assertTrue(ok)
        self.assertIn("GET", calls)

    def test_the_encoder_is_woken_before_connecting(self):
        # on-demand encoders: without this the mount 404s and the service
        # restart-loops, which is what it did on the server (2026-09-25)
        seen = []

        class R:
            def read(self, *a): return b""
            def __enter__(self): return self
            def __exit__(self, *a): return False

        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "player"; fake.write_text(f"#!{sys.executable}\nimport time; time.sleep(5)\n"); fake.chmod(0o755)
            pl = speaker.Player("http://127.0.0.1:8020", str(fake), wake="http://127.0.0.1:8011")
            with mock.patch.object(speaker.urllib.request, "urlopen",
                                   side_effect=lambda req, timeout=None: seen.append(
                                       (req.full_url, req.get_header("X-original-uri"))) or R()):
                pl.play("rain")
            pl.stop()
        self.assertEqual(seen, [("http://127.0.0.1:8011/wake", "/radio/rain.mp3")])

    def test_a_failed_wake_still_tries_the_mount(self):
        """wake_wait=0: give up immediately. The point of this test is the
        fallback, not the waiting — see the next one for that."""
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "player"; fake.write_text(f"#!{sys.executable}\nimport time; time.sleep(5)\n"); fake.chmod(0o755)
            pl = speaker.Player("http://127.0.0.1:8020", str(fake), wake="http://127.0.0.1:8011")
            with mock.patch.object(speaker.urllib.request, "urlopen", side_effect=OSError("down")):
                pl.play("rain", wake_wait=0)
            alive = pl.alive(); pl.stop()
        self.assertTrue(alive)

    def test_the_wake_is_retried_while_liquidsoap_is_still_coming_up(self):
        """After a deploy this service and Liquidsoap start in the same second,
        and the wake answers 503 until Liquidsoap's control socket exists. One
        attempt then gave up and ffmpeg was pointed at a mount Icecast did not
        have yet — about fifteen seconds of silence on every deploy, in a 404
        restart loop (2026-09-29)."""
        pl = speaker.Player("http://127.0.0.1:8020", "/bin/true", wake="http://127.0.0.1:8011")
        calls = []

        def flaky(req, timeout=None):
            calls.append(1)
            if len(calls) < 3:
                raise OSError("503 wake failed: no such file or directory")
            return mock.MagicMock(read=lambda: b"")

        with mock.patch.object(speaker.urllib.request, "urlopen", side_effect=flaky), \
             mock.patch.object(speaker.time, "sleep"):          # no real waiting in a test
            self.assertTrue(pl.wake("rain", wait=30))
        self.assertEqual(len(calls), 3, "the wake gave up instead of waiting for Liquidsoap")

    def test_the_wake_gives_up_eventually_rather_than_blocking_for_ever(self):
        pl = speaker.Player("http://127.0.0.1:8020", "/bin/true", wake="http://127.0.0.1:8011")
        with mock.patch.object(speaker.urllib.request, "urlopen", side_effect=OSError("down")), \
             mock.patch.object(speaker.time, "sleep"):
            self.assertFalse(pl.wake("rain", wait=4))

    def test_the_device_is_an_argument_not_an_environment_variable(self):
        # The previous version of this test asserted `pl.device == "plughw:0,0"`
        # and passed all the while the player was silent: ffplay took its device
        # from AUDIODEV, and the sdl2-compat/SDL3 stack ignores AUDIODEV. The
        # attribute was right; nothing carried it to the process. So assert the
        # argv — the thing the kernel actually sees.
        argv = speaker.Player("http://x", "/bin/ffmpeg").command("rain")
        self.assertIn("-f", argv)
        self.assertEqual(argv[argv.index("-f") + 1], "alsa")
        self.assertEqual(argv[-1], "plughw:0,0", "the device must be the output argument")
        self.assertNotIn("default", argv, "`default` is PipeWire's, and PipeWire is per-user")
        self.assertEqual(argv[argv.index("-i") + 1], "http://x/rain.mp3")

    def test_an_explicit_device_reaches_the_argv(self):
        argv = speaker.Player("http://x", "/bin/ffmpeg", device="plughw:1,0").command("rain")
        self.assertEqual(argv[-1], "plughw:1,0")

    def test_setting_a_level_does_not_unmute(self):
        m = FakeMixer(); m.muted = True
        m.set(50)
        self.assertTrue(m.muted, "changing the volume must not bring the sound back")

    def test_mute_NEVER_writes_the_codec_switch(self):
        """The click the owner heard is the pin widget's mute bit stepping the
        analog stage's DC, not an envelope step: a 120 ms fade left it
        completely unchanged (2026-09-26). A transient that does not care about
        the volume is not in the signal, so the switch is simply never used —
        `Master` bottoms out at -64 dB, which is inaudible.
        """
        m = FakeMixer(35)
        m.calls.clear()
        m.mute(True)
        self.assertEqual(m.switches(), [], "the codec's mute must never be written")
        levels = m.levels()
        self.assertGreater(len(levels), 4, "expected a ramp, not a single write")
        self.assertEqual(levels[-1], 0)
        self.assertTrue(all(a >= b for a, b in zip(levels, levels[1:])), f"monotonic down: {levels}")
        self.assertTrue(m.state()[1], "and it must still REPORT muted")

    def test_unmute_ramps_back_up_with_no_switch_either(self):
        m = FakeMixer(35)
        m.mute(True)
        self.assertEqual(m.level, 0, "muted means the level really is at the bottom")
        m.calls.clear()
        m.mute(False)
        levels = m.levels()
        self.assertEqual(m.switches(), [])
        # _ramp writes the first STEP, not the starting value it already sits at
        self.assertLess(levels[0], 10, f"climbs from the bottom: {levels}")
        self.assertEqual(levels[-1], 35, "returns to the level it was muted at")
        self.assertTrue(all(a <= b for a, b in zip(levels, levels[1:])), f"monotonic up: {levels}")
        self.assertFalse(m.state()[1])

    def test_the_startup_unmute_happens_exactly_once(self):
        """The switch is never touched again, so a stale `[off]` in alsa-state
        would make the service permanently silent while it ramped a volume
        nobody could hear. Clear it once, at construction."""
        m = FakeMixer(35)
        self.assertEqual(m.switches(), ["unmute"], "one unmute, at startup")
        m.calls.clear()
        m.mute(True); m.mute(False); m.choose(50)
        self.assertEqual(m.switches(), [], "and never again")

    def test_a_card_muted_by_hand_still_reads_as_muted(self):
        m = FakeMixer(35)
        m.muted = True                      # somebody ran amixer themselves
        self.assertTrue(m.state()[1])

    def test_the_level_chosen_while_muted_is_where_unmute_returns(self):
        m = FakeMixer(35)
        m.mute(True)
        m.choose(60)
        self.assertTrue(m.state()[1], "choosing a level must not unmute")
        self.assertEqual(m.level, 0, "and must not raise the output either")
        m.mute(False)
        self.assertEqual(m.level, 60)

    def test_a_volume_change_slides_rather_than_jumps(self):
        m = FakeMixer(20); m.calls.clear()
        m.choose(80)
        levels = m.levels()
        self.assertGreater(len(levels), 4, f"expected a slide: {levels}")
        self.assertEqual(levels[-1], 80)

    def test_the_mixer_set_command_carries_no_unmute(self):
        seen = []
        m = speaker.Mixer.__new__(speaker.Mixer)
        m.out, m.control = speaker.AlsaOutput("0", control="Master"), "Master"
        with mock.patch.object(speaker.AlsaOutput, "_run", side_effect=lambda *a: seen.append(a) or ""):
            m.set(40)
        self.assertNotIn("unmute", seen[0])


class Shutdown(unittest.TestCase):
    """SIGTERM must end the process promptly.

    It did not: the handler called `srv.shutdown()` while the main thread was
    inside `serve_forever()`, and since Python runs handlers on the main
    thread, shutdown() waited for a loop that could not run. systemd took the
    full 90 s TimeoutStop and SIGKILLed — 90 s of silence on every deploy, and
    the window the owner hit on 2026-09-26 when the station "wouldn't play".

    This runs the real CLI in a subprocess, because the bug only exists in the
    interaction between a real signal, a real main thread and a real server.
    """

    def test_sigterm_exits_promptly(self):
        import os
        import signal as sig
        import socket
        import subprocess
        import time as t

        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "ffmpeg"
            fake.write_text(f"#!{sys.executable}\nimport time\nwhile True: time.sleep(1)\n")
            fake.chmod(0o755)
            with socket.socket() as s:                     # a port nobody is on
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
            p = subprocess.Popen(
                [sys.executable, "-m", "netradio.cli", "speaker",
                 "--listen", "127.0.0.1", "--port", str(port),
                 "--card", "99",                            # no such card: the mixer no-ops
                 "--icecast", "http://127.0.0.1:1",
                 "--default-mount", "rain",
                 "--ffmpeg", str(fake)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
                # Its own process group. The service spawns the fake ffmpeg as a
                # CHILD, and killing only the parent orphaned that child to loop
                # for ever — one was found still running two and a half days
                # after a test run (2026-09-29). The group is cleaned up below.
                start_new_session=True)
            try:
                # wait for it to be listening, so we are not racing startup
                for _ in range(100):
                    with socket.socket() as c:
                        if c.connect_ex(("127.0.0.1", port)) == 0:
                            break
                    t.sleep(0.1)
                else:
                    self.skipTest("the service never came up; nothing to prove about its exit")

                began = t.monotonic()
                p.send_signal(sig.SIGTERM)
                try:
                    p.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    self.fail("SIGTERM did not end the process within 15 s — systemd would "
                              "wait out TimeoutStop and SIGKILL it, silencing the station")
                self.assertLess(t.monotonic() - began, 15)
            finally:
                if p.poll() is None:
                    p.kill()
                    p.wait(timeout=10)
                # and anything it spawned: the player is a child of the service,
                # not of this test, so it outlives p unless the group is signalled
                try:
                    os.killpg(os.getpgid(p.pid), sig.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass


class VolumeCeiling(unittest.TestCase):
    """Nothing may drive the card past the configured ceiling.

    The owner found the live level at 100% on 2026-09-26 with no way to attribute
    it — the service logged no level changes at all. Both halves are fixed: a
    cap that cannot be exceeded, and an INFO line for every change.
    """

    def capped(self, cap, level=35):
        m = FakeMixer.__new__(FakeMixer)
        m.calls, m.level, m.muted = [], level, False
        m.STEP_MS, m.fade_in_ms, m.fade_out_ms = 1, 30, 12
        speaker.Mixer.__init__(m, cap=cap, output=_FakeAlsa(m))
        return m

    def test_an_explicit_level_is_clamped(self):
        m = self.capped(80)
        m.choose(100)
        self.assertEqual(m.level, 80)
        self.assertEqual(m.target, 80, "and the remembered level is the capped one")

    def test_stepping_cannot_climb_past_it(self):
        m = self.capped(60, level=55)
        for _ in range(10):
            m.step(5)
        self.assertEqual(m.level, 60)

    def test_a_raw_set_is_capped_too(self):
        m = self.capped(50)
        m.set(99)
        self.assertEqual(m.level, 50, "the ramp uses set(), so the ceiling has to hold there")

    def test_unmuting_cannot_exceed_it(self):
        m = self.capped(70, level=70)
        m.mute(True)
        m.target = 100          # however it got there
        m.mute(False)
        self.assertLessEqual(m.level, 70)

    def test_the_default_ceiling_changes_nothing(self):
        m = self.capped(100)
        m.choose(100)
        self.assertEqual(m.level, 100)


class FadeShape(unittest.TestCase):
    """The owner asked for a longer fade IN than out (2026-09-27): coming back
    should be gentle, going away and ordinary volume changes should feel
    immediate."""

    def test_the_fade_in_is_longer_than_the_fade_out(self):
        self.assertGreater(speaker.Mixer.fade_in_ms, speaker.Mixer.fade_out_ms)
        self.assertEqual(speaker.Mixer.fade_in_ms, 300)
        self.assertEqual(speaker.Mixer.fade_out_ms, 120)

    def test_unmuting_takes_more_steps_than_muting(self):
        m = FakeMixer(35)
        m.calls.clear(); m.mute(True)
        out = len(m.levels())
        m.calls.clear(); m.mute(False)
        back = len(m.levels())
        self.assertGreater(back, out, f"fade in {back} steps vs out {out}")

    def test_the_ramp_honours_the_clock_not_a_flat_sleep(self):
        """Every write forks amixer, so sleeping a flat interval per step made
        the fade reliably longer than asked. Pace against monotonic time."""
        import time as t
        m = FakeMixer(35)
        m.STEP_MS, m.fade_out_ms = 4, 120
        began = t.monotonic()
        m._ramp(35, 0, 120)
        took = (t.monotonic() - began) * 1000
        self.assertGreater(took, 90, f"should not finish early: {took:.0f} ms")
        self.assertLess(took, 400, f"should not overshoot badly: {took:.0f} ms")

    def test_a_zero_duration_still_reaches_the_target(self):
        m = FakeMixer(35)
        m.calls.clear()
        m._ramp(35, 60, 0)
        self.assertEqual(m.level, 60, "no time to fade is not permission to skip the change")


class MuteReleasesTheMount(unittest.TestCase):
    """A muted speaker must not hold the on-demand encoder open.

    The encoders only run while something is listening (see Player.wake). This
    service was that listener around the clock, so a box sitting muted still
    paid for a continuous MP3 encode — 9.7% of one core measured on the server on
    2026-10-28, feeding a jack ramped to -64 dB.

    Real Player, real handler, real HTTP; only the ffmpeg binary is a stub, so
    the wiring under test is the shipped wiring.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        fake = Path(self.tmp.name) / "player"
        fake.write_text(f"#!{sys.executable}\nimport time; time.sleep(30)\n")
        fake.chmod(0o755)
        self.fake = str(fake)
        self.mixer = FakeMixer()

    def tearDown(self):
        self.srv.shutdown()
        self.player.stop()
        self.tmp.cleanup()

    def serve(self, disconnect=True):
        self.player = speaker.Player("http://127.0.0.1:8020", self.fake)
        self.srv = ThreadingHTTPServer(
            ("127.0.0.1", 0), speaker.make_handler(self.player, self.mixer, disconnect))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.url + path, method=method,
                                     data=json.dumps(body or {}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.load(r)

    def test_mute_drops_the_connection_and_unmute_brings_it_back(self):
        self.serve()
        self.call("POST", "/play", {"mount": "rainymood"})
        self.assertTrue(self.player.alive(), "precondition: playing")

        self.call("POST", "/mute", {"on": True})
        self.assertFalse(self.player.alive(), "mute must release the mount")
        self.assertTrue(self.player.suspended)
        self.assertEqual(self.player.mount, "rainymood",
                         "the station stays SELECTED — only the connection goes")

        self.call("POST", "/mute", {"on": False})
        self.assertTrue(self.player.alive(), "unmute must reconnect")
        self.assertFalse(self.player.suspended)
        self.assertEqual(self.player.mount, "rainymood")

    def test_the_watchdog_does_not_undo_a_deliberate_suspend(self):
        """watch() restarts a player that died. A suspend is not a death — and
        without the `suspended` guard it would reconnect within five seconds
        of every mute, so the encoder would never actually stop."""
        self.serve()
        self.call("POST", "/play", {"mount": "rainymood"})
        self.call("POST", "/mute", {"on": True})

        stop = threading.Event()
        t = threading.Thread(target=self.player.watch, args=(stop, 0.01), daemon=True)
        t.start()
        time.sleep(0.2)          # ~20 watchdog passes
        stop.set(); t.join(timeout=2)

        self.assertFalse(self.player.alive(), "the watchdog reconnected a muted speaker")
        self.assertTrue(self.player.suspended)

    def test_hold_stream_while_muted_keeps_the_old_behaviour(self):
        self.serve(disconnect=False)
        self.call("POST", "/play", {"mount": "rainymood"})
        self.call("POST", "/mute", {"on": True})
        self.assertTrue(self.player.alive(),
                        "with the opt-out set, a mute must not touch the stream")
        self.assertFalse(self.player.suspended)

    def test_selecting_a_station_while_suspended_records_it_without_connecting(self):
        """--start-muted suspends before the default mount is chosen, so a box
        that boots silent never wakes an encoder at all."""
        self.serve()
        self.player.suspend()
        self.player.play("rainymood")
        self.assertFalse(self.player.alive(), "a suspended player must not dial out")
        self.assertEqual(self.player.mount, "rainymood", "but it must remember the choice")

        self.player.resume()
        self.assertTrue(self.player.alive(), "resume connects to what was recorded")


# A sink-input as `pactl -f json list sink-inputs` reports one.
#
# The key names here were checked against the pactl binary itself (17.0):
# `value_percent`, `properties`, `application.name`, `index`, `mute` and
# `volume` all appear in it, and `-f json` plus `set-sink-input-volume #N` are
# both in its --help. What is NOT from a live server is the nesting, so the
# parser deliberately iterates the volume map's VALUES rather than looking up a
# channel name — channel keys come from libpulse's channel map, not from pactl,
# and they vary with the sink.
PACTL_SINK_INPUTS = json.dumps([
    {
        "index": 7,
        "sink": 1,
        "mute": False,
        "volume": {
            "front-left": {"value": 65536, "value_percent": "100%", "db": "0.00dB"},
            "front-right": {"value": 65536, "value_percent": "100%", "db": "0.00dB"},
        },
        "properties": {"application.name": "firefox", "media.name": "a video"},
    },
    {
        "index": 42,
        "sink": 1,
        "mute": False,
        "volume": {
            "front-left": {"value": 26214, "value_percent": "40%", "db": "-18.06dB"},
            "front-right": {"value": 26214, "value_percent": "40%", "db": "-18.06dB"},
        },
        "properties": {"application.name": "netradio", "media.name": "netradio"},
    },
])


def _stay_alive_player(case) -> str:
    """A stand-in player that just stays alive: no shell, no PATH lookup — the
    build sandbox has neither (2026-09-23, that broke the deploy once)."""
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    fake = Path(tmp.name) / "player"
    fake.write_text(f"#!{sys.executable}\nimport time; time.sleep(30)\n")
    fake.chmod(0o755)
    return str(fake)


class PulseOutputTests(unittest.TestCase):
    """The output for a machine with a desktop session.

    The property worth protecting here is that the level is OUR STREAM's and
    never the sink's: on a box where a game is playing through the same sink,
    setting the sink would turn the game down too.
    """

    def out(self, **kw):
        return speaker.PulseOutput(pactl="pactl", **kw)

    def test_ffmpeg_is_told_pulse_and_the_sink(self):
        args = self.out(sink="alsa_output.pci-0000_0a_00.4.analog-stereo").output_args()
        self.assertIn("-f", args)
        self.assertEqual(args[args.index("-f") + 1], "pulse")
        self.assertIn("-device", args)
        self.assertEqual(args[args.index("-device") + 1],
                         "alsa_output.pci-0000_0a_00.4.analog-stereo")

    def test_no_sink_means_the_sessions_default_and_no_device_flag(self):
        self.assertNotIn("-device", self.out().output_args())

    def test_the_level_is_read_from_our_own_stream_not_someone_elses(self):
        o = self.out()
        with mock.patch.object(speaker.PulseOutput, "_run", return_value=PACTL_SINK_INPUTS):
            self.assertEqual(o.read_state(), (40, False),
                             "read firefox's 100% instead of netradio's 40%")

    def test_the_level_is_written_to_the_sink_input_never_the_sink(self):
        o = self.out()
        seen = []
        with mock.patch.object(speaker.PulseOutput, "_run",
                               side_effect=lambda *a: seen.append(a) or PACTL_SINK_INPUTS):
            o.write_level(25)
        wrote = [a for a in seen if a and a[0].startswith("set-")]
        self.assertEqual(wrote, [("set-sink-input-volume", "42", "25%")],
                         "the level must land on our stream index, not on the sink")

    def test_nothing_playing_yet_reads_as_no_level_rather_than_raising(self):
        o = self.out()
        with mock.patch.object(speaker.PulseOutput, "_run", return_value="[]"):
            self.assertEqual(o.read_state(), (None, False))
            o.write_level(50)          # and this is a no-op, not an exception

    def test_pactl_that_is_not_json_does_not_take_the_player_down(self):
        o = self.out()
        with mock.patch.object(speaker.PulseOutput, "_run", return_value="not json at all"):
            self.assertEqual(o.read_state(), (None, False))

    def test_a_pulse_output_can_mix_a_second_stream_and_alsa_cannot(self):
        # This is what decides whether the rain bed is offered on a device.
        self.assertTrue(speaker.PulseOutput().mixes)
        self.assertFalse(speaker.AlsaOutput().mixes)


class PlayerUsesTheOutput(unittest.TestCase):
    def test_the_command_ends_in_the_outputs_arguments(self):
        pl = speaker.Player("http://127.0.0.1:8020", "/bin/true",
                            output=speaker.PulseOutput(sink="sinkname"))
        cmd = pl.command("rain")
        self.assertNotIn("alsa", cmd)
        self.assertIn("pulse", cmd)
        self.assertIn("sinkname", cmd)

    def test_alsa_is_still_the_default_for_a_player_built_the_old_way(self):
        pl = speaker.Player("http://127.0.0.1:8020", "/bin/true", "plughw:1,0")
        cmd = pl.command("rain")
        self.assertEqual(cmd[-3:], ["-f", "alsa", "plughw:1,0"])

    def test_the_level_is_reapplied_after_every_connect(self):
        """A new sink-input starts at full, so without this the first moment
        of each (re)connect is at 100% whatever level was chosen."""
        called = []
        pl = speaker.Player("http://127.0.0.1:8020", _stay_alive_player(self),
                            on_start=lambda: called.append(1))
        pl.play("rain")
        pl.stop()
        self.assertEqual(called, [1], "the level was not re-applied after the player started")

    def test_a_failing_reapply_does_not_stop_the_music(self):
        def boom():
            raise RuntimeError("no mixer")
        pl = speaker.Player("http://127.0.0.1:8020", _stay_alive_player(self), on_start=boom)
        pl.play("rain")                      # must not raise
        self.assertEqual(pl.mount, "rain")
        pl.stop()


class LoopbackDetection(unittest.TestCase):
    def test_what_counts_as_loopback(self):
        for host in ("127.0.0.1", "127.0.0.53", "::1", "localhost"):
            self.assertTrue(speaker.is_loopback(host), host)

    def test_a_wildcard_bind_is_not_loopback(self):
        # The case the fail-closed check exists for: these are reachable from
        # the network, and an empty string is the easiest one to get wrong.
        for host in ("", "0.0.0.0", "::", "192.168.1.37", "100.66.171.120"):
            self.assertFalse(speaker.is_loopback(host), host)


class TokenAuth(unittest.TestCase):
    """A speaker on a second machine is reachable by anything on the network,
    so the token is the only thing between the house speakers and the LAN."""

    def setUp(self):
        self.player = speaker.Player("http://127.0.0.1:8020", _stay_alive_player(self))
        self.mixer = FakeMixer()

    def serve(self, token):
        srv = ThreadingHTTPServer(("127.0.0.1", 0),
                                  speaker.make_handler(self.player, self.mixer, True, token))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    def get(self, base, token=None):
        req = urllib.request.Request(base + "/state")
        if token is not None:
            req.add_header("Authorization", "Bearer " + token)
        return urllib.request.urlopen(req, timeout=5)

    def test_no_token_configured_means_no_check(self):
        base = self.serve("")
        self.assertEqual(self.get(base).status, 200)

    def test_a_configured_token_is_required(self):
        base = self.serve("s3cret")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get(base)
        self.assertEqual(caught.exception.code, 401)

    def test_the_right_token_is_accepted(self):
        base = self.serve("s3cret")
        self.assertEqual(self.get(base, "s3cret").status, 200)

    def test_a_wrong_token_is_refused(self):
        base = self.serve("s3cret")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get(base, "guess")
        self.assertEqual(caught.exception.code, 401)

    def test_a_post_is_guarded_too_not_only_the_read(self):
        base = self.serve("s3cret")
        req = urllib.request.Request(base + "/play", data=json.dumps({"mount": "rain"}).encode(),
                                     method="POST")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(caught.exception.code, 401)
        self.assertEqual(self.player.mount, "", "an unauthorised request still played something")


class FailsClosed(unittest.TestCase):
    """Binding off loopback without a token must not start. This service turns
    speakers on in someone's house; the quiet default is the wrong one."""

    def test_off_loopback_without_a_token_refuses_to_start(self):
        rc = speaker.main(["--listen", "0.0.0.0", "--output", "pulse"])
        self.assertEqual(rc, 2)

    def test_an_empty_token_file_is_refused_rather_than_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            empty = Path(d) / "token"
            empty.write_text("   \n")
            rc = speaker.main(["--listen", "0.0.0.0", "--token-file", str(empty)])
            self.assertEqual(rc, 2, "an empty token file authorises everything")

    def test_an_unreadable_token_file_refuses_to_start(self):
        rc = speaker.main(["--token-file", "/nonexistent/token"])
        self.assertEqual(rc, 2)

    def test_the_opt_out_is_explicit_and_then_permitted(self):
        """Binding wide with no token is allowed, but only when asked for: the
        pattern is legitimate behind an interface-scoped firewall or an
        overlay network, and illegitimate by accident."""
        # Refused without it...
        self.assertEqual(speaker.main(["--listen", "0.0.0.0"]), 2)
        # ...and permitted with it. main() would then serve forever, so this
        # checks the decision rather than the server: the argument parses and
        # the guard lets it through.
        args = speaker.build_parser().parse_args(["--listen", "0.0.0.0",
                                                  "--allow-unauthenticated"])
        self.assertTrue(args.allow_unauthenticated)
        self.assertFalse(speaker.is_loopback(args.listen))


PACTL_SINKS = json.dumps([
    {"name": "alsa_output.pci-0000_0a_00.4.analog-stereo",
     "description": "Built-in Audio Analogue Stereo"},
    {"name": "alsa_output.pci-0000_08_00.1.hdmi-stereo",
     "description": "GPU Digital Stereo (HDMI)"},
])


class OutputPicker(unittest.TestCase):
    """Choosing where the sound goes. The station has to be rebuilt to move
    it, because the destination is an ffmpeg argument."""

    def pulse(self):
        return speaker.PulseOutput(pactl="pactl")

    def test_sinks_are_listed_with_their_human_names(self):
        o = self.pulse()
        def fake(*a):
            return PACTL_SINKS if "list" in a else "alsa_output.pci-0000_0a_00.4.analog-stereo\n"
        with mock.patch.object(speaker.PulseOutput, "_run", side_effect=fake):
            got = o.list_targets()
        self.assertEqual([t["name"] for t in got],
                         ["Built-in Audio Analogue Stereo", "GPU Digital Stereo (HDMI)"])
        self.assertEqual([t["default"] for t in got], [True, False],
                         "the session's default sink was not marked")

    def test_an_unknown_sink_is_refused_rather_than_silently_accepted(self):
        o = self.pulse()
        with mock.patch.object(speaker.PulseOutput, "_run", return_value=PACTL_SINKS):
            self.assertFalse(o.set_target("alsa_output.does-not-exist"))
            self.assertTrue(o.set_target("alsa_output.pci-0000_08_00.1.hdmi-stereo"))
        self.assertEqual(o.sink, "alsa_output.pci-0000_08_00.1.hdmi-stereo")

    def test_empty_means_follow_the_sessions_default_again(self):
        o = self.pulse()
        o.sink = "alsa_output.pci-0000_08_00.1.hdmi-stereo"
        self.assertTrue(o.set_target(""))
        self.assertEqual(o.sink, "")

    def test_a_card_reports_the_one_device_it_has_and_will_not_switch(self):
        o = speaker.AlsaOutput("0", device="plughw:1,0")
        self.assertEqual([t["id"] for t in o.list_targets()], ["plughw:1,0"])
        self.assertFalse(o.set_target("plughw:2,0"),
                         "an ALSA device is not something the page gets to change")


class RainBed(unittest.TestCase):
    """A second stream under the station, mixed HERE rather than in a browser
    — the page's own bed is the browser's sound, which is no use to speakers
    in another room."""

    def bed(self, mixes=True, mount="rain"):
        out = speaker.PulseOutput(stream_name="netradio-bed") if mixes \
            else speaker.AlsaOutput("0")
        pl = speaker.Player("http://127.0.0.1:8020", _stay_alive_player(self), output=out)
        self.addCleanup(pl.stop)
        return speaker.Bed(pl, out, mount=mount, level=25)

    def test_a_mixing_output_can_have_one(self):
        self.assertTrue(self.bed().available())

    def test_an_exclusive_card_cannot_and_says_so(self):
        # The reason it is a declared capability: the page must not offer a
        # rain toggle that cannot work.
        self.assertFalse(self.bed(mixes=False).available())

    def test_no_bed_mount_configured_means_unavailable_not_broken(self):
        self.assertFalse(self.bed(mount="").available())

    def test_it_starts_and_stops(self):
        b = self.bed()
        with mock.patch.object(speaker.PulseOutput, "_run", return_value="[]"):
            b.start()
            self.assertTrue(b.on())
            b.stop()
            self.assertFalse(b.on())

    def test_the_bed_carries_a_different_stream_name_so_levels_stay_apart(self):
        """The station's volume slider must not move the rain. On PulseAudio a
        stream's level is found by its application name, so the two streams
        have to be distinguishable."""
        station = speaker.PulseOutput(stream_name="netradio")
        b = self.bed()
        self.assertNotEqual(station.stream_name, b.out.stream_name)
        self.assertIn("netradio-bed", b.out.output_args())

    def test_the_level_is_reported_and_clamped(self):
        b = self.bed()
        b.set_level(300)
        self.assertEqual(b.state()["level"], 100)
        b.set_level(-5)
        self.assertEqual(b.state()["level"], 0)


class BedAndOutputsOverHttp(unittest.TestCase):
    def setUp(self):
        self.out = speaker.PulseOutput(pactl="pactl")
        self.mixer = FakeMixer()
        self.mixer.out = self.out          # so /outputs reports the pulse one
        self.player = speaker.Player("http://127.0.0.1:8020", _stay_alive_player(self))
        bed_pl = speaker.Player("http://127.0.0.1:8020", _stay_alive_player(self))
        self.addCleanup(bed_pl.stop)
        self.bed = speaker.Bed(bed_pl, self.out, mount="rain")
        srv = ThreadingHTTPServer(("127.0.0.1", 0),
                                  speaker.make_handler(self.player, self.mixer, True, "", self.bed))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.base = f"http://127.0.0.1:{srv.server_address[1]}"

    def get(self, path):
        return json.loads(urllib.request.urlopen(self.base + path, timeout=5).read())

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     method="POST")
        return json.loads(urllib.request.urlopen(req, timeout=5).read())

    def test_outputs_are_served_for_a_picker(self):
        with mock.patch.object(speaker.PulseOutput, "_run", return_value=PACTL_SINKS):
            got = self.get("/outputs")
        self.assertEqual(len(got["outputs"]), 2)
        self.assertIn("current", got)

    def test_state_advertises_the_bed_so_the_page_knows_whether_to_offer_it(self):
        with mock.patch.object(speaker.PulseOutput, "_run", return_value="[]"):
            st = self.get("/state")
        self.assertTrue(st["mixes"])
        self.assertTrue(st["bed"]["available"])
        self.assertFalse(st["bed"]["on"])

    def test_a_device_that_cannot_mix_refuses_the_bed_with_400_not_500(self):
        """Answering correctly that it cannot is not an error on this side."""
        mixer = FakeMixer()                         # an AlsaOutput underneath
        pl = speaker.Player("http://127.0.0.1:8020", _stay_alive_player(self))
        bed = speaker.Bed(pl, mixer.out, mount="rain")
        srv = ThreadingHTTPServer(("127.0.0.1", 0),
                                  speaker.make_handler(pl, mixer, True, "", bed))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.addCleanup(pl.stop)
        req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/bed",
                                     data=json.dumps({"on": True}).encode(), method="POST")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(caught.exception.code, 400)

    def test_choosing_an_unknown_output_is_refused(self):
        with mock.patch.object(speaker.PulseOutput, "_run", return_value=PACTL_SINKS):
            req = urllib.request.Request(self.base + "/output",
                                         data=json.dumps({"target": "nope"}).encode(),
                                         method="POST")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(req, timeout=5)
        self.assertEqual(caught.exception.code, 400)
