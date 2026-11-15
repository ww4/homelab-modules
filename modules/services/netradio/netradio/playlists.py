"""Turn the library, the profile and the runtime config into pools.

Inputs
    the library roots (walked; tags cached by size+mtime)
    profile.json          the profiler's facts (era, instruments, talk)
    <config>/feeds.json   specialty feeds and their rules
    <config>/stations.json
Outputs (all atomic)
    playlists/library.m3u          every audio file (the profiler's input)
    playlists/<mount>.m3u          what each station plays outside segments (its own genre)
    playlists/<mount>-fringe.m3u   matched by a later tag only: the DJ's occasional excursion
    pools/feeds/<feed>.m3u         each feed's pool (segments draw on these)
    pools/artists/<slug>.m3u       each artist's tracks (spotlights)
    <config>/artists.json          {artist: {tracks, families, slug}}
    <config>/stations.yml          YCast's menu: Curated / Specialty
    now/stations.json              counts for the radio page
    feeds.json                     `count` refreshed per feed

Talk tracks (profiler) are kept out of everything. A curated station's base
rule and a feed's rule are the same shape (feeds.py); families come from
the genre words (config.FAMILY_WORDS).
"""

from __future__ import annotations

import argparse
import collections
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from netradio.config import FAMILY_SOUND, FAMILY_WORDS, Config, write_atomic
from netradio.conventions import COVER_NAMES, NOT_AN_ARTIST

log = logging.getLogger("netradio.playlists")

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wav", ".wma", ".aif", ".aiff"}
EXCLUDE_WORDS = ("instructional", "audiobook", "spoken", "podcast", "lesson")
CACHE_VERSION = 2


@dataclass
class Track:
    path: str
    genre: str
    artist: str
    genres: list[str] = field(default_factory=list)   # split words
    yamnet: dict | None = None
    era: str = ""
    # The <Artist>/<Album> folders, taken RELATIVE to the library root this
    # file was found under. Absolute path indices were wrong the moment a
    # second root of a different depth was added: under
    # /srv/media/arr/media/music, parts[4] read "media" and the album read
    # "music", so folder-artist matching and `Artist :: album` scoping could
    # never hit a Lidarr-imported album (found 2026-09-26).
    folder_artist: str = ""
    folder_album: str = ""


def split_genre(tag: str) -> list[str]:
    tag = re.sub(r"singer\s*/\s*songwriter", "singer songwriter", tag.lower())   # one genre, not two
    parts = re.split(r"[/;,|]+", tag)
    out = []
    for p in parts:
        p = re.sub(r"\s+", " ", re.sub(r"[-_]+", " ", p)).strip()
        if p:
            out.append(p)
    return out


def word_in(word: str, genres: list[str]) -> bool:
    pat = re.compile(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])")
    return any(pat.search(g) for g in genres)


def read_tags(path: Path) -> tuple[str, str]:
    """(genre, artist) from the file's tags; '' when absent or unreadable."""
    import mutagen  # imported here so `--help` and the tests don't need it

    try:
        f = mutagen.File(path, easy=True)
    except Exception as e:
        log.debug("unreadable tags: %s (%s)", path, e)
        return "", ""
    if f is None or not f.tags:
        return "", ""
    g = f.tags.get("genre") or []
    a = f.tags.get("artist") or f.tags.get("albumartist") or []
    return " / ".join(str(x) for x in g if x), " ".join(str(x) for x in a[:1] if x)


class TagCache:
    """{path: [size, mtime_ns, genre, artist]} — rewritten whole at the end."""

    def __init__(self, path: Path):
        self.path = path
        self.hits = 0
        self.misses = 0
        try:
            raw = json.loads(path.read_text())
            self.data = raw.get("entries", {}) if isinstance(raw, dict) and raw.get("v") == CACHE_VERSION else {}
        except (OSError, ValueError):
            self.data = {}
        self.seen: dict[str, list] = {}

    def tags(self, path: Path, st: os.stat_result) -> tuple[str, str]:
        key = str(path)
        cached = self.data.get(key)
        if cached and cached[0] == st.st_size and cached[1] == st.st_mtime_ns:
            self.hits += 1
            genre, artist = cached[2], cached[3]
        else:
            self.misses += 1
            genre, artist = read_tags(path)
        self.seen[key] = [st.st_size, st.st_mtime_ns, genre, artist]
        return genre, artist

    def save(self) -> None:
        write_atomic(self.path, json.dumps({"v": CACHE_VERSION, "entries": self.seen}, separators=(",", ":")))


