"""Listen to every track once and say whether it is talk.

Some albums cut the stage patter, the spoken introduction or the band
intros into their own tracks, and in a shuffle those are dead air. Duration
and titles don't settle it: old-time fiddle tunes run 45 seconds, "Talking
Blues" is a song, and a spoken sermon runs twelve minutes. So the profiler
listens, with two independent signals that must AGREE before a track is
called talk (measured on this library, 2026-09-16):

  1. YAMNet (AudioSet classifier, ONNX): the fraction of 0.48 s frames that
     score Speech > 0.5 and Music < 0.5. Talk tracks: >= 0.53. Songs: <= 0.22.
     But it hears unaccompanied ballad singing as speech (0.48 on Addie
     Graham), and this library has a lot of that — hence:
  2. Pitch stability: the fraction of voiced frames sitting inside a held
     note (< 35 cents over 190 ms). Speech glides: talk <= 0.10 (a five-
     minute spoken introduction sat right at 0.10). Anyone singing holds
     notes: a cappella songs >= 0.22; the songs that measured 0.11-0.13 were
     instrumentals the classifier already scored as music.

The same two signals on the last 20 s say whether a song ENDS in chatter
(the DJ then lets it finish instead of crossfading over the talk), and on
the first 20 s whether it starts with it.

Era, as an audio-quality judgement (curation plan phase 1): the tag dates in
this library are reissue dates, so the sound decides. Measured on 35 tracks
of certain era (2026-09-16): every 1920s-40s side has bandwidth 5.2-7.3 kHz
and is mono; 1950s-70s runs 4.7-17.8 kHz (early Stanleys/Hank 5-7, 60s
stereo Nashville 13-18), mostly mono; 1980s+ 10-21 kHz, mostly stereo. So:
bandwidth (highest frequency within 50 dB of the peak, 60 s window), the L/R
correlation, and the file's bitrate (a 96 kbps MP3 lowpasses at ~11 kHz and
must not read as tape) are stored; rules.py draws the lines.

YAMNet's other classes come for free from the same run: instruments
(banjo, mandolin, fiddle, steel guitar, accordion, harmonica, guitars,
piano, organ, drum kit) and genres (bluegrass, country, swing, folk, gospel,
blues, jazz, rock and roll, R&B, soul, a capella, choir, yodeling). Their
per-track means are stored so rules can tag instrumentation and style; the
thresholds get set once a library-wide pass shows the distributions.

profile.json holds only the MEASUREMENTS (cached by size+mtime; a first
pass over the library is hours at Nice 19, every night after that is
seconds). What they mean — the thresholds, and any further filter — lives in
rules.py and is evaluated when the scanner and the DJ read the profile, so
a rule change never means listening to the library again. A report of what
the rules flagged is written next to the profile for review, and an
overrides file ({path: "talk" | "music"}) wins over them.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from netradio.rules import Verdict, apply_overrides, evaluate

log = logging.getLogger("netradio.profile")

SR = 16000
HEAD_SECONDS = 60.0     # enough to know a song is a song
EDGE_SECONDS = 20.0     # the tail/head windows for "ends/starts in chatter"

# AudioSet class indices (yamnet_class_map.csv): the model's output columns.
SPEECH_CLASSES = [0, 2, 3]      # Speech; Conversation; Narration, monologue
MUSIC_CLASSES = [132]           # Music

# The classes whose per-track mean is kept as a fact (name -> column).
KEPT_CLASSES = {
    "singing": 24, "choir": 25, "yodeling": 26,
    "guitar": 135, "electric_guitar": 136, "bass_guitar": 137, "acoustic_guitar": 138,
    "steel_guitar": 139, "banjo": 142, "mandolin": 144, "piano": 148, "organ": 150,
    "drum_kit": 157, "violin": 186, "harmonica": 203, "accordion": 204,
    "rock_and_roll": 219, "rhythm_and_blues": 221, "soul": 222, "country": 224,
    "swing": 225, "bluegrass": 226, "folk": 228, "jazz": 230, "blues": 246,
    "vocal_music": 249, "a_capella": 250, "christian": 253, "gospel": 254,  # leak-scan-ok: a genre word, not a name
    "traditional": 259,
}
FULL_SR = 44100   # the era measurements need the full band, not YAMNet's 16 kHz

@dataclass
class Facts:
    """What is measured per track. Add a field here (and bump PROFILE_VERSION)
    when a rule needs something new; rules.py decides what it means."""
    duration: float
    talk_frames: float      # first HEAD_SECONDS: fraction of frames speech-and-not-music
    pitch_stable: float     # first HEAD_SECONDS: fraction of voiced frames in a held note
    head_talk_frames: float  # first EDGE_SECONDS
    head_pitch_stable: float
    tail_talk_frames: float  # last EDGE_SECONDS
    tail_pitch_stable: float
    # era (phase 1)
    bandwidth_hz: float = 0.0    # highest frequency within 50 dB of the peak, first HEAD_SECONDS
    stereo_corr: float = 1.0     # L/R correlation: ~1.0 is mono
    bitrate: int = 0             # kbps from the container; a codec lowpass looks like tape
    codec: str = ""
    # the tags that matter to the rules and the report
    date: str = ""
    artist: str = ""
    album: str = ""
    # YAMNet class means over the first HEAD_SECONDS, {name: mean}
    yamnet: dict | None = None
    # the ending (phase 3): levels in dB relative to the song's body, measured
    # back from the last moment of sound — a fade-out slides down for seconds,
    # a hard stop stays at level until the last one
    end_l1_db: float | None = None   # mean level of the last second of sound
    end_l3_db: float | None = None   # the second ending 3 s before the sound ends
    end_drop_s: float | None = None  # seconds from "still at level" to "gone"
    # loudness (phase 4): EBU R128 over the whole track, for a per-track gain
    # at playback (the Sound Check idea: nothing written back to the files)
    loudness_lufs: float | None = None   # integrated loudness
    true_peak_db: float | None = None    # dBTP
    loudness_range_lu: float | None = None


PROFILE_VERSION = 4   # bump when Facts gains a field: entries without it are re-measured


# --- listening -----------------------------------------------------------------

def decode(path: str, start: float | None = None, dur: float | None = None):
    """Mono 16 kHz float32 via ffmpeg; the only decoder the library needs."""
    import numpy as np
    cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", path]
    if dur is not None:
        cmd += ["-t", f"{dur:.3f}"]
    cmd += ["-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


ENDING_WIN = 0.1   # RMS window for the ending envelope


def ending_of(tail) -> dict:
    """How the sound ends, from the last EDGE_SECONDS: levels relative to the
    tail's body, in 100 ms RMS windows. The end of sound is the last window
    within 40 dB of the tail's peak (so a file's trailing silence is skipped);
    the body is everything up to 2 s before that. Returns the three `end_*`
    facts, or nothing when the tail is too short to say."""
    import numpy as np
    n = int(SR * ENDING_WIN)
    m = len(tail) // n
    if m < 30:
        return {}
    frames = tail[: m * n].reshape(m, n)
    db = 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-9)
    live = np.where(db > db.max() - 40)[0]
    end = int(live[-1])
    body = db[: max(1, end - 20)]
    ref = float(np.percentile(body, 60))

    def level(t: float) -> float:   # mean level of the second ending t s before the end
        a, b = max(0, end - int((t + 1) / ENDING_WIN)), max(1, end - int(t / ENDING_WIN))
        return float(db[a:b].mean() - ref)

    hi = np.where(db[: end + 1] >= ref - 6)[0]
    t_hi = int(hi[-1]) if len(hi) else 0
    lo = np.where(db[t_hi:end + 1] <= ref - 25)[0]
    t_lo = t_hi + int(lo[0]) if len(lo) else end
    return {"end_l1_db": round(level(1), 1), "end_l3_db": round(level(3), 1),
            "end_drop_s": round((t_lo - t_hi) * ENDING_WIN, 1)}


class Yamnet:
    def __init__(self, model: Path, threads: int = 1):
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads   # one core per worker process; parallelism is across tracks
        self.session = ort.InferenceSession(str(model), opts, providers=["CPUExecutionProvider"])

    def scores(self, x):
        import numpy as np
        if len(x) < SR:  # under a second: nothing to say
            return None
        return self.session.run(["output_0"], {"waveform": x.astype(np.float32)})[0]

    @staticmethod
    def talk_frames_of(scores) -> float:
        if scores is None:
            return 0.0
        speech = scores[:, SPEECH_CLASSES].max(axis=1)
        music = scores[:, MUSIC_CLASSES].max(axis=1)
        return float(((speech > 0.5) & (music < 0.5)).mean())

    def talk_frames(self, x) -> float:
        return self.talk_frames_of(self.scores(x))

    @staticmethod
    def class_means(scores) -> dict:
        if scores is None:
            return {}
        return {name: round(float(scores[:, col].mean()), 3) for name, col in KEPT_CLASSES.items()}


def pitch_stability(x, frame: int = 1024, hop: int = 512, fmin: float = 70.0, fmax: float = 1000.0,
                    run: int = 6, tol_cents: float = 35.0) -> float:
    """Fraction of voiced frames inside a held note. Autocorrelation pitch,
    voiced when periodic (peak > 0.6) and not near-silent."""
    import numpy as np
    lo, hi = int(SR / fmax), int(SR / fmin)
    f0 = []
    win = np.hanning(frame)
    for i in range(0, len(x) - frame, hop):
        w = x[i:i + frame] * win
        if np.sqrt(np.mean(w ** 2)) < 0.01:
            f0.append(0.0)
            continue
        ac = np.correlate(w, w, mode="full")[frame - 1:]
        ac = ac / (ac[0] + 1e-9)
        seg = ac[lo:hi]
        k = int(np.argmax(seg))
        f0.append(SR / (lo + k) if seg[k] > 0.6 else 0.0)
    f0 = np.array(f0)
    cents = np.where(f0 > 0, 1200 * np.log2(np.maximum(f0, 1.0) / 55.0), np.nan)
    voiced = ~np.isnan(cents)
    if voiced.sum() < 10:
        return 0.0
    stable = np.zeros(len(cents), bool)
    for i in range(len(cents) - run):
        seg = cents[i:i + run]
        if not np.isnan(seg).any() and (seg.max() - seg.min()) < tol_cents:
            stable[i:i + run] = True
    return float(stable[voiced].mean())


def decode_full(path: str, dur: float):
    """Stereo at the file's own band (resampled to 44.1 kHz) for the era facts."""
    import numpy as np
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-t", f"{dur:.3f}",
           "-ac", "2", "-ar", str(FULL_SR), "-f", "s16le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32).reshape(-1, 2) / 32768.0


