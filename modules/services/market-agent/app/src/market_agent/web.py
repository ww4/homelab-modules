"""A tiny HTTP endpoint for stars, behind the same vhost as the static page.

The page is static and rebuilt by every run; stars have to outlive that and be
the same for everyone who opens the page, so they live in the database. This
serves exactly two things under /api/:

  GET  /api/favorites  -> {"<brief>": ["<listing id>", ...]}
  POST /api/favorites  <- {"brief": "...", "id": "...", "starred": true|false}
  POST /api/dismissed  <- {"brief": "...", "id": "...", "dismissed": true|false}  ("not interested")

and watches (briefs), for the page's "+ New" and Edit form:

  GET    /api/briefs                -> [{name, title, kind, paused}]
  GET    /api/briefs/defaults       -> form fields for a new watch (home etc. from the last one)
  GET    /api/briefs/<name>         -> form fields
  POST   /api/briefs                <- form fields (+ "name" to update an existing one)
  POST   /api/briefs/<name>/pause   <- {"paused": true|false}
  DELETE /api/briefs/<name>         -> moved to briefs/.deleted/, results kept

Saving a watch asks for a run of it: a file in <home>/requests/ that a systemd
.path unit turns into a start of the normal market-agent service, so this
endpoint never runs searches or the judge itself (and keeps its hardening).
The page is re-rendered after any change. There is no authentication: it binds loopback and
is meant to sit behind the same reverse proxy (and whatever gates it) as the
page. Inputs are validated against the briefs that exist and numeric ids.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from . import brief as briefs
from .config import Settings
from .store import Store

log = logging.getLogger(__name__)
LISTING_ID = re.compile(r"^\d{5,25}$")
BRIEF_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
MAX_BODY = 32768


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

        def _brief_path(self, name: str) -> Path | None:
            if not BRIEF_NAME.match(name):
                return None
            p = settings.briefs_dir / f"{name}.toml"
            return p if p.is_file() else None

        def _body(self) -> Any:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                raise ValueError("body required, at most 32 KB")
            return json.loads(self.rfile.read(length))

        def do_DELETE(self) -> None:  # noqa: N802
            parts = self.path.split("?")[0].strip("/").split("/")
            if len(parts) == 3 and parts[:2] == ["api", "briefs"]:
                src = self._brief_path(parts[2])
                if not src:
                    self._send(404, {"error": "no such watch"})
                    return
                trash = settings.briefs_dir / ".deleted"
                trash.mkdir(exist_ok=True)
                src.rename(trash / f"{src.stem}-{int(time.time())}.toml")
                rerender.poke()
                self._send(200, {"ok": True, "deleted": parts[2]})
                return
            self._send(404, {"error": "not found"})

        def do_GET(self) -> None:  # noqa: N802
            parts = self.path.split("?")[0].strip("/").split("/")
            if parts[:2] == ["api", "briefs"]:
                if len(parts) == 2:
                    out = []
                    for p in sorted(settings.briefs_dir.glob("*.toml")):
                        try:
                            b = briefs.load(p)
                            out.append({"name": b.name, "title": b.title, "kind": b.kind, "paused": b.paused})
                        except Exception as e:  # noqa: BLE001 - list broken ones too
                            out.append({"name": p.stem, "title": p.stem, "error": str(e)[:200]})
                    self._send(200, out)
                elif parts[2] == "defaults":
                    latest = sorted(settings.briefs_dir.glob("*.toml"), key=lambda p: p.stat().st_mtime)
                    form: dict[str, Any] = {"kind": "general", "max_miles": 50}
                    if latest:
                        last = briefs.to_form(briefs.load(latest[-1]))
                        # Home and range carry over; extra places (a friend's work) are per watch.
                        form.update({k: last[k] for k in ("home", "max_miles")})
                    self._send(200, form)
                elif (p := self._brief_path(parts[2])):
                    self._send(200, briefs.to_form(briefs.load(p)))
                else:
                    self._send(404, {"error": "no such watch"})
                return
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
            parts = self.path.split("?")[0].strip("/").split("/")
            if parts[:2] == ["api", "briefs"]:
                self._post_brief(parts[2:])
                return
            route = self.path.split("?")[0]
            if route not in ("/api/favorites", "/api/dismissed"):
                self._send(404, {"error": "not found"})
                return
            field_name = "starred" if route == "/api/favorites" else "dismissed"
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                self._send(400, {"error": "body required, at most 4 KB"})
                return
            try:
                body = json.loads(self.rfile.read(length))
                brief, lid, on = body["brief"], str(body["id"]), bool(body[field_name])
            except (ValueError, KeyError, TypeError):
                self._send(400, {"error": f"expected {{brief, id, {field_name}}}"})
                return
            if not BRIEF_NAME.match(str(brief)) or not (settings.briefs_dir / f"{brief}.toml").is_file():
                self._send(400, {"error": f"no brief named {brief!r}"})
                return
            if not LISTING_ID.match(lid):
                self._send(400, {"error": "listing id must be the numeric Facebook id"})
                return
            store = Store(settings.db_path)
            try:
                if field_name == "starred":
                    store.set_favorite(brief, lid, on)
                else:
                    store.set_dismissed(brief, lid, on)
            finally:
                store.close()
            rerender.poke()
            self._send(200, {"ok": True, "brief": brief, "id": lid, field_name: on})

        def _post_brief(self, rest: list[str]) -> None:
            try:
                body = self._body()
            except ValueError as e:
                self._send(400, {"error": str(e)})
                return
            if len(rest) == 2 and rest[1] == "pause":
                p = self._brief_path(rest[0])
                if not p:
                    self._send(404, {"error": "no such watch"})
                    return
                b = briefs.load(p).model_copy(update={"paused": bool(body.get("paused"))})
                briefs.save(b, settings.briefs_dir)
                if not b.paused:
                    request_run(settings, b.name)
                rerender.poke()
                self._send(200, {"ok": True, "name": b.name, "paused": b.paused})
                return
            if rest:
                self._send(404, {"error": "not found"})
                return
            name = body.get("name") or None
            base = None
            if name:
                p = self._brief_path(str(name))
                if not p:
                    self._send(404, {"error": f"no watch named {name!r}"})
                    return
                base = briefs.load(p)
            try:
                b = briefs.from_form(body, name=name, base=base)
            except Exception as e:  # noqa: BLE001 - pydantic and value errors become a 400 the form shows
                self._send(400, {"error": _plain(e)})
                return
            if base is None:  # new: never overwrite another watch by title collision
                stem, n = b.name, 2
                while (settings.briefs_dir / f"{b.name}.toml").exists():
                    b = b.model_copy(update={"name": f"{stem}-{n}"})
                    n += 1
            briefs.save(b, settings.briefs_dir)
            if not b.paused:
                request_run(settings, b.name)
            rerender.poke()
            self._send(200, {"ok": True, "name": b.name, "searching": not b.paused})

    return Handler


def _plain(e: Exception) -> str:
    """A pydantic error as one line a person can act on."""
    errs = getattr(e, "errors", None)
    if callable(errs):
        try:
            first = errs()[0]
            where = ".".join(str(x) for x in first.get("loc", ()))
            return f"{where}: {first.get('msg')}" if where else str(first.get("msg"))
        except Exception:  # noqa: BLE001
            pass
    return str(e)[:300]


def request_run(settings: Settings, name: str) -> None:
    """Ask the service to search one watch now (see module header)."""
    d = settings.home / "requests"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(str(int(time.time())))


def serve(settings: Settings, port: int, rebuild: Callable[[], None], host: str = "127.0.0.1") -> None:
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise ValueError("market-agent serve has no authentication; bind loopback and put a proxy in front")
    server = ThreadingHTTPServer((host, port), make_handler(settings, _Rerender(rebuild)))
    log.warning("market-agent API listening on %s:%d", host, port)
    server.serve_forever()


__all__ = ["serve", "make_handler", "Path"]