def walk(roots: list[Path], cache: TagCache, extra_genres: dict[str, list[str]] | None = None) -> tuple[list[Track], dict]:
    """Every audio file under the roots as a Track. `extra_genres` is the
    catalogue's word for each path ({path: [words]}, exported from beets +
    Last.fm — "honky tonk", "western swing", "delta blues"); it is merged
    with the file's own genre tag, never replacing it."""
    extra_genres = extra_genres or {}
    counts = {"files": 0, "untagged": 0, "excluded": 0, "unreadable_dirs": 0, "catalogue_genres": 0}
    tracks: list[Track] = []
    for root in roots:
        if not root.is_dir():
            log.warning("library root missing or unreadable: %s", root)
            counts["unreadable_dirs"] += 1
            continue
        for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: _walk_error(e, counts)):
            dirnames.sort()
            for name in sorted(filenames):
                p = Path(dirpath) / name
                if p.suffix.lower() not in AUDIO_EXTENSIONS:
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                counts["files"] += 1
                genre, artist = cache.tags(p, st)
                genres = split_genre(genre)
                more = [w for w in split_genre("; ".join(extra_genres.get(str(p), []))) if w not in genres]
                if more:
                    genres, genre = genres + more, "; ".join(x for x in [genre, *more] if x)
                    counts["catalogue_genres"] += 1
                if not genres:
                    counts["untagged"] += 1
                if any(word_in(w, genres) for w in EXCLUDE_WORDS):
                    counts["excluded"] += 1
                    continue
                rel = p.relative_to(root).parts
                folder_artist = rel[0] if len(rel) > 1 else ""
                folder_album = rel[1] if len(rel) > 2 else ""
                if not artist:
                    artist = folder_artist
                if "holiday" not in genres and HOLIDAY_NAME.search(f"{p.parent.name} {p.stem}"):
                    # a Christmas album tagged "Cowboy" is still a Christmas album  # leak-scan-ok: a genre word, not a name
                    genre, genres = (genre + "; holiday").strip("; "), genres + ["holiday"]
                    counts["holiday_by_name"] = counts.get("holiday_by_name", 0) + 1
                tracks.append(Track(str(p), genre, artist, genres,
                                    folder_artist=folder_artist, folder_album=folder_album))
    return tracks, counts


# Holiday material named rather than tagged: the album folder or the file.
# Not the bare word "holiday" — Billie Holiday's folder is not a Christmas  # leak-scan-ok: a genre word, not a name
# record; not "santa" alone — the Santa Fe Trail and Santa Ana's Retreat are
# not either; not "frosty" — Frosty Morn is a fiddle tune. Hank Snow's
# Reindeer Boogie got onto Classic Country past the first list (2026-09-19).
HOLIDAY_NAME = re.compile(
    r"christmas|xmas|noel|nativity|yuletide|reindeer|rudolph|mistletoe|sleigh|"  # leak-scan-ok: a genre word, not a name
    r"santa'?s\b|santa claus|here comes santa|santa baby|santa looked|"
    r"jingle bell|silent night|silver bells|white christmas|let it snow|winter wonderland|frosty the snowman|"  # leak-scan-ok: a genre word, not a name
    r"little drummer boy|deck the halls|o holy night|hark the herald|joy to the world|holly jolly|"
    r"feliz navidad|auld lang syne|god rest ye|we three kings|away in a manger|o come all ye|"
    r"first noel|good king wenceslas|blue christmas|here comes santa", re.I)  # leak-scan-ok: a genre word, not a name


def _walk_error(err: OSError, counts: dict) -> None:
    counts["unreadable_dirs"] += 1
    log.info("skipping unreadable directory: %s", err.filename)


