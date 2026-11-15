"""The heart: Jellyfin's favourite flag, used as netradio's "more of this".

The owner wanted stars to show in Jellyfin and Jellyfin's stars to influence the
ratings. The first half does not exist, and it is worth recording why rather
than shipping something that only looks like it works.

**Jellyfin 10.11.11 has no star rating for a music track.** Measured against the
live server, not assumed:

  * an Audio item's `UserData` is
    `PlaybackPositionTicks, PlayCount, IsFavorite, Played, Key, ItemId`
    — no `Rating` field
  * the item itself carries no rating field of any kind
  * 0 of 400 randomly sampled audio items have a `CommunityRating` (that comes
    from metadata providers for films and shows, not from a listener)

What a track does have is the **heart**, and the owner already used it — seven
tracks were favourited before any of this existed. So the heart is the channel,
in both directions: the page toggles it, Jellyfin stores it, and the DJ plays
hearted tracks more often. One flag, one meaning, in one place.

⚠️ There is no way to look an item up by path. `?path=` is accepted and
ignored — it returns the whole library (24,348 items when tried). `searchTerm`
plus an exact `Path` match is the way, and it is what `item_for_path` does.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from netradio import ratings
from netradio.config import Config

log = logging.getLogger("netradio.jellyfin")


class Jellyfin:
    def __init__(self, url: str, key: str, user: str = "", timeout: float = 60.0):
        self.url = url.rstrip("/")
        self.key = key
        self._user = user
        self.timeout = timeout

    # -- transport
    def _req(self, method: str, path: str, **params):
        q = urllib.parse.urlencode(params)
        req = urllib.request.Request(f"{self.url}{path}{'?' + q if q else ''}",
                                     method=method, headers={"X-Emby-Token": self.key})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            body = r.read()
            if not body:
                return {}
            try:
                return json.loads(body)
            except ValueError:
                return {}

    def user(self) -> str:
        """Whose opinion counts: the first administrator unless told otherwise.
        A household has more than one account, and the other one's favourites
        are not this listener's."""
        if self._user:
            return self._user
        users = self._req("GET", "/Users")
        for u in users:
            if (u.get("Policy") or {}).get("IsAdministrator"):
                self._user = u["Id"]
                return self._user
        self._user = users[0]["Id"] if users else ""
        return self._user

    # -- reads
    def favourites(self) -> list[dict]:
        """Every favourited audio track, with its path on disk — the same key
        ratings.json uses, so nothing has to be matched up by name."""
        user = self.user()
        if not user:
            return []
        out: list[dict] = []
        start = 0
        while True:
            page = self._req("GET", f"/Users/{user}/Items", IncludeItemTypes="Audio",
                             Recursive="true", IsFavorite="true", Fields="Path,AlbumArtist",
                             Limit=200, StartIndex=start)
            items = page.get("Items") or []
            out += [{"id": it.get("Id"), "path": it.get("Path") or "",
                     "title": it.get("Name") or "", "artist": it.get("AlbumArtist") or ""}
                    for it in items if it.get("Path")]
            start += len(items)
            if not items or start >= int(page.get("TotalRecordCount") or 0):
                break
        return out

    def music_roots(self) -> list[str]:
        """Where Jellyfin thinks the music is. For someone who already runs it,
        this is the whole library configuration already done — and it catches
        folders a hand-written list forgets: on the server it named
        /srv/media/XMAS/Music (786 files) and /srv/media/pinchflat/music,
        neither of which netradio was scanning."""
        out: list[str] = []
        for folder in self._req("GET", "/Library/VirtualFolders") or []:
            if (folder.get("CollectionType") or "") == "music":
                out += [p for p in (folder.get("Locations") or []) if p]
        return out

    def item_for_path(self, path: str) -> str | None:
        """The item id for a file. Searches by the track's title and then
        matches the path exactly, because `?path=` does not filter."""
        user = self.user()
        stem = Path(path).stem
        # drop a leading track number: "03 Uncle Pen" searches better as "Uncle Pen"
        term = stem.split(" ", 1)[-1].strip() if stem[:1].isdigit() else stem
        for attempt in (term, stem):
            if not attempt:
                continue
            try:
                page = self._req("GET", f"/Users/{user}/Items", IncludeItemTypes="Audio",
                                 Recursive="true", Fields="Path", Limit=50, searchTerm=attempt)
            except Exception as e:
                log.warning("search for %r failed: %s", attempt, e)
                return None
            for it in page.get("Items") or []:
                if (it.get("Path") or "") == path:
                    return it.get("Id")
        log.warning("no Jellyfin item matches %s", path)
        return None

    # -- writes
    # Ticks are Jellyfin's unit everywhere: 10,000,000 to the second.
    TICKS = 10_000_000

    def now_playing(self) -> dict:
        """What is on a screen somewhere, or {} when nothing is.

        The radio can say what is playing because it CHOSE the track. A
        streaming box cannot be asked the same question — a Roku will name the
        app and nothing else — but Jellyfin is the server, so when the thing on
        the television came from here, it knows exactly (2026-10-09).

        Two things the live data taught that guessing would not have:

          * SEVERAL sessions can share a DeviceName. This server had three
            "Roku Express 4K (3941X)" — two left over from a fortnight earlier
            and one real. So the session cannot be chosen by device; it is the
            one actually holding a NowPlayingItem, most recently active if more
            than one does.
          * A file can be SEVERAL episodes. `IndexNumberEnd` is set when it is,
            and the Little Bear episode playing at the time was 40 through 42.
            "S1E40" would have been wrong and would have looked right.
        """
        try:
            sessions = self._req("GET", "/Sessions")
        except Exception as e:
            log.debug("jellyfin sessions: %s", e)
            return {}
        live = [s for s in (sessions or []) if s.get("NowPlayingItem")]
        if not live:
            return {}
        live.sort(key=lambda s: s.get("LastActivityDate") or "", reverse=True)
        return self._describe(live[0])

    def _describe(self, session: dict) -> dict:
        item = session.get("NowPlayingItem") or {}
        play = session.get("PlayState") or {}
        kind = (item.get("Type") or "").lower()

        ep = ""
        first, last = item.get("IndexNumber"), item.get("IndexNumberEnd")
        if first is not None:
            ep = f"{first}" if not last or last == first else f"{first}\u2013{last}"

        series = item.get("SeriesName") or ""
        title = item.get("Name") or ""
        season = item.get("ParentIndexNumber")
        if kind == "episode":
            head = series or title
            tag = (f"S{season}E{ep}" if season is not None and ep else ep)
            sub = " \u00b7 ".join(x for x in (tag, title) if x)
        else:
            head = title
            sub = str(item.get("ProductionYear") or "")

        def secs(t):
            return int(t) // self.TICKS if isinstance(t, (int, float)) else None

        return {
            "playing": True,
            "kind": kind,
            "head": head, "sub": sub,
            "series": series, "title": title,
            "season": season, "episode": ep,
            "year": item.get("ProductionYear"),
            "position": secs(play.get("PositionTicks")) or 0,
            "duration": secs(item.get("RunTimeTicks")),
            "paused": bool(play.get("IsPaused")),
            # The SERIES poster where there is one: an episode's own image is
            # usually a frame grab, which reads as a muddy rectangle at the
            # size this is shown.
            "art_id": item.get("SeriesId") or item.get("Id") or "",
            "device": session.get("DeviceName") or "",
            "client": session.get("Client") or "",
        }

    def set_favourite(self, item_id: str, on: bool) -> bool:
        user = self.user()
        try:
            self._req("POST" if on else "DELETE", f"/Users/{user}/FavoriteItems/{item_id}")
            return True
        except Exception as e:
            log.warning("could not %s the heart on %s: %s", "set" if on else "clear", item_id, e)
            return False


