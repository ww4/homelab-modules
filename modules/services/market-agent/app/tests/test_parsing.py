from market_agent.facebook.locations import lookup_candidates
from market_agent.facebook.parse import (
    extract_feed_unit_edges,
    extract_json_array,
    needs_search_page,
    parse_listings,
    parse_price,
    read_feed_units,
)


def _edge(i, title="2009 Honda CR-V", price="$5,699", **extra):
    listing = {"id": str(i), "marketplace_listing_title": title, "listing_price": {"formatted_amount": price}}
    listing.update(extra)
    return {"node": {"__typename": "MarketplaceFeedListingStoryObject", "listing": listing}}


def test_parse_price():
    assert parse_price("$5,699") == 5699
    assert parse_price("Free") == 0
    assert parse_price("Price not listed") is None


def test_sold_and_pending_are_dropped():
    edges = [_edge(1), _edge(2, is_sold=True), _edge(3, is_pending=True), _edge(4, title="SOLD - old truck")]
    assert [x.id for x in parse_listings(edges, 24)] == ["1"]


def test_gated_reading_wants_the_page():
    stub = {"node": {"__typename": "Story"}}
    reading = read_feed_units({"edges": [stub, _edge(1)], "page_info": {"has_next_page": True}}, 24, "q")
    assert reading.malformed and needs_search_page(reading, 24)
    full = read_feed_units({"edges": [_edge(i) for i in range(10)]}, 24, "q")
    assert not needs_search_page(full, 24)
    assert needs_search_page(None, 24)


def test_json_array_scan_respects_brackets_in_strings():
    text = 'x"edges":[{"t":"a ] tricky [ title"},{"t":"b"}] tail'
    assert extract_json_array(text, text.index("[")) == [{"t": "a ] tricky [ title"}, {"t": "b"}]


def test_page_extraction_prefers_feed_with_listings():
    import json

    empty = json.dumps({"feed_units": {"edges": []}})
    real = json.dumps({"feed_units": {"edges": [_edge(7), _edge(8)]}})
    html = f"<script>{empty}</script><script>{real}</script>"
    edges = extract_feed_unit_edges(html)
    assert [e["node"]["listing"]["id"] for e in edges] == ["7", "8"]


def test_location_candidates_spell_out_state():
    assert lookup_candidates("Springfield, IL") == ["springfield, illinois", "springfield"]
    assert lookup_candidates("decatur") == ["decatur"]
