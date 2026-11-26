"""The admin API behind <radio host>/admin — edits the runtime
config (feeds, stations, schedule) and files requests for the privileged
side (compile a feed, apply: rescan + restart Liquidsoap when mounts change).

Loopback only; nginx proxies /admin/api/ to it. The vhost is the
Tailscale/LAN gate. Everything it writes is a JSON file under the config
dir; the scanner and the DJ read those.

  GET    /api/state                      everything the page shows
  GET    /api/options?station=<mount>    feeds + artists compatible with a station
  POST   /api/feeds                      {title, description} → new feed, compile requested
  PUT    /api/feeds/<id>                 {title?, description?, rule?, family?, listenable?}
  POST   /api/feeds/<id>/compile         (re)compile from the description
  DELETE /api/feeds/<id>
  PUT    /api/stations/<mount>           {name?, family?, base?, breaks_every?, excursion?}
  PUT    /api/schedule                   the whole list of slots
  POST   /api/apply                      rescan now (and restart Liquidsoap if mounts changed)
  GET    /api/pandora                    what the receiver played on Pandora: artists per station, in-library flags
  GET    /api/watching                   what Jellyfin is playing on a screen in the house, if anything
  GET    /api/watching/art?item=<id>     the poster for it, proxied (the page cannot hold the API key)
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import itertools
import json
import logging
import os
import re
import sys
import threading
import time
import unicodedata
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from netradio import feeds as feedrules
from netradio.conventions import COVER_NAMES, is_item_id, is_mount, sidecar_names
from netradio import devices
from netradio import jellyfin
from netradio import ratings
from netradio import schedule as sched
from netradio.config import write_atomic, FAMILIES, Config, compatible, new_id

log = logging.getLogger("netradio.admin")
LOCK = threading.Lock()


def _fold(s: str) -> str:
    """Match artist names across accents and case: the folder is whatever the
    ripper wrote, Jellyfin's name is whatever the metadata said."""
    return unicodedata.normalize("NFKD", s).casefold().strip()