LOUDNESS_RE = re.compile(r"^\s*(I|LRA|Peak):\s+(-?[\d.]+|-inf)\s+(LUFS|LU|dBFS)", re.M)


def loudness_of(path: str) -> dict:
    """EBU R128 over the whole track via ffmpeg's ebur128 filter (~1 s of
    CPU per track): integrated loudness, true peak, loudness range. Empty
    when ffmpeg can't say (a silent or undecodable file)."""
    cmd = ["ffmpeg", "-v", "info", "-nostdin", "-i", path, "-vn", "-af", "ebur128=peak=true", "-f", "null", "-"]
    out = subprocess.run(cmd, capture_output=True, text=True, errors="replace").stderr
    summary = out[out.rfind("Summary:"):]
    got = {m.group(1): m.group(2) for m in LOUDNESS_RE.finditer(summary)}
    facts = {}
    for key, name in (("I", "loudness_lufs"), ("Peak", "true_peak_db"), ("LRA", "loudness_range_lu")):
        try:
            facts[name] = round(float(got[key]), 1)
        except (KeyError, ValueError):
            pass
    return facts if "loudness_lufs" in facts else {}


def bandwidth_hz(mono, n: int = 4096, floor_db: float = -50.0) -> float:
    """Highest frequency whose long-term spectrum is within floor_db of the
    peak. Shellac transfers stop at 5-7 kHz, tape at 10-15, digital at the
    codec's lowpass."""
    import numpy as np
    if len(mono) < n * 4:
        return 0.0
    spec = np.zeros(n // 2 + 1)
    win = np.hanning(n)
    for i in range(0, len(mono) - n, n):
        spec += np.abs(np.fft.rfft(mono[i:i + n] * win)) ** 2
    spec /= spec.max() + 1e-12
    db = 10 * np.log10(spec + 1e-12)
    above = np.where(db > floor_db)[0]
    freqs = np.fft.rfftfreq(n, 1 / FULL_SR)
    return float(freqs[above[-1]]) if len(above) else 0.0


def stereo_corr(x) -> float:
    import numpy as np
    l, r = x[:, 0], x[:, 1]
    if np.std(l) < 1e-6 or np.std(r) < 1e-6:
        return 1.0
    return float(np.corrcoef(l, r)[0, 1])


def file_info(path: str) -> dict:
    """bitrate/codec and the tags the rules and report use."""
    out = {"bitrate": 0, "codec": "", "date": "", "artist": "", "album": ""}
    try:
        import mutagen
        f = mutagen.File(path, easy=True)
        if f is None:
            return out
        out["bitrate"] = int(getattr(f.info, "bitrate", 0) or 0) // 1000
        out["codec"] = type(f).__name__.replace("Easy", "").lower()
        t = f.tags or {}
        for k in ("date", "artist", "album"):
            v = t.get(k) or (t.get("originaldate") if k == "date" else None)
            out[k] = str(v[0]) if v else ""
    except Exception:
        pass
    return out


def duration_of(path: str) -> float:
    """ffprobe's container duration; some MP3s carry none ("N/A" — no Xing
    header), then the tag reader's estimate; then a full decode, counted."""
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", path], capture_output=True, text=True, check=True).stdout.strip()
    try:
        d = float(out)
        if d > 0:
            return d
    except ValueError:
        pass
    try:
        import mutagen
        f = mutagen.File(path)
        d = float(getattr(f.info, "length", 0.0) or 0.0) if f else 0.0
        if d > 0:
            return d
    except Exception:
        pass
    return len(decode(path)) / SR


def analyse(path: str, model: Yamnet) -> Facts:
    dur = duration_of(path)
    head = decode(path, dur=min(dur, HEAD_SECONDS))
    edge = min(EDGE_SECONDS, dur)
    first = head[: int(edge * SR)]
    last = decode(path, start=max(0.0, dur - edge), dur=edge) if dur > EDGE_SECONDS else head
    head_scores = model.scores(head)
    full = decode_full(path, min(dur, HEAD_SECONDS))
    info = file_info(path)
    return Facts(
        duration=dur,
        talk_frames=model.talk_frames_of(head_scores), pitch_stable=pitch_stability(head),
        head_talk_frames=model.talk_frames(first), head_pitch_stable=pitch_stability(first),
        tail_talk_frames=model.talk_frames(last), tail_pitch_stable=pitch_stability(last),
        bandwidth_hz=round(bandwidth_hz(full.mean(axis=1)), 0), stereo_corr=round(stereo_corr(full), 3),
        bitrate=info["bitrate"], codec=info["codec"],
        date=info["date"], artist=info["artist"], album=info["album"],
        yamnet=model.class_means(head_scores),
        **ending_of(last),
        **loudness_of(path),
    )


# --- the store -----------------------------------------------------------------

class Profile:
    """profile.json: {path: {size, mtime_ns, v, title, <Facts fields>}} —
    measurements only. Read (through rules.py) by the scanner and the DJ."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.data: dict[str, dict] = json.loads(path.read_text())
        except (OSError, ValueError):
            self.data = {}

    def current(self, track: str, st: os.stat_result) -> dict | None:
        e = self.data.get(track)
        if (e and e.get("size") == st.st_size and e.get("mtime_ns") == st.st_mtime_ns
                and e.get("v", 0) >= PROFILE_VERSION):
            return e
        return None

    def verdicts(self, overrides: dict[str, str] | None = None) -> dict[str, Verdict]:
        out = {p: evaluate(p, e, e.get("title", "")) for p, e in self.data.items()}
        return apply_overrides(out, overrides or {})

    def save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, separators=(",", ":"), sort_keys=True))
        os.replace(tmp, self.path)

    @staticmethod
    def load_verdicts(path: Path, overrides: Path | None = None) -> dict[str, Verdict]:
        """What the scanner and DJ consume: {path: Verdict} — the rules
        applied to the stored facts, then the overrides."""
        return Profile(path).verdicts(load_overrides(overrides))


def load_overrides(path: Path | None) -> dict[str, str]:
    if not path:
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return {p: k for p, k in data.items() if k in ("talk", "music", "shellac", "vintage", "hifi")}


def read_title(path: str) -> str:
    try:
        import mutagen
        f = mutagen.File(path, easy=True)
        return " ".join(str(x) for x in (f.tags.get("title") or [])) if f and f.tags else ""
    except Exception:
        return ""


def write_report(profile: Profile, report: Path, overrides: dict[str, str]) -> None:
    verdicts = profile.verdicts(overrides)
    rows = [(p, profile.data.get(p, {}), v) for p, v in verdicts.items() if v.talk or v.head_talk or v.tail_talk]
    eras = {e: sum(1 for v in verdicts.values() if v.era == e) for e in ("shellac", "vintage", "hifi", "")}
    lines = [f"# netradio profile report — {time.strftime('%Y-%m-%d %H:%M')}",
             f"# {len(profile.data)} tracks profiled; {sum(1 for r in rows if r[2].talk)} talk (kept off the stations), "
             f"{sum(1 for r in rows if r[2].tail_talk)} end in chatter, {sum(1 for r in rows if r[2].head_talk)} start with it.",
             f"# era: {eras['shellac']} shellac, {eras['vintage']} vintage, {eras['hifi']} hifi, {eras['']} not yet measured.",
             f"# Overrides ({len(overrides)}): a JSON object {{path: \"talk\" | \"music\" | \"shellac\" | \"vintage\" | \"hifi\"}} next to this file wins.", ""]
    for p, e, v in sorted(rows, key=lambda r: (r[0] not in overrides, not r[2].talk, r[0])):
        kind = "TALK " if v.talk else ("tail " if v.tail_talk else "head ")
        ov = " (override)" if p in overrides else ""
        lines.append(f"{kind} {e.get('duration', 0):6.0f}s  {e.get('title', '')[:50]!r:52} {p}{ov}  [{v.reason}]")
    report.write_text("\n".join(lines) + "\n")


# --- the worker pool -----------------------------------------------------------
# One process per core, each with its own model session (the session is not
# shareable across processes). Tracks are independent, so this is a plain
# map; the parent owns profile.json and saves as results come in.

_worker_model: Yamnet | None = None
_worker_model_path = ""
_worker_remotes: list[str] = []


def _worker_init(model_path: str, remotes: list[str] | None = None) -> None:
    global _worker_model_path, _worker_remotes
    _worker_model_path = model_path
    _worker_remotes = list(remotes or [])
    if not _worker_remotes:
        _load_local_model()


def _load_local_model() -> None:
    global _worker_model
    if _worker_model is None:
        _worker_model = Yamnet(Path(_worker_model_path), threads=1)


def analyse_remote(track: str, url: str, timeout: float = 300.0) -> tuple[dict, str]:
    """POST the file to a profile server (profile_server.py); (facts, title).
    Raises on any failure — the caller falls back to local."""
    import urllib.request
    data = Path(track).read_bytes()
    req = urllib.request.Request(f"{url.rstrip('/')}/analyse", data=data, method="POST",
                                 headers={"Content-Type": "application/octet-stream",
                                          "X-Filename": Path(track).name})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.load(r)
    if out.get("profile_version") != PROFILE_VERSION:
        raise RuntimeError(f"profile server is at version {out.get('profile_version')}, this is {PROFILE_VERSION}")
    return out["facts"], out.get("title", "")


def remote_ready(url: str) -> tuple[bool, str]:
    """(usable, why): the profile server answers /health at THIS version."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/health", timeout=10) as r:
            v = json.load(r).get("profile_version")
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if v != PROFILE_VERSION:
        return False, f"version {v}, this is {PROFILE_VERSION}"
    return True, "ok"


