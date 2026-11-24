"""A tiny HTTP endpoint for stars, behind the same vhost as the static page.

The page is static and rebuilt by every run; stars have to outlive that and be
the same for everyone who opens the page, so they live in the database. This
serves exactly two things under /api/:

  GET  /api/favorites  -> {"<brief>": ["<listing id>", ...]}
  POST /api/favorites  <- {"brief": "...", "id": "...", "starred": true|false}

and re-renders the page after a change, so a reload shows the Starred section
the server now knows about. There is no authentication: it binds loopback and
is meant to sit behind the same reverse proxy (and whatever gates it) as the
page. Inputs are validated against the briefs that exist and numeric ids.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .store import Store

log = logging.getLogger(__name__)
LISTING_ID = re.compile(r"^\d{5,25}$")
BRIEF_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
MAX_BODY = 4096


class _Rerender:
    """Coalesces bursts of star clicks into one page rebuild."""

    def __init__(self, fn: Callable[[], None], delay: float = 2.0) -> None:
        self.fn, self.delay = fn, delay
        self.lock = threading.Lock()
        self.timer: threading.Timer | None = None

    def poke(self) -> None:
        with self.lock:
            if self.timer:
                self.timer.cancel()
            self.timer = threading.Timer(self.delay, self._run)
            self.timer.daemon = True
            self.timer.start()

    def _run(self) -> None:
        try:
            self.fn()
        except Exception:  # noqa: BLE001 - the star is saved; a failed rebuild is logged, next run fixes the page
            log.exception("page re-render after a star change failed")


def make_handler(settings: Settings, rerender: _Rerender) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "market-agent"

        def log_message(self, fmt: str, *args: Any) -> None:  # quieter than stderr per request
            log.info("%s %s", self.address_string(), fmt % args)

        def _send(self, code: int, body: Any) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.split("?")[0] == "/api/favorites":
                store = Store(settings.db_path)
                try:
                    self._send(200, store.all_favorites())
                finally:
                    store.close()
            elif self.path == "/api/health":
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path.split("?")[0] != "/api/favorites":
                self._send(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                self._send(400, {"error": "body required, at most 4 KB"})
                return
            try:
                body = json.loads(self.rfile.read(length))
                brief, lid, starred = body["brief"], str(body["id"]), bool(body["starred"])
            except (ValueError, KeyError, TypeError):
                self._send(400, {"error": "expected {brief, id, starred}"})
                return
            if not BRIEF_NAME.match(str(brief)) or not (settings.briefs_dir / f"{brief}.toml").is_file():
                self._send(400, {"error": f"no brief named {brief!r}"})
                return
            if not LISTING_ID.match(lid):
                self._send(400, {"error": "listing id must be the numeric Facebook id"})
                return
            store = Store(settings.db_path)
            try:
                store.set_favorite(brief, lid, starred)
            finally:
                store.close()
            rerender.poke()
            self._send(200, {"ok": True, "brief": brief, "id": lid, "starred": starred})

    return Handler


def serve(settings: Settings, port: int, rebuild: Callable[[], None], host: str = "127.0.0.1") -> None:
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise ValueError("market-agent serve has no authentication; bind loopback and put a proxy in front")
    server = ThreadingHTTPServer((host, port), make_handler(settings, _Rerender(rebuild)))
    log.warning("market-agent API listening on %s:%d", host, port)
    server.serve_forever()


__all__ = ["serve", "make_handler", "Path"]