class Admin:
    def __init__(self, cfg: Config, dj_dir: Path | None = None, playlists: Path | None = None,
                 receiver_api: str = "", now_dir: Path | None = None, wake_url: str = "",
                 jellyfin_url: str = "", jellyfin_key: str = "", jellyfin_user: str = "",
                 thumb_cache: Path | None = None, thumb_cache_max: int = 5000,
                 station_art: Path | None = None, jellyfin_hearts: bool = False,
                 devices_file: Path | None = None):
        self.cfg = cfg
        # The device registry, read from a file rather than taken as a value so
        # that editing it later needs no restart — and so the page's own calls
        # to a device can be forwarded from here (netradio/devices.py says why
        # they come through this process at all).
        self.devices_file = devices_file
        self.dj_dir = dj_dir            # the DJ's state: inbox/ takes skip + request files
        self.playlists = playlists      # library.m3u for search
        self.receiver_api = receiver_api   # for the page's resume button
        self.now_dir = now_dir
        self.wake_url = wake_url
        self.jellyfin_url, self.jellyfin_key, self.jellyfin_user = jellyfin_url, jellyfin_key, jellyfin_user
        self.jellyfin_hearts = jellyfin_hearts   # mirror favourites, as opposed to just reading artwork
        self._library: list[str] | None = None
        self._library_set: frozenset[str] = frozenset()
        self._library_mtime = 0.0
        self._inbox_n = itertools.count()
        # Where resized covers are kept between requests. Without it every
        # thumbnail costs a full mutagen decode of the track (some are 29 MB)
        # plus a Pillow decode/resize — 147 of those made the desktop wall take
        # ~17 s to paint on a LAN (2026-09-28).
        self.thumb_cache = thumb_cache
        self.thumb_cache_max = thumb_cache_max
        self.station_art = station_art      # <mount>.jpg pictures for stations
        self._jf_artists: dict[str, str] | None = None   # normalised name -> item id
        self._jf_artists_at = 0.0
        self._thumb_writes = itertools.count()

    def resume_receiver(self) -> dict:
        """The page's resume button: the same thing netradio-resume does after a
        Liquidsoap restart, on a finger instead of a unit. Same guardrails —
        `force` is deliberately NOT exposed, so this can never yank a receiver
        off something somebody is listening to."""
        if not (self.receiver_api and self.now_dir):
            return {"ok": False, "message": "no receiver configured"}
        from netradio import resume as resume_mod
        msg = resume_mod.resume(self.receiver_api, self.now_dir, self.wake_url, force=False)
        return {"ok": msg.startswith("resumed"), "message": msg}

    # -- reads
    def pandora(self) -> dict:
        """What the receiver has played on Pandora (netradio pandora writes
        <config>/pandora.jsonl): artists per station, thumbed first, marked
        by whether the library has them. The list to grow the library from."""
        from netradio.pandora import read_log, summarise
        records = read_log(self.cfg.root / "pandora.jsonl")
        return {"stations": summarise(records, set(self.cfg.artists())), "plays": len(records)}

    def state(self) -> dict:
        feeds = self.cfg.feeds()
        stations = self.cfg.stations()
        listenable = {s.get("feed") for s in stations if s.get("kind") == "specialty"}
        for fid, f in feeds.items():
            f["listenable"] = fid in listenable
        artists = self.cfg.artists()
        pending = sorted(p.name for p in (self.cfg.root / "requests").glob("*.json")) if (self.cfg.root / "requests").exists() else []
        today = dt.date.today()
        picks = self.cfg.picks()
        todays = {}
        for s in stations:
            if s.get("kind") == "specialty":
                continue
            items = []
            for a, b, slot in sched.slots_today(s["mount"], self.cfg.schedule(), today):
                r, _ = sched.resolve(slot, today, picks, station=s, artists=artists, feeds=feeds)
                items.append({"start": a.strftime("%H:%M"), "end": b.strftime("%H:%M"), "name": r.get("name"),
                              "kind": r.get("kind"), "value": r.get("feed") or r.get("artist"), "id": slot.get("id")})
            todays[s["mount"]] = items
        return {"feeds": feeds, "stations": stations, "schedule": self.cfg.schedule(),
                "artists": {a: {"tracks": i["tracks"], "families": i["families"]} for a, i in artists.items()},
                "families": FAMILIES, "pending_requests": pending, "today": todays,
                "now": dt.datetime.now().strftime("%Y-%m-%d %H:%M")}

    def options(self, mount: str) -> dict:
        station = next((s for s in self.cfg.stations() if s["mount"] == mount), None)
        fam = (station or {}).get("family")
        feeds = self.cfg.feeds()
        artists = self.cfg.artists()
        return {
            "feeds": [{"id": fid, "title": f.get("title", fid), "count": f.get("count", 0)}
                      for fid, f in sorted(feeds.items()) if f.get("status") == "ready" and compatible(fam, f.get("family"))],
            "artists": [{"name": a, "tracks": i["tracks"]}
                        for a, i in sorted(artists.items(), key=lambda kv: -kv[1]["tracks"])
                        if i["tracks"] >= sched.MIN_TRACKS_FOR_SPOTLIGHT and compatible(fam, i.get("families"))],
        }

    # -- writes
    def add_feed(self, body: dict) -> dict:
        title = str(body.get("title") or "").strip()
        if not title:
            raise ValueError("a title is required")
        feeds = self.cfg.feeds()
        fid = new_id(title, set(feeds))
        feeds[fid] = {"title": title, "description": str(body.get("description") or "").strip(),
                      "family": body.get("family") or [], "status": "pending", "rule": None, "count": 0,
                      "shellac": bool(body.get("shellac", False)), "note": "waiting for the compile step"}
        self.cfg.save_feeds(feeds)
        self.cfg.request("compile", {"feed": fid})
        if body.get("listenable", True):
            self._set_listenable(fid, True)
        return {"id": fid}

    def update_feed(self, fid: str, body: dict) -> dict:
        feeds = self.cfg.feeds()
        if fid not in feeds:
            raise KeyError(fid)
        f = feeds[fid]
        recompile = False
        if "title" in body:
            f["title"] = str(body["title"]).strip() or f["title"]
        if "description" in body and str(body["description"]).strip() != f.get("description", ""):
            f["description"] = str(body["description"]).strip()
            recompile = "rule" not in body
        if "family" in body:
            fam = body["family"] or []
            if any(x not in FAMILIES for x in fam):
                raise ValueError(f"family must be from {FAMILIES}")
            f["family"] = fam
        if "shellac" in body:
            f["shellac"] = bool(body["shellac"])
        if "rule" in body and body["rule"] is not None:
            errs = feedrules.validate(body["rule"])
            if errs:
                raise ValueError("; ".join(errs))
            f["rule"] = body["rule"]
            f["status"] = "ready"
            f["note"] = "rule edited by hand"
        self.cfg.save_feeds(feeds)
        if "listenable" in body:
            self._set_listenable(fid, bool(body["listenable"]))
        if recompile:
            f["status"] = "pending"
            self.cfg.save_feeds(feeds)
            self.cfg.request("compile", {"feed": fid})
        else:
            self.cfg.request("apply", {"reason": f"feed {fid} edited"})
        return {"ok": True, "recompile": recompile}

    def compile_feed(self, fid: str) -> dict:
        feeds = self.cfg.feeds()
        if fid not in feeds:
            raise KeyError(fid)
        feeds[fid]["status"] = "pending"
        feeds[fid]["note"] = "waiting for the compile step"
        self.cfg.save_feeds(feeds)
        self.cfg.request("compile", {"feed": fid})
        return {"ok": True}

    def delete_feed(self, fid: str) -> dict:
        feeds = self.cfg.feeds()
        feeds.pop(fid, None)
        self.cfg.save_feeds(feeds)
        self._set_listenable(fid, False)
        self.cfg.save_schedule([s for s in self.cfg.schedule() if s.get("feed") != fid])
        self.cfg.request("apply", {"reason": f"feed {fid} removed"})
        return {"ok": True}

    def _set_listenable(self, fid: str, on: bool) -> None:
        stations = self.cfg.stations()
        have = [s for s in stations if s.get("kind") == "specialty" and s.get("feed") == fid]
        if on and not have:
            feed = self.cfg.feeds().get(fid, {})
            mount = fid if is_mount(fid) else new_id(fid, {s["mount"] for s in stations})
            if any(s["mount"] == mount for s in stations):
                mount = new_id(fid, {s["mount"] for s in stations})
            stations.append({"mount": mount, "name": feed.get("title", fid), "kind": "specialty", "feed": fid,
                             "family": feed.get("family") or []})
        elif not on and have:
            stations = [s for s in stations if s not in have]
        else:
            return
        self.cfg.save_stations(stations)

    def update_station(self, mount: str, body: dict) -> dict:
        stations = self.cfg.stations()
        st = next((s for s in stations if s["mount"] == mount), None)
        if st is None:
            raise KeyError(mount)
        if "name" in body:
            st["name"] = str(body["name"]).strip() or st["name"]
        if "family" in body:
            fam = body["family"] or []
            if any(x not in FAMILIES for x in fam):
                raise ValueError(f"family must be from {FAMILIES}")
            st["family"] = fam
        if "breaks_every" in body:
            v = body["breaks_every"]
            if isinstance(v, str) and re.match(r"^\d+-\d+$", v):
                st["breaks_every"] = v
            else:
                try:
                    n = int(v)
                except (TypeError, ValueError):
                    raise ValueError("breaks_every must be a whole number (0 = never) or a range like 3-4")
                if n < 0 or n > 50:
                    raise ValueError("breaks_every must be 0..50")
                st["breaks_every"] = n
        if "excursion" in body and st.get("kind") != "specialty":
            try:
                pct = float(body["excursion"])
            except (TypeError, ValueError):
                raise ValueError("excursion must be a percentage, 0-50")
            if not 0 <= pct <= 50:
                raise ValueError("excursion must be 0..50 (percent of base picks from the fringe)")
            st["excursion"] = round(pct / 100, 3)
        if "base" in body and st.get("kind") != "specialty":
            errs = feedrules.validate(body["base"] or {})
            if errs:
                raise ValueError("; ".join(errs))
            st["base"] = body["base"]
        self.cfg.save_stations(stations)
        self.cfg.request("apply", {"reason": f"station {mount} edited"})
        return {"ok": True}

    def save_schedule(self, slots: list) -> dict:
        if not isinstance(slots, list):
            raise ValueError("schedule must be a list")
        mounts = {s["mount"] for s in self.cfg.stations() if s.get("kind") != "specialty"}
        feeds = self.cfg.feeds()
        seen: set[str] = set()
        clean = []
        for s in slots:
            if not isinstance(s, dict):
                raise ValueError("each slot must be an object")
            if s.get("station") not in mounts:
                raise ValueError(f"unknown curated station {s.get('station')!r}")
            kind = s.get("kind")
            if kind not in ("feed", "artist", "auto"):
                raise ValueError(f"slot kind must be feed, artist or auto, not {kind!r}")
            if kind == "feed" and s.get("feed") not in feeds:
                raise ValueError(f"unknown feed {s.get('feed')!r}")
            if kind == "artist" and not s.get("artist"):
                raise ValueError("an artist slot needs an artist")
            if kind == "auto" and s.get("like") not in ("feed", "artist"):
                raise ValueError("an auto slot needs like: feed or artist")
            if sched.slot_window({**s, "days": "daily"}, dt.date.today()) is None:
                raise ValueError(f"bad start time {s.get('start')!r} (HH:MM)")
            minutes = int(s.get("minutes") or 60)
            if not 15 <= minutes <= 720:
                raise ValueError("minutes must be 15..720")
            sid = s.get("id") or new_id(f"{s['station']}-{s.get('name') or kind}", seen)
            while sid in seen:
                sid = new_id(sid, seen)
            seen.add(sid)
            clean.append({**s, "id": sid, "minutes": minutes})
        self.cfg.save_schedule(clean)
        return {"ok": True, "count": len(clean)}

    def apply(self) -> dict:
        self.cfg.request("apply", {"reason": "requested from the admin page"})
        return {"ok": True}

    # -- listener feedback (2026-09-17): skip, never again, less of, requests
    def _inbox(self, mount: str, payload: dict) -> dict:
        if not self.dj_dir:
            raise ValueError("no DJ directory configured")
        inbox = self.dj_dir / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        name = f"{mount}-{int(dt.datetime.now().timestamp() * 1000)}-{next(self._inbox_n):04d}.json"
        write_atomic(inbox / name, json.dumps(payload))
        return {"ok": True, "queued": name}

    def skip(self, mount: str) -> dict:
        return self._inbox(mount, {"action": "skip"})

    def request(self, mount: str, body: dict) -> dict:
        path = str(body.get("path") or "")
        if not self.in_library(path):
            raise ValueError("not a library track")
        # deliberately no requester name: nothing in the app sends one, and an
        # unauthenticated free-text field would let anyone on the tailnet put a
        # name in the DJ's mouth (2026-09-25)
        return self._inbox(mount, {"action": "request", "path": path})

    def feedback(self, body: dict) -> dict:
        """One gesture from the listener.

            {"kind": "skip",  "path":…, "artist":…, "title":…, "mount":…}
            {"kind": "heart", "path":…, "on": true|false, "artist":…, "title":…}

        Skip and "less of this" are one press (2026-09-27): skipping a
        track both moves the station on and counts against it, and four skips
        retire it without anything being declared "never".

        The heart is a TOGGLE, not a tally — "it's not cumulative… heart tracks
        play more often, that's all". A flag cannot be run up the way a counter
        can, so a favourite cannot be pressed twenty times into unbalancing a
        station. It is stored locally whether or not Jellyfin is configured, and
        mirrored there when it is.
        """
        kind = str(body.get("kind") or "")
        if kind not in ("skip", "heart"):
            raise ValueError('kind must be "skip" or "heart"')
        path = str(body.get("path") or "")
        artist, title = str(body.get("artist") or ""), str(body.get("title") or "")

        if kind == "heart":
            if not path:
                raise ValueError("path required")
            return jellyfin.toggle(self.cfg, self._jellyfin(), path,
                                   bool(body.get("on", True)), artist, title)

        store = self.cfg.ratings()
        ratings.record_skip(store, path, artist, title)
        self.cfg.save_ratings(store)
        out = {"kind": "skip", "path": path, "skips": ratings.skips(store, path),
               "heart": ratings.hearted(store, path),
               "out_of_rotation": ratings.weight(store, path) == 0.0}
        mount = str(body.get("mount") or "")
        if mount:
            out.update(self._inbox(mount, {"action": "skip"}))
        return out

    def heart_state(self, path: str) -> dict:
        """Whether a track is hearted, for the page to light its button."""
        store = self.cfg.ratings()
        return {"path": path, "heart": ratings.hearted(store, path),
                "skips": ratings.skips(store, path)}

    def _jellyfin(self):
        """The Jellyfin mirror, or None when it is not configured — in which
        case the heart still works and simply stays local.

        `jellyfin_hearts` is separate from having a URL and key, because artist
        artwork wants the same credentials without implying that favourites
        should be mirrored. Connecting Jellyfin for pictures must not quietly
        start writing to somebody's library.
        """
        if not (self.jellyfin_url and self.jellyfin_key and self.jellyfin_hearts):
            return None
        return jellyfin.Jellyfin(self.jellyfin_url, self.jellyfin_key, self.jellyfin_user)

    def dislike(self, body: dict) -> dict:
        """{"path": …, "scope": "track"|"artist", "title": …, "artist": …}.
        A track dislike keeps that file off every station from the next scan
        (and the DJ stops choosing it at once); an artist dislike plays that
        artist a quarter as often."""
        d = self.cfg.dislikes()
        when = dt.datetime.now().isoformat(timespec="minutes")
        path, scope = str(body.get("path") or ""), body.get("scope", "track")
        if scope == "artist":
            artist = str(body.get("artist") or "").strip().lower()
            if not artist:
                raise ValueError("artist required")
            d.setdefault("artists", {})[artist] = {"when": when, "title": body.get("title", "")}
        else:
            if not path:
                raise ValueError("path required")
            d.setdefault("tracks", {})[path] = {"when": when, "artist": body.get("artist", ""), "title": body.get("title", "")}
        self.cfg.save_dislikes(d)
        if scope != "artist" and body.get("mount"):
            self._inbox(str(body["mount"]), {"action": "skip"})
        return {"ok": True, "tracks": len(d.get("tracks", {})), "artists": len(d.get("artists", {}))}

    def undislike(self, body: dict) -> dict:
        d = self.cfg.dislikes()
        (d.get("artists", {}) if body.get("scope") == "artist" else d.get("tracks", {})).pop(str(body.get("key") or ""), None)
        self.cfg.save_dislikes(d)
        return {"ok": True}

    def art(self, path: str) -> tuple[bytes, str] | None:
        """Cover art for a library track: the file's embedded picture, else a
        cover/folder image beside it, else the artist's picture from Jellyfin.
        (bytes, mime) or None."""
        # A station's own picture is not a library track, but it is ours and it
        # sits in a directory we were told about.
        if self.station_art is not None and self._under(path, self.station_art):
            data = Path(path).read_bytes()
            return data, "image/png" if path.lower().endswith(".png") else "image/jpeg"
        # in_library(), not `path not in set(self.library())`, which rebuilt a
        # 22,857-entry set on every call. #361 claimed this fix and applied it
        # one method too early — to request(), which is not the hot path.
        if not self.in_library(path):
            raise ValueError("not a library track")
        # A picture named after the track and sitting beside it wins over
        # everything: it is the most specific thing anyone can say about what
        # this one track looks like, and the only way to correct a wrong
        # embedded cover without rewriting the file (2026-09-29).
        for fp in sidecar_names(path):
            if os.access(fp, os.R_OK):
                return fp.read_bytes(), _image_type(fp.name)
        try:
            import mutagen
            f = mutagen.File(path)
        except Exception:
            f = None
        if f is not None:
            tags = getattr(f, "tags", None) or {}
            for key in list(tags.keys()):
                if key.startswith("APIC"):                       # mp3
                    pic = tags[key]
                    return bytes(pic.data), ("image/jpeg" if (pic.mime or "").lower() in ("", "image/jpg") else pic.mime)
            covr = tags.get("covr") if hasattr(tags, "get") else None
            if covr:                                             # m4a
                data = covr[0]
                return bytes(data), "image/png" if bytes(data[:4]) == b"\x89PNG" else "image/jpeg"
            for pic in getattr(f, "pictures", []) or []:         # flac
                return bytes(pic.data), pic.mime or "image/jpeg"
        folder = Path(path).parent
        for name in COVER_NAMES:
            fp = folder / name
            if os.access(fp, os.R_OK):
                return fp.read_bytes(), _image_type(name)
        # Nothing on disk. Jellyfin knows what a lot of these artists look like
        # even when the album has no sleeve — 502 of 1,668 artists had a picture
        # when this was added, which lifted cover coverage from 79% to 84% and
        # put faces on the ones that actually come round often: Bill Monroe, the
        # Stanley Brothers, the Louvins (2026-09-28).
        return self.artist_art(path)

    @staticmethod
    def _under(path: str, root: Path) -> bool:
        """Is this path inside the station-art directory?

        Normalised LEXICALLY, not resolved. `Path.resolve()` follows symlinks,
        and the pictures are installed as symlinks into the Nix store — so
        resolving turned /var/lib/netradio/config/art/rain.jpg into
        /nix/store/…-rain.jpg, which is not under the art directory, and every
        station picture came back 400 (2026-09-28). normpath still collapses
        `..`, so nothing escapes the directory that way; the directory itself
        is root-created and service-owned, and what is in it was put there by
        the module.
        """
        try:
            p = os.path.normpath(os.path.abspath(path))
            r = os.path.normpath(os.path.abspath(root))
            return p == r or p.startswith(r + os.sep)
        except (OSError, ValueError):
            return False

    def watching(self) -> dict:
        """What is playing on a screen in the house, from Jellyfin.

        Gated on url+key only, NOT on `jellyfin_hearts`: that flag is about
        permission to WRITE favourites into somebody's library, and asking what
        is on the television writes nothing. Same gate the artwork below uses,
        for the same reason.
        """
        if not (self.jellyfin_url and self.jellyfin_key):
            return {"playing": False}
        jf = jellyfin.Jellyfin(self.jellyfin_url, self.jellyfin_key, self.jellyfin_user)
        try:
            return jf.now_playing() or {"playing": False}
        except Exception as e:
            # A media server being down is not this page failing.
            log.debug("watching: %s", e)
            return {"playing": False}

    def watching_art(self, item: str) -> tuple[bytes, str] | None:
        """The poster for what is playing, proxied — the page cannot hold the
        API key, so it asks us and we ask Jellyfin."""
        if not (self.jellyfin_url and self.jellyfin_key and is_item_id(item)):
            return None
        try:
            req = urllib.request.Request(
                f"{self.jellyfin_url.rstrip('/')}/Items/{item}/Images/Primary?maxHeight=700",
                headers={"X-Emby-Token": self.jellyfin_key})
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read(), r.headers.get("Content-Type") or "image/jpeg"
        except Exception as e:
            log.debug("no poster for %s: %s", item, e)
            return None

    def artist_art(self, path: str) -> tuple[bytes, str] | None:
        """The artist's picture from Jellyfin, for a track whose album has no
        sleeve. The library is laid out .../Music/<Artist>/<Album>/<track>."""
        if not (self.jellyfin_url and self.jellyfin_key):
            return None
        parts = Path(path).parts
        if len(parts) < 3:
            return None
        item = self._jellyfin_artist_id(parts[-3])
        if not item:
            return None
        try:
            req = urllib.request.Request(
                f"{self.jellyfin_url.rstrip('/')}/Items/{item}/Images/Primary?maxHeight=700",
                headers={"X-Emby-Token": self.jellyfin_key})
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read(), r.headers.get("Content-Type") or "image/jpeg"
        except Exception as e:
            log.debug("no Jellyfin picture for %s: %s", parts[-3], e)
            return None

    def _jellyfin_artist_id(self, name: str) -> str:
        """Artists that HAVE a picture, by normalised name. Fetched once an hour:
        it is one request for the whole library, against a name we would
        otherwise have to search for per track."""
        now = time.time()
        if self._jf_artists is None or now - self._jf_artists_at > 3600:
            self._jf_artists = {}
            try:
                req = urllib.request.Request(
                    f"{self.jellyfin_url.rstrip('/')}/Artists?Recursive=true&Limit=5000&Fields=ImageTags",
                    headers={"X-Emby-Token": self.jellyfin_key})
                with urllib.request.urlopen(req, timeout=60) as r:
                    for it in (json.loads(r.read()).get("Items") or []):
                        if (it.get("ImageTags") or {}).get("Primary") and it.get("Name"):
                            self._jf_artists[_fold(it["Name"])] = it["Id"]
                log.info("Jellyfin: %d artists with a picture", len(self._jf_artists))
            except Exception as e:
                log.warning("could not list Jellyfin artists: %s", e)
            self._jf_artists_at = now
        return self._jf_artists.get(_fold(name), "")

    # ---- the thumbnail cache --------------------------------------------
    # A miss costs a mutagen decode of the whole track plus a Pillow resize;
    # a hit is one read(). The page asks for up to four covers per station and
    # the rail asks again at a smaller size, so on a first visit this is ~150
    # requests — which is what made the wall take ~17 s on a LAN.
    #
    # MISSES ARE CACHED TOO. "This track has no artwork" is the expensive
    # answer: it means mutagen read the entire file (some are 29 MB) and found
    # nothing, and without a negative entry that happens again on every load.

    def _thumb_path(self, path: str, size: int) -> Path | None:
        if self.thumb_cache is None:
            return None
        key = hashlib.sha256(f"{path}\0{size}\0{art_stamp(path)}".encode()).hexdigest()
        return self.thumb_cache / key[:2] / key

    def cached_art(self, path: str, size: int) -> tuple[bytes, str] | None:
        """`art()` plus the resize, memoised on disk. None means no artwork."""
        cache = self._thumb_path(path, size)
        if cache is not None:
            try:
                if cache.with_suffix(".none").exists():
                    return None                       # remembered miss
                return cache.read_bytes(), "image/jpeg"
            except OSError:
                pass                                  # not cached yet, or unreadable: fall through

        try:
            got = self.art(path)
        except OSError as e:                          # unreadable file or cover
            log.warning("art %s: %s", path, e)
            got = None
        if got and size:
            got = thumbnail(got[0], size)

        if cache is not None:
            self._thumb_store(cache, got)
        return got

    def _thumb_store(self, cache: Path, got: tuple[bytes, str] | None) -> None:
        """Write the entry, atomically so a half-written file is never served."""
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            target = cache if got else cache.with_suffix(".none")
            tmp = target.with_name(target.name + f".tmp{os.getpid()}")
            tmp.write_bytes(got[0] if got else b"")
            tmp.replace(target)
        except OSError as e:
            log.warning("could not cache %s: %s", cache, e)
            return
        if next(self._thumb_writes) % 200 == 199:
            self._thumb_prune()

    def _thumb_prune(self) -> None:
        """Keep the cache bounded. Entries are never invalidated in place — a
        changed cover just hashes to a new key — so the old ones have to be
        swept up eventually."""
        if self.thumb_cache is None:
            return
        try:
            files = [f for f in self.thumb_cache.rglob("*") if f.is_file()]
        except OSError:
            return
        if len(files) <= self.thumb_cache_max:
            return
        files.sort(key=lambda f: f.stat().st_mtime if f.exists() else 0)
        drop = files[: len(files) - int(self.thumb_cache_max * 0.8)]
        for f in drop:
            try:
                f.unlink()
            except OSError:
                pass
        log.info("thumbnail cache: pruned %d of %d entries", len(drop), len(files))

    def library(self) -> list[str]:
        if not self.playlists:
            return []
        f = self.playlists / "library.m3u"
        try:
            mtime = f.stat().st_mtime
        except OSError:
            return []
        if self._library is None or mtime != self._library_mtime:
            self._library = [l.strip() for l in f.read_text().splitlines() if l.strip() and not l.startswith("#")]
            # `art` asks "is this a library track?" on every request, and
            # `set(self.library())` rebuilt a 22,857-entry set each time.
            self._library_set = frozenset(self._library)
            self._library_mtime = mtime
        return self._library

    def in_library(self, path: str) -> bool:
        self.library()                      # refresh if library.m3u changed
        return path in self._library_set

    def search(self, q: str, limit: int = 30) -> list[dict]:
        """Library tracks whose artist/album/file name carry every word of q."""
        words = [w for w in q.lower().split() if w]
        if not words:
            return []
        out = []
        for path in self.library():
            rel = path.split("/Music/")[-1] if "/Music/" in path else path
            low = rel.lower()
            if all(w in low for w in words):
                parts = rel.split("/")
                out.append({"path": path, "artist": parts[0] if len(parts) > 2 else "", "album": parts[-2] if len(parts) > 2 else "",
                            "title": re.sub(r"^\d+[\s.-]+", "", parts[-1].rsplit(".", 1)[0])})
                if len(out) >= limit:
                    break
        return out


