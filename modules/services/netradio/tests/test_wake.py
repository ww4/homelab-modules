import socket
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlparse

from netradio import wake


def status(*sources):
    """Icecast's status-json shape, including its one-vs-many quirk."""
    if not sources:
        return {"icestats": {"dummy": None}}
    objs = [{"listenurl": f"http://127.0.0.1:8000/{m}.mp3", "listeners": n} for m, n in sources]
    return {"icestats": {"source": objs[0] if len(objs) == 1 else objs}}


class ParseListeners(unittest.TestCase):
    def test_none_one_many(self):
        self.assertEqual(wake.parse_listeners(status()), {})
        self.assertEqual(wake.parse_listeners(status(("rock", 1))), {"rock": 1})
        self.assertEqual(wake.parse_listeners(status(("rock", 0), ("folk", 2))), {"rock": 0, "folk": 2})

    def test_foreign_mounts_ignored(self):
        st = {"icestats": {"source": {"listenurl": "http://h/relay/live", "listeners": 3}}}
        self.assertEqual(wake.parse_listeners(st), {})


class FakeLiquidsoap:
    def __init__(self, icecast):
        self.icecast = icecast
        self.log = []

    def command(self, cmd):
        self.log.append(cmd)
        mount, op = cmd.split(".")
        if op == "start":
            self.icecast.up[mount] = 0
        elif op == "stop":
            self.icecast.up.pop(mount, None)
        return "OK"


class FakeIcecast:
    def __init__(self):
        self.up = {}

    def listeners(self):
        return dict(self.up)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pl = Path(self.tmp.name)
        (self.pl / "rock.m3u").write_text("#EXTM3U\n/m/a.mp3\n")
        (self.pl / "gospel.m3u").write_text("#EXTM3U\n")
        self.ic = FakeIcecast()
        self.ls = FakeLiquidsoap(self.ic)
        self.ctl = wake.Controller({"rock", "gospel"}, self.ls, self.ic, self.pl,
                                   idle_after=300, start_timeout=1)

    def tearDown(self):
        self.tmp.cleanup()

    def test_wake_starts_then_is_idempotent(self):
        self.assertEqual(self.ctl.wake("rock")[0], 200)
        self.assertEqual(self.ls.log, ["rock.start"])
        self.assertEqual(self.ctl.wake("rock")[0], 200)
        self.assertEqual(self.ls.log, ["rock.start"])  # already up: no second start

    def test_unknown_and_empty_stations_refuse(self):
        self.assertEqual(self.ctl.wake("jazz")[0], 404)
        self.assertEqual(self.ctl.wake("gospel")[0], 503)
        self.assertEqual(self.ls.log, [])

    def test_start_failure_is_503_not_hang(self):
        class Dead(FakeLiquidsoap):
            def command(self, cmd):
                self.log.append(cmd)
                return "no such output"
        self.ls = Dead(self.ic)
        self.ctl.ls = self.ls
        code, msg = self.ctl.wake("rock")
        self.assertEqual(code, 503)
        self.assertIn("no such output", msg)

    def test_idle_stop_after_grace_and_reset_by_listener(self):
        self.ctl.wake("rock")
        t = 1000.0
        self.assertEqual(self.ctl.tick(t), [])            # idle clock starts
        self.assertEqual(self.ctl.tick(t + 200), [])
        self.ic.up["rock"] = 1                             # someone tuned in
        self.assertEqual(self.ctl.tick(t + 250), [])
        self.ic.up["rock"] = 0                             # and left
        self.assertEqual(self.ctl.tick(t + 300), [])      # clock restarted at t+300
        self.assertEqual(self.ctl.tick(t + 599), [])
        self.assertEqual(self.ctl.tick(t + 600), ["rock"])
        self.assertEqual(self.ls.log[-1], "rock.stop")
        self.assertNotIn("rock", self.ic.up)

    def test_wake_resets_idle_clock(self):
        self.ctl.wake("rock")
        self.ctl.tick(1000.0)
        self.ctl.wake("rock")                              # re-tune before anyone attached
        self.assertEqual(self.ctl.tick(1299.0), [])       # clock restarts here, not at 1000
        self.assertEqual(self.ctl.tick(1300.0), [])
        self.assertEqual(self.ctl.tick(1598.0), [])
        self.assertEqual(self.ctl.tick(1599.0), ["rock"])

    def test_tick_leaves_foreign_mounts_alone(self):
        self.ic.up["live"] = 0
        self.assertEqual(self.ctl.tick(0.0), [])
        self.assertEqual(self.ctl.tick(10_000.0), [])
        self.assertEqual(self.ls.log, [])


