"""Vehicle hard limits, and the references the judge may cite."""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from ..brief import Vehicle
from ..models import Candidate, Check, Signal
from ..store import Store
from . import nhtsa
from .knowledge import Knowledge

log = logging.getLogger(__name__)

ALL_WHEEL = {"awd", "4wd"}
RECALL_CACHE_SECONDS = 7 * 86400


def _drive_from_generation(drivetrain: str | None) -> str | None:
    return {"awd-standard": "awd", "4wd-standard": "4wd", "fwd-only": "fwd"}.get(drivetrain or "")


def vehicle_checks(rules: Vehicle, facts: dict[str, Any]) -> list[Check]:
    checks: list[Check] = []

    if rules.drivetrain:
        allowed = set(rules.drivetrain)
        drive = facts.get("drivetrain")
        why = facts.get("drivetrain_evidence") or ""
        # Makers use AWD and 4WD loosely (Honda sells its AWD as "Real Time
        # 4WD"), so either satisfies a brief that allows either; the detail
        # keeps the exact word for the judge.
        if drive and (drive in allowed or (drive in ALL_WHEEL and allowed & ALL_WHEEL)):
            checks.append(Check(name="drivetrain", status="pass", detail=f"{drive.upper()} ({why})"))
        elif drive:
            checks.append(Check(name="drivetrain", status="fail", detail=f"{drive.upper()} ({why})"))
        else:
            checks.append(Check(name="drivetrain", status="unknown", detail=f"not established: {why}"))

    if rules.title == "clean":
        t = facts.get("title_status")
        if t == "clean":
            checks.append(Check(name="title", status="pass", detail=f"clean ({facts.get('title_source')})"))
        elif t:
            checks.append(Check(name="title", status="fail", detail=f"{t} ({facts.get('title_source')})"))
        else:
            checks.append(Check(name="title", status="unknown", detail="title status not stated"))

    trans = facts.get("transmission")
    ttype = facts.get("transmission_type")
    if rules.transmission in ("automatic", "conventional-automatic"):
        if trans == "manual":
            checks.append(Check(name="transmission", status="fail", detail="manual"))
        elif rules.transmission == "conventional-automatic" and ttype == "cvt":
            checks.append(Check(name="transmission", status="fail", detail="CVT for this model year"))
        elif trans == "automatic":
            detail = "automatic" + (f" ({facts['transmission_spec']})" if facts.get("transmission_spec") else "")
            checks.append(Check(name="transmission", status="pass", detail=detail))
        else:
            checks.append(Check(name="transmission", status="unknown", detail="not stated"))
    elif rules.transmission == "manual":
        status = "pass" if trans == "manual" else ("fail" if trans == "automatic" else "unknown")
        checks.append(Check(name="transmission", status=status, detail=trans or "not stated"))

    miles = facts.get("mileage")
    if rules.max_mileage:
        if miles is None:
            checks.append(Check(name="mileage", status="unknown", detail="mileage not stated"))
        elif miles > rules.max_mileage:
            checks.append(Check(name="mileage", status="fail", detail=f"{miles:,} mi, over {rules.max_mileage:,}"))
        else:
            checks.append(Check(name="mileage", status="pass", detail=f"{miles:,} mi"))

    year = facts.get("year")
    if rules.min_year or rules.max_year:
        if year is None:
            checks.append(Check(name="year", status="unknown", detail="year not stated"))
        elif (rules.min_year and year < rules.min_year) or (rules.max_year and year > rules.max_year):
            checks.append(Check(name="year", status="fail", detail=f"{year} is outside the brief's years"))
        else:
            checks.append(Check(name="year", status="pass", detail=str(year)))
    return checks


def apply_knowledge(c: Candidate, kb: Knowledge) -> None:
    """Fill make/model/generation facts and attach the table's issue entries."""
    f = c.facts
    text = f"{(c.detail.title if c.detail else None) or c.listing.title}"
    entry = kb.identify(text, f.get("make"), f.get("model"))
    if not entry:
        return
    f["make"], f["model"] = entry.make, entry.model
    gen = entry.generation(f.get("year"))
    if gen:
        f["transmission_spec"] = gen.transmission
        f["transmission_type"] = gen.transmission_type
        if gen.transmission_type == "depends-on-engine":
            if re.search(r"\b2\.5\s*i?\b", text, re.I):
                f["transmission_type"], f["transmission_spec"] = "cvt", f"{gen.transmission} (2.5i in title: CVT)"
            elif re.search(r"\b3\.6\s*r?\b", text, re.I):
                f["transmission_type"], f["transmission_spec"] = "conventional", f"{gen.transmission} (3.6R in title)"
        f["drivetrain_options"] = gen.drivetrain
        if gen.engine:
            f["engine"] = gen.engine
        implied = _drive_from_generation(gen.drivetrain)
        if implied and not f.get("drivetrain"):
            f["drivetrain"] = implied
            f["drivetrain_evidence"] = f"every {entry.make} {entry.model} of this generation is {implied.upper()}"
    for issue in kb.issues_for(entry, f.get("year"), f.get("drivetrain")):
        c.references.append(
            {
                "type": issue.kind,
                "title": issue.title,
                "detail": issue.detail,
                "ask": issue.ask,
                "source": issue.source,
                "verified": issue.verified,
            }
        )


async def apply_nhtsa(c: Candidate, http: httpx.AsyncClient, store: Store) -> None:
    f = c.facts
    if f.get("vin"):
        key = f"vin:{f['vin']}"
        decoded = store.cache_get(key, max_age=None)
        if decoded is None:
            try:
                decoded = await nhtsa.decode_vin(http, f["vin"])
                store.cache_put(key, decoded)
            except nhtsa.NhtsaError as e:
                c.signals.append(Signal(name="VIN decode failed", detail=str(e), severity="info"))
        if decoded:
            c.references.append({"type": "vin_decode", "title": f"VIN {f['vin']}", "detail": decoded,
                                 "source": "https://vpic.nhtsa.dot.gov/", "verified": True})
            drive = nhtsa.drive_from_vpic(decoded.get("DriveType"))
            if drive:
                f["drivetrain"] = drive
                f["drivetrain_evidence"] = f"VIN decode: {decoded['DriveType']}"
    if f.get("make") and f.get("model") and f.get("year"):
        key = f"recalls:{f['make']}:{f['model']}:{f['year']}".lower()
        found = store.cache_get(key, max_age=RECALL_CACHE_SECONDS)
        if found is None:
            try:
                found = await nhtsa.recalls(http, f["make"], f["model"], f["year"])
                store.cache_put(key, found)
            except nhtsa.NhtsaError as e:
                c.signals.append(Signal(name="recall lookup failed", detail=str(e), severity="caution"))
                return
        for r in found:
            c.references.append({"type": "nhtsa_recall", "title": f"Recall {r['campaign']}: {r['component']}",
                                 "detail": r["summary"], "remedy": r["remedy"], "source": r["source"],
                                 "verified": True})