def _image_type(name: str) -> str:
    """The content type for a picture filename. Only the handful of suffixes
    this module will serve; anything else is treated as JPEG, which is what the
    library is full of."""
    low = name.lower()
    if low.endswith(".png"):
        return "image/png"
    if low.endswith(".webp"):
        return "image/webp"
    return "image/jpeg"


def art_stamp(path: str) -> str:
    """What a cached thumbnail actually depends on: the track itself (an
    embedded picture), a picture named after it, and any cover file beside it.
    All stat()s, so this is cheap enough to do on every request — and it is
    EXACT, unlike keying on the track's mtime alone, which would serve a stale
    thumbnail forever after someone dropped a new cover.jpg into the folder.

    The sidecar belongs here for the same reason, and more urgently: the whole
    point of it is that somebody adds a picture to a library that already has
    thumbnails cached for every track in it. Left out, the new picture would
    not appear until the cache was pruned (2026-09-29).
    """
    parts: list[str] = []
    try:
        st = os.stat(path)
        parts.append(f"t:{st.st_mtime_ns}:{st.st_size}")
    except OSError:
        parts.append("t:-")
    for fp in sidecar_names(path):
        try:
            st = os.stat(fp)
            parts.append(f"s:{fp.suffix}:{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            pass
    folder = Path(path).parent
    for name in COVER_NAMES:
        try:
            st = os.stat(folder / name)
            parts.append(f"{name}:{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            pass
    return "|".join(parts)


def thumbnail(data: bytes, size: int) -> tuple[bytes, str]:
    """Square-ish JPEG thumbnail, `size` px on the long edge; the original
    if Pillow can't read it."""
    try:
        from io import BytesIO
        from PIL import Image
        im = Image.open(BytesIO(data))
        im.thumbnail((max(64, min(size, 1024)),) * 2)
        out = BytesIO()
        im.convert("RGB").save(out, "JPEG", quality=82, optimize=True)
        return out.getvalue(), "image/jpeg"
    except Exception:
        return data, "image/jpeg"


def make_handler(admin: Admin):
    class Handler(BaseHTTPRequestHandler):
        def _json(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def _reply(self, code: int, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _route(self, method: str):
            u = urlparse(self.path)
            parts = [p for p in u.path.split("/") if p]
            q = parse_qs(u.query)
            try:
                # Devices first, and NOT under LOCK. A device call is network
                # I/O to another machine; holding the global lock across it
                # would park every other admin request behind an unreachable
                # speaker until its timeout. Nothing here touches shared state.
                if parts[:2] == ["api", "devices"]:
                    got = self._devices(parts[2:], method)
                    if got is not None:
                        return self._reply(*got)
                with LOCK:
                    if parts == ["api", "state"] and method == "GET":
                        return self._reply(200, admin.state())
                    if parts == ["api", "watching"] and method == "GET":
                        return self._reply(200, admin.watching())
                    if parts == ["api", "options"] and method == "GET":
                        return self._reply(200, admin.options((q.get("station") or [""])[0]))
                    if parts == ["api", "feeds"] and method == "POST":
                        return self._reply(201, admin.add_feed(self._json()))
                    if len(parts) == 3 and parts[:2] == ["api", "feeds"]:
                        if method == "PUT":
                            return self._reply(200, admin.update_feed(parts[2], self._json()))
                        if method == "DELETE":
                            return self._reply(200, admin.delete_feed(parts[2]))
                    if len(parts) == 4 and parts[:2] == ["api", "feeds"] and parts[3] == "compile" and method == "POST":
                        return self._reply(200, admin.compile_feed(parts[2]))
                    if len(parts) == 4 and parts[:2] == ["api", "feeds"] and parts[3] == "answer" and method == "GET":
                        return self._reply(200, {"answer": admin.cfg.answer(parts[2])})
                    if len(parts) == 3 and parts[:2] == ["api", "stations"] and method == "PUT":
                        return self._reply(200, admin.update_station(parts[2], self._json()))
                    if parts == ["api", "schedule"] and method == "PUT":
                        return self._reply(200, admin.save_schedule(self._json()))
                    if len(parts) == 4 and parts[:2] == ["api", "dj"] and parts[3] == "skip" and method == "POST":
                        return self._reply(200, admin.skip(parts[2]))
                    if len(parts) == 4 and parts[:2] == ["api", "dj"] and parts[3] == "request" and method == "POST":
                        return self._reply(200, admin.request(parts[2], self._json()))
                    if parts == ["api", "feedback"] and method == "POST":
                        return self._reply(200, admin.feedback(self._json()))
                    if parts == ["api", "heart"] and method == "GET":
                        return self._reply(200, admin.heart_state((q.get("path") or [""])[0]))
                    if parts == ["api", "resume"] and method == "POST":
                        return self._reply(200, admin.resume_receiver())
                    if parts == ["api", "dislike"] and method == "POST":
                        return self._reply(200, admin.dislike(self._json()))
                    if parts == ["api", "dislike"] and method == "DELETE":
                        return self._reply(200, admin.undislike(self._json()))
                    if parts == ["api", "dislikes"] and method == "GET":
                        return self._reply(200, admin.cfg.dislikes())
                    if parts == ["api", "pandora"] and method == "GET":
                        return self._reply(200, admin.pandora())
                    if parts == ["api", "art"] and method == "GET":
                        qs = parse_qs(u.query)
                        path = qs.get("path", [""])[0]
                        raw = qs.get("size", [""])[0]
                        # a thumbnail for the tiles (a full cover can be 600 KB)
                        size = int(raw) if raw.isdigit() else 0
                        got = admin.cached_art(path, size)
                        if not got:
                            return self._reply(404, {"error": "no art"})
                        data, mime = got
                        self.send_response(200)
                        self.send_header("Content-Type", mime)
                        self.send_header("Cache-Control", "public, max-age=86400")
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                        return
                    if parts == ["api", "watching", "art"] and method == "GET":
                        got = admin.watching_art(parse_qs(u.query).get("item", [""])[0])
                        if not got:
                            return self._reply(404, {"error": "no poster"})
                        data, mime = got
                        self.send_response(200)
                        self.send_header("Content-Type", mime)
                        # Short, not a day: the poster changes when the episode
                        # does, and an episode is twenty minutes long.
                        self.send_header("Cache-Control", "public, max-age=300")
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                        return
                    if parts == ["api", "search"] and method == "GET":
                        q = parse_qs(u.query).get("q", [""])[0]
                        return self._reply(200, admin.search(q))
                    if parts == ["api", "apply"] and method == "POST":
                        return self._reply(200, admin.apply())
                return self._reply(404, {"error": "not found"})
            except KeyError as e:
                return self._reply(404, {"error": f"no such item: {e}"})
            except (ValueError, TypeError) as e:
                return self._reply(400, {"error": str(e)})
            except Exception as e:
                log.exception("%s %s failed", method, self.path)
                return self._reply(500, {"error": f"{type(e).__name__}: {e}"})

        # ---- the device registry, and the page's calls to one -------------
        def _devices(self, rest: list[str], method: str):
            """`/api/devices` and `/api/devices/<id>/<verb>`.

            Returns None when the path is not one of ours, so the normal
            routing carries on.
            """
            registry = _devices_of(admin)
            if rest == [] and method == "GET":
                return 200, devices.public(registry)
            if len(rest) != 2:
                return None
            dev_id, verb = rest
            try:
                device = devices.find(registry, dev_id)
            except KeyError:
                return 404, {"error": f"no such device: {dev_id}"}
            if method == "GET":
                if verb in ("state", device.get("statePath") or "state"):
                    return devices.state(device)
                if verb == "outputs":
                    return devices.outputs(device)
                return None
            if method == "POST":
                return devices.call(device, verb, self._json())
            return None

        def do_GET(self): self._route("GET")
        def do_POST(self): self._route("POST")
        def do_PUT(self): self._route("PUT")
        def do_DELETE(self): self._route("DELETE")

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

    return Handler


def _devices_of(admin) -> list[dict]:
    """The registry, re-read on use. A list, in declared order."""
    p = getattr(admin, "devices_file", None)
    if not p:
        return []
    try:
        got = json.loads(Path(p).read_text())
    except (OSError, ValueError) as e:
        log.warning("cannot read the device registry %s (%s)", p, e)
        return []
    return got if isinstance(got, list) else []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", required=True, type=Path)
    ap.add_argument("--receiver-api", default="", help="the receiver API, for the page's resume button")
    ap.add_argument("--now-dir", type=Path, help="where receiver.json lives (for resume)")
    ap.add_argument("--wake", default="", help="the wake service, so resume can start the encoder first")
    ap.add_argument("--jellyfin-url", default="", help="mirror hearts to this Jellyfin (optional)")
    ap.add_argument("--jellyfin-key-file", type=Path, help="a file holding the Jellyfin API key")
    ap.add_argument("--jellyfin-user", default="", help="Jellyfin user id (default: the first admin)")
    ap.add_argument("--jellyfin-hearts", action="store_true",
                    help="mirror the heart to Jellyfin favourites (artwork needs no such permission)")
    ap.add_argument("--station-art", type=Path, help="directory of <mount>.jpg pictures for stations")
    ap.add_argument("--thumb-cache", type=Path,
                    help="directory for resized cover art; without it every thumbnail is recomputed")
    ap.add_argument("--thumb-cache-max", type=int, default=5000,
                    help="entries to keep before pruning the oldest")
    ap.add_argument("--listen", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8012)
    ap.add_argument("--devices", type=Path, dest="devices_file",
                    help="JSON list of registered devices (id, name, endpoint, capabilities, tokenFile)")
    ap.add_argument("--dj-dir", type=Path, help="the DJ's state dir (inbox/ for skip + request)")
    ap.add_argument("--playlists", type=Path, help="playlists dir (library.m3u for search)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s", stream=sys.stdout)
    srv = ThreadingHTTPServer((args.listen, args.port), make_handler(
        Admin(Config(args.config), args.dj_dir, args.playlists,
              receiver_api=args.receiver_api, now_dir=args.now_dir, wake_url=args.wake,
              jellyfin_url=args.jellyfin_url,
              jellyfin_key=jellyfin.read_key(key_file=args.jellyfin_key_file),
              jellyfin_user=args.jellyfin_user,
              thumb_cache=args.thumb_cache, thumb_cache_max=args.thumb_cache_max,
              station_art=args.station_art, jellyfin_hearts=args.jellyfin_hearts,
              devices_file=args.devices_file)))
    log.info("admin API on %s:%d, config %s", args.listen, args.port, args.config)
    srv.serve_forever()
    return 0