def is_core(rule: dict, track: "Track", artist_info: dict | None) -> bool | None:
    """Whether a track that already matches a station's base rule is of the
    station's own genre — or an excursion.

    Core: the track's FIRST genre (its own tag, before the catalogue's
    additions) hits one of the rule's genre words; a rule that picks by
    artist, instrument, or `all` is core throughout. Fringe: the first tag
    is something else and a tag hits one of the rule's `fringe_genres` —
    the neighbours the station yields to (Folk yields to bluegrass and
    country: the Stanleys carry "Americana" but are not a folk station's
    own; Blues yields to soul: Aretha is Blues by her fourth tag). With no
    such hit the track is core — "Country; Bluegrass" is bluegrass. None:
    out altogether — the feeds vouch for the artist as one of the rule's
    EXCLUDED families and not its own (Dan Gellert's tag says Country; the
    old-time feeds hold 91% of him), which the negative clause meant to
    keep off the station. Spotlights ignore all of this.
    """
    words = [w.lower() for w in rule.get("genres") or []]
    if rule.get("exclude_genres") and artist_info:
        excluded = families_of([w.lower() for w in rule["exclude_genres"]])
        own = families_of(words)
        vouched = artist_info.get("feed_share") or {}
        if any(vouched.get(f, 0) >= 0.5 for f in excluded) and not any(vouched.get(f, 0) >= 0.5 for f in own):
            return None
    if not words or rule.get("all"):
        return True
    from netradio.feeds import artist_hit   # feeds imports this module
    if artist_hit(rule.get("artists") or [], track.artist, track.path):
        return True
    if any(word_in(w, track.genres[:1]) for w in words):
        return True
    return not any(word_in(w.lower(), track.genres) for w in rule.get("fringe_genres") or [])


def families_of(genres: list[str]) -> list[str]:
    return [fam for fam, words in FAMILY_WORDS.items() if any(word_in(w, genres) for w in words)]


def artist_slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "unknown"


def build_artists(tracks: list[Track]) -> dict:
    """{artist: {tracks, families, slug, albums, share, sound, holiday}} — a
    family counts when it covers at least a third of the artist's tracks (an
    artist can have two). The rest is what the spotlight chooser weighs:
    `share` is the fraction of their tracks tagged into each family
    (`feed_share`, added by build(), the fraction the feeds vouch for), `sound`
    the classifier's mean for each family's classes over their tracks,
    `albums` how many folders they span. Holiday tracks never get here."""
    by = collections.defaultdict(list)
    for t in tracks:
        if t.artist:
            by[t.artist].append(t)
    out = {}
    slugs: set[str] = set()
    for name, ts in by.items():
        fam_counts = collections.Counter(f for t in ts for f in families_of(t.genres))
        fams = [f for f, n in fam_counts.items() if n >= max(1, len(ts) / 3)] or ["unknown"]
        slug = artist_slug(name)
        while slug in slugs:
            slug += "-2"
        slugs.add(slug)
        heard = [t.yamnet for t in ts if t.yamnet]
        sound = {fam: round(sum(max(y.get(c, 0.0) for c in classes) for y in heard) / len(heard), 3)
                 for fam, classes in FAMILY_SOUND.items()} if heard else {}
        out[name] = {"tracks": len(ts), "families": sorted(fams), "slug": slug,
                     "albums": len({str(Path(t.path).parent) for t in ts}),
                     "share": {f: round(n / len(ts), 2) for f, n in fam_counts.items()},
                     "sound": sound,
                     # how many of their tracks fall in each recording era ("" = unmeasured):
                     # a station that excludes shellac must not spotlight an artist whose
                     # catalogue is mostly shellac (the Carter Family on Classic Country, 2026-09-17)
                     "eras": dict(collections.Counter(t.era or "" for t in ts))}
    return out




def has_art(path: str) -> bool:
    """A cover file beside the track, or a picture inside it (mp3 APIC /
    m4a covr / flac pictures). The tile picker asks this of a handful of
    files per station, so the tag read is affordable.

    ⚠️ READABLE, not merely present. `Path.exists()` needs only directory
    traversal, so it says yes to a cover this process cannot open — and then
    `admin/api/art` 404s on a path the manifest promised. 46 covers fetched by
    an agent on 2026-09-18 were written mode 0600, and every station tile that
    drew one came up a square short (found 2026-09-28; the files were chmod'd
    to 0664). `os.access` is the version of this check that can actually fail.
    """
    folder = Path(path).parent
    if any(os.access(folder / n, os.R_OK) for n in COVER_NAMES):
        return True
    try:
        import mutagen
        f = mutagen.File(path)
    except Exception:
        return False
    if f is None:
        return False
    tags = getattr(f, "tags", None) or {}
    if any(str(k).startswith("APIC") for k in getattr(tags, "keys", lambda: [])()):
        return True
    if hasattr(tags, "get") and tags.get("covr"):
        return True
    return bool(getattr(f, "pictures", None))


