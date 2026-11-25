"""What a model year was actually sold as, from the EPA list (data/vehicle_configs.json).

Answers the questions a seller shouldn't have to: was a manual ever offered on
a 2008 CR-V (no), is every Murano a CVT (yes), was there a FWD Forester (no).
Facts are only implied when EVERY US configuration agrees; otherwise the
options are recorded and the listing still has to say.

Not covered (EPA doesn't rate them): vehicles over 8,500 lb GVWR, i.e.
heavy-duty pickups and the Hummer H2. Those return nothing rather than
borrowing a light-duty sibling's facts.

Rebuild the data with tools/build_vehicle_configs.py.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any

ALL_WHEEL = {"awd", "4wd", "4x4"}
# 4x4 is EPA's pre-2010 "4-Wheel or All-Wheel Drive": one label for both.
DRIVE_LABEL = {"fwd": "FWD", "rwd": "RWD", "2wd": "2WD", "awd": "AWD", "4wd": "4WD", "4x4": "4WD/AWD"}
HYBRID = re.compile(r"\b(hybrid|plug-?in|phev|prime|energi)\b", re.I)
ELECTRIC = re.compile(r"\b(ev|electric|lightning)\b", re.I)

# Seller spellings -> EPA make. Multi-word makes are matched squashed ("landrover").
MAKE_ALIASES = {
    "chevy": "Chevrolet", "chev": "Chevrolet", "vw": "Volkswagen", "mercedes": "Mercedes-Benz",
    "benz": "Mercedes-Benz", "mercedesbenz": "Mercedes-Benz", "landrover": "Land Rover",
    "alfaromeo": "Alfa Romeo", "rangerover": "Land Rover", "mini": "MINI",
}
# Ram trucks were Dodge until 2011 and EPA files them under both.
SAME_MAKE = {"Ram": ("Ram", "Dodge"), "Dodge": ("Dodge", "Ram")}

# Not in the EPA data; a light-duty sibling's facts would be wrong for them.
HEAVY = re.compile(
    r"\b((silverado|sierra|ram|chevy|chevrolet|gmc|dodge|[ck])\s*-?\s*[2-5]500|[2-5]500\s*hd|hd|heavy\s*duty"
    r"|super\s*duty|f-?\s?(250|350|450|550)|h1|h2)\b", re.I)
DISPL = re.compile(r"(?<![\d.])([1-8]\.\d)\s*(?:l\b|liter|litre)?", re.I)


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


@dataclass(frozen=True)
class Config:
    displ: str
    drive: str
    trans: str  # manual | auto | cvt | dct | single
    spec: str
    kind: str = ""  # "" | hybrid | phev | ev
    erange: int = 0  # electric range when new, miles (EV: all of it; plug-in: battery only)
    variant: str = ""  # EV/plug-in trim as EPA names it ("Long Range")


@lru_cache(maxsize=1)
def _data() -> dict[str, Any]:
    raw = resources.files("market_agent").joinpath("data/vehicle_configs.json").read_text()
    return json.loads(raw)


# Model names that are also ordinary listing words; never matched without the make.
NOT_ALONE = {"sport", "limited", "touring", "classic", "coupe", "sedan", "wagon", "convertible", "truck",
             "pickup", "cargo", "hybrid", "base", "premium", "special", "custom", "escape", "element",
             "pilot", "liberty", "patriot", "compass", "commander", "odyssey", "passport", "ranger",
             "focus", "fusion", "edge", "flex", "journey", "spirit", "aspire", "probe", "contour", "eclipse"}


@lru_cache(maxsize=1)
def _index() -> tuple[dict[str, str], dict[str, list[tuple[str, str]]], dict[str, set[str]]]:
    """make squash -> EPA make; EPA make -> [(model part squashed, key)]; part -> keys (any make)."""
    makes: dict[str, str] = {}
    models: dict[str, list[tuple[str, str]]] = {}
    anywhere: dict[str, set[str]] = {}
    for key in _data()["models"]:
        make, model = key.split("|", 1)
        makes[_squash(make)] = make
        for part in model.split("/"):
            if sq := _squash(part):
                models.setdefault(make, []).append((sq, key))
                anywhere.setdefault(sq, set()).add(key)
                if len(sq) <= 3:  # "Mazda 5" is written "mazda5"
                    models[make].append((_squash(make) + sq, key))
    for alias, make in MAKE_ALIASES.items():
        makes.setdefault(alias, make)
    return makes, models, anywhere


def _grams(text: str) -> set[str]:
    toks = [_squash(t) for t in re.split(r"[\s·,|()]+", text) if _squash(t)]
    out = set(toks)
    for n in (2, 3):
        out.update("".join(toks[i:i + n]) for i in range(len(toks) - n + 1))
    return out


@dataclass
class Lookup:
    make: str
    model: str  # as EPA names it
    year: int
    configs: list[Config]
    narrowed_by: list[str]


def lookup(text: str, year: int | None, make: str | None = None, model: str | None = None) -> Lookup | None:
    """The configurations sold in the US for this listing's model year, or None."""
    if year is None or HEAVY.search(f"{text} {model or ''}"):
        return None
    makes, models, anywhere = _index()
    grams = _grams(f"{make or ''} {model or ''} {text}")
    found = {makes[g] for g in grams if g in makes}
    if make and _squash(make) in makes:
        found = {makes[_squash(make)]}
    candidates = {m2 for m in found for m2 in SAME_MAKE.get(m, (m,))}
    hits = [(len(sq), key) for m in candidates for sq, key in models.get(m, []) if sq in grams]
    if not candidates:
        # No make in the title ("2006 F150 Lariat"): a model name only one make uses.
        for g in grams:
            keys_g = anywhere.get(g, set())
            if (len(g) >= 5 or (len(g) == 4 and re.search(r"\d", g) and re.search(r"[a-z]", g))) and g not in NOT_ALONE and len({k.split("|")[0] for k in keys_g}) == 1:
                hits.extend((len(g), k) for k in keys_g)
    # Longest model name that appears and has this year wins ("grand cherokee"
    # over "cherokee"; Ram's 2016 "1500" over Dodge's older "Ram 1500").
    configs: set[Config] = set()
    keys: list[str] = []
    for n in sorted({n for n, _ in hits}, reverse=True):
        keys = sorted({key for m, key in hits if m == n})
        for key in keys:
            for y0, y1, cs in _data()["models"][key]:
                if y0 <= year <= y1:
                    configs.update(Config(*c) for c in cs)
        if configs:
            break
    if not configs:
        return None
    keys = [k for k in keys if any(y0 <= year <= y1 for y0, y1, _ in _data()["models"][k])]
    mk, md = keys[0].split("|", 1)
    # "Legacy/Outback" is one EPA name for two models; show the one the seller wrote.
    md = next((p for p in md.split("/") if _squash(p) in grams or _squash(mk) + _squash(p) in grams), md)
    return Lookup(make=mk, model=md, year=year, configs=sorted(configs, key=lambda c: (c.displ, c.drive, c.trans)),
                  narrowed_by=[])


