"""One-time changes to the RUNTIME config, applied on the box.

The station list lives in config/stations.json and is the owner's to edit, so a
seed change in the flake only reaches a fresh install. When a change should
reach an existing box too — splitting a station, adding a setting — it is
written here as a named, idempotent migration; the credentials oneshot runs
`netradio migrate` after seeding and records what ran in migrations.json.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from netradio.config import Config


def split_blues_jazz_soul(cfg: Config) -> str:
    """Blues, Jazz & Soul → Blues / Jazz / Soul & R&B (2026-09-16). Slots on
    the old station move to Blues."""
    stations = cfg.stations()
    mounts = {s["mount"] for s in stations}
    if "blues-jazz" not in mounts or {"blues", "jazz", "soul"} & mounts:
        return "nothing to do"
    old = next(s for s in stations if s["mount"] == "blues-jazz")
    i = stations.index(old)
    era = (old.get("base") or {}).get("era")
    def st(mount, name, genres):
        s = {"mount": mount, "name": name, "kind": "curated", "family": ["blues-jazz"], "base": {"genres": genres}}
        if era:
            s["base"]["era"] = era
        if old.get("breaks_every") is not None:
            s["breaks_every"] = old["breaks_every"]
        return s
    stations[i:i + 1] = [
        st("blues", "Blues", ["blues"]),
        st("jazz", "Jazz", ["jazz", "big band", "swing", "fusion", "bebop", "dixieland"]),
        st("soul", "Soul & R&B", ["soul and r&b", "soul", "r&b", "funk", "motown"]),
    ]
    cfg.save_stations(stations)
    slots = cfg.schedule()
    for s in slots:
        if s.get("station") == "blues-jazz":
            s["station"] = "blues"
    cfg.save_schedule(slots)
    return "split blues-jazz into blues / jazz / soul"


def feeds_keep_shellac(cfg: Config) -> str:
    """Feeds now exclude shellac unless `shellac: true` (2026-09-16). Feeds
    that exist already keep what they play today: the flag is set on them."""
    feeds = cfg.feeds()
    changed = 0
    for f in feeds.values():
        if "shellac" not in f:
            f["shellac"] = True
            changed += 1
    if changed:
        cfg.save_feeds(feeds)
    return f"kept shellac on {changed} existing feed(s)" if changed else "nothing to do"


def country_excludes_bluegrass(cfg: Config) -> str:
    """The catalogue's genres (beets + Last.fm, 2026-09-17) tag Monroe, the
    Stanleys and Sparks "Country" as well as "Bluegrass"; without a negative
    clause 1,545 bluegrass tracks walked into Classic Country. The owner drew
    that line on purpose, so the Country base keeps bluegrass words out."""
    from netradio.config import FAMILY_WORDS
    stations = cfg.stations()
    for s in stations:
        if s.get("mount") == "country" and s.get("kind") == "curated":
            base = s.setdefault("base", {})
            if "exclude_genres" in base:
                return "nothing to do"
            base["exclude_genres"] = list(FAMILY_WORDS["bluegrass"])
            cfg.save_stations(stations)
            return "Classic Country now excludes bluegrass words"
    return "no curated country station"


# What each curated station yields to: a track whose own (first) tag is not
# the station's, and which carries one of these words, is fringe — played a
# little, never the bulk. The neighbours, by specificity: bluegrass over
# country over folk; soul over blues over jazz; everything over rock.
FRINGE_GENRES = {
    "bluegrass": ["jazz", "rock", "alternative", "pop"],
    "country": ["rock", "alternative", "pop", "blues", "jazz", "folk", "singer songwriter", "soul", "r&b"],
    "folk": ["bluegrass", "old time", "oldtime", "newgrass", "string band", "country", "western swing", "honky tonk",
             "rock", "alternative", "pop", "world", "classical"],
    "rock": ["country", "bluegrass", "folk", "blues", "jazz", "soul", "r&b", "funk", "classical", "vocal",
             "easy listening", "world", "latin", "soundtrack", "new age"],
    "blues": ["soul", "r&b", "funk", "motown", "rock", "alternative", "jazz", "country", "bluegrass", "folk"],
    "jazz": ["rock", "alternative", "pop", "country", "western swing", "soul", "r&b", "funk", "new age", "blues"],
    "soul": ["blues", "jazz", "rock", "pop", "gospel"],
    "gospel": ["country", "rock", "pop", "bluegrass", "blues", "soul"],
    "classical": ["rock", "alternative", "pop", "soundtrack", "new age", "jazz", "world", "electronic"],
}


def stations_yield_to_neighbours(cfg: Config) -> str:
    """2026-09-19: every station sticks to its genre with limited
    excursion; spotlights go further afield. The base rules get
    `fringe_genres` (see FRINGE_GENRES); the scanner splits each base into
    core + fringe and the DJ plays the fringe a tenth of the time."""
    stations = cfg.stations()
    changed = 0
    for s in stations:
        if s.get("kind") == "specialty" or s.get("mount") not in FRINGE_GENRES:
            continue
        base = s.setdefault("base", {})
        if "fringe_genres" in base:
            continue
        base["fringe_genres"] = list(FRINGE_GENRES[s["mount"]])
        changed += 1
    if changed:
        cfg.save_stations(stations)
    return f"{changed} station(s) now yield to their neighbours" if changed else "nothing to do"


def add_rain_station(cfg: Config) -> str:
    """2026-09-23: a rain station on a loop, and the server's own sound
    card as a place to play it. `kind: fixed` means the scanner never writes
    its playlist (the ambient fetcher does) and the DJ never talks over it."""
    stations = cfg.stations()
    if any(s.get("mount") == "rain" for s in stations):
        return "nothing to do"
    stations.append({"mount": "rain", "name": "Rain", "kind": "fixed", "family": ["any"],
                     "breaks_every": 0, "base": {}})
    cfg.save_stations(stations)
    return "rain station added (fixed playlist, no DJ)"


def add_rainymood_station(cfg: Config) -> str:
    """2026-09-24: "variety when I want it and rainy mood for
    sleeping". A second fixed station holding one long seamless loop,
    beside the five-bed `rain`."""
    stations = cfg.stations()
    if any(s.get("mount") == "rainymood" for s in stations):
        return "nothing to do"
    at = next((i for i, s in enumerate(stations) if s.get("mount") == "rain"), len(stations) - 1)
    stations.insert(at + 1, {"mount": "rainymood", "name": "Rainy Mood", "kind": "fixed",
                             "family": ["any"], "breaks_every": 0, "base": {}})
    cfg.save_stations(stations)
    return "Rainy Mood station added (fixed playlist, no DJ)"


# The Celtic roster. A genre rule alone catches barely half of it: Lidarr
# imports keep whatever genre the release shipped with, and that vocabulary
# is not the catalogue's — Clannad arrives tagged "Folk", Dougie MacLean
# "Scottish Instrumental & Vocal", Patty Gurdy "Pop, Rock" (measured
# 2026-09-25). So the station names its artists as well as its genres.
# Seeded from the owner's YouTube playlist, then widened through Last.fm's
# similar-artist graph (the same one the spotlight chooser uses).
CELTIC_ARTISTS = [
    # from the playlist
    "Patty Gurdy", "Celtic Woman", "Ella Roberts", "Old Blind Dogs", "Julie Fowlis",
    "UCD Choral Scholars", "Choral Scholars of University College Dublin", "The High Kings",
    "Nathan Carter", "Griogair Labhruidh",
    # the trad expansion
    "Solas", "Capercaillie", "Silly Wizard", "Dervish", "Lúnasa", "Planxty", "Altan",
    "The Bothy Band", "The Dubliners", "The Irish Rovers", "The Clancy Brothers",
    "Christy Moore", "Gaelic Storm", "Clannad", "Loreena McKennitt", "Cara Dillon",  # leak-scan-ok: a genre word, not a name
    "Mary Black", "Talisk", "Dougie MacLean", "Karine Polwart", "The Chieftains",
    # the Outlander score only — the rest of his catalogue is sci-fi and horror
    "Bear McCreary :: Outlander",
]
CELTIC_GENRES = ["celtic", "irish", "scottish", "gaelic", "celtic folk", "irish folk"]


def add_celtic_station(cfg: Config) -> str:
    """2026-09-25: a Celtic station seeded from a YouTube playlist.

    Folk's base already claimed `celtic` and `irish`, so without this the two
    would play the same tracks: Folk now excludes those words AND the Celtic
    roster by name (a tag-only exclusion would leave Clannad on Folk, since
    that is how the release is tagged)."""
    stations = cfg.stations()
    if any(s.get("mount") == "celtic" for s in stations):
        return "nothing to do"
    folk = next((s for s in stations if s.get("mount") == "folk"), None)
    if folk is not None:
        base = folk.setdefault("base", {})
        base["genres"] = [g for g in (base.get("genres") or []) if g not in ("celtic", "irish")]
        base["exclude_genres"] = sorted(set(base.get("exclude_genres") or []) | set(CELTIC_GENRES))
        base["exclude_artists"] = sorted(set(base.get("exclude_artists") or []) | set(CELTIC_ARTISTS))
    at = next((i for i, s in enumerate(stations) if s.get("mount") == "folk"), len(stations) - 1)
    stations.insert(at + 1, {
        "mount": "celtic", "name": "Celtic", "kind": "curated", "family": ["folk"],
        "base": {"genres": list(CELTIC_GENRES), "artists": list(CELTIC_ARTISTS),
                 "era": {"exclude": ["shellac"]},
                 "fringe_genres": ["folk", "singer songwriter", "traditional", "acoustic",
                                   "world", "rock", "pop", "new age", "soundtrack"]},
    })
    cfg.save_stations(stations)
    return f"Celtic station added ({len(CELTIC_ARTISTS)} artists); Folk yields celtic/irish to it"


MIGRATIONS = [
    ("split-blues-jazz-soul", split_blues_jazz_soul),
    ("feeds-keep-shellac", feeds_keep_shellac),
    ("country-excludes-bluegrass", country_excludes_bluegrass),
    ("stations-yield-to-neighbours", stations_yield_to_neighbours),
    ("add-rain-station", add_rain_station),
    ("add-rainymood-station", add_rainymood_station),
    ("add-celtic-station", add_celtic_station),
]


def run(cfg: Config) -> list[str]:
    done = cfg._read("migrations.json", {})
    out = []
    for name, fn in MIGRATIONS:
        if name in done:
            continue
        result = fn(cfg)
        done[name] = result
        cfg._write("migrations.json", done)
        out.append(f"{name}: {result}")
        if result != "nothing to do":
            # the station list may have changed: rescan, and restart Liquidsoap
            # if the mounts did (the apply path unit decides)
            cfg.request("apply", {"reason": f"migration {name}"})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", required=True, type=Path)
    args = ap.parse_args(argv)
    for line in run(Config(args.config)):
        print(line)
    return 0
