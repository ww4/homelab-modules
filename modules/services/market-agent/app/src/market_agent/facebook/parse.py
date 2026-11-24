"""Reading search results out of GraphQL responses and the HTML search page.

Ported from secondhand-mcp parse.ts / route.ts.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from ..models import Listing

log = logging.getLogger(__name__)

INFORMATIONAL_NODES = {"MarketplaceSearchFeedNoResults"}
MIN_TRUSTED_LISTINGS = 5


@dataclass
class FeedReading:
    listings: list[Listing]
    malformed: bool
    has_next_page: bool


def parse_price(text: str | None) -> float | None:
    if not text:
        return None
    if text.strip().lower() == "free":
        return 0.0
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)", text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _is_informational(edge: dict[str, Any] | None) -> bool:
    return ((edge or {}).get("node") or {}).get("__typename") in INFORMATIONAL_NODES


def _is_stub(edge: dict[str, Any] | None) -> bool:
    node = (edge or {}).get("node")
    return bool(node) and not node.get("listing") and not _is_informational(edge)


def read_feed_units(feed_units: dict[str, Any] | None, limit: int, query: str) -> FeedReading:
    edges = (feed_units or {}).get("edges")
    if not isinstance(edges, list):
        log.warning("unexpected GraphQL response structure")
        return FeedReading([], True, False)
    return FeedReading(
        listings=parse_listings(edges, limit, query),
        malformed=any(_is_stub(e) for e in edges),
        has_next_page=((feed_units or {}).get("page_info") or {}).get("has_next_page") is True,
    )


def page_answered(edges: list[Any]) -> bool:
    """Listings, or an explicit no-results. Placeholder feeds say nothing."""
    return any(((e or {}).get("node") or {}).get("listing") or _is_informational(e) for e in edges)


def _unavailable(listing: dict[str, Any]) -> bool:
    if listing.get("is_sold") is True or listing.get("is_pending") is True or listing.get("is_hidden") is True:
        return True
    if listing.get("is_live") is False:
        return True
    title = (listing.get("marketplace_listing_title") or "").upper()
    return title.startswith("[SOLD]") or title.startswith("SOLD -") or title == "SOLD"


def _location(listing: dict[str, Any]) -> str | None:
    geo = ((listing.get("location") or {}).get("reverse_geocode")) or {}
    city_page = geo.get("city_page") or {}
    if city_page.get("display_name"):
        return city_page["display_name"]
    if geo.get("city"):
        return ", ".join(x for x in (geo.get("city"), geo.get("state")) if x)
    return None


def parse_listings(edges: list[Any], limit: int, query: str = "") -> list[Listing]:
    out: list[Listing] = []
    for edge in edges:
        if len(out) >= limit:
            break
        listing = ((edge or {}).get("node") or {}).get("listing")
        if not listing or not listing.get("id") or _unavailable(listing):
            continue
        price_text = ((listing.get("listing_price") or {}).get("formatted_amount")) or "Price not listed"
        out.append(
            Listing(
                id=str(listing["id"]),
                title=listing.get("marketplace_listing_title") or "Untitled listing",
                price_text=price_text,
                price=parse_price(price_text),
                location_text=_location(listing),
                image=((listing.get("primary_listing_photo") or {}).get("image") or {}).get("uri"),
                seller=(listing.get("marketplace_listing_seller") or {}).get("name"),
                query=query,
            )
        )
    return out


def needs_search_page(reading: FeedReading | None, limit: int) -> bool:
    """GraphQL refused, or handed us the gated version (thin and stubby)."""
    if reading is None:
        return True
    thin = len(reading.listings) < min(limit, MIN_TRUSTED_LISTINGS)
    return thin and (reading.malformed or reading.has_next_page)


def extract_feed_unit_edges(html: str) -> list[Any] | None:
    """The page embeds several feed_units payloads; keep the richest one."""
    best: list[Any] | None = None
    best_rank = (-1, -1)
    anchor = html.find('"feed_units"')
    while anchor != -1:
        edges_at = html.find('"edges":', anchor)
        if edges_at != -1 and edges_at <= anchor + 200:
            edges = extract_json_array(html, html.find("[", edges_at))
            if isinstance(edges, list):
                rank = (
                    sum(1 for e in edges if ((e or {}).get("node") or {}).get("listing")),
                    1 if any(_is_informational(e) for e in edges) else 0,
                )
                if rank > best_rank:
                    best, best_rank = edges, rank
        anchor = html.find('"feed_units"', anchor + 1)
    return best


def extract_json_array(text: str, start: int) -> Any:
    """Balanced-bracket scan that respects strings (titles contain brackets)."""
    if start < 0:
        return None
    depth = 0
    in_string = False
    i = start
    n = len(text)
    while i < n:
        c = text[i]
        if in_string:
            if c == "\\":
                i += 1
            elif c == '"':
                in_string = False
        elif c == '"':
            in_string = True
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except ValueError:
                    return None
        i += 1
    return None
