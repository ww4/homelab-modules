"""A listing is judged from its page or not at all.

A rate-limited run once stored 29 table-saw listings with empty descriptions
(the page half of the read failed, the photos half didn't) and the judge wrote
"the ad has no description" about ads that had one, scoring a saw with a dead
motor 7.0.
"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from market_agent import brief as briefs
from market_agent import pipeline
from market_agent.config import Settings
from market_agent.facebook import client as fb_client
from market_agent.facebook import transport as tr
from market_agent.models import Candidate, Coordinates, Detail, Evaluation, Listing, awaiting_read, readable
from market_agent.report import sections_from_store
from market_agent.store import Store

RATE_LIMITED = {"errors": [{"message": "Rate limit exceeded", "severity": "CRITICAL"}]}


def _transport(handler) -> tr.Transport:
    t = tr.Transport(min_interval=0.0)
    t._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return t


def test_rate_limit_backs_off_then_succeeds_and_slows_the_run(monkeypatch):
    monkeypatch.setattr(tr, "RATE_LIMIT_BACKOFF_S", (0.0, 0.0))
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=RATE_LIMITED if len(calls) == 1 else {"data": {"ok": True}})

    t = _transport(handler)
    t._min_interval = 1.0
    monkeypatch.setattr(tr.Transport, "_pace", lambda self: asyncio.sleep(0))
    assert asyncio.run(t.graphql("doc", {}))["data"] == {"ok": True}
    assert len(calls) == 2
    assert t._min_interval == 2.0  # slower for the rest of the run


def test_persistent_rate_limit_raises_rate_limited_not_refusal(monkeypatch):
    monkeypatch.setattr(tr, "RATE_LIMIT_BACKOFF_S", (0.0, 0.0))
    t = _transport(lambda r: httpx.Response(200, json=RATE_LIMITED))
    with pytest.raises(tr.RateLimited):
        asyncio.run(t.graphql("doc", {}))


def test_other_graphql_errors_are_still_refusals():
    t = _transport(lambda r: httpx.Response(200, json={"errors": [{"message": "doc_id unknown"}]}))
    with pytest.raises(tr.RefusalError):
        asyncio.run(t.graphql("doc", {}))


class _T:
    """Answers the two detail queries: photos and info (the page)."""

    def __init__(self, info, photos):
        self.info, self.photos = info, photos

    async def graphql(self, doc_id, variables):
        part = self.info if doc_id == fb_client.DETAIL_INFO_DOC_ID else self.photos
        if isinstance(part, Exception):
            raise part
        return {"data": {"viewer": {"marketplace_product_details_page": {"target": part}}}}


PAGE = {"marketplace_listing_title": "Saw", "redacted_description": {"text": "Motor is shot"},
        "listing_price": {"amount": "400"}, "location": {"latitude": 38.9, "longitude": -84.4}}


def test_detail_raises_when_the_page_half_fails():
    mp = fb_client.Marketplace(_T(tr.RateLimited("limited"), {"listing_photos": []}))
    with pytest.raises(tr.RateLimited):
        asyncio.run(mp.detail("1"))


def test_detail_without_photos_is_still_a_reading():
    mp = fb_client.Marketplace(_T(PAGE, tr.RefusalError("no photos")))
    d = asyncio.run(mp.detail("1"))
    assert d.description == "Motor is shot" and d.missing_parts == ["photos"] and readable(d)


def test_awaiting_read_covers_old_blind_rows():
    ev = Evaluation(score=7.0, verdict="top", headline="no description at all")
    photos_only = Candidate(listing=Listing(id="1", title="Saw", price_text="$300"), evaluation=ev,
                            detail=Detail(id="1", missing_parts=["description"]))
    no_detail = Candidate(listing=Listing(id="2", title="Saw", price_text="$300"), evaluation=ev)
    good = Candidate(listing=Listing(id="3", title="Saw", price_text="$300"), evaluation=ev, detail=Detail(id="3", description="x"))
    excluded_unread = Candidate(listing=Listing(id="4", title="Saw", price_text="$300"))  # dropped on price, never read
    assert awaiting_read(photos_only) and awaiting_read(no_detail)
    assert not awaiting_read(good) and not awaiting_read(excluded_unread)


class _FakeMarket:
    """Search finds a, b, c; reading b hits a rate limit that doesn't clear."""

    reads: list[str] = []

    def __init__(self, transport):
        pass

    async def coordinates(self, place):
        return Coordinates(latitude=38.9, longitude=-84.4)

    async def search(self, q, *a, **k):
        return fb_client.SearchOutcome(q, [Listing(id=i, title=f"Saw {i}", price=300, price_text="$300")
                                           for i in ("a", "b", "c")])

    async def detail(self, lid):
        _FakeMarket.reads.append(lid)
        if lid == "a":
            return Detail(id="a", description="Cabinet saw, runs great", price=300,
                          coords=Coordinates(latitude=38.9, longitude=-84.4))
        raise tr.RateLimited("limited")


def _saw_brief():
    return briefs.Brief.model_validate({
        "name": "saw", "title": "Saw", "kind": "general", "location": {"home": "Springfield, IL", "max_miles": 50},
        "price": {"max": 800}, "search": {"queries": ["table saw"], "price_bands": 1},
    })


def test_rate_limited_run_never_judges_unread_listings(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(pipeline, "Marketplace", _FakeMarket)
    _FakeMarket.reads = []
    s = Settings(home=tmp_path, judge="none")
    b = _saw_brief()
    # An old photos-only reading of c, from before the fix: must be read again, not reused.
    Store(s.db_path).cache_put("detail:c", Detail(id="c", missing_parts=["description"]).model_dump())
    result = asyncio.run(pipeline.run(s, b, judge=False))
    assert _FakeMarket.reads == ["a", "b"]  # stopped reading after the rate limit
    assert result.rate_limited == 2  # b and c, counted for the page
    stored = {c.listing.id for c in Store(s.db_path).all("saw")}
    assert stored == {"a"}
    assert json.loads(json.dumps(result.summary()))["rate_limited"] == 2


def test_page_hides_blind_rows_and_says_so(tmp_path: Path):
    s = Settings(home=tmp_path, judge="none")
    s.ensure_dirs()
    st = Store(s.db_path)
    ev = Evaluation(score=7.0, verdict="top", headline="the ad has no description")
    st.save("saw", Candidate(listing=Listing(id="1", title="Saw", price_text="$300"), evaluation=ev,
                             detail=Detail(id="1", missing_parts=["description"])), "k")
    st.save("saw", Candidate(listing=Listing(id="2", title="Saw 2", price_text="$300"), evaluation=ev,
                             detail=Detail(id="2", description="runs")), "k")
    [sec] = sections_from_store(st, [_saw_brief()])
    assert [c.listing.id for c in sec.candidates] == ["2"] and sec.awaiting == 1
