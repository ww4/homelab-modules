"""Read the transmission off the listing photos when nothing else settles it.

Only runs when it matters: the transmission is still open after Facebook's
field, the listing text and the EPA model-year list, or "manual" rests on the
text alone; and the model year was actually offered with a manual.

Biased toward automatic, which most cars are: an answer counts only at high or
medium confidence, and overriding a stated "manual" takes high. A blind test on
this watch's photos (2026-11-24) was right 45 of 45 times at high/medium
confidence and wrong once at low, so low is ignored. Interior shots are
usually late in a listing, so every photo is fetched for this (up to 12).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..config import Settings
from ..models import Candidate
from ..store import Store

log = logging.getLogger(__name__)

MAX_PHOTOS = 12
COUNTS = ("high", "medium")

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "interior_photo": {"type": ["integer", "null"],
                           "description": "number of the photo that shows the shifter area, if any"},
        "shifter_visible": {"type": "boolean"},
        "transmission": {"type": "string", "enum": ["automatic", "manual", "unclear"]},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "evidence": {"type": "string", "description": "what in the photo shows it, one sentence"},
    },
    "required": ["interior_photo", "shifter_visible", "transmission", "confidence", "evidence"],
    "additionalProperties": False,
}

SYSTEM = """You look at used-car listing photos and decide, from the photos alone, whether the car has an automatic or a manual transmission.

Automatic: a gear selector marked P R N D (often with L, 2, 1, S, M or +/-), a column-mounted selector on the steering column, a rotary dial, or push buttons; no clutch pedal.
Manual: a shift knob with a numbered H-pattern (1-2-3-4-5, R), a stick in a boot with no P R N D plate, and/or a clutch pedal (three pedals in the footwell).

Rules:
- Use only what is visible. Do not guess from the kind of car.
- "high" only when the selector markings, the H-pattern, or the pedals are clearly readable/visible.
- If no photo shows the shifter area, or it is too blurry/dark/small to read, answer "unclear".
- A PRND gate with a +/- or M side gate is still an automatic."""


def needs_check(facts: dict[str, Any]) -> bool:
    if facts.get("manual_offered") is False:
        return False  # the EPA list says every version was automatic (or electric)
    if not facts.get("transmission"):
        return True
    # "manual" from the text alone: worth a look, the photos may say otherwise.
    return facts.get("transmission") == "manual" and facts.get("transmission_source") == "listing text"


def interpret(facts: dict[str, Any], raw: dict[str, Any]) -> None:
    said, conf = raw.get("transmission"), raw.get("confidence")
    n = raw.get("interior_photo")
    facts["transmission_photo_check"] = f"{said} ({conf})"
    if said not in ("automatic", "manual") or conf not in COUNTS:
        return
    where = f"photo {n}" if n else "the photos"
    stated_manual = facts.get("transmission") == "manual"
    if said == "automatic" and stated_manual and conf != "high":
        facts["transmission_note"] = f"the text says manual; {where} looks automatic ({conf} confidence)"
        return
    if said == "automatic" and stated_manual:
        facts["transmission_note"] = "the text said manual, but the photos clearly show an automatic"
    facts["transmission"], facts["transmission_source"] = said, "photos"
    facts["transmission_photo"] = n
    facts["transmission_evidence"] = f"{where}: {raw.get('evidence', '').strip()}"


async def apply(
    c: Candidate,
    settings: Settings,
    store: Store,
    download: Callable[[Candidate, Path, int], Awaitable[list[Path]]],
) -> None:
    """Fetch all photos, ask once (cached per listing and photo count), and record the answer."""
    from .. import judge  # late: judge imports models/brief, not vehicles

    images = c.detail.images if c.detail else []
    if not images:
        return
    key = f"shifter:{c.listing.id}:{len(images)}"
    raw = store.cache_get(key, max_age=None)
    if raw is None:
        workdir = settings.photos_dir / c.listing.id
        photos = await download(c, workdir, MAX_PHOTOS)
        if not photos:
            return
        listed = "\n".join(f"{i + 1}. {p}" for i, p in enumerate(photos))
        try:
            raw = await judge.ask(
                settings,
                prompt_cli=f"Photos of one listing:\n{listed}\nRead each photo, then answer.",
                prompt_api="These are the photos of one listing, in order (photo 1 first). Answer from them.",
                schema=SCHEMA, system=SYSTEM, effort="low", photos=photos, workdir=workdir,
            )
        except Exception as e:  # noqa: BLE001 - the check is a bonus; the listing stays "not stated"
            log.warning("photo transmission check failed for %s: %s", c.listing.id, e)
            return
        store.cache_put(key, raw)
    interpret(c.facts, raw)


__all__ = ["apply", "interpret", "needs_check"]
