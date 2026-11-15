"""`netradio pandora` — write down what the receiver plays on Pandora.

The owner tunes a Pandora station that is what a library station ought to
sound like (Dinner Jazz Radio, 2026-09-19) and wants the library to grow
towards it. The receiver's Play_Info says station, track and album, and
whether the track was thumbed; this polls ync-api while Pandora is the
input and appends one JSON line per track to <config>/pandora.jsonl:

    {"at": "2026-09-19T17:09:54", "station": "Dinner Jazz Radio", "title": "The Man I Love",
     "artist": "Zoot Sims Quartet", "album": "That Old Feeling", "feedback": "Thumb Up", "seconds": 214}

Pandora folds the artist into the track text ("The Man I Love by Zoot
Sims Quartet") on this unit; it is split back out. A track is written
when the next one starts (so the thumb given during it is on the line),
and on shutdown. The admin page reads the file back as artists per
station, marked by whether the library already has them; the Lidarr
hand-off is the operator's (lidarr-fill.py, from that list).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import signal
import sys
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("netradio.pandora")

BY = re.compile(r"^(?P<title>.+?)\s+by\s+(?P<artist>[^()]+?)\s*$")


def split_track(track: str, artist: str) -> tuple[str, str]:
    """('title', 'artist') from Pandora's "Title by Artist" when the artist
    field is empty; a title with " by " in it and a real artist is left alone."""
    if artist:
        return track, artist
    m = BY.match(track or "")
    return (m.group("title"), m.group("artist")) if m else (track, "")


class Logger:
    def __init__(self, api: str, out: Path, timeout: float = 10.0):
        self.api = api.rstrip("/")
        self.out = out
        self.timeout = timeout
        self.current: dict | None = None
        self.started = 0.0

    def status(self) -> dict | None:
        try:
            with urllib.request.urlopen(f"{self.api}/status", timeout=self.timeout) as r:
                return json.load(r)
        except Exception as e:      # the unit in standby, ync-api restarting: try again next tick
            log.debug("status failed: %s", e)
            return None

    def observe(self, st: dict | None, now: float | None = None) -> None:
        """One poll. A track is written when it is replaced (with the thumb
        it got, if any) — and Pandora going away ends the current one."""
        now = now or time.time()
        np = (st or {}).get("now_playing") or {}
        playing = bool(st) and st.get("input") == "Pandora" and bool(np.get("track"))
        if not playing:
            self.flush(now)
            return
        title, artist = split_track(np.get("track", ""), np.get("artist", ""))
        key = (np.get("station", ""), title, artist, np.get("album", ""))
        if self.current is not None and self.current["_key"] == key:
            fb = np.get("feedback", "")
            if fb and fb != "---":
                self.current["feedback"] = fb
            return
        self.flush(now)
        self.current = {"_key": key, "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
                        "station": key[0], "title": title, "artist": artist, "album": key[3],
                        "feedback": (np.get("feedback") if np.get("feedback") not in ("", "---", None) else "")}
        self.started = now

    def flush(self, now: float | None = None) -> None:
        if self.current is None:
            return
        rec = {k: v for k, v in self.current.items() if not k.startswith("_")}
        rec["seconds"] = int((now or time.time()) - self.started)
        self.current = None
        if rec["seconds"] < 20:         # a skip, not a listen
            return
        try:
            self.out.parent.mkdir(parents=True, exist_ok=True)
            with self.out.open("a") as fh:
                fh.write(json.dumps(rec) + "\n")
            log.info("%s: %s — %s (%s)%s", rec["station"], rec["artist"], rec["title"], rec["album"],
                     f" [{rec['feedback']}]" if rec["feedback"] else "")
        except OSError as e:
            log.warning("could not write %s: %s", self.out, e)


def read_log(path: Path) -> list[dict]:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def summarise(records: list[dict], library_artists: set[str]) -> dict:
    """{station: {plays, artists: [{name, plays, thumbs, in_library, tracks: [..]}]}}
    — artists by plays, thumbed ones first."""
    norm = {a.lower(): a for a in library_artists}
    stations: dict[str, dict] = {}
    for r in records:
        st = stations.setdefault(r.get("station") or "Pandora", {"plays": 0, "artists": {}})
        st["plays"] += 1
        a = st["artists"].setdefault(r.get("artist") or "?", {"name": r.get("artist") or "?", "plays": 0, "thumbs": 0, "tracks": []})
        a["plays"] += 1
        if (r.get("feedback") or "").lower().startswith("thumb up"):
            a["thumbs"] += 1
        t = f"{r.get('title', '')} ({r.get('album', '')})" if r.get("album") else r.get("title", "")
        if t and t not in a["tracks"]:
            a["tracks"].append(t)
    for st in stations.values():
        arts = list(st["artists"].values())
        for a in arts:
            a["in_library"] = a["name"].lower() in norm
        st["artists"] = sorted(arts, key=lambda a: (-a["thumbs"], -a["plays"], a["name"]))
    return stations


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--api", default="http://127.0.0.1:8791", help="ync-api base URL")
    ap.add_argument("--out", required=True, type=Path, help="pandora.jsonl to append to")
    ap.add_argument("--interval", type=float, default=15.0)
    # This poller is already asking the receiver what it is doing every few
    # seconds, so it is also the natural place to remember it: a receiver that
    # has stopped reports no station, so the last one seen playing has to be
    # written down while it still can be (see netradio/resume.py).
    ap.add_argument("--receiver-state", type=Path,
                    help="write the receiver's state here for `netradio resume` (now/receiver.json)")
    ap.add_argument("--menu", type=Path,
                    help="YCast's stations.yml, to map a station name back to its mount")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    lg = Logger(args.api, args.out)
    stop = False

    def on_term(*_):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGTERM, on_term)
    log.info("watching %s for Pandora, writing %s", args.api, args.out)
    if args.receiver_state:
        log.info("remembering the receiver's station in %s", args.receiver_state)
    while not stop:
        st = lg.status()
        lg.observe(st)
        if args.receiver_state:
            try:
                from netradio import resume as resume_mod
                menu = args.menu.read_text() if args.menu and args.menu.exists() else ""
                resume_mod.save(args.receiver_state.parent,
                                resume_mod.observe(st, menu, resume_mod.load(args.receiver_state.parent)))
            except Exception:
                log.exception("could not write the receiver state")
        for _ in range(int(args.interval * 10)):
            if stop:
                break
            time.sleep(0.1)
    lg.flush()
    return 0
