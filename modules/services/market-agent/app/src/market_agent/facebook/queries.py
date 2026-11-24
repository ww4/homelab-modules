"""GraphQL document ids and request variables (from secondhand-mcp queries.ts).

The doc_ids are Facebook frontend internals. When search starts coming back
empty everywhere, these are the first suspects.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

LOCATION_DOC_ID = "5585904654783609"
SEARCH_DOC_ID = "27517490627932547"
DETAIL_PHOTOS_DOC_ID = "10059604367394414"
DETAIL_INFO_DOC_ID = "26090240497332612"

DEFAULT_RADIUS_MILES = 25
MAX_RADIUS_MILES = 500
KM_PER_MILE = 1.609
API_PAGE_SIZE = 24
MAX_PRICE_SENTINEL = 214748364700  # Facebook's "no upper limit"


def clamp_radius(miles: float | None) -> float:
    if not miles or miles <= 0:
        return DEFAULT_RADIUS_MILES
    return min(miles, MAX_RADIUS_MILES)


def location_variables(query: str) -> dict[str, Any]:
    return {
        "params": {
            "caller": "MARKETPLACE",
            "page_category": ["CITY", "SUBCITY", "NEIGHBORHOOD", "POSTAL_CODE"],
            "query": query,
        }
    }


def search_variables(
    query: str,
    lat: float,
    lon: float,
    limit: int,
    min_price: float | None,
    max_price: float | None,
    radius_miles: float,
    listed_within_days: int | None = None,
) -> dict[str, Any]:
    # Facebook's own client sends every field below; leaving the newer ones out
    # makes the query return data-less story stubs.
    return {
        "buyLocation": {"latitude": lat, "longitude": lon},
        "contextual_data": None,
        "count": min(limit, API_PAGE_SIZE),
        "cursor": None,
        "params": {
            "bqf": {"callsite": "COMMERCE_MKTPLACE_WWW", "query": query},
            "browse_request_params": {
                "commerce_enable_local_pickup": True,
                "commerce_enable_shipping": True,
                "commerce_search_and_rp_available": True,
                "commerce_search_and_rp_category_id": [],
                "commerce_search_and_rp_condition": None,
                "commerce_search_and_rp_ctime_days": listed_within_days or None,
                "filter_location_latitude": lat,
                "filter_location_longitude": lon,
                "filter_price_lower_bound": int(min_price or 0),
                "filter_price_upper_bound": int(max_price) if max_price else MAX_PRICE_SENTINEL,
                "filter_radius_km": round(radius_miles * KM_PER_MILE),
            },
            "custom_request_params": {
                "browse_context": None,
                "contextual_filters": [],
                "referral_code": None,
                "referral_ui_component": None,
                "saved_search_strid": None,
                "search_vertical": "C2C",
                "seo_url": None,
                "serp_landing_settings": {"virtual_category_id": ""},
                "surface": "SEARCH",
                "virtual_contextual_filters": [],
            },
        },
        "savedSearchID": None,
        "savedSearchQuery": query,
        "scale": 2,
        "shouldDeferNonCritical": False,
        "shouldIncludePopularSearches": False,
        "topicPageParams": {"location_id": None, "url": None},
        "__relay_internal__pv__GHLShouldChangeMarketplaceSponsoredDataFieldNamerelayprovider": True,
    }


def search_page_url(
    page_id: str,
    query: str,
    min_price: float | None,
    max_price: float | None,
    radius_miles: float,
    listed_within_days: int | None = None,
) -> str:
    params: dict[str, str] = {"query": query}
    if min_price is not None:
        params["minPrice"] = str(int(min_price))
    if max_price is not None:
        params["maxPrice"] = str(int(max_price))
    params["radius"] = str(round(radius_miles))
    if listed_within_days:
        params["daysSinceListed"] = str(listed_within_days)
    return f"https://www.facebook.com/marketplace/{page_id}/search?{urlencode(params)}"


def detail_photos_variables(listing_id: str) -> dict[str, Any]:
    return {"targetId": listing_id}


def detail_info_variables(listing_id: str) -> dict[str, Any]:
    return {
        "targetId": listing_id,
        "scale": 2,
        "feedbackSource": 56,
        "feedLocation": "MARKETPLACE_MEGAMALL",
        "referralCode": "marketplace_top_picks",
        "enableJobEmployerActionBar": False,
        "enableJobSeekerActionBar": False,
        "useDefaultActor": False,
        "__relay_internal__pv__CometUFICommentActionLinksRewriteEnabledrelayprovider": False,
        "__relay_internal__pv__CometUFICommentAvatarStickerAnimatedImagerelayprovider": False,
        "__relay_internal__pv__CometUFIReactionsEnableShortNamerelayprovider": False,
        "__relay_internal__pv__CometUFIShareActionMigrationrelayprovider": True,
        "__relay_internal__pv__CometUFI_dedicated_comment_routable_dialog_gkrelayprovider": False,
        "__relay_internal__pv__GHLShouldChangeAdIdFieldNamerelayprovider": True,
        "__relay_internal__pv__GHLShouldChangeSponsoredDataFieldNamerelayprovider": True,
        "__relay_internal__pv__IsWorkUserrelayprovider": False,
        "__relay_internal__pv__ShouldUpdateMarketplaceBoostListingBoostedStatusrelayprovider": False,
    }


def dumps(v: dict[str, Any]) -> str:
    return json.dumps(v, separators=(",", ":"))
