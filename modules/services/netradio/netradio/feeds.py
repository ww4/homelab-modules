"""Feed rules: what puts a track into a feed.

A feed's rule is a small structured object — no query language to parse:

    artists      ["Bob Wills", "Asleep at the Wheel"]   name match, normalised
    genres       ["western swing", "honky tonk"]        whole word in the genre tag
    instruments  {"banjo": [0.3, null], "singing": [null, 0.05]}
                                                        YAMNet means, ALL must hold
    era          {"only": ["shellac"], "exclude": ["shellac"]}
    exclude_genres ["bluegrass", "old time"]           any hit keeps the track OUT
    all          true                                   everything (era still applies)

Each clause that is present must hold: artists/genres together say WHO or
WHAT (a track needs to hit one of them), instruments say how it sounds (all
thresholds), era says when — so "old-time fiddle" is genres [old time] AND
violin >= 0.15, and "banjo instrumentals" is instruments alone. `all` stands
in for the who/what clause. Talk tracks are kept out of everything by the
scanner before rules run. The AI compile step writes rules of exactly this
shape from a feed's description; the owner can edit them by hand.
"""

from __future__ import annotations

import re

from netradio.playlists import split_genre, word_in


def norm_artist(name: str) -> str:
    s = name.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    if s.startswith("the "):
        s = s[4:]
    return s


def artist_hit(rule_artists: list[str], track_artist: str, track_path: str = "",
               folder_artist: str = "", folder_album: str = "") -> bool:
    """The rule's name inside the track's artist (so 'george jones' hits
    'Ralph Stanley & George Jones' too), or the library folder's artist.

    An entry may be narrowed to part of an artist's work by writing
    ``Artist :: album words`` — only albums whose folder carries those words
    count. The owner wants the Outlander score on Celtic and none of Bear
    McCreary's sci-fi and horror scoring (2026-09-25); scoping what is
    ALLOWED in stays right as an artist's catalogue grows, where a list of
    exclusions has to be extended every time they release something."""
    if not rule_artists:
        return False
    hay = [norm_artist(track_artist)]
    # The scanner resolves these against the library root and passes them in.
    # Without them (a caller holding only a path) read the TAIL —
    # <Artist>/<Album>/<file> — which is right at any depth, where the old
    # absolute indices were right only under the first root.
    if not folder_artist and track_path:
        parts = track_path.split("/")
        folder_artist = parts[-3] if len(parts) > 2 else ""
        folder_album = parts[-2] if len(parts) > 1 else ""
    if folder_artist:
        hay.append(norm_artist(folder_artist))
    album = norm_artist(folder_album)
    for a in rule_artists:
        name, _, want_album = a.partition("::")
        n = norm_artist(name)
        if not n or not any(n in h for h in hay):
            continue
        if want_album and norm_artist(want_album) not in album:
            continue
        return True
    return False


def instruments_hit(spec: dict, yamnet: dict | None) -> bool:
    if not spec:
        return False
    if not yamnet:
        return False
    for name, (lo, hi) in ((k, (v + [None, None])[:2]) for k, v in spec.items()):
        v = yamnet.get(name)
        if v is None:
            return False
        if lo is not None and v < lo:
            return False
        if hi is not None and v > hi:
            return False
    return True


def era_ok(spec: dict | None, era: str) -> bool:
    if not spec:
        return True
    only = spec.get("only") or []
    exclude = spec.get("exclude") or []
    if only and era not in only:      # unmeasured ("") never satisfies an `only`
        return False
    if era and era in exclude:
        return False
    return True


def matches(rule: dict, *, artist: str, path: str, genre: str, yamnet: dict | None, era: str,
            folder_artist: str = "", folder_album: str = "") -> bool:
    if not era_ok(rule.get("era"), era):
        return False
    artists = rule.get("artists") or []
    genre_words = rule.get("genres") or []
    instruments = rule.get("instruments") or {}
    if not (rule.get("all") or artists or genre_words or instruments):
        return False
    genres = split_genre(genre or "")
    # the negative clause: "Classic Country" is country words but NOT
    # bluegrass ones — the catalogue tags Monroe "Country" too (2026-09-17)
    if any(word_in(w.lower(), genres) for w in rule.get("exclude_genres") or []):
        return False
    # …and the same for artists a station has handed to another one: Clannad
    # is tagged plain "Folk", so only naming them keeps Folk off the Celtic
    # roster (2026-09-25 — Lidarr imports keep whatever genre the release
    # shipped with, which is not the catalogue's vocabulary)
    if artist_hit(rule.get("exclude_artists") or [], artist, path, folder_artist, folder_album):
        return False
    if (artists or genre_words) and not rule.get("all"):
        if not (artist_hit(artists, artist, path, folder_artist, folder_album) or any(word_in(w.lower(), genres) for w in genre_words)):
            return False
    if instruments and not instruments_hit(instruments, yamnet):
        return False
    return True


def validate(rule: dict) -> list[str]:
    """Problems with a rule as written (the admin page shows them)."""
    errs = []
    for k in ("artists", "genres", "exclude_genres", "fringe_genres", "exclude_artists"):
        v = rule.get(k)
        if v is not None and (not isinstance(v, list) or not all(isinstance(x, str) for x in v)):
            errs.append(f"{k} must be a list of strings")
    inst = rule.get("instruments")
    if inst is not None:
        if not isinstance(inst, dict):
            errs.append("instruments must be an object of name -> [min, max]")
        else:
            for k, v in inst.items():
                if not (isinstance(v, list) and 1 <= len(v) <= 2 and all(x is None or isinstance(x, (int, float)) for x in v)):
                    errs.append(f"instruments.{k} must be [min, max] (either may be null)")
    era = rule.get("era")
    if era is not None and not isinstance(era, dict):
        errs.append("era must be {only: [...], exclude: [...]}")
    if not (rule.get("all") or rule.get("artists") or rule.get("genres") or rule.get("instruments")):
        errs.append("the rule selects nothing (no artists, genres, instruments, or all)")
    return errs
