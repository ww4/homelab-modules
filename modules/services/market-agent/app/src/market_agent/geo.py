"""Distances. Facebook ignores the search radius, so we measure ourselves."""

from __future__ import annotations

import math

import httpx

EARTH_RADIUS_MILES = 3958.8
# Rural roads wind; straight-line x 1.3 is a deliberately cautious
# stand-in for driving distance when no OSRM server is configured.
ROAD_FACTOR = 1.3


def miles_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = math.radians
    h = math.sin(r(lat2 - lat1) / 2) ** 2 + math.cos(r(lat1)) * math.cos(r(lat2)) * math.sin(r(lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(h))


async def driving_miles(
    http: httpx.AsyncClient | None,
    osrm_url: str | None,
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
) -> tuple[float, str]:
    """(miles, method). Falls back to the estimate if OSRM is absent or fails."""
    if osrm_url and http is not None:
        url = f"{osrm_url.rstrip('/')}/route/v1/driving/{lon1},{lat1};{lon2},{lat2}?overview=false"
        try:
            r = await http.get(url, timeout=10)
            r.raise_for_status()
            meters = r.json()["routes"][0]["distance"]
            return meters / 1609.344, "osrm"
        except (httpx.HTTPError, KeyError, IndexError, ValueError):
            pass
    return miles_between(lat1, lon1, lat2, lon2) * ROAD_FACTOR, "estimate"