STATION_ART_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")


def station_art(art_dir, mount: str):
    """A picture for the station itself, if one has been put there.

    Stations with no artists to draw a mosaic from — an ambient rain bed, or
    "Everything", which is the whole library rather than four acts — showed a
    monogram and nothing else. Dropping <art_dir>/<mount>.jpg gives them a
    face, and because the result goes into the tile's `covers` it travels
    through the art endpoint and the pages unchanged.
    """
    if not art_dir:
        return None
    for ext in STATION_ART_SUFFIXES:
        f = art_dir / f"{mount}{ext}"
        try:
            if f.is_file() and os.access(f, os.R_OK):
                return f
        except OSError:
            pass
    return None


def station_tiles(stations: list[dict], station_pools: dict[str, list[str]], artists: dict, artist_of: dict,
                  primary: dict[str, str] | None = None, want: int = 4, shortlist: int = 12,
                  art_dir=None) -> dict:
    """What each station's tile shows: {mount: {"covers": [track paths]}}
    — up to `want` covers from the artists with the most tracks in the
    station's own pool, one album each, no cover reused across stations
    (a station left with fewer than `want` shows its first as a hero, so a
    hero is never another station's mosaic piece either). A station that
    plays everything ({"all": true}) gets {"icon": "radio"} instead of
    artists — no four names stand for the whole library."""
    used: set[str] = set()      # album folders already on a tile
    tiles: dict = {}
    # stations with the smallest pools first: they have the fewest covers to choose from
    order = sorted(stations, key=lambda s: len(station_pools.get(s["mount"], [])))
    for s in order:
        m = s["mount"]
        base = s.get("base") or {}
        art = station_art(art_dir, m)
        if art is not None:
            # `art: true` marks a picture chosen FOR this station, as opposed to
            # a mosaic of its albums. The phone's now-playing hero uses it as a
            # stand-in when the track itself has no cover; it must not do that
            # with a mosaic, which would show some other album as though it were
            # the one playing.
            tiles[m] = {"covers": [str(art)], "art": True}
            continue
        if s.get("kind") == "fixed":
            tiles[m] = {"icon": "rain", "covers": []}       # an ambient bed has no artists to show
            continue
        if base.get("all") and not base.get("era") and not base.get("genres") and s.get("kind") != "specialty":
            tiles[m] = {"icon": "radio", "covers": []}      # the whole library: a radio, not four artists
            continue
        pool = station_pools.get(m, [])
        by_artist: dict[str, list[str]] = collections.defaultdict(list)
        for path in pool:
            a = artist_of.get(path)
            if a and not NOT_AN_ARTIST.search(a):
                by_artist[a].append(path)
        # the archetypes first: artists whose tracks' FIRST genre word (the
        # file's own tag — the owner's word, kept first by the retag) is one of
        # the station's words, by how many; then the rest by count. So Jazz
        # shows Ellington, not a soul or rock act with a jazz tag somewhere.
        words = [w.lower() for w in (base.get("genres") or [])]
        primary = primary or {}
        def archetype_count(a: str) -> int:
            return sum(1 for path in by_artist[a] if primary.get(path, "") in words) if words else 0
        ranked = sorted(by_artist, key=lambda a: (-archetype_count(a), -len(by_artist[a])))[:shortlist]
        covers: list[str] = []
        picked: list[str] = []      # normalised artist names on this tile: "Elvis Costello & the Attractions" is Elvis Costello
        for a in ranked:
            key = artist_slug(a)
            if any(key.startswith(k) or k.startswith(key) for k in picked):
                continue
            folders_seen: set[str] = set()
            # the artist's own folders before a Compilations/Various folder
            for path in sorted(by_artist[a], key=lambda p: bool(NOT_AN_ARTIST.search(Path(p).parent.parent.name))):
                folder = str(Path(path).parent)
                if folder in folders_seen or folder in used:
                    continue
                folders_seen.add(folder)
                if has_art(path):
                    covers.append(path)
                    used.add(folder)
                    picked.append(key)
                    break
            if len(covers) >= want:
                break
        tiles[m] = {"covers": covers}
    return tiles


