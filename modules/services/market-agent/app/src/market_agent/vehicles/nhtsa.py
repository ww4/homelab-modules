"""NHTSA's public APIs: recalls by make/model/year, and VIN decoding.

Only public vehicle identifiers are sent (make, model, year, VIN).
"""

from __future__ import annotations

from typing import Any

import httpx

RECALLS_URL = "https://api.nhtsa.gov/recalls/recallsByVehicle"
VPIC_URL = "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{vin}?format=json"


class NhtsaError(Exception):
    pass


async def recalls(http: httpx.AsyncClient, make: str, model: str, year: int) -> list[dict[str, Any]]:
    try:
        r = await http.get(RECALLS_URL, params={"make": make, "model": model, "modelYear": year}, timeout=15)
        r.raise_for_status()
        results = r.json().get("results")
    except (httpx.HTTPError, ValueError) as e:
        raise NhtsaError(f"recall lookup failed for {year} {make} {model}: {e}") from e
    if not isinstance(results, list):
        raise NhtsaError(f"recall lookup for {year} {make} {model} returned no results list")
    return [
        {
            "campaign": x.get("NHTSACampaignNumber"),
            "component": x.get("Component"),
            "summary": x.get("Summary"),
            "remedy": x.get("Remedy"),
            "date": x.get("ReportReceivedDate"),
            "source": f"https://www.nhtsa.gov/recalls?nhtsaId={x.get('NHTSACampaignNumber')}",
        }
        for x in results
    ]


async def decode_vin(http: httpx.AsyncClient, vin: str) -> dict[str, Any]:
    try:
        r = await http.get(VPIC_URL.format(vin=vin), timeout=15)
        r.raise_for_status()
        row = (r.json().get("Results") or [{}])[0]
    except (httpx.HTTPError, ValueError) as e:
        raise NhtsaError(f"VIN decode failed for {vin}: {e}") from e
    keep = ("ModelYear", "Make", "Model", "Trim", "DriveType", "TransmissionStyle", "TransmissionSpeeds",
            "DisplacementL", "EngineCylinders", "BodyClass", "ErrorCode", "ErrorText")
    return {k: row.get(k) for k in keep if row.get(k)}


def drive_from_vpic(drive_type: str | None) -> str | None:
    if not drive_type:
        return None
    d = drive_type.lower()
    if "awd" in d or "all-wheel" in d or "all wheel" in d:
        return "awd"
    if "4wd" in d or "4x4" in d or "four" in d:
        return "4wd"
    if "fwd" in d or "front" in d:
        return "fwd"
    if "rwd" in d or "rear" in d or "4x2" in d:
        return "rwd"
    return None
