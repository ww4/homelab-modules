"""The rules: what the facts in profile.json mean for a station.

profile.json holds only what was MEASURED per track (netradio profile — a
few hours of listening for the whole library). This file turns those facts
into verdicts, and is evaluated every time the scanner or the DJ reads the
profile — so changing a threshold, or adding a filter over facts already
measured, takes effect the same night with no re-listening. A rule that
needs a new fact adds it to `Facts` in profile.py and is measured on the
next pass (the cache key is the file, so only the field's absence triggers
re-analysis if you bump PROFILE_VERSION there).

Add a filter: write a function over (path, facts, title) that returns the
tag names it asserts, and list it in RULES. Tags are what consumers act on:

  talk        keep the track off every station (the scanner drops it)
  head_talk   starts with chatter: the DJ doesn't fade the previous song in
              over it, and gives the previous song a short exit
  tail_talk   ends with chatter: the DJ lets it finish, no crossfade
  hard_stop   ends on a stop, not a fade (the bluegrass ending): the DJ lets
              it land — no fade-out, no overlap, then the next song
  gain_db     what to amplify the track by so it plays at the station's
              target loudness (Sound Check, done our way): from the
              measured EBU R128 loudness, capped so the true peak can't clip

and an `era` — an audio-quality bucket, not a date (the library's dates are
reissue dates): `shellac` (old scratchy records), `vintage` (tape era),
`hifi` (modern). Stations filter on it. Empty when the track was profiled
before era facts existed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Verdict:
    talk: bool
    head_talk: bool
    tail_talk: bool
    reason: str
    era: str = ""      # "", "shellac", "vintage", "hifi"
    hard_stop: bool = False
    gain_db: float | None = None   # None = unmeasured, play as is


ERAS = ("shellac", "vintage", "hifi")


# Thresholds from the labelled set (see the profile.py docstring). Both must
# hold: the classifier hears speech and no music, AND nobody is holding notes.
TALK_FRAMES_MIN = 0.45
PITCH_STABLE_MAX = 0.15

TITLE_HINT = re.compile(r"\b(intro|introduction|introducing|talking intro|spoken|banter|"
                        r"announcer|band intro|stage talk|monolog|applause|tuning)\b", re.I)


def is_talk(talk_frames: float, pitch_stable: float) -> bool:
    return talk_frames >= TALK_FRAMES_MIN and pitch_stable < PITCH_STABLE_MAX


def rule_talk(path: str, f: dict, title: str) -> list[str]:
    """The first minute is speech (a whole talk track, OR a song with a long
    spoken intro — either way not wanted on a radio station)."""
    return ["talk"] if is_talk(f.get("talk_frames", 0.0), f.get("pitch_stable", 1.0)) else []


def rule_edges(path: str, f: dict, title: str) -> list[str]:
    tags = []
    if is_talk(f.get("head_talk_frames", 0.0), f.get("head_pitch_stable", 1.0)):
        tags.append("head_talk")
    if is_talk(f.get("tail_talk_frames", 0.0), f.get("tail_pitch_stable", 1.0)):
        tags.append("tail_talk")
    return tags


# --- the ending ----------------------------------------------------------------
# From 116 tracks (Monroe / Osbornes / Flatt & Scruggs / Martin / Skaggs
# studio sides against Dire Straits / Eagles / Fleetwood Mac / Krauss
# fade-outs, 2026-09-16): a hard stop is still at the song's level 3 s
# before the sound ends and gone within ~2.5 s (a final chord ringing out
# can pull its last second down to -16 dB); a fade has been sliding for 5 s
# or longer and is 17 dB or more down by the last second. The thresholds
# lean toward calling a stop: a fade tagged as a stop only loses a
# crossfade it had already faded through, a stop tagged as a fade gets the
# crossfade the owner does not want. Unmeasured tails (pre-v3) say nothing.
HARD_STOP_L1_MIN = -15.0
HARD_STOP_L3_MIN = -6.0
HARD_STOP_DROP_MAX = 3.0


def rule_ending(path: str, f: dict, title: str) -> list[str]:
    l1, l3, drop = f.get("end_l1_db"), f.get("end_l3_db"), f.get("end_drop_s")
    if l1 is None or l3 is None or drop is None:
        return []
    if l1 >= HARD_STOP_L1_MIN or (l3 >= HARD_STOP_L3_MIN and drop <= HARD_STOP_DROP_MAX):
        return ["hard_stop"]
    return []


# --- loudness ----------------------------------------------------------------
# Every track is brought to TARGET_LUFS at playback. -16 is where the
# streaming services and most radio processing sit; old records measured
# around -18 to -20 get a lift, modern masters at -8 to -10 come down. The
# gain is capped so the true peak stays under PEAK_CEILING_DB (no clipping
# for a quiet-but-spiky track), and within ±GAIN_LIMIT_DB — beyond that the
# measurement is more likely wrong than the mastering.
TARGET_LUFS = -16.0
PEAK_CEILING_DB = -1.0
GAIN_LIMIT_DB = 15.0


def gain_for(f: dict) -> float | None:
    lufs, peak = f.get("loudness_lufs"), f.get("true_peak_db")
    if lufs is None or lufs < -60:
        return None
    gain = TARGET_LUFS - lufs
    if peak is not None:
        gain = min(gain, PEAK_CEILING_DB - peak)
    return round(max(-GAIN_LIMIT_DB, min(GAIN_LIMIT_DB, gain)), 1)


# --- era ---------------------------------------------------------------------
# From 35 tracks of certain era (profile.py docstring): shellac sides stop at
# 5-7 kHz and are mono; tape reaches 10-15 kHz, mostly mono; modern masters
# reach the codec's lowpass and are stereo. A low-bitrate MP3 caps the band
# too, so a file whose band ends where its codec ends says nothing about the
# recording — that is treated as modern unless it is also mono.
SHELLAC_MAX_HZ = 8000.0
# Stereo: modern VBR MP3s lowpass anywhere from 11.5 kHz up, and the average
# bitrate doesn't say where (Skaggs 1997 at 11.8-13.6 kHz, 190 kbps). Tape-era
# stereo mostly sits at 10-11 (Flatt & Scruggs 10.3); a wide-stereo 60s
# reissue at 12.0 is the one measured exception and reads as hifi.
HIFI_MIN_HZ = 11500.0
HIFI_MONO_MIN_HZ = 15000.0   # a modern mono master (solo guitar) still reaches this
MONO_CORR = 0.98


def codec_cap_hz(f: dict) -> float:
    """Where the codec's lowpass sits for this file's bitrate (lossy only)."""
    codec = (f.get("codec") or "").lower()
    if codec in ("flac", "wave", "aiff", "wavpack", ""):
        return 22050.0
    br = int(f.get("bitrate") or 0)
    if br <= 0:
        return 22050.0
    for limit, cap in ((64, 10000.0), (96, 12000.0), (112, 13500.0), (128, 16000.0), (160, 17000.0)):
        if br <= limit:
            return cap
    return 20000.0


def tag_year(f: dict) -> int:
    m = re.match(r"(\d{4})", str(f.get("date") or ""))
    return int(m.group(1)) if m else 0


def era_of(f: dict) -> str:
    bw = float(f.get("bandwidth_hz") or 0.0)
    if bw <= 0.0:
        return ""
    mono = float(f.get("stereo_corr", 1.0)) >= MONO_CORR
    cap = codec_cap_hz(f)
    codec_limited = bw >= 0.8 * cap
    if bw < SHELLAC_MAX_HZ and mono and cap >= 10000.0:
        return "shellac"
    if bw >= HIFI_MONO_MIN_HZ or (bw >= HIFI_MIN_HZ and not mono) or (codec_limited and not mono):
        # The tag date is a reissue date more often than not, so it can only
        # ever DEMOTE: a clean stereo file that honestly says 1965 is the
        # tape era; a modern recording never carries a date like that.
        year = tag_year(f)
        return "vintage" if 0 < year <= 1979 else "hifi"
    return "vintage"


def rule_era(path: str, f: dict, title: str) -> list[str]:
    era = era_of(f)
    return [f"era:{era}"] if era else []


RULES = [rule_talk, rule_edges, rule_ending, rule_era]


def evaluate(path: str, facts: dict, title: str = "") -> Verdict:
    tags: set[str] = set()
    for rule in RULES:
        tags.update(rule(path, facts, title))
    talk = "talk" in tags
    if talk:
        reason = f"speech {facts.get('talk_frames', 0):.2f}, held notes {facts.get('pitch_stable', 0):.2f}"
        if TITLE_HINT.search(title or ""):
            reason += ", title agrees"
    else:
        reason = "music"
    era = next((t[4:] for t in tags if t.startswith("era:")), "")
    return Verdict(talk=talk, head_talk="head_talk" in tags and not talk,
                   tail_talk="tail_talk" in tags and not talk, reason=reason, era=era,
                   hard_stop="hard_stop" in tags, gain_db=gain_for(facts))


def apply_overrides(verdicts: dict[str, Verdict], overrides: dict[str, str]) -> dict[str, Verdict]:
    """A hand-written {path: "talk" | "music" | "shellac" | "vintage" | "hifi"}
    wins over the rules; the edge hints are kept either way."""
    for p, kind in overrides.items():
        v = verdicts.get(p, Verdict(False, False, False, ""))
        if kind in ERAS:
            verdicts[p] = Verdict(v.talk, v.head_talk, v.tail_talk, f"{v.reason}; era override: {kind}", kind, v.hard_stop, v.gain_db)
        else:
            verdicts[p] = Verdict(kind == "talk", v.head_talk, v.tail_talk, f"override: {kind}", v.era, v.hard_stop, v.gain_db)
    return verdicts
