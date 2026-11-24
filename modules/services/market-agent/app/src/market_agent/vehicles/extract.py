"""Vehicle facts from Facebook's fields first, the seller's words second.

Each fact records where it came from, so the judge and the report can tell
"Facebook's odometer field says 188,000" from "the description mentions 188k".
"""

from __future__ import annotations

import re
from typing import Any

from ..models import Detail, Listing

YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-3]\d)\b")

# Words that mean all wheels or four wheels are driven.
_ALL = [
    (re.compile(r"\ball[\s-]*wheel[\s-]*drive\b|\bawd\b", re.I), "awd"),
    (re.compile(r"\b4\s*x\s*4\b|\b4wd\b|\bfour[\s-]*wheel[\s-]*drive\b|\b4[\s-]*wheel[\s-]*drive\b", re.I), "4wd"),
]
_TWO = [
    (re.compile(r"\bfront[\s-]*wheel[\s-]*drive\b|\bfwd\b", re.I), "fwd"),
    (re.compile(r"\brear[\s-]*wheel[\s-]*drive\b|\brwd\b|\b2wd\b|\b4\s*x\s*2\b|\bpre[\s-]?runner\b|\btwo[\s-]*wheel[\s-]*drive\b", re.I), "rwd"),
]
_NEGATION = re.compile(r"(?:\bno\b|\bnot\b|\bisn'?t\b|\bwithout\b|\bnon[\s-]?)\W*(?:\w+\W+){0,2}$", re.I)


def year_from(text: str) -> int | None:
    m = YEAR_RE.search(text or "")
    return int(m.group(1)) if m else None


def drivetrain_mentions(text: str) -> tuple[str | None, str]:
    """(drive, evidence). drive is awd/4wd/fwd/rwd or None when unclear.

    A negated all-wheel mention ("no four wheel drive") counts as two-wheel.
    """
    found_all: list[tuple[str, str]] = []
    found_two: list[tuple[str, str]] = []
    for pattern, label in _ALL:
        for m in pattern.finditer(text):
            before = text[max(0, m.start() - 25) : m.start()]
            if _NEGATION.search(before):
                found_two.append(("2wd", f"says {before.strip()[-12:]} {m.group(0)}".strip()))
            else:
                found_all.append((label, m.group(0)))
    for pattern, label in _TWO:
        for m in pattern.finditer(text):
            found_two.append((label, m.group(0)))
    if found_two and not found_all:
        return found_two[0][0], f"listing says {found_two[0][1]!r}"
    if found_all and not found_two:
        labels = {x[0] for x in found_all}
        drive = "4wd" if labels == {"4wd"} else "awd"
        return drive, f"listing says {found_all[0][1]!r}"
    if found_all and found_two:
        return None, f"listing mentions both {found_all[0][1]!r} and {found_two[0][1]!r}"
    return None, "listing doesn't say"


_MILES_PATTERNS = [
    re.compile(r"\b(\d{1,3}(?:,\d{3})+|\d{4,6})\s*(?:miles|mi\b)", re.I),
    re.compile(r"\b(\d{2,3}(?:\.\d)?)\s*k\s*(?:miles|mi\b)", re.I),
]


_MILES_LABEL = re.compile(
    r"\b(?:mileage|miles|odometer|odo)\b\s*(?:is|are|at|of|reads?)?\s*[:\-–=]?\s*~?\s*(\d{1,3}(?:,\d{3})+|\d{4,6}|\d{2,3}(?:\.\d)?\s*k)\b",
    re.I,
)


