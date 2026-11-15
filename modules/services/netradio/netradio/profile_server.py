"""The profiler as a service, for offloading the listening to a faster box.

the server's library lives on the server; the CPU that gets through it fastest is
the compute box's. So the compute box runs this: POST a track's bytes to /analyse and get the
same Facts JSON `netradio profile` would have computed locally. The client
(profile.py, --remote) tries it first and falls back to analysing locally
when nobody answers — the whisper/Kokoro shape from the switchboard. There
is no state here: the file is written to a private temp dir, measured,
deleted. Bind it to the tailnet and scope the firewall; it has no auth.
"""

from __future__ import annotations

import argparse
import json
import logging
import multiprocessing as mp
import os
import sys
import tempfile
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from netradio import profile

log = logging.getLogger("netradio.profile_server")

MAX_BYTES = 200 * 1024 * 1024   # a 12-minute FLAC is ~150 MB; nothing legitimate is larger

_model: profile.Yamnet | None = None


def _init(model_path: str) -> None:
    global _model
    _model = profile.Yamnet(Path(model_path), threads=1)


def _analyse_bytes(data: bytes, suffix: str) -> dict:
    """Runs in a pool worker: the file must exist on disk for ffmpeg/mutagen."""
    fd, tmp = tempfile.mkstemp(suffix=suffix or ".bin")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        f = profile.analyse(tmp, _model)
        return {"facts": asdict(f), "title": profile.read_title(tmp), "profile_version": profile.PROFILE_VERSION}
    finally:
        os.unlink(tmp)


def make_handler(pool):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                self._reply(200, {"ok": True, "profile_version": profile.PROFILE_VERSION})
            else:
                self._reply(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/analyse":
                self._reply(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BYTES:
                self._reply(413, {"error": f"bad length {length}"})
                return
            suffix = Path(self.headers.get("X-Filename", "")).suffix.lower()[:8]
            data = self.rfile.read(length)
            t0 = time.monotonic()
            try:
                result = pool.apply(_analyse_bytes, (data, suffix))
            except Exception as e:
                log.warning("analyse failed (%s, %d bytes): %s", suffix, length, e)
                self._reply(500, {"error": f"{type(e).__name__}: {e}"})
                return
            log.debug("analysed %d bytes in %.1fs", length, time.monotonic() - t0)
            self._reply(200, result)

        def _reply(self, code: int, obj: dict):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

    return Handler


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--model", required=True, type=Path)
    ap.add_argument("--listen", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--workers", type=int, default=0, help="analysis processes (0 = one per core)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s", stream=sys.stdout)
    workers = args.workers or (os.cpu_count() or 1)
    ctx = mp.get_context("spawn")
    with ctx.Pool(workers, initializer=_init, initargs=(str(args.model),)) as pool:
        srv = ThreadingHTTPServer((args.listen, args.port), make_handler(pool))
        log.info("profile server on %s:%d, %d workers, profile version %d",
                 args.listen, args.port, workers, profile.PROFILE_VERSION)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0