def wants_holiday(rule: dict) -> bool:
    return any(word_in(w, [g.lower() for g in rule.get("genres") or []]) for w in FAMILY_WORDS["holiday"])


def feed_rule(feed: dict) -> dict:
    """A feed's rule as applied: old scratchy records (shellac) stay out
    unless the feed says `shellac: true` — the owner: fine as a default, but
    Bill Monroe's discs are legitimately from that period. A rule with its
    own era clause is left alone."""
    rule = dict(feed.get("rule") or {})
    if not feed.get("shellac") and not rule.get("era"):
        rule["era"] = {"exclude": ["shellac"]}
    return rule


def read_m3u(path: Path) -> list[str]:
    try:
        return [l.strip() for l in path.read_text().splitlines() if l.strip() and not l.startswith("#")]
    except OSError:
        return []


def write_m3u(path: Path, paths: list[str]) -> None:
    write_atomic(path, "#EXTM3U\n" + "".join(p + "\n" for p in paths))


def build(tracks: list[Track], cfg: Config, out: Path, pools: Path, *, talk: set[str],
          summary: Path | None = None, ycast: Path | None = None, public_base: str = "",
          internet_radio: list[dict] | None = None, web_base: str = "",
          menu_codecs: set[str] | None = None, station_art_dir: Path | None = None) -> dict:
    """Everything after the walk. Returns {mount: count}."""
    from netradio import feeds as feedrules

    feeds = cfg.feeds()
    stations = cfg.stations()
    never = set((cfg.dislikes().get("tracks") or {}))    # listener "never again" (2026-09-17)
    everything = [t for t in tracks if t.path not in talk and t.path not in never]
    # The owner: no holiday music outside the holiday station, ever. A track
    # tagged holiday reaches a pool only when the rule asks for it by name —
    # so the "all" base, the feeds, and the artist spotlights never see it.
    playable = [t for t in everything if "holiday" not in families_of(t.genres)]

    def pool(rule: dict) -> list[str]:
        source = everything if wants_holiday(rule) else playable
        return [t.path for t in source
                if feedrules.matches(rule, artist=t.artist, path=t.path, genre=t.genre, yamnet=t.yamnet, era=t.era,
                                     folder_artist=t.folder_artist, folder_album=t.folder_album)]

    feed_pools: dict[str, list[str]] = {}
    for fid, f in feeds.items():
        if f.get("status") == "ready" and f.get("rule"):
            feed_pools[fid] = pool(feed_rule(f))
            write_m3u(pools / "feeds" / f"{fid}.m3u", feed_pools[fid])
            log.info("feed %-22s %6d tracks  (%s)", fid, len(feed_pools[fid]), f.get("title", ""))
        f["count"] = len(feed_pools.get(fid, []))
    cfg.save_feeds(feeds)

    artists = build_artists(playable)
    by_artist = collections.defaultdict(list)
    artist_of = {}
    for t in playable:
        if t.artist:
            by_artist[t.artist].append(t.path)
            artist_of[t.path] = t.artist
    # An artist in a feed inherits the feed's families: the Carter Family's
    # tags say "other", but a feed that holds them says what they are.
    in_feeds: dict[str, dict[str, set[str]]] = collections.defaultdict(lambda: collections.defaultdict(set))
    for fid, paths in feed_pools.items():
        fams = feeds[fid].get("family") or []
        if not fams:
            continue
        for path in paths:
            a = artist_of.get(path)
            if a and a in artists:
                cur = [f for f in artists[a]["families"] if f != "unknown"]
                artists[a]["families"] = sorted(set(cur) | set(fams))
                for f in fams:
                    in_feeds[a][f].add(path)
    # the feed-given share: what the feeds vouch for, kept beside the tagged
    # share (the chooser uses it only for an artist whose tags say nothing)
    for a, per_family in in_feeds.items():
        artists[a]["feed_share"] = {f: round(len(paths) / artists[a]["tracks"], 2) for f, paths in per_family.items()}
    for name, info in artists.items():
        write_m3u(pools / "artists" / f"{info['slug']}.m3u", by_artist[name])
    # what each curated station's OWN base rule would play of each artist —
    # the spotlight chooser's measure of fit (genres, exclusions and era in
    # one number: Jimmy Martin has 0 for Classic Country, the Carter Family
    # only their non-shellac sides)
    # A curated base is two lists: the station's own genre (what the
    # station plays, what the spotlight chooser and the tiles measure) and
    # the fringe — matched only by a later tag — that the DJ lets in a
    # little (dj.EXCURSION). A specialty station is its feed, all core.
    station_pools: dict[str, list[str]] = {}
    fringe_pools: dict[str, list[str]] = {}
    for s in stations:
        if s.get("kind") == "fixed":
            # an ambient station: its m3u is kept by hand (or by the ambient
            # fetcher), never by the scanner — read it, don't write it
            station_pools[s["mount"]] = read_m3u(out / f"{s['mount']}.m3u")
            fringe_pools[s["mount"]] = []
            continue
        if s.get("kind") == "specialty":
            station_pools[s["mount"]] = feed_pools.get(s.get("feed", ""), [])
            fringe_pools[s["mount"]] = []
            continue
        rule = s.get("base") or {"all": True}
        core, fringe = [], []
        matched = set(pool(rule))
        for t in (everything if wants_holiday(rule) else playable):
            if t.path not in matched:
                continue
            kind = is_core(rule, t, artists.get(t.artist))
            if kind is None:
                continue
            (core if kind else fringe).append(t.path)
        station_pools[s["mount"]], fringe_pools[s["mount"]] = core, fringe
    for mount, paths in station_pools.items():
        for path in paths:
            a = artist_of.get(path)
            if a and a in artists:
                artists[a].setdefault("stations", {})[mount] = artists[a].setdefault("stations", {}).get(mount, 0) + 1
    write_atomic(cfg.root / "artists.json", json.dumps(artists, sort_keys=True))
    words = sorted({w for t in playable for w in t.genres})
    write_atomic(cfg.root / "genre-words.json", json.dumps(words))   # for the compile prompt

    counts = {}
    for s in stations:
        paths, fringe = station_pools[s["mount"]], fringe_pools[s["mount"]]
        if s.get("kind") != "fixed":
            write_m3u(out / f"{s['mount']}.m3u", paths)
            write_m3u(out / f"{s['mount']}-fringe.m3u", fringe)
        counts[s["mount"]] = len(paths)
        log.info("%-14s %6d tracks  +%d fringe  (%s)", s["mount"], len(paths), len(fringe), s.get("name", ""))
    if summary:
        write_atomic(summary, json.dumps({m: {"tracks": n, "fringe": len(fringe_pools[m])} for m, n in counts.items()}))
        # the radio page's station list and the radio-app playlists, beside it
        now = summary.parent
        write_atomic(now / "catalogue.json", json.dumps([{"mount": s["mount"], "name": s["name"], "kind": s.get("kind", "curated")}
                                                          for s in stations]))
        write_atomic(now / "internet-radio.json", json.dumps(internet_radio or []))   # the receiver's menu has them too
        primary = {t.path: (t.genres[0] if t.genres else "") for t in playable}
        write_atomic(now / "tiles.json", json.dumps(station_tiles(stations, station_pools, artists, artist_of, primary,
                                                                   art_dir=station_art_dir)))
        if web_base:
            write_atomic(now / "stations.m3u", m3u(stations, web_base, ""))
            write_atomic(now / "stations-lo.m3u", m3u(stations, web_base, "-lo"))
            write_atomic(now / "stations.pls", pls(stations, web_base))
    if ycast:
        write_atomic(ycast, ycast_yaml(stations, public_base, internet_radio or [], menu_codecs))
    return counts