def mileage_from_text(text: str) -> int | None:
    # "Mileage: 193,000" - a labelled value beats a number that merely precedes "miles".
    m = _MILES_LABEL.search(text)
    if m:
        raw = m.group(1).lower().replace(",", "").replace(" ", "")
        v = int(float(raw[:-1]) * 1000) if raw.endswith("k") else int(raw)
        if 1000 <= v <= 500000:
            return v
    m = _MILES_PATTERNS[0].search(text)
    if m:
        v = int(m.group(1).replace(",", ""))
        if 1000 <= v <= 500000:
            return v
    m = _MILES_PATTERNS[1].search(text)
    if m:
        v = int(float(m.group(1)) * 1000)
        if 10000 <= v <= 500000:
            return v
    m = re.search(r"\b(\d{2,3})x,?xxx\b|\b(\d{3})xxx\b", text, re.I)
    if m:
        digits = m.group(1) or m.group(2)
        return int(digits) * (10000 if m.group(1) else 1000)
    return None


TITLE_BAD = re.compile(
    r"\b(rebuilt|reconstructed|salvage|branded|flood)\s*(?:title)?\b|\btitle\s*(?:status)?\s*[:\-–]\s*(rebuilt|reconstructed|salvage|branded)\b",
    re.I,
)
TITLE_CLEAN = re.compile(
    r"\bclean\s*(?:ky\s*|oh\s*|in\s*)?title\b|\btitle\s*(?:status)?\s*[:\-–]\s*clean\b|\btitle\s+is\s+clean\b",
    re.I,
)
VIN_RE = re.compile(r"\b([A-HJ-NPR-Z0-9]{17})\b")


def vin_from(text: str, field: str | None) -> str | None:
    if field and VIN_RE.fullmatch(field.strip().upper()):
        return field.strip().upper()
    for m in VIN_RE.finditer(text.upper()):
        v = m.group(1)
        if sum(c.isdigit() for c in v) >= 5 and sum(c.isalpha() for c in v) >= 3:
            return v
    return None


def vehicle_facts(listing: Listing, detail: Detail | None) -> dict[str, Any]:
    d = detail
    v = d.vehicle if d else {}
    title = (d.title if d and d.title else None) or listing.title
    text = f"{title}\n{d.description if d else ''}"
    facts: dict[str, Any] = {}

    facts["year"] = year_from(title) or year_from(text)
    facts["make"] = v.get("vehicle_make_display_name")
    facts["model"] = v.get("vehicle_model_display_name")
    facts["trim"] = v.get("vehicle_trim_display_name")

    odo = v.get("vehicle_odometer_data") or {}
    if odo.get("value"):
        miles = int(odo["value"]) * (1.609 if str(odo.get("unit", "")).upper().startswith("KILO") else 1)
        facts["mileage"] = int(miles)
        facts["mileage_source"] = "Facebook odometer field"
    else:
        m = mileage_from_text(text)
        facts["mileage"] = m
        facts["mileage_source"] = "description" if m else None

    status = v.get("vehicle_title_status")
    bad = TITLE_BAD.search(text)
    if bad:
        facts["title_status"], facts["title_source"] = "branded", f"description says {bad.group(0)!r}"
    elif status:
        facts["title_status"], facts["title_source"] = str(status).lower(), "Facebook title field"
    elif TITLE_CLEAN.search(text):
        facts["title_status"], facts["title_source"] = "clean", "description"
    else:
        facts["title_status"], facts["title_source"] = None, None

    facts["transmission"] = (v.get("vehicle_transmission_type") or "").lower() or None
    if not facts["transmission"]:
        if re.search(r"\b(manual|stick ?shift|\d[\s-]*speed manual)\b", text, re.I):
            facts["transmission"] = "manual"
        elif re.search(r"\bautomatic\b", text, re.I):
            facts["transmission"] = "automatic"
    if re.search(r"\bcvt\b", text, re.I):
        facts["transmission_note"] = "listing mentions CVT"

    drive, evidence = drivetrain_mentions(text)
    facts["drivetrain"], facts["drivetrain_evidence"] = drive, evidence
    facts["vin"] = vin_from(text, v.get("vehicle_identification_number"))
    facts["seller_type_claimed"] = v.get("vehicle_seller_type")
    facts["condition"] = v.get("vehicle_condition")
    return facts
