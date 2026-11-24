"""Marketplace search and listing details."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ..models import Coordinates, Detail, Listing
from . import locations
from .parse import (
    FeedReading,
    extract_feed_unit_edges,
    needs_search_page,
    page_answered,
    parse_listings,
    read_feed_units,
)
from .queries import (
    API_PAGE_SIZE,
    DETAIL_INFO_DOC_ID,
    DETAIL_PHOTOS_DOC_ID,
    SEARCH_DOC_ID,
    clamp_radius,
    detail_info_variables,
    detail_photos_variables,
    search_page_url,
    search_variables,
)
from .transport import RefusalError, Transport

log = logging.getLogger(__name__)


@dataclass
class SearchOutcome:
    query: str
    listings: list[Listing] = field(default_factory=list)
    ok: bool = True
    source: str = ""  # "graphql" | "page" | ""
    error: str | None = None


class ListingGone(Exception):
    pass


class Marketplace:
    def __init__(self, transport: Transport) -> None:
        self.t = transport
        self._page_ids: dict[str, str | None] = {}

    async def coordinates(self, place: str) -> Coordinates | None:
        return await locations.coordinates(self.t, place)

    async def search(
        self,
        query: str,
        location: str,
        coords: Coordinates,
        *,
        radius_miles: float,
        min_price: float | None = None,
        max_price: float | None = None,
        listed_within_days: int | None = None,
        limit: int = API_PAGE_SIZE,
    ) -> SearchOutcome:
        radius = clamp_radius(radius_miles)
        reading: FeedReading | None = None
        graph_error: str | None = None
        try:
            payload = await self.t.graphql(
                SEARCH_DOC_ID,
                search_variables(
                    query, coords.latitude, coords.longitude, limit, min_price, max_price, radius, listed_within_days
                ),
            )
            feed = (((payload.get("data") or {}).get("marketplace_search")) or {}).get("feed_units")
            reading = read_feed_units(feed, limit, query)
        except RefusalError as e:
            graph_error = str(e)
            log.info("graphql search refused for %r: %s", query, e)
        except Exception as e:  # noqa: BLE001 - transport exhausted its retries
            return SearchOutcome(query, ok=False, error=f"search failed: {e}")

        page: list[Listing] | None = None
        if needs_search_page(reading, limit):
            page = await self._search_page(query, location, coords, radius, min_price, max_price, listed_within_days, limit)

        if reading is None:
            if page is not None:
                return SearchOutcome(query, page, source="page")
            return SearchOutcome(query, ok=False, error=graph_error or "search refused and page unreadable")
        if page is not None and len(page) > len(reading.listings):
            return SearchOutcome(query, page, source="page")
        if page is None and reading.malformed and not reading.listings:
            return SearchOutcome(
                query, ok=False, error="unexpected response from Facebook and the search page was unreadable (doc_id may need updating)"
            )
        return SearchOutcome(query, reading.listings, source="graphql")

    async def _search_page(
        self,
        query: str,
        location: str,
        coords: Coordinates,
        radius: float,
        min_price: float | None,
        max_price: float | None,
        days: int | None,
        limit: int,
    ) -> list[Listing] | None:
        try:
            if location not in self._page_ids:
                self._page_ids[location] = await locations.city_page_id(self.t, location, coords)
            page_id = self._page_ids[location]
            if not page_id:
                return None
            html = await self.t.search_page(search_page_url(page_id, query, min_price, max_price, radius, days))
            edges = extract_feed_unit_edges(html)
            if not edges or not page_answered(edges):
                return None
            return parse_listings(edges, limit, query)
        except Exception as e:  # noqa: BLE001 - fallback path; caller reports overall failure
            log.info("search page fallback failed for %r: %s", query, e)
            return None

    async def detail(self, listing_id: str) -> Detail:
        photos, info = await asyncio.gather(
            self.t.graphql(DETAIL_PHOTOS_DOC_ID, detail_photos_variables(listing_id)),
            self.t.graphql(DETAIL_INFO_DOC_ID, detail_info_variables(listing_id)),
            return_exceptions=True,
        )
        p = _target(photos)
        i = _target(info)
        if p is None and i is None:
            err = next((x for x in (photos, info) if isinstance(x, Exception)), None)
            if err:
                raise err
            raise ListingGone(f"Facebook returned no listing {listing_id}; it may have been removed")
        missing = [name for name, part in (("photos", p), ("description", i)) if part is None]
        i = i or {}
        loc = i.get("location") or None
        price = (i.get("listing_price") or {}).get("amount")
        return Detail(
            id=listing_id,
            title=i.get("marketplace_listing_title"),
            price=float(price) if price not in (None, "") else None,
            description=((i.get("redacted_description") or {}).get("text")) or "",
            images=[ph["image"]["uri"] for ph in ((p or {}).get("listing_photos") or []) if ph and (ph.get("image") or {}).get("uri")],
            location_text=(i.get("location_text") or {}).get("text"),
            coords=Coordinates(latitude=loc["latitude"], longitude=loc["longitude"]) if loc and "latitude" in loc else None,
            created_at=i.get("creation_time"),
            is_sold=bool(i.get("is_sold")),
            is_pending=bool(i.get("is_pending")),
            condition=i.get("condition"),
            vehicle={k: v for k, v in i.items() if k.startswith("vehicle_") and v not in (None, [], {})},
            missing_parts=missing,
            fetched_at=int(time.time()),
        )


def _target(result: Any) -> dict[str, Any] | None:
    if isinstance(result, Exception):
        log.info("listing detail request failed: %s", result)
        return None
    return ((((result or {}).get("data") or {}).get("viewer") or {}).get("marketplace_product_details_page") or {}).get(
        "target"
    )