def m3u(stations: list[dict], base: str, suffix: str) -> str:
    return "#EXTM3U\n" + "".join(f"#EXTINF:-1,{s['name']}\n{base}/{s['mount']}{suffix}.mp3\n" for s in stations)


def pls(stations: list[dict], base: str) -> str:
    out = "[playlist]\n"
    for i, s in enumerate(stations, 1):
        out += f"File{i}={base}/{s['mount']}.mp3\nTitle{i}={s['name']}\nLength{i}=-1\n"
    return out + f"NumberOfEntries={len(stations)}\nVersion=2\n"


def ycast_yaml(stations: list[dict], base: str, internet_radio: list[dict],
               codecs: set[str] | None = None) -> str:
    """YCast's stations.yml: category → name → url. Hand-emitted so the order
    is the catalogue order; values are JSON strings, which is valid YAML.

    `codecs` is what the device browsing this menu can decode, from its own
    declaration. A station it cannot play is worse than one it is not shown —
    the listener selects it and gets silence — so anything in a codec it lacks
    is left out. The library's own stations are always MP3, so only the outside
    Internet Radio are filtered (2026-09-27)."""
    def line(name, url):
        return f"  {json.dumps(name)}: {json.dumps(url)}\n"
    out = "Curated:\n"
    for s in stations:
        if s.get("kind") != "specialty":
            out += line(s["name"], f"{base}/{s['mount']}.mp3")
    out += "\nSpecialty:\n"
    for s in stations:
        if s.get("kind") == "specialty":
            out += line(s["name"], f"{base}/{s['mount']}.mp3")
    # A station this device cannot decode is not dropped if there is a relay for
    # it: the relay is the same station re-encoded to MP3 on demand, on our own
    # mount, so the listener sees one list either way. The codec is plumbing,
    # not a category (2026-09-28).
    shown, dropped = [], []
    for q in internet_radio:
        if codecs is None or (q.get("codec") or "mp3") in codecs:
            shown.append((q["name"], q["url"]))
        elif q.get("relay"):
            shown.append((q["name"], f"{base}/{q['relay']}.mp3"))
        else:
            dropped.append(q)
    if dropped:
        log.info("%d outside station(s) kept out of the menu — this device cannot decode %s "
                 "and no relay is configured for them",
                 len(dropped), ", ".join(sorted({(q.get("codec") or "mp3") for q in dropped})))
    if shown:
        out += "\nInternet Radio:\n"
        for name, url in shown:
            out += line(name, url)
    return out