def toggle(cfg: Config, jf: "Jellyfin | None", path: str, on: bool,
           artist: str = "", title: str = "") -> dict:
    """The page's heart button.

    The heart is stored LOCALLY whatever happens — that is what makes the button
    work for somebody who does not run Jellyfin, and it is what the DJ reads, so
    a pick never waits on a network call. When Jellyfin is connected the flag is
    mirrored there too, and `heart_synced` records whether that succeeded: a
    heart set while Jellyfin is unreachable is pushed up by the next sync rather
    than being lost or silently disagreeing.
    """
    store = cfg.ratings()
    ratings.set_heart(store, path, on, artist, title)
    entry = store["tracks"][path]
    mirrored = False
    note = ""

    if jf is not None:
        item = jf.item_for_path(path)
        if item and jf.set_favourite(item, on):
            mirrored = True
        else:
            note = " (Jellyfin did not take it; it will go up on the next sync)"
    entry["heart_synced"] = mirrored
    if not on and not mirrored:
        entry.pop("heart_synced", None)
    cfg.save_ratings(store)

    return {"ok": True, "heart": on, "mirrored": mirrored,
            "message": ("hearted — more of this" if on else "heart removed") + note}


def sync(cfg: Config, jf: Jellyfin, dry_run: bool = False) -> dict:
    """Merge the local hearts with Jellyfin's.

    The heart lives locally whether or not Jellyfin is connected, so when it IS
    connected the two have to be reconciled — and a heart made locally while
    Jellyfin was away looks exactly like a heart REMOVED in Jellyfin unless
    something remembers which. `heart_synced` on each track is that something:

      * hearted locally, never synced        -> push it up to Jellyfin
      * hearted in Jellyfin, not locally     -> pull it down
      * hearted locally, synced, now absent  -> removed in Jellyfin; clear it
      * everything else                      -> already agreed

    So connecting Jellyfin for the first time UNIONS the two sets rather than
    letting either erase the other, and after that Jellyfin's removals carry.
    """
    counts = {"remote": 0, "pulled": 0, "pushed": 0, "cleared": 0, "failed": 0}
    try:
        favs = jf.favourites()
    except Exception as e:
        log.warning("could not read Jellyfin favourites: %s", e)
        return counts
    remote = {f["path"]: f for f in favs}
    counts["remote"] = len(remote)
    store = cfg.ratings()
    tracks = store.setdefault("tracks", {})

    for path, f in remote.items():
        e = tracks.get(path) or {}
        if not e.get("heart"):
            counts["pulled"] += 1
            log.info("pulled a heart from Jellyfin: %s", f.get("title") or path)
            if not dry_run:
                ratings.set_heart(store, path, True, f.get("artist", ""), f.get("title", ""))
        if not dry_run:
            tracks.setdefault(path, {}).setdefault("skips", 0)
            tracks[path]["heart_synced"] = True

    for path, e in list(tracks.items()):
        if not e.get("heart") or path in remote:
            continue
        if e.get("heart_synced"):
            # it was in step and is gone from Jellyfin: the listener removed it
            counts["cleared"] += 1
            log.info("heart removed in Jellyfin: %s", e.get("title") or path)
            if not dry_run:
                ratings.set_heart(store, path, False)
                e.pop("heart_synced", None)
        else:
            # hearted here while Jellyfin was not connected: send it up
            item = jf.item_for_path(path)
            if item and (dry_run or jf.set_favourite(item, True)):
                counts["pushed"] += 1
                log.info("pushed a local heart to Jellyfin: %s", e.get("title") or path)
                if not dry_run:
                    e["heart_synced"] = True
            else:
                counts["failed"] += 1

    if not dry_run and any(counts[k] for k in ("pulled", "pushed", "cleared")):
        cfg.save_ratings(store)
    return counts


def read_key(key: str = "", key_file: Path | None = None) -> str:
    """A bare key, or a `JELLYFIN_API_KEY=…` line — the secret is shaped for
    `set -a; . file` use elsewhere, so both forms have to work."""
    if key:
        return key
    if not key_file or not key_file.exists():
        return ""
    text = key_file.read_text().strip()
    for line in text.splitlines():
        if line.startswith("JELLYFIN_API_KEY="):
            return line.split("=", 1)[1].strip()
    return text.splitlines()[0].strip() if text else ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", required=True, type=Path, help="the runtime config dir")
    ap.add_argument("--url", default="http://127.0.0.1:8096")
    ap.add_argument("--key", default="", help="API key; prefer --key-file")
    ap.add_argument("--key-file", type=Path, help="a file holding the key (a sops secret)")
    ap.add_argument("--user", default="", help="Jellyfin user id (default: the first admin)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s", stream=sys.stdout)
    key = read_key(args.key, args.key_file)
    if not key:
        log.error("no API key: pass --key-file or --key")
        return 2
    counts = sync(Config(args.config), Jellyfin(args.url, key, args.user), args.dry_run)
    log.info("hearts: %s", ", ".join(f"{k}={v}" for k, v in counts.items()))
    return 0
