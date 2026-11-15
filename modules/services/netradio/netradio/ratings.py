"""What the listener thinks of a track, and what that does to how often it plays.

Two gestures, and they are deliberately not symmetrical.

  SKIP    skips the track now AND counts against it. Pressed once it is a
          shrug; pressed on the same track repeatedly it stops coming back.
          Nothing has to be declared "never" for that to happen.
  HEART   a TOGGLE, not a tally, and it lives in Jellyfin. A hearted track
          plays more often. That is all it does.

The owner settled the positive side on 2026-09-27: "It\'s not cumulative, it\'s just
a toggle. Heart tracks play more often. That\'s all. Simplify the heuristic and
[it] doesn\'t throw the station off balance if somebody clicks the thumb too many
times." A counter can be pressed twenty times; a flag cannot, so the boost is
bounded by construction rather than by a cap bolted on afterwards.

The weights:

    skips     0     1     2     3     4+
    weight    1    1/2   1/4   1/8   out

    hearted:  4x, whatever the skips say — the heart is the more deliberate
              gesture, and a track someone went and favourited should not be
              quietly suppressed because they skipped it one distracted evening

Skips are still a running count, because "less of this" only means anything if
it accumulates. There is no artist score here: `dislikes.json` already carries an
explicit "less of this artist", and a second, implicit artist penalty was a way
for one skipped song to quietly mute a whole catalogue.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from typing import Iterable

log = logging.getLogger("netradio.ratings")

EMPTY: dict = {"tracks": {}}

# Four skips and a track is out of rotation.
OUT = 4
# What a heart is worth. One number, not a curve — the whole point is that it
# cannot be run up.
HEART = 4.0


def now() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat(timespec="minutes")


def _entry(store: dict, path: str) -> dict:
    return store.setdefault("tracks", {}).setdefault(path, {"skips": 0, "heart": False})


def record_skip(store: dict, path: str, artist: str = "", title: str = "") -> dict:
    """Count a skip against a track."""
    if not path:
        return store
    e = _entry(store, path)
    e["skips"] = int(e.get("skips", 0)) + 1
    e["artist"], e["title"], e["when"] = artist or e.get("artist", ""), title or e.get("title", ""), now()
    return store


def set_heart(store: dict, path: str, on: bool, artist: str = "", title: str = "") -> dict:
    """Set or clear the heart. A toggle: calling it twice is the same as once.

    Un-hearting does not add a skip. Removing a heart says "not a favourite",
    which is a long way from "play this less", and inferring the stronger
    statement from the weaker one is how a system starts contradicting the
    person using it.
    """
    if not path:
        return store
    e = _entry(store, path)
    e["heart"] = bool(on)
    e["artist"], e["title"], e["when"] = artist or e.get("artist", ""), title or e.get("title", ""), now()
    return store


def hearted(store: dict, path: str) -> bool:
    return bool(((store.get("tracks") or {}).get(path) or {}).get("heart"))


def skips(store: dict, path: str) -> int:
    return int(((store.get("tracks") or {}).get(path) or {}).get("skips", 0))


def weight(store: dict, path: str) -> float:
    """How often this track should play relative to an untouched one. 0.0 means
    do not offer it."""
    if hearted(store, path):
        return HEART
    n = skips(store, path)
    if n >= OUT:
        return 0.0
    return 2.0 ** -n


def weights(store: dict, paths: Iterable[str]) -> list[float]:
    return [weight(store, p) for p in paths]


def pick(store: dict, paths: list[str], rng) -> str | None:
    """One path, chosen with the weights above.

    Falls back to an unweighted choice when every candidate weighs zero, which
    happens when a small station has been skipped out entirely: better to play
    something once skipped than to go silent, and the DJ\'s `recent` list still
    keeps it from repeating.
    """
    if not paths:
        return None
    ws = weights(store, paths)
    if not any(w > 0 for w in ws):
        log.info("every candidate is skipped out (%d of them); playing anyway", len(paths))
        return rng.choice(paths)
    total = math.fsum(ws)
    r = rng.random() * total
    upto = 0.0
    for p, w in zip(paths, ws):
        upto += w
        if upto >= r:
            return p
    return paths[-1]


def summary(store: dict) -> str:
    tracks = store.get("tracks") or {}
    hearts = sum(1 for e in tracks.values() if e.get("heart"))
    out = sum(1 for e in tracks.values() if int(e.get("skips", 0)) >= OUT and not e.get("heart"))
    return f"{len(tracks)} tracks known, {hearts} hearted, {out} skipped out"
