"""Turn a feed's title + description into a rule, with the agent's help.

The owner adds "Western Swing — Bob Wills-style dance-hall country with steel and
fiddle, 1930s–50s and revivalists" on the admin page. The admin service
files a compile request; a path unit runs (as the claude user):

    netradio compile-prompt --config C --feed ID   > prompt
    claude -p "$(cat prompt)"                      > result
    netradio compile-apply  --config C --feed ID --result result

The prompt carries what the rule can be made of — the library's artists
with their track counts, the genre words in use, the instrument measures —
and asks for one JSON object. `compile-apply` validates it, stores the rule,
marks the feed ready and requests an apply (scan + Liquidsoap restart).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from netradio import feeds as feedrules
from netradio.config import FAMILIES, Config

INSTRUMENTS = ("banjo", "mandolin", "violin", "steel_guitar", "acoustic_guitar", "electric_guitar", "piano",
               "organ", "harmonica", "accordion", "drum_kit", "a_capella", "choir", "singing", "yodeling")


def build_prompt(cfg: Config, feed_id: str, genre_words: list[str]) -> str:
    feed = cfg.feeds()[feed_id]
    artists = cfg.artists()
    inv = "\n".join(f"- {name} ({info['tracks']} tracks; {', '.join(info.get('families') or ['unknown'])})"
                    for name, info in sorted(artists.items(), key=lambda kv: -kv[1]["tracks"]))
    return f"""You are helping build a specialty radio feed from a personal music library.

Feed title: {feed.get('title', feed_id)}
Description: {feed.get('description', '')}

Produce a RULE that selects the right tracks from this library. A rule is a JSON object with any of:
- "artists": list of artist names (matched case-insensitively as substrings of the track's artist, so "Louvin Brothers" hits "The Louvin Brothers"). ONLY use names from the artist inventory below; do not invent artists we do not own.
- "genres": list of genre words matched as whole words in the track's genre tag. Words in use in this library: {', '.join(sorted(genre_words))}
- "instruments": object of measured instrument presence -> [min, max] (either may be null). Values are 0..1 means from an audio classifier over the first minute; typical strong presence is 0.2-0.4, near-absence under 0.03. Available: {', '.join(INSTRUMENTS)}. Use these only when the description is about instrumentation (e.g. "banjo instrumentals": {{"banjo": [0.3, null], "singing": [null, 0.05]}}).
- "exclude_genres": list of genre words that keep a track OUT even when it matches (a country feed that must not drift into bluegrass: {{"exclude_genres": ["bluegrass", "old time", "newgrass"]}}).
- "era": {{"only": [...]}} or {{"exclude": [...]}} over "shellac" (1920s-40s records), "vintage" (tape era), "hifi" (modern). Omit unless the description is about recording era.
Clauses combine with AND: a track must hit one of artists/genres (if any are given), AND satisfy every instrument threshold (if given), AND satisfy era. So "old-time fiddle tunes" is {{"genres": ["old time", "oldtime"], "instruments": {{"violin": [0.15, null]}}}}; "brother duets" is just artists (plus "genres": ["brother duets"]).

Also choose "family": a list from {', '.join(FAMILIES)} — which broad stations this feed fits (a western swing feed fits ["country"]; brother duets fit ["bluegrass", "country"]).

Artist inventory (name, track count, families):
{inv}

Answer with ONE JSON object and nothing else, of the form:
{{"rule": {{...}}, "family": [...], "note": "one sentence on how you built it and what is thin"}}
"""


def parse_result(text: str) -> dict:
    """The JSON object in the model's answer, wherever it sits — the answer
    may be wrapped in a code fence or prose with braces of its own, so try
    to decode at every `{` and take the first object that has a rule. When
    the object carries no note but the model wrote prose around it, that
    prose becomes the note: what it found should never be lost."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
    dec = json.JSONDecoder()
    first_error = None
    for m in re.finditer(r"\{", text):
        try:
            obj, end = dec.raw_decode(text, m.start())
        except ValueError as e:
            first_error = first_error or e
            # an answer cut off before its closing brace(s) — seen 2026-09-17,
            # `{"rule": {...}, "family": [...], "note": "..."` with no final
            # `}` — is worth more than its inner rule object: try closing it
            obj = None
            for tail in ('"}', "}", '"]}', "]}", "}}", '"}}'):
                try:
                    obj, end = dec.raw_decode(text[m.start():] + tail, 0)
                    end += m.start()
                    break
                except ValueError:
                    continue
            if obj is None:
                continue
        if isinstance(obj, dict) and ("rule" in obj or "artists" in obj or "genres" in obj):
            obj = obj if "rule" in obj else {"rule": obj}
            if not str(obj.get("note") or "").strip():
                prose = " ".join((text[:m.start()] + " " + text[end:]).split())
                prose = re.sub(r"^```\w*\s*|\s*```$", "", prose).strip()
                if prose:
                    obj["note"] = prose[:900]
            return obj
    raise ValueError(f"no JSON object with a rule in the result ({first_error or 'no braces at all'})")


def apply_result(cfg: Config, feed_id: str, result: dict) -> list[str]:
    """Validate and store; returns problems (empty = stored, feed ready)."""
    rule = result.get("rule")
    if not isinstance(rule, dict):
        return ["result has no rule object"]
    errs = feedrules.validate(rule)
    family = result.get("family") or ["any"]
    if not isinstance(family, list) or any(f not in FAMILIES for f in family):
        errs.append(f"family must be a list from {FAMILIES}")
    feeds = cfg.feeds()
    feed = feeds.setdefault(feed_id, {})
    if errs:
        feed["status"] = "failed"
        feed["note"] = "; ".join(errs)
        cfg.save_feeds(feeds)
        return errs
    feed.update({"rule": rule, "family": family, "status": "ready",
                 "note": str(result.get("note") or "").strip() or "the model returned the rule without a note"})
    cfg.save_feeds(feeds)
    cfg.request("apply", {"reason": f"feed {feed_id} compiled"})
    return []


def main_prompt(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="print the compile prompt for a feed")
    ap.add_argument("--config", required=True, type=Path)
    ap.add_argument("--feed", required=True)
    ap.add_argument("--genre-words", type=Path, help="JSON list of genre words in the library (optional)")
    args = ap.parse_args(argv)
    words = []
    if args.genre_words and args.genre_words.exists():
        words = json.loads(args.genre_words.read_text())
    sys.stdout.write(build_prompt(Config(args.config), args.feed, words))
    return 0


def main_apply(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="store a compiled rule for a feed")
    ap.add_argument("--config", required=True, type=Path)
    ap.add_argument("--feed", required=True)
    ap.add_argument("--result", required=True, type=Path)
    args = ap.parse_args(argv)
    cfg = Config(args.config)
    try:
        raw = args.result.read_text()
        cfg.keep_answer(args.feed, raw)
        result = parse_result(raw)
    except (OSError, ValueError) as e:
        feeds = cfg.feeds()
        feeds.setdefault(args.feed, {}).update({"status": "failed", "note": f"could not read the model's answer: {e}"})
        cfg.save_feeds(feeds)
        print(f"failed: {e}", file=sys.stderr)
        return 1
    errs = apply_result(cfg, args.feed, result)
    if errs:
        print("rejected: " + "; ".join(errs), file=sys.stderr)
        return 1
    print(f"feed {args.feed}: rule stored, apply requested")
    return 0
