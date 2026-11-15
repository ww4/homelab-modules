"""Start a station's encoder when someone tunes in; stop it when they leave.

Seven MP3 encoders running around the clock for a receiver that is on a few
hours a week is the wrong shape, and Icecast has no "listener arrived" hook
to start one from. So the receiver reaches the streams through nginx, and
nginx asks this service (auth_request) before it proxies a listener to
Icecast:

    GET /wake, X-Original-URI: /radio/<mount>.mp3
      -> tell Liquidsoap to start output <mount>
      -> wait until Icecast lists the mount (the encoder connected)
      -> 200, and nginx hands the listener on; 503 and the receiver shows
         an error instead of silence

A background tick polls Icecast's public status page and stops any mount
that has had no listeners for `idle_after` seconds. Nothing here needs a
credential: the status page is unauthenticated and the Liquidsoap control
socket is a file only this user can open.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import socket
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger("netradio.wake")

# The stream path nginx hands us, NOT a mount name — it carries one.
STREAM_PATH = re.compile(r"^/radio/([a-z0-9-]+)\.mp3$")


class Liquidsoap:
    """The Liquidsoap server over its unix socket. Replies end with an END line.

    One connection, held open and reused for the process's lifetime, with every
    command serialized through a lock rather than opened fresh per call. The one
    `Liquidsoap` instance built in main() is shared by every StationDJ thread (26
    stations as of 2026-10), each polling its queue every POLL=5s — a connect per
    command was ~5 connects/sec of journald noise (2 lines each: liquidsoap logs
    a plain close as "disconnected without saying goodbye") for no reason, since
    the socket protocol is strictly request/reply and a kept-open connection
    serves every caller just as correctly. Reconnects lazily on the next command
    after any failure (liquidsoap restarting mid-deploy drops the socket)."""

    def __init__(self, path: Path, timeout: float = 5.0):
        self.path = path
        self.timeout = timeout
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None

    def _connect(self) -> socket.socket:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(str(self.path))
        return s

    def command(self, cmd: str) -> str:
        with self._lock:
            last_err: OSError | None = None
            for _attempt in (1, 2):
                if self._sock is None:
                    self._sock = self._connect()
                try:
                    self._sock.sendall((cmd + "\n").encode())
                    buf = b""
                    while not re.search(rb"(^|\r?\n)END\r?\n", buf):
                        chunk = self._sock.recv(4096)
                        if not chunk:
                            raise OSError("liquidsoap closed the connection")
                        buf += chunk
                    reply = buf.decode(errors="replace")
                    return re.sub(r"\r?\nEND\r?\n$", "", reply).strip()
                except OSError as e:
                    last_err = e
                    try:
                        self._sock.close()
                    except OSError:
                        pass
                    self._sock = None
            raise last_err


class Icecast:
    def __init__(self, url: str, timeout: float = 3.0):
        self.url = url
        self.timeout = timeout

    def status(self) -> dict:
        with urllib.request.urlopen(self.url, timeout=self.timeout) as r:
            return json.load(r)

    def listeners(self) -> dict[str, int]:
        """{mount: listeners} for every mount currently fed by a source."""
        return parse_listeners(self.status())


def parse_listeners(status: dict) -> dict[str, int]:
    """Icecast's quirk: `source` is an object with one mount up, a list with
    several, and absent with none."""
    src = status.get("icestats", {}).get("source")
    if src is None:
        return {}
    if isinstance(src, dict):
        src = [src]
    out = {}
    for s in src:
        path = urlparse(s.get("listenurl", "")).path
        m = re.match(r"^/([a-z0-9-]+)\.mp3$", path)
        if m:
            out[m.group(1)] = int(s.get("listeners", 0))
    return out


class Controller:
    def __init__(self, mounts: set[str], liquidsoap: Liquidsoap, icecast: Icecast,
                 playlist_dir: Path, idle_after: float = 300.0, start_timeout: float = 10.0):
        self.mounts = mounts
        self.ls = liquidsoap
        self.ic = icecast
        self.playlist_dir = playlist_dir
        self.idle_after = idle_after
        self.start_timeout = start_timeout
        self.idle_since: dict[str, float] = {}
        self.lock = threading.Lock()

    def wake(self, mount: str) -> tuple[int, str]:
        if mount not in self.mounts:
            return 404, f"no such station: {mount}"
        playlist = self.playlist_dir / f"{base_mount(mount)}.m3u"
        if not has_tracks(playlist):
            # Silence would be the alternative; an error on the receiver says
            # "run the scan", silence says nothing.
            return 503, f"station {mount} has an empty playlist ({playlist})"
        with self.lock:
            self.idle_since.pop(mount, None)
            if mount in self.ic.listeners():
                return 200, f"{mount} already up"
            reply = self.ls.command(f"{mount}.start")
            log.info("start %s: %s", mount, reply)
            deadline = time.monotonic() + self.start_timeout
            while time.monotonic() < deadline:
                if mount in self.ic.listeners():
                    return 200, f"{mount} up"
                time.sleep(0.25)
        return 503, f"{mount} did not come up within {self.start_timeout:.0f}s (liquidsoap said: {reply})"

    def tick(self, now: float | None = None) -> list[str]:
        """Stop mounts idle for longer than idle_after. Returns what it stopped."""
        now = time.monotonic() if now is None else now
        stopped = []
        with self.lock:
            live = self.ic.listeners()
            for mount in list(self.idle_since):
                if mount not in live or live[mount] > 0:
                    del self.idle_since[mount]
            for mount, n in live.items():
                if n > 0 or mount not in self.mounts:
                    continue
                since = self.idle_since.setdefault(mount, now)
                if now - since >= self.idle_after:
                    reply = self.ls.command(f"{mount}.stop")
                    log.info("stop %s (idle %.0fs): %s", mount, now - since, reply)
                    del self.idle_since[mount]
                    stopped.append(mount)
        return stopped


QUALITY_SUFFIXES = ("-lo",)   # <mount>-lo is the 96 kbps encode of the same program


def base_mount(mount: str) -> str:
    for suf in QUALITY_SUFFIXES:
        if mount.endswith(suf):
            return mount[: -len(suf)]
    return mount


def all_mounts(stations: list[dict], relays: list[dict] | None = None) -> set[str]:
    """Every mount the wake service may be asked to start.

    A relay — an outside station re-encoded for a device that cannot decode the
    original — is wakeable exactly like a library station, and idles out the
    same way. It has no quality variants: there is one upstream and re-encoding
    it twice would be paying twice for the same thing."""
    out = {s["mount"] + suf for s in stations for suf in ("",) + QUALITY_SUFFIXES}
    return out | {r["mount"] for r in relays or []}


def has_tracks(playlist: Path) -> bool:
    try:
        with playlist.open() as fh:
            return any(line.strip() and not line.startswith("#") for line in fh)
    except OSError:
        return False


def make_handler(ctl: Controller):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/wake":
                self._reply(404, "not found")
                return
            # $request_uri is the raw request line's URI: query string included,
            # nothing decoded (nginx's own gixy check refuses $uri in a header).
            m = STREAM_PATH.match(urlparse(self.headers.get("X-Original-URI", "")).path)
            if not m:
                self._reply(400, "X-Original-URI is not a /radio/<mount>.mp3 path")
                return
            try:
                code, msg = ctl.wake(m.group(1))
            except Exception as e:  # a dead socket or icecast must not kill the server
                log.exception("wake %s failed", m.group(1))
                code, msg = 503, f"wake failed: {e}"
            if code != 200:
                log.warning("wake %s -> %d %s", m.group(1), code, msg)
            self._reply(code, msg)

        def _reply(self, code: int, msg: str):
            body = (msg + "\n").encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):  # nginx already logs the request
            log.debug(fmt, *args)

    return Handler


def idle_loop(ctl: Controller, interval: float) -> None:
    while True:
        time.sleep(interval)
        try:
            ctl.tick()
        except Exception:
            log.exception("idle tick failed")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stations", required=True, type=Path)
    ap.add_argument("--relays", type=Path, help="JSON [{mount,name,url}] of on-demand relays")
    ap.add_argument("--playlists", required=True, type=Path)
    ap.add_argument("--socket", required=True, type=Path, help="liquidsoap server socket")
    ap.add_argument("--icecast-status", default="http://127.0.0.1:8000/status-json.xsl")
    ap.add_argument("--listen", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8011)
    ap.add_argument("--idle-after", type=float, default=300.0, help="seconds with no listeners before stopping")
    ap.add_argument("--tick", type=float, default=30.0, help="seconds between idle checks")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s", stream=sys.stdout)

    relays = []
    if args.relays and args.relays.exists():
        try:
            relays = json.loads(args.relays.read_text())
        except ValueError as e:
            log.warning("ignoring %s: %s", args.relays, e)
    mounts = all_mounts(json.loads(args.stations.read_text()), relays)
    ctl = Controller(mounts, Liquidsoap(args.socket), Icecast(args.icecast_status),
                     args.playlists, idle_after=args.idle_after)
    threading.Thread(target=idle_loop, args=(ctl, args.tick), daemon=True).start()
    srv = ThreadingHTTPServer((args.listen, args.port), make_handler(ctl))
    log.info("listening on %s:%d for %d stations, idle stop after %.0fs",
             args.listen, args.port, len(mounts), args.idle_after)
    srv.serve_forever()
    return 0
