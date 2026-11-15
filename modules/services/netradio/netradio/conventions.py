"""The conventions a netradio install depends on, in one place.

These are facts about the shape of the world — what a cover file is called, what
a mount may be named, which folder names are not an artist — that several
modules have to agree on. They were previously written out wherever they were
needed, which is fine until two copies disagree:

  * `COVER_NAMES` was duplicated verbatim in `playlists` and `admin`. It is the
    pair that matters most: `playlists.has_art` decides what goes into
    `tiles.json`, and `admin.art` has to be able to serve everything it lists.
    A cover name in one and not the other means the manifest promises a picture
    the endpoint answers 404 for, and the page shows a gap with nothing to say
    why.
  * `NOT_AN_ARTIST` was duplicated verbatim in `playlists` and `schedule`.
  * `MOUNT_RE` existed three times under one name with THREE DIFFERENT
    meanings — `admin` required a leading alphanumeric and capped the length,
    `speaker` allowed a leading hyphen and any length, and `wake`'s was not a
    mount name at all but a URL path. So `-rain` was a legal mount to one
    service and rejected by another (2026-09-29).

Nothing here is configurable. Anything an operator should be able to change
belongs in `options.homelab.netradio`, not in this file.
"""

from __future__ import annotations

import re

# Cover files that count as a track's artwork, in preference order. Case
# variants are listed explicitly rather than matched case-insensitively,
# because the library may sit on a case-sensitive filesystem and the order is
# the preference.
COVER_NAMES = ("cover.jpg", "Cover.jpg", "folder.jpg", "Folder.jpg",
               "cover.png", "front.jpg", "Front.jpg", "album.jpg")

# A picture for ONE track, named after it and sitting beside it:
#
#   music_for_programming_42-datassette.mp3
#   music_for_programming_42-datassette.jpg
#
# A folder cover is an album's sleeve, and it is the right idea for an album.
# It is the wrong idea for a folder that is not an album — Music For
# Programming is 107 separate mixes in one directory, with a different picture
# for each and no sleeve to share (2026-09-29). The sidecar also gives
# any single track an override, which is the only way to correct one bad
# embedded cover without rewriting the file's tags.
SIDECAR_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".JPG", ".PNG")

# A folder at artist level that is not an artist: a year, or one of the usual
# catch-alls a ripper leaves behind.
NOT_AN_ARTIST = re.compile(r"\b(19|20)\d\d\b|various|unknown|compilation|soundtrack|sampler", re.I)

# A station mount. It becomes an Icecast mount point, a filename under `now/`,
# and part of a URL, so it is deliberately narrow: lowercase, starts with a
# letter or digit, hyphens inside, and short enough to keep those paths sane.
MOUNT = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


# A Jellyfin item id: 32 hex characters. Checked before one is pasted into a
# URL we then fetch, so a crafted value cannot steer the request somewhere else.
ITEM_ID = re.compile(r"^[0-9a-f]{32}$")


def is_item_id(value: str) -> bool:
    return bool(value) and bool(ITEM_ID.match(value))


def sidecar_names(path) -> tuple:
    """The picture filenames that would belong to this track, in preference
    order. Names only — whether any of them is there is the caller's business,
    and `os.access(R_OK)` is the right question rather than `exists()`: a file
    that cannot be read is not a picture the page can show (2026-09-28)."""
    from pathlib import Path
    p = Path(path)
    return tuple(p.with_suffix(s) for s in SIDECAR_SUFFIXES)


def is_mount(name: str) -> bool:
    """Whether `name` may be used as a station mount. Every service that
    accepts a mount from outside itself should ask this, so that a name is
    either valid everywhere or nowhere."""
    return bool(name) and bool(MOUNT.match(name))
