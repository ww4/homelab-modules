"""The runtime configuration: feeds, stations, schedule — JSON files the owner
edits through the admin page, seeded once from the flake.

    <config>/feeds.json      {id: Feed}      specialty feeds (title, description,
                                             the rule that realises it, status)
    <config>/stations.json   [Station]       curated + specialty stations
    <config>/schedule.json   [Slot]          themed segments and spotlights
    <config>/picks.json      {slot: [...]}   the DJ's automatic picks, by date
    <config>/stations.yml    YCast's menu    written by the scanner
    <config>/artists.json    inventory       written by the scanner

Families are the compatibility vocabulary: a station declares which it is
(Classic Country → country), a feed which it fits, an artist gets one from
its tags. The admin page offers a station only the feeds and artists whose
families overlap its own; "any" matches everything.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

FAMILIES = ["bluegrass", "country", "folk", "rock", "blues-jazz", "gospel", "classical", "holiday", "any"]

# Genre words → family, for artists (majority of their tracks) and for the
# curated bases. Same vocabulary the stations were built on.
FAMILY_WORDS = {
    "bluegrass": ["bluegrass", "old time", "oldtime", "newgrass", "string band", "brother duets", "appalachian"],
    "country": ["country", "western swing", "honky tonk", "western", "cowboy", "early country"],
    "folk": ["folk", "singer songwriter", "celtic", "traditional", "americana", "acoustic", "irish", "cajun", "zydeco"],
    "rock": ["rock", "alternative", "pop", "punk", "metal", "indie", "new wave", "symphonic rock", "electronic", "dance"],
    "blues-jazz": ["blues", "jazz", "soul", "r&b", "funk", "big band", "swing", "motown", "fusion", "vocal", "easy listening"],
    "gospel": ["gospel", "religious", "christian", "hymns", "sacred", "spiritual", "lined singing", "rp psalms"],  # leak-scan-ok: a genre word, not a name
    "classical": ["classical", "baroque", "orchestral", "opera", "chamber", "original score", "soundtrack"],
    "holiday": ["holiday", "christmas", "xmas"],  # leak-scan-ok: a genre word, not a name
}

# What the audio classifier hears when a track is quintessentially of a
# family (YAMNet class means kept in the profile) — the second opinion on
# an artist beside their genre tags, for choosing spotlights.
FAMILY_SOUND = {
    "bluegrass": ["bluegrass"],
    "country": ["country"],
    "folk": ["folk"],
    "rock": ["rock_and_roll"],
    "blues-jazz": ["blues", "jazz", "swing", "soul", "rhythm_and_blues"],
    "gospel": ["gospel", "christian"],  # leak-scan-ok: a genre word, not a name
}


def compatible(a: list[str] | None, b: list[str] | None) -> bool:
    a = a or ["any"]
    b = b or ["any"]
    return "any" in a or "any" in b or bool(set(a) & set(b))


class Config:
    def __init__(self, root: Path):
        self.root = Path(root)

    # -- files
    def _read(self, name: str, default):
        try:
            return json.loads((self.root / name).read_text())
        except (OSError, ValueError):
            return default

    def _write(self, name: str, data) -> None:
        write_atomic(self.root / name, json.dumps(data, indent=1, sort_keys=True))

    # -- listener feedback (2026-09-17): what never plays again, and whose
    # tracks play less. Written by the admin API, read by the scanner (track
    # dislikes leave every pool) and the DJ (artist dislikes are weighted).
    def dislikes(self) -> dict:
        return self._read("dislikes.json", {"tracks": {}, "artists": {}})

    def save_dislikes(self, data: dict) -> None:
        self._write("dislikes.json", data)

    def ratings(self) -> dict:
        """The soft, accumulating opinion: skips and thumbs per track and
        artist (netradio/ratings.py). Beside dislikes.json, which is the
        explicit permanent no."""
        return self._read("ratings.json", {"tracks": {}, "artists": {}})

    def save_ratings(self, data: dict) -> None:
        self._write("ratings.json", data)

    def feeds(self) -> dict:
        return self._read("feeds.json", {})

    def save_feeds(self, feeds: dict) -> None:
        self._write("feeds.json", feeds)

    def keep_answer(self, feed_id: str, text: str) -> None:
        """The model's last raw answer for a feed, kept whole so what it
        said is readable even when the JSON carried no note."""
        (self.root / "answers").mkdir(exist_ok=True)
        write_atomic(self.root / "answers" / f"{feed_id}.txt", text)

    def answer(self, feed_id: str) -> str:
        try:
            return (self.root / "answers" / f"{feed_id}.txt").read_text()
        except OSError:
            return ""

    def stations(self) -> list[dict]:
        return self._read("stations.json", [])

    def save_stations(self, stations: list[dict]) -> None:
        self._write("stations.json", stations)

    def schedule(self) -> list[dict]:
        return self._read("schedule.json", [])

    def save_schedule(self, slots: list[dict]) -> None:
        self._write("schedule.json", slots)

    def picks(self) -> dict:
        return self._read("picks.json", {})

    def save_picks(self, picks: dict) -> None:
        self._write("picks.json", picks)

    def artists(self) -> dict:
        return self._read("artists.json", {})

    # -- seeding: only files that don't exist yet; the owner's edits are never overwritten
    def seed(self, feeds: dict | None, stations: list | None, schedule: list | None) -> list[str]:
        done = []
        for name, data in (("feeds.json", feeds), ("stations.json", stations), ("schedule.json", schedule)):
            if data is not None and not (self.root / name).exists():
                self._write(name, data)
                done.append(name)
        return done

    # -- requests to the privileged side (path units watch these)
    def request(self, kind: str, payload: dict | None = None) -> Path:
        d = self.root / "requests"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{kind}-{int(time.time() * 1000)}.json"
        write_atomic(p, json.dumps(payload or {}))
        return p


def write_atomic(path: Path, text: str, mode: int = 0o664) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def new_id(title: str, existing: set[str]) -> str:
    base = "".join(c if c.isalnum() else "-" for c in title.lower()).strip("-")
    base = "-".join(p for p in base.split("-") if p) or "feed"
    cand, i = base, 2
    while cand in existing:
        cand, i = f"{base}-{i}", i + 1
    return cand