class Qualities(unittest.TestCase):
    def test_variants_and_base(self):
        self.assertEqual(wake.all_mounts([{"mount": "rock"}]), {"rock", "rock-lo"})
        self.assertEqual(wake.base_mount("rock-lo"), "rock")
        self.assertEqual(wake.base_mount("rock"), "rock")

    def test_lo_mount_wakes_against_the_base_playlist(self):
        with tempfile.TemporaryDirectory() as d:
            pl = Path(d)
            (pl / "rock.m3u").write_text("#EXTM3U\n/m/a.mp3\n")
            ic = FakeIcecast(); ls = FakeLiquidsoap(ic)
            ctl = wake.Controller({"rock", "rock-lo"}, ls, ic, pl, idle_after=300, start_timeout=1)
            self.assertEqual(ctl.wake("rock-lo")[0], 200)
            self.assertEqual(ls.log, ["rock-lo.start"])


class FakeLiquidsoapServer:
    """A minimal stand-in for liquidsoap's unix control socket: accepts
    connections, answers every line with "OK\\nEND\\n", and counts how many
    separate connections it ever accepted — the number the real fix is about."""

    def __init__(self, path):
        self.path = path
        self.accepted = 0
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(str(path))
        self._srv.listen(5)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                self._srv.settimeout(0.2)
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.accepted += 1
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        with conn:
            buf = b""
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    return
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line == b"__close__":
                        return
                    conn.sendall(b"OK\nEND\n")

    def close(self):
        self._stop.set()
        self._srv.close()
        self._thread.join(timeout=2)


class LiquidsoapClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sock_path = Path(self.tmp.name) / "liq.sock"
        self.server = FakeLiquidsoapServer(self.sock_path)
        self.clients = []

    def tearDown(self):
        for ls in self.clients:
            if ls._sock is not None:
                ls._sock.close()
        self.server.close()
        self.tmp.cleanup()

    def _client(self):
        ls = wake.Liquidsoap(self.sock_path)
        self.clients.append(ls)
        return ls

    def test_many_commands_reuse_one_connection(self):
        """This is the whole fix: 26 stations polling every 5s must not mean
        26 connects every 5s. One client, many commands, one accept()."""
        ls = self._client()
        for _ in range(20):
            self.assertEqual(ls.command("q_rock.queue"), "OK")
        self.assertEqual(self.server.accepted, 1)

    def test_shared_client_is_thread_safe(self):
        """dj.py's main() builds exactly one Liquidsoap and hands it to every
        StationDJ thread — concurrent callers must not see each other's reply
        on a shared connection."""
        ls = self._client()
        errors = []

        def hammer():
            try:
                for _ in range(30):
                    reply = ls.command("q_rock.queue")
                    if reply != "OK":
                        errors.append(reply)
            except Exception as e:  # pragma: no cover - failure path reported via errors
                errors.append(repr(e))

        threads = [threading.Thread(target=hammer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        self.assertEqual(errors, [])

    def test_reconnects_after_the_connection_drops(self):
        """A liquidsoap restart mid-deploy drops the socket; the next command
        must transparently reconnect rather than wedging the DJ forever."""
        ls = self._client()
        self.assertEqual(ls.command("q_rock.queue"), "OK")
        self.assertEqual(self.server.accepted, 1)
        ls._sock.close()  # simulate the peer going away between polls; _sock is still set
        self.assertEqual(ls.command("q_rock.queue"), "OK")
        self.assertEqual(self.server.accepted, 2)


class StreamPathRegex(unittest.TestCase):
    def test_paths(self):
        self.assertEqual(wake.STREAM_PATH.match("/radio/blues-jazz.mp3").group(1), "blues-jazz")
        self.assertIsNone(wake.STREAM_PATH.match("/radio/../etc.mp3"))
        self.assertIsNone(wake.STREAM_PATH.match("/radio/rock.mp3?x=1"))
        self.assertEqual(wake.STREAM_PATH.match(urlparse("/radio/rock.mp3?x=1").path).group(1), "rock")
        self.assertIsNone(wake.STREAM_PATH.match("/rock.mp3"))


if __name__ == "__main__":
    unittest.main()