def parse_ycast_yaml(text: str) -> list[tuple[str, str, str]]:
    """(category, name, url) per entry — the inverse of ycast_yaml, and kept
    beside it so the two cannot drift.

    This file is the receiver's own truth about where a station lives in its
    menu, which is why it is read back rather than the category being derived
    a second time from a station's kind: Rain and Rainy Mood are `fixed`, the
    page lists them under Specialty, and the menu has them under Curated. Any
    second guess would be wrong for those two.
    """
    out: list[tuple[str, str, str]] = []
    category = ""
    for raw in text.splitlines():
        if not raw.strip():
            continue
        if not raw.startswith(" "):                 # a category header
            category = raw.rstrip().rstrip(":")
            continue
        name, sep, url = raw.strip().partition(": ")
        if not sep:
            continue
        try:                                        # values are JSON strings
            out.append((category, json.loads(name), json.loads(url)))
        except ValueError:
            continue
    return out


def menu_entry_for_mount(text: str, mount: str) -> tuple[str, str] | None:
    """(category, name) for the station serving `mount`, from the menu file."""
    want = f"/{mount}.mp3"
    for category, name, url in parse_ycast_yaml(text):
        if url.endswith(want):
            return category, name
    return None


def main(argv: list[str] | None = None) -> int:
    from netradio.profile import Profile, load_overrides

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", action="append", default=[], type=Path, help="library root (repeatable); may be empty when Jellyfin names the roots")
    ap.add_argument("--config", required=True, type=Path, help="the runtime config dir")
    ap.add_argument("--out", required=True, type=Path, help="playlist directory")
    ap.add_argument("--pools", required=True, type=Path, help="pool directory (feeds/, artists/)")
    ap.add_argument("--cache", required=True, type=Path, help="tag cache file")
    ap.add_argument("--genres", type=Path, help="JSON {path: [genre words]} from the library catalogue (beets + Last.fm); merged with the file tags")
    ap.add_argument("--profile", type=Path, help="profile.json from `netradio profile`")
    ap.add_argument("--overrides", type=Path, help="profile-overrides.json")
    ap.add_argument("--summary", type=Path, help="write counts here (the radio page reads it)")
    ap.add_argument("--ycast", type=Path, help="write YCast's stations.yml here")
    ap.add_argument("--public-base", default="http://radioyamaha.vtuner.com/radio", help="stream URL prefix for YCast")
    ap.add_argument("--web-base", default="", help="stream URL prefix for the page's m3u/pls (https)")
    ap.add_argument("--internet-radio", type=Path, help="JSON [{name,url,codec}] appended to YCast's menu")
    ap.add_argument("--menu-codecs", default="",
                    help="what the device browsing the menu can decode, comma separated. An outside "
                         "station in anything else is left out: unplayable is worse than absent")
    ap.add_argument("--station-art", type=Path, help="directory of <mount>.jpg pictures for stations")
    ap.add_argument("--jellyfin-url", default="", help="ask this Jellyfin where the music is (optional)")
    ap.add_argument("--jellyfin-key-file", type=Path, help="a file holding the Jellyfin API key")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s", stream=sys.stdout)

    # Where the music is: the named roots, plus whatever Jellyfin says. For
    # somebody who already runs Jellyfin its library configuration IS the
    # answer, and a hand-written list is a second place to forget a folder — on
    # the server Jellyfin named two roots netradio was not scanning, one of them 786
    # Christmas files (2026-09-27).  # leak-scan-ok: a genre word, not a name
    roots = list(args.root)
    if args.jellyfin_url:
        from netradio import jellyfin as jf_mod
        key = jf_mod.read_key(key_file=args.jellyfin_key_file)
        if not key:
            log.warning("--jellyfin-url given but no readable key; using the named roots only")
        else:
            try:
                found = jf_mod.Jellyfin(args.jellyfin_url, key).music_roots()
            except Exception as e:
                found = []
                log.warning("could not ask Jellyfin for the music folders: %s", e)
            for r in found:
                if Path(r) not in roots:
                    log.info("Jellyfin names a music folder we were not scanning: %s", r)
                    roots.append(Path(r))
    if not roots:
        ap.error("no library roots: pass --root, or --jellyfin-url with a readable key")
    args.root = roots

    cache = TagCache(args.cache)
    extra = {}
    if args.genres and args.genres.exists():
        try:
            extra = json.loads(args.genres.read_text())
        except ValueError as e:
            log.warning("ignoring %s: %s", args.genres, e)
    tracks, counts = walk(args.root, cache, extra)
    cache.save()
    write_m3u(args.out / "library.m3u", [t.path for t in tracks])

    talk: set[str] = set()
    if args.profile:
        prof = Profile(args.profile)
        verdicts = prof.verdicts(load_overrides(args.overrides))
        talk = {p for p, v in verdicts.items() if v.talk}
        for t in tracks:
            e = prof.data.get(t.path)
            if e:
                t.yamnet = e.get("yamnet")
                t.era = verdicts[t.path].era

    quick = []
    if args.internet_radio:
        try:
            quick = json.loads(args.internet_radio.read_text())
        except (OSError, ValueError):
            quick = []
    station_counts = build(tracks, Config(args.config), args.out, args.pools, talk=talk, summary=args.summary,
                           ycast=args.ycast, public_base=args.public_base, internet_radio=quick,
                           web_base=args.web_base,
                           menu_codecs=({c.strip() for c in args.menu_codecs.split(",") if c.strip()}
                                        or None),
                           station_art_dir=args.station_art)

    log.info("%d audio files, %d untagged, %d excluded, %d talk, %d unreadable dirs, %d with catalogue genres; tag cache %d hits / %d reads",
             counts["files"], counts["untagged"], counts["excluded"], len(talk), counts["unreadable_dirs"], counts["catalogue_genres"],
             cache.hits, cache.misses)
    empty = [m for m, n in station_counts.items() if not n]
    if empty:
        log.warning("empty stations (their mount will refuse to start): %s", ", ".join(empty))
    if counts["files"] == 0:
        log.error("no audio files found under %s — is the library mounted?", args.root)
        return 1
    return 0
