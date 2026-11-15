"""`netradio ambient` — fetch the ambient beds (rain) a fixed station loops.

A fixed station's playlist is not built from the library: it points at a
handful of long recordings kept in <state>/ambient/<name>/. This fetches
them from the Internet Archive — freely licensed recordings only, which
also happen to loop better than the short clips a streaming site serves
(longer takes, no re-download when someone's CDN moves) — and writes
playlists/<mount>.m3u.

    netradio ambient --dir /var/lib/netradio/ambient --playlist /var/lib/netradio/playlists/rain.m3u

Idempotent: a file already on disk with the right size is left alone, so
this can run at boot and after every deploy. Add your own recordings by
dropping them in the directory; anything readable there is played.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("netradio.ambient")

AUDIO = (".mp3", ".flac", ".ogg", ".opus", ".m4a", ".wav")

# Identify the client. Python's default `Python-urllib/3.x` is blocked
# outright by some CDNs — rainymood's returns 403 to it on both HEAD and
# GET, while any descriptive agent is served normally (measured
# 2026-09-24), which is what left the Rainy Mood station with an empty
# playlist on its first deploy.
UA = {"User-Agent": "netradio-ambient/1.0 (self-hosted radio station; personal use)"}

@dataclass(frozen=True)
class Bed:
    """One ambient recording: where it comes from, what it is called on disk,
    and — when it is a single file a station loops forever — how to cut a
    seamless loop out of it."""
    url: str
    name: str
    licence: str
    loop: "Loop | None" = None


@dataclass(frozen=True)
class Loop:
    """Turning a recording into something that can repeat unnoticed.

    A produced track fades in and out; looped raw, that is a hole in the
    sound every time round. So the fades are cut off (`head`/`tail` seconds)
    and the body's own tail is crossfaded over its head, which puts the
    junction *inside* the file — wherever the player wraps, the seam has
    already happened. `qsin` because rain is noise: an equal-power curve
    holds the level, a linear one dips ~3 dB in the middle (measured)."""
    head: float       # seconds to drop off the front (the fade-in)
    tail: float       # seconds to drop off the end (the fade-out)
    xfade: float      # crossfade length


def archive(item: str, name: str, licence: str) -> Bed:
    return Bed(f"https://archive.org/download/{item}/{urllib.parse.quote(name)}", name, licence)


# The rain beds, chosen by MEASUREMENT rather than by title (2026-09-23).
# An earlier set was a studio sound-effects tape whose every cut opens with
# an announcer slating it — the owner heard "number four" on air. These were
# picked by fingerprinting the bed he likes into octave bands and ranking
# Creative Commons recordings by how close their spectral shape is to it.
RAIN = [
    archive("aporee_49306_56200", "soundmap202005212.mp3", "CC BY-NC-ND 3.0 — radio aporee, Chaozhou Township, Pingtung County"),
    archive("aporee_42317_48252", "2017629rainnightwindowshuters0021.mp3", "CC BY-NC-ND 3.0 — radio aporee, Unije: rain and window shutters at night"),
    archive("aporee_51774_59139", "2012160067ChuvacarroPiso.mp3", "Public Domain Mark 1.0 — radio aporee, Praia do Pisao: heavy rain at the beach"),
    archive("aporee_71764_83802", "180223009.mp3", "CC BY-NC 3.0 — radio aporee, Mbarara City, Uganda: heavy rain"),
    archive("aporee_35521_40790", "132RainTent15Mar86Knockree4416.mp3", "CC BY-NC-ND 3.0 — radio aporee, Co. Wicklow: rain on a tent"),
]

# Its own station, one file, looping all night (2026-09-24: "variety
# when I want it and rainy mood for sleeping"). Measured on the file: a
# ~1 s fade-in at the head and a ~25 s fade-out at the tail, so 1.5 s and
# 35 s come off before the 12 s loop crossfade. NOT redistributable — a
# personal copy of a stream the owner listens to, on his own machine.
RAINYMOOD = [
    Bed("https://media.rainymood.com/0.m4a", "rainymood.m4a",
        "rainymood.com — NOT Creative Commons. A personal copy for the owner's own "
        "listening, at his direction (2026-09-23); do not redistribute.",
        loop=Loop(head=1.5, tail=35.0, xfade=12.0)),
]

SETS = {"rain": RAIN, "rainymood": RAINYMOOD}

# Beds this tool shipped before and must clean up, even on a host whose
# manifest predates the manifest (the studio sound-effects tape, and the
# muffled field recording that went with it).
RETIRED = {
    "rain": ["G46-01-Light Rain and Natural Thunder.flac", "G46-03-Long Thunder Storm.flac",
             "G46-04-Distant Storm.flac", "G46-09-Steady Rain and Thunder.flac",
             "G46-12-Thunderclap Fox.flac", "Storm from 30 to 45.flac"],
}


def fetch(bed: "Bed", dest: Path, timeout: float = 600.0) -> bool:
    """Download one bed unless it is already here and whole."""
    size = 0
    try:
        with urllib.request.urlopen(urllib.request.Request(bed.url, method="HEAD", headers=UA), timeout=30) as r:
            size = int(r.headers.get("Content-Length") or 0)
    except Exception as e:
        # a server that refuses HEAD is no reason not to try the download
        log.info("%s: HEAD refused (%s), fetching anyway", bed.name, e)
        if dest.exists():
            return True
    if dest.exists() and (not size or dest.stat().st_size == size):
        log.debug("%s: already here", bed.name)
        return True
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(urllib.request.Request(bed.url, headers=UA), timeout=timeout) as r, tmp.open("wb") as fh:
            shutil.copyfileobj(r, fh)
        tmp.replace(dest)
        log.info("%s: %.1f MB", bed.name, dest.stat().st_size / 1e6)
        return True
    except Exception as e:
        tmp.unlink(missing_ok=True)
        log.warning("%s: download failed (%s)", bed.name, e)
        return dest.exists()


def duration(path: Path) -> float:
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=120)
        return float(r.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0.0


def make_loop(src: Path, dest: Path, spec: "Loop") -> bool:
    """Cut the fades off `src` and crossfade its tail over its head, so the
    result repeats with no audible seam. Skipped if `dest` is already newer
    than `src` — this runs at every boot."""
    if dest.exists() and dest.stat().st_mtime >= src.stat().st_mtime:
        return True
    d = duration(src)
    body_end = d - spec.tail
    xstart = body_end - spec.xfade
    if d <= 0 or xstart <= spec.head:
        log.warning("%s: %.0fs is too short to loop with these trims", src.name, d)
        return False
    tmp = dest.with_suffix(".tmp.flac")
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-filter_complex",
           f"[0:a]atrim=start={spec.head}:end={xstart:.3f},asetpts=N/SR/TB[m];"
           f"[0:a]atrim=start={xstart:.3f}:end={body_end:.3f},asetpts=N/SR/TB[t];"
           f"[t][m]acrossfade=d={spec.xfade}:c1=qsin:c2=qsin[o]",
           "-map", "[o]", "-c:a", "flac", "-compression_level", "5", str(tmp)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("%s: loop failed (%s)", src.name, e)
        tmp.unlink(missing_ok=True)
        return False
    if r.returncode != 0 or not tmp.exists():
        log.warning("%s: loop failed (%s)", src.name, (r.stderr or "").strip()[-200:])
        tmp.unlink(missing_ok=True)
        return False
    tmp.replace(dest)
    log.info("%s: seamless loop of %.1f min (cut %.1fs head, %.0fs tail, %.0fs crossfade)",
             dest.name, duration(dest) / 60, spec.head, spec.tail, spec.xfade)
    return True


def slate_or_gap(path: Path, ffprobe_window: float = 14.0) -> str:
    """'' if the file is a usable bed, else why it is not.

    A sound-effects tape slates each cut — a second or two of an announcer,
    a pause, then the effect. That is audible on a radio station and it
    reached the owner before anything caught it (2026-09-23). The signature is
    cheap to test for: a short burst of audio followed by a real silence,
    near the head of the file. A long silence anywhere in the window is
    also disqualifying — a rain bed should never go quiet.
    """
    try:
        r = subprocess.run(["ffmpeg", "-v", "error", "-t", str(ffprobe_window), "-i", str(path),
                            "-af", "silencedetect=noise=-45dB:d=0.4,ametadata=mode=print:file=-", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("%s: could not inspect (%s) — using it anyway", path.name, e)
        return ""
    starts = [float(m) for m in re.findall(r"silence_start=([\d.]+)", r.stderr + r.stdout)]
    ends = [float(m) for m in re.findall(r"silence_end=([\d.]+)", r.stderr + r.stdout)]
    # A slate is AUDIO, then a gap. A file that merely begins quiet has
    # silence starting at 0.0 with nothing before it to be a slate — the
    # first run rejected a perfectly good beach recording that way.
    if starts and 0.3 <= starts[0] < 4.0 and ends:
        return f"looks slated: audio stops at {starts[0]:.1f}s, resumes at {ends[0]:.1f}s"
    for a, b in zip(starts, ends):
        if b - a > 3.0:
            return f"goes silent for {b - a:.0f}s at {a:.0f}s"
    return ""


def playable(d: Path) -> list[str]:
    """Everything audible in the directory, sorted — the owner's own recordings
    dropped in here play beside the fetched ones."""
    try:
        return sorted(str(p) for p in d.iterdir() if p.suffix.lower() in AUDIO and p.is_file())
    except OSError:
        return []


def build(name: str, dir_: Path, playlist: Path) -> int:
    """Fetch the set's beds, make any loops, and write the station's m3u.

    Raw downloads live in <set>/src/ so that only what should PLAY sits in
    <set>/ — which is also where anything dropped in by hand goes."""
    d = dir_ / name
    src = d / "src"
    d.mkdir(parents=True, exist_ok=True)
    beds = SETS.get(name, [])
    manifest = d / ".fetched.json"
    try:
        was = set(json.loads(manifest.read_text()))
    except (OSError, ValueError):
        was = set()
    wanted = {played_name(b) for b in beds}
    # a bed this tool fetched that is no longer wanted goes; anything dropped
    # in by hand is never touched (that is how you add your own recordings)
    for stale in (was | set(RETIRED.get(name, []))) - wanted:
        if (d / stale).exists():
            (d / stale).unlink()
            log.info("%s: dropped (no longer in the set)", stale)
    for bed in beds:
        raw = (src / bed.name) if bed.loop else (d / bed.name)
        if not fetch(bed, raw):
            continue
        if bed.loop:
            make_loop(raw, d / played_name(bed), bed.loop)
    manifest.write_text(json.dumps(sorted(wanted)))
    (d / "LICENCES.txt").write_text(
        f"Ambient beds for the '{name}' station, fetched by `netradio ambient`.\n\n" +
        "".join(f"{played_name(b)}\n    {b.licence}\n    {b.url}\n\n" for b in beds))
    files, rejected = [], []
    for f in playable(d):
        why = slate_or_gap(Path(f))
        (rejected if why else files).append(f)
        if why:
            log.warning("%s: NOT a usable bed — %s", Path(f).name, why)
    playlist.parent.mkdir(parents=True, exist_ok=True)
    tmp = playlist.with_suffix(".m3u.tmp")
    tmp.write_text("#EXTM3U\n" + "".join(f + "\n" for f in files))
    tmp.replace(playlist)
    log.info("%s: %d file(s) → %s%s", name, len(files), playlist,
             f" ({len(rejected)} rejected)" if rejected else "")
    return len(files)


def played_name(bed: "Bed") -> str:
    """What ends up in the playlist: the loop for a bed that gets one."""
    return (Path(bed.name).stem + "-loop.flac") if bed.loop else bed.name


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dir", required=True, type=Path, help="where the beds live (<state>/ambient)")
    ap.add_argument("--playlists", required=True, type=Path, help="the playlist dir; each set writes <set>.m3u")
    ap.add_argument("--set", action="append", dest="sets", choices=sorted(SETS),
                    help="build only this set (repeatable); default: all of them")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    total = 0
    for name in (args.sets or sorted(SETS)):
        total += build(name, args.dir, args.playlists / f"{name}.m3u")
    if not total:
        print("no audio in the ambient directories — nothing to play", file=sys.stderr)
        return 1
    return 0
