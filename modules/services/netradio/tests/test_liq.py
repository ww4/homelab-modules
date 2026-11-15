"""The Liquidsoap script has to PARSE, and that is not checked anywhere else.

`netradio liq` renders /run/netradio/netradio.liq at service start, so a typo
in the template is found by Liquidsoap itself — at which point every station is
off the air at once and the page shows a wall of dead mounts. The same shape as
the nginx lesson in this repo: a green `nix build` is not the same as the
daemon accepting its config.

`liquidsoap --check` parses AND type-checks without opening a single output, so
it is safe to run in a build. It is the only thing that would catch calling a
method a source does not have — `q.set_queue([])` in the flush command, say.

Point NETRADIO_LIQUIDSOAP at the binary to run this; without it the parse test
skips, so a plain `python3 -m unittest` on a machine with no Liquidsoap still
works. The Nix build always sets it.

Unlike the other two build-time checks this one IMPORTS the package rather than
reading files, so the build puts a directory actually named `netradio` on
PYTHONPATH — a store path is called <hash>-netradio, which is not importable.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from netradio import liq


LIQUIDSOAP = os.environ.get("NETRADIO_LIQUIDSOAP", "")

STATIONS = [
    {"mount": "ambient", "name": "Ambient"},
    {"mount": "bluegrass", "name": "Bluegrass"},
]


def render() -> str:
    return liq.render(STATIONS, socket="/run/netradio/liquidsoap.sock",
                      playlists="/var/lib/netradio/playlists",
                      now_dir="/var/lib/netradio/now", port=8020, relays=[])


class TheScriptParses(unittest.TestCase):

    @unittest.skipUnless(LIQUIDSOAP, "set NETRADIO_LIQUIDSOAP to the liquidsoap binary")
    def test_liquidsoap_accepts_the_rendered_script(self):
        with tempfile.NamedTemporaryFile("w", suffix=".liq", delete=False) as f:
            f.write(render())
            path = f.name
        try:
            p = subprocess.run([LIQUIDSOAP, "--check", path],
                               capture_output=True, text=True, timeout=180)
        finally:
            os.unlink(path)
        self.assertEqual(p.returncode, 0,
                         f"liquidsoap rejected the rendered script:\n{p.stdout}\n{p.stderr}")

    def test_the_skip_is_registered_on_the_fallback_not_the_queue(self):
        """The DJ's skip has to end whatever the FALLBACK selected. Aimed at the
        request queue it is silent whenever the station's shuffle holds the air,
        which is the case after every restart — the owner pressed skip four times
        against a track the shuffle had picked up and heard all of it
        (2026-09-29)."""
        script = render()
        self.assertIn("sw.skip()", script)
        self.assertNotIn("q.skip()", script)

    def test_the_server_label_is_quieter_than_the_rest(self):
        """At log level 3 the server logs a line pair per connection, and the
        DJ opens one connection per station every 5 s — about five a second
        here, ~900,000 lines a day. With `SystemMaxUse=1G` that left roughly a
        day and a half of journal, so the evening a deploy restarted the
        speakers had already been vacuumed away when it was needed
        (2026-10-24). The connections are not the fault; the logging is.

        It also produced a false finding: the same query returned 1,932 lines
        and then 0 a quarter of an hour later, which reads exactly like an
        event beginning at that timestamp. It was the retention horizon
        moving. A boundary is not an event.
        """
        script = render()
        self.assertIn("settings.server.log.level := 2", script,
                      "the server label is back at the global level and will flood the journal")
        self.assertIn("settings.log.level := 3", script,
                      "the global level dropped too — track changes and errors go with it")

    def test_there_is_a_flush_that_leaves_the_air_alone(self):
        """Dropping a stale running order and taking the current track off are
        two separate acts, because the DJ writes a new running order in
        between. Liquidsoap's own flush_and_skip fuses them."""
        script = render()
        self.assertIn("q.set_queue([])", script)


if __name__ == "__main__":
    unittest.main()
