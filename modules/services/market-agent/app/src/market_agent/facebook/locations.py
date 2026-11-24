"""Turning "Springfield, IL" into coordinates and a Marketplace city page.

Ported from secondhand-mcp locations.ts. Facebook's city search is literal:
"kansas city, mo" lands on Mound City, Kansas, so the state code is spelled
out, and the bare city name is only a last resort.
"""

from __future__ import annotations

from typing import Any

from ..geo import miles_between
from ..models import Coordinates
from .queries import LOCATION_DOC_ID, location_variables
from .transport import Transport

CITY_PAGE_MAX_MILES = 50

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",  # leak-scan-ok: US state table
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}


class LocationLookupError(Exception):
    """Facebook couldn't be asked; says nothing about whether the place exists."""


def lookup_candidates(query: str) -> list[str]:
    typed = query.strip().lower()
    parts = [p.strip() for p in typed.split(",")]
    city = parts[0]
    state = US_STATES.get(parts[-1].upper()) if len(parts) > 1 else None
    preferred = f"{city}, {state.lower()}" if state else typed
    out: list[str] = []
    for c in (preferred, city):
        if c and c not in out:
            out.append(c)
    return out


def _subtitle_kind(node: dict[str, Any]) -> str | None:
    sub = node.get("subtitle")
    return sub.split(" ·")[0] if sub else None


def _is_city(node: dict[str, Any]) -> bool:
    return _subtitle_kind(node) == "City"


async def places_matching(t: Transport, query: str) -> list[dict[str, Any]]:
    payload = await t.graphql(LOCATION_DOC_ID, location_variables(query))
    edges = ((((payload.get("data") or {}).get("city_street_search") or {}).get("street_results")) or {}).get(
        "edges"
    ) or []
    return [e["node"] for e in edges if e and e.get("node")]


async def coordinates(t: Transport, query: str) -> Coordinates | None:
    """First spelling that resolves; stop at a failure rather than guess looser."""
    try:
        for candidate in lookup_candidates(query):
            places = await places_matching(t, candidate)
            node = next((p for p in places if _is_city(p)), places[0] if places else None)
            if node and node.get("location"):
                loc = node["location"]
                return Coordinates(
                    latitude=loc["latitude"],
                    longitude=loc["longitude"],
                    name=node.get("single_line_address") or candidate,
                )
    except Exception as e:  # noqa: BLE001 - re-raised with the place named
        raise LocationLookupError(f"couldn't look up {query!r} on Facebook: {e}") from e
    return None


async def city_page_id(t: Transport, location: str, near: Coordinates) -> str | None:
    for candidate in lookup_candidates(location):
        try:
            places = await places_matching(t, candidate)
        except Exception:  # noqa: BLE001 - the page fallback is best-effort
            return None
        nearby = []
        for node in places:
            page_id = (node.get("page") or {}).get("id")
            loc = node.get("location")
            if not page_id or not loc:
                continue
            miles = miles_between(near.latitude, near.longitude, loc["latitude"], loc["longitude"])
            if miles <= CITY_PAGE_MAX_MILES:
                nearby.append((miles, node, page_id))
        nearby.sort(key=lambda x: x[0])
        city = next((x for x in nearby if _is_city(x[1])), nearby[0] if nearby else None)
        if city:
            return city[2]
    return None
