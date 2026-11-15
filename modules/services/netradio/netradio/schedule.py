"""Segments and spotlights: when a curated station plays something narrower
than its base, and what the DJ says about it.

A slot (schedule.json):

    {"id": "country-sat-swing", "station": "country", "name": "Western Swing Hour",
     "kind": "feed" | "artist" | "auto",
     "feed": "western-swing" | "artist": "George Jones" | "like": "feed" | "artist",
     "days": "daily" | "weekdays" | "weekends" | ["mon", "sat"],
     "start": "13:00", "minutes": 60}

`auto` slots are filled by the DJ once per day: something compatible with
the station that hasn't been picked for that slot recently (picks.json
remembers), so a spotlight the owner set up once keeps happening with a
different artist each time even when he sets nothing.

An auto artist spotlight goes to someone quintessential — the owner, after a
country spotlight landed on Michael Martin Murphey's one Christmas album:  # leak-scan-ok: a genre word, not a name
"choose artists which are quintessential and good representatives of the
genre and the station — and something we have plenty of". So the pick is
weighted by how much of the artist we own (tracks, across several albums),
how much of it is tagged into the station's family, and how much of it the
audio classifier hears as that family; the top of that list is drawn from,
not the whole roster.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import math
import random
import re

from netradio.config import compatible
from netradio.conventions import NOT_AN_ARTIST

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MIN_TRACKS_FOR_SPOTLIGHT = 8      # to be offered on the admin page at all
RECENT_PICKS = 6
# an AUTO spotlight wants more: plenty of them, across albums, mostly this family
AUTO_MIN_TRACKS = 24
AUTO_MIN_ALBUMS = 2
AUTO_MIN_SHARE = 0.5
AUTO_FULL_MARKS_TRACKS = 80       # depth stops counting past this
AUTO_SOUND_FULL = 0.15            # a classifier mean this high is unmistakably the family
AUTO_SHORTLIST = 25               # the pool the day's pick is drawn from
# a folder that is not an artist: a year, a label sampler, an unknown


def slot_days(spec) -> set[str]:
    if spec in (None, "", "daily"):
        return set(DAYS)
    if spec == "weekdays":
        return set(DAYS[:5])
    if spec == "weekends":
        return {"sat", "sun"}
    return {d.lower()[:3] for d in spec}


def slot_window(slot: dict, date: dt.date) -> tuple[dt.datetime, dt.datetime] | None:
    if DAYS[date.weekday()] not in slot_days(slot.get("days")):
        return None
    try:
        h, m = (int(x) for x in str(slot.get("start", "")).split(":"))
    except ValueError:
        return None
    start = dt.datetime.combine(date, dt.time(h, m))
    return start, start + dt.timedelta(minutes=int(slot.get("minutes") or 60))


def slots_today(station: str, slots: list[dict], date: dt.date) -> list[tuple[dt.datetime, dt.datetime, dict]]:
    out = []
    for s in slots:
        if s.get("station") != station:
            continue
        w = slot_window(s, date)
        if w:
            out.append((w[0], w[1], s))
    return sorted(out, key=lambda x: x[0])


def active_slot(station: str, slots: list[dict], now: dt.datetime) -> dict | None:
    for start, end, s in slots_today(station, slots, now.date()):
        if start <= now < end:
            return s
    return None


# --- auto picks ---------------------------------------------------------------

def playable_count(info: dict, era_rule: dict | None) -> int:
    """How many of an artist's tracks a station with this era rule may play.
    An inventory without era counts, or a station without an era rule,
    counts everything."""
    eras = info.get("eras")
    if not era_rule or not eras:
        return info.get("tracks", 0)
    from netradio.feeds import era_ok
    return sum(n for era, n in eras.items() if era_ok(era_rule, era))


def spotlight_score(info: dict, families: list[str] | None, era_rule: dict | None = None, mount: str = "") -> float:
    """How good a spotlight this artist makes for a station of these
    families, 0..1: depth (how much we own, log-scaled to full marks at
    AUTO_FULL_MARKS_TRACKS) times representativeness — mostly the share
    of their tracks tagged as the family (for an untagged artist, the
    share a feed vouches for: the Delmores are a brother duet), with
    what the classifier hears as a small bonus (its genre classes are
    weak: Hank Williams hears as "country" 0.03). 0 when they don't clear
    the bar. An inventory from before `share`/`sound` existed falls back
    to the family list alone."""
    fams = [f for f in (families or []) if f != "any"] or [f for f in info.get("families", []) if f != "unknown"]
    if mount and "stations" in info:
        # the station's own base rule already judged every track: how many it
        # would play is the depth, and what fraction of the artist that is,
        # the purity (Jimmy Martin: 0 of 151 for Classic Country)
        n = info["stations"].get(mount, 0)
        purity = n / max(1, info.get("tracks", 0))
    else:
        n = playable_count(info, era_rule)
        if "share" not in info:
            share = {f: 1.0 for f in info.get("families", [])}
        else:   # the tags; a feed's word only for an artist whose tags say nothing
            share = info["share"] or info.get("feed_share") or {}
        purity = max((share.get(f, 0.0) for f in fams), default=0.0)
    if n < AUTO_MIN_TRACKS or info.get("albums", AUTO_MIN_ALBUMS) < AUTO_MIN_ALBUMS:
        return 0.0
    if purity < AUTO_MIN_SHARE:
        return 0.0
    sound = info.get("sound") or {}
    heard = max((min(1.0, sound.get(f, 0.0) / AUTO_SOUND_FULL) for f in fams if f in sound), default=0.0)
    depth = min(1.0, math.log(n / (AUTO_MIN_TRACKS / 2)) / math.log(AUTO_FULL_MARKS_TRACKS / (AUTO_MIN_TRACKS / 2)))
    return round(depth * (0.85 * purity + 0.15 * heard), 3)


def spotlight_ranked(artists: dict, families: list[str] | None, era_rule: dict | None = None, mount: str = "") -> list[tuple[str, float]]:
    """[(artist, score)] best first — the auto chooser's view of the roster
    for a station: by what its base rule plays of each artist when the
    inventory knows (`stations`), else by family and era."""
    scored = [(a, spotlight_score(info, families, era_rule, mount)) for a, info in artists.items()
              if compatible(families, info.get("families")) and not NOT_AN_ARTIST.search(a)]
    return sorted(((a, sc) for a, sc in scored if sc > 0), key=lambda x: (-x[1], x[0]))


def feed_candidates(feeds: dict, families: list[str] | None) -> list[str]:
    return sorted(fid for fid, f in feeds.items()
                  if f.get("status") == "ready" and (f.get("count") or 0) >= MIN_TRACKS_FOR_SPOTLIGHT
                  and compatible(families, f.get("family")))


def resolve(slot: dict, date: dt.date, picks: dict, *, station: dict, artists: dict, feeds: dict) -> tuple[dict, bool]:
    """The concrete slot for `date` and whether picks changed. A non-auto
    slot is itself. An auto slot becomes a feed or artist slot for the day,
    remembered in picks[slot id] so every reader agrees and so it isn't the
    same pick as the last few."""
    if slot.get("kind") != "auto":
        return slot, False
    key = slot.get("id") or slot.get("name") or "auto"
    history = picks.get(key, [])
    today = date.isoformat()
    for h in history:
        if h.get("date") == today:
            return {**slot, "kind": h["kind"], h["kind"]: h["value"],
                    "name": slot.get("name") or default_name(h["kind"], h["value"], feeds)}, False
    like = slot.get("like") or "artist"
    fam = station.get("family")
    recent = [h["value"] for h in history[-RECENT_PICKS:]]
    rng = random.Random(hashlib.sha256(f"{key}:{today}".encode()).hexdigest())
    if like == "artist":
        ranked = spotlight_ranked(artists, fam, (station.get("base") or {}).get("era"), station.get("mount", ""))[:AUTO_SHORTLIST]
        fresh = [(a, sc) for a, sc in ranked if a not in recent] or ranked
        if not fresh:
            return slot, False
        # weighted by score squared: the quintessential names come round often,
        # the merely qualified now and then
        value = rng.choices([a for a, _ in fresh], weights=[sc * sc for _, sc in fresh])[0]
    else:
        pool = feed_candidates(feeds, fam)
        fresh = [p for p in pool if p not in recent] or pool
        if not fresh:
            return slot, False
        value = rng.choice(fresh)
    history.append({"date": today, "kind": like, "value": value})
    picks[key] = history[-30:]
    return {**slot, "kind": like, like: value, "name": slot.get("name") or default_name(like, value, feeds)}, True


def default_name(kind: str, value: str, feeds: dict) -> str:
    if kind == "artist":
        return f"{value} spotlight"
    return (feeds.get(value) or {}).get("title") or value


# --- what the DJ says ----------------------------------------------------------

def say_time(t: dt.datetime) -> str:
    # Spoken by Kokoro: "8 PM" reads cleanly; "p.m." followed by a period does not.
    h = t.hour % 12 or 12
    ampm = "AM" if t.hour < 12 else "PM"
    return f"{h} {ampm}" if t.minute == 0 else f"{h}:{t.minute:02d} {ampm}"


def part_of_day(t: dt.datetime) -> str:
    if t.hour < 12:
        return "this morning"
    if t.hour < 17:
        return "this afternoon"
    return "tonight"


PROMO_FEED = [
    "Stay tuned at {time} for {name}.",
    "Coming up at {time}: {name}.",
    "{Part}, at {time}, it's {name}.",
    "Don't go far — {name} at {time}.",
]
PROMO_ARTIST = [
    "{Part}'s spotlight is on {artist}, at {time}.",
    "At {time} we're spotlighting {artist}.",
    "Stay with us: an hour of {artist} at {time}.",
    "{artist} gets the spotlight at {time}.",
]
INTRO_FEED = [
    "It's {name}, here on {station}.",
    "This is {name} — the next while on {station}.",
    "Time for {name} on {station}.",
]
INTRO_ARTIST = [
    "This hour we're spotlighting {artist}, on {station}.",
    "It's the {artist} spotlight, here on {station}.",
    "Settle in — an hour with {artist} on {station}.",
]


def promo(station_name: str, upcoming: list[tuple[dt.datetime, dt.datetime, dict]], rng: random.Random,
          at_most: int = 2) -> str:
    """A sentence or two about what's coming later today on this station."""
    parts = []
    for start, _end, s in upcoming[:at_most]:
        ctx = {"time": say_time(start), "Part": part_of_day(start).capitalize(), "part": part_of_day(start),
               "name": s.get("name") or "a special hour", "artist": s.get("artist") or ""}
        if s.get("kind") == "artist" and s.get("artist"):
            parts.append(rng.choice(PROMO_ARTIST).format(**ctx))
        else:
            parts.append(rng.choice(PROMO_FEED).format(**ctx))
    return " ".join(parts)


def intro(station_name: str, slot: dict, rng: random.Random) -> str:
    ctx = {"station": station_name, "name": slot.get("name") or "a special hour", "artist": slot.get("artist") or ""}
    if slot.get("kind") == "artist" and slot.get("artist"):
        return rng.choice(INTRO_ARTIST).format(**ctx)
    return rng.choice(INTRO_FEED).format(**ctx)


def upcoming(station: str, slots: list[dict], now: dt.datetime) -> list[tuple[dt.datetime, dt.datetime, dict]]:
    return [(a, b, s) for a, b, s in slots_today(station, slots, now.date()) if a > now]