def wait_for_remotes(urls: list[str], wait: float) -> list[str]:
    """The remotes that are up at this version, giving each up to `wait`
    seconds to get there — a deploy reaches the fast box a few minutes
    after this one, and profiling locally instead takes hours."""
    ready, t0 = [], time.monotonic()
    for url in urls:
        while True:
            ok, why = remote_ready(url)
            if ok:
                ready.append(url)
                break
            if time.monotonic() - t0 > wait:
                log.warning("profile server %s not usable (%s); measuring locally", url, why)
                break
            log.info("waiting for profile server %s (%s)", url, why)
            time.sleep(30)
    return ready


def _worker_analyse(track: str) -> tuple[str, dict | None, str]:
    """(track, facts-as-dict or None, error-or-title). A remote server is
    tried first when configured (the fast box); anything failing there —
    box off, timeout, 5xx — falls back to measuring here."""
    for url in _worker_remotes:
        try:
            facts, title = analyse_remote(track, url)
            return track, facts, title
        except Exception as e:
            log.debug("remote %s failed for %s: %s", url, track, e)
    try:
        _load_local_model()
        f = analyse(track, _worker_model)
        return track, asdict(f), read_title(track)
    except Exception as e:  # reported by the parent, the pool keeps going
        return track, None, f"{type(e).__name__}: {e}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--playlist", required=True, type=Path, help="the m3u naming every track (all.m3u)")
    ap.add_argument("--model", required=True, type=Path, help="yamnet.onnx")
    ap.add_argument("--profile", required=True, type=Path, help="profile.json (read + written)")
    ap.add_argument("--overrides", type=Path, help="profile-overrides.json")
    ap.add_argument("--report", type=Path, help="human-readable list of what was flagged")
    ap.add_argument("--limit", type=int, default=0, help="stop after N new tracks (0 = all)")
    ap.add_argument("--workers", type=int, default=0, help="worker processes (0 = one per core)")
    ap.add_argument("--remote", action="append", default=[],
                    help="profile server URL to try first (repeatable); local analysis is the fallback")
    ap.add_argument("--remote-wait", type=float, default=900.0,
                    help="seconds to wait for a remote to be up at this profile version before going local")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s", stream=sys.stdout)

    if not args.playlist.exists():
        log.warning("%s does not exist yet (the scanner writes it) — nothing to profile", args.playlist)
        return 0
    tracks = [l.rstrip("\n") for l in args.playlist.read_text().splitlines() if l.strip() and not l.startswith("#")]
    profile = Profile(args.profile)
    todo: list[tuple[str, os.stat_result]] = []
    skipped = 0
    for track in tracks:
        try:
            st = os.stat(track)
        except OSError:
            continue
        if profile.current(track, st):
            skipped += 1
            continue
        todo.append((track, st))
        if args.limit and len(todo) >= args.limit:
            break
    stats = {t: st for t, st in todo}
    workers = args.workers or (os.cpu_count() or 1)
    if todo and args.remote:
        args.remote = wait_for_remotes(args.remote, args.remote_wait)
    log.info("%d tracks to analyse (%d cached), %d workers%s", len(todo), skipped, workers,
             f", remote first: {', '.join(args.remote)}" if args.remote else "")

    done = failed = 0
    t0 = time.monotonic()
    if todo:
        import multiprocessing as mp
        ctx = mp.get_context("spawn")   # a fresh interpreter per worker: no forked ONNX state
        with ctx.Pool(workers, initializer=_worker_init, initargs=(str(args.model), args.remote)) as pool:
            for track, facts, extra in pool.imap_unordered(_worker_analyse, [t for t, _ in todo], chunksize=4):
                if facts is None:
                    failed += 1
                    log.warning("could not analyse %s: %s", track, extra)
                    continue
                st = stats[track]
                title = extra
                profile.data[track] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "v": PROFILE_VERSION,
                                       "title": title, **facts}
                v = evaluate(track, profile.data[track], title)
                done += 1
                if v.talk:
                    log.info("talk: %r %.0fs (%s) %s", title, facts["duration"], v.reason, track)
                if done % 200 == 0:
                    profile.save()
                    log.info("%d analysed, %d cached, %d failed, %.2f s/track wall", done, skipped, failed,
                             (time.monotonic() - t0) / done)
    # drop entries for files that no longer exist in the playlist
    present = set(tracks)
    for p in [p for p in profile.data if p not in present]:
        del profile.data[p]
    profile.save()
    overrides = load_overrides(args.overrides)
    if args.report:
        write_report(profile, args.report, overrides)
    verdicts = profile.verdicts(overrides).values()
    talk = sum(1 for v in verdicts if v.talk)
    stops = sum(1 for v in verdicts if v.hard_stop and not v.talk)
    log.info("done: %d analysed this run, %d cached, %d failed; %d of %d tracks are talk, %d end on a hard stop",
             done, skipped, failed, talk, len(profile.data), stops)
    lufs = sorted(e["loudness_lufs"] for e in profile.data.values() if e.get("loudness_lufs") is not None)
    if lufs:
        log.info("loudness measured for %d tracks: median %.1f LUFS, quietest tenth under %.1f, loudest tenth over %.1f",
                 len(lufs), lufs[len(lufs) // 2], lufs[len(lufs) // 10], lufs[-len(lufs) // 10])
    return 0