def _narrow(cs: list[Config], keep, why: str, notes: list[str]) -> list[Config]:
    kept = [c for c in cs if keep(c)]
    if kept and len(kept) < len(cs):
        notes.append(why)
        return kept
    return cs


def apply(facts: dict[str, Any], text: str) -> bool:
    """Fill drivetrain/transmission facts that follow from model + year. True if the model was found."""
    found = lookup(text, facts.get("year"), facts.get("make"), facts.get("model"))
    if not found:
        return False
    name = f"{found.year} {facts.get('make') or found.make} {facts.get('model') or found.model}"
    facts.setdefault("make", found.make)
    facts.setdefault("model", found.model)
    facts["spec_source"] = "EPA fueleconomy.gov configurations"
    cs = found.configs
    grams = _grams(text)
    notes: list[str] = []
    # A gas model's facts, unless the title says hybrid or electric (a RAV4
    # Hybrid's eCVT shares the 2.5L with the gas car's 6-speed automatic).
    if HYBRID.search(text):
        cs = _narrow(cs, lambda c: c.kind in ("hybrid", "phev"), "hybrid", notes)
    elif ELECTRIC.search(text):
        cs = _narrow(cs, lambda c: c.kind == "ev", "electric", notes)
    else:
        cs = _narrow(cs, lambda c: c.kind == "", "gas", [])
    trims = [c for c in cs if c.variant]
    if trims:
        # "2019 Tesla Model 3 Long Range": the trim settles the range. Longest named trim wins.
        named = {c.variant for c in trims if all(_squash(w) in grams for w in c.variant.split())}
        if named:
            best = max(named, key=len)
            cs = _narrow(cs, lambda c: c.variant == best, best, notes)
    if m := DISPL.search(text):
        cs = _narrow(cs, lambda c: c.displ == m.group(1), f"{m.group(1)}L engine", notes)

    drives = {c.drive for c in cs}
    stated = facts.get("drivetrain")
    if stated:
        want = ALL_WHEEL if stated in ALL_WHEEL else {stated}
        cs = _narrow(cs, lambda c: c.drive in want, DRIVE_LABEL.get(stated, stated.upper()), notes)
    elif drives <= ALL_WHEEL:
        facts["drivetrain"] = "awd" if "awd" in drives else "4wd"
        facts["drivetrain_evidence"] = f"every {name} sold in the US was {'/'.join(DRIVE_LABEL[d] for d in sorted(drives))}"
    elif not drives & ALL_WHEEL:
        facts["drivetrain"] = sorted(drives)[0]
        facts["drivetrain_evidence"] = f"no AWD or 4WD {name} was sold in the US"
    facts["drivetrain_options"] = " or ".join(DRIVE_LABEL[d] for d in sorted(drives))

    qual = f" ({', '.join(notes)})" if notes else ""
    classes = {c.trans for c in cs}
    autos = [c for c in cs if c.trans != "manual"]
    facts["manual_offered"] = "manual" in classes
    if facts.get("transmission") == "manual" and facts.get("transmission_source") == "listing text" \
            and "manual" not in classes:
        # The text said "manual" but no manual was ever offered: the text loses.
        facts["transmission_note"] = "the listing text reads as manual, but none was offered"
        facts["transmission"] = None
    if not facts.get("transmission"):
        if "manual" not in classes:
            facts["transmission"], facts["transmission_source"] = "automatic", "EPA model-year list"
            facts["transmission_evidence"] = f"no manual was offered on a {name}{qual}"
        elif not autos:
            facts["transmission"], facts["transmission_source"] = "manual", "EPA model-year list"
            facts["transmission_evidence"] = f"every {name}{qual} was a manual"
    if autos:
        kinds = {c.trans for c in autos}
        if kinds == {"single"}:
            facts["transmission_type"] = "electric"
        elif kinds == {"cvt"}:
            facts["transmission_type"] = "cvt"
        elif "cvt" in kinds:
            facts["transmission_type"] = "depends-on-engine"
            by_engine: dict[str, set[str]] = {}
            for c in autos:
                by_engine.setdefault(f"{c.displ}L" if c.displ else "electric", set()).add(c.spec)
            facts["engine"] = "; ".join(f"{e}: {' or '.join(sorted(s))}" for e, s in sorted(by_engine.items()))
        else:
            facts["transmission_type"] = "conventional"
    facts["transmission_spec"] = " or ".join(sorted({c.spec for c in cs}))

    electric = [c for c in cs if c.kind in ("ev", "phev")]
    if electric and len(electric) == len(cs):
        facts["powertrain"] = "electric" if all(c.kind == "ev" for c in electric) else "plug-in hybrid"
        ranges = sorted({c.erange for c in electric if c.erange})
        if ranges:
            lo, hi = ranges[0], ranges[-1]
            facts["rated_range_miles"] = [lo, hi]
            what = "range" if facts["powertrain"] == "electric" else "electric-only range"
            span = f"{lo}" if lo == hi else f"{lo}-{hi}"
            facts["range_evidence"] = f"EPA-rated {what} {span} mi when new ({name}{qual}); a used battery has less"
    return True


__all__ = ["Config", "Lookup", "apply", "lookup"]
