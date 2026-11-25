"""Bookmark semantics: a good listing stays until it sells, vanishes, or you say "not interested".

2026-11-24: the watch page reshuffled after every change. Listings whose score
was invalidated hid in "not scored yet" until a later run re-scored them, the
top pick (sold) dropped silently into "ruled out", and anything that left the
search results was only tracked for keep_days.
"""

import asyncio
import json
import urllib.request
from pathlib import Path

from market_agent import brief as briefs
from market_agent import judge as judge_mod
from market_agent import pipeline
from market_agent.config import Settings
from market_agent.facebook import client as fb_client
from market_agent.models import Coordinates, Detail, Evaluation, Listing
from market_agent.report import render, sections_from_store
from market_agent.store import Store

HOME = Coordinates(latitude=38.9, longitude=-84.4)


class Market:
    """Search returns whatever `listed` holds; pages say what `sold` says."""

    listed: list[str] = []
    sold: set[str] = set()
    reads: list[str] = []

    def __init__(self, transport):
        pass

    async def coordinates(self, place):
        return HOME

    async def search(self, q, *a, **k):
        return fb_client.SearchOutcome(q, [Listing(id=i, title=f"Saw {i}", price=300, price_text="$300")
                                           for i in Market.listed])

    async def detail(self, lid):
        Market.reads.append(lid)
        return Detail(id=lid, title=f"Saw {lid}", description=f"cabinet saw {lid}, runs, owner {lid} selling", price=300, coords=HOME,
                      is_sold=lid in Market.sold)


def _setup(tmp_path, monkeypatch, score=6.0):
    monkeypatch.setattr(pipeline, "Marketplace", Market)
    Market.listed, Market.sold, Market.reads = ["a", "b"], set(), []
    judged: list[str] = []

    async def fake_evaluate(settings, brief, c, photos, workdir, depth="full"):
        judged.append(c.listing.id)
        return Evaluation(score=score, verdict="consider", headline=f"v{judge_mod.JUDGE_VERSION}", depth=depth)

    async def no_photos(*a, **k):
        return []

    monkeypatch.setattr(judge_mod, "evaluate", fake_evaluate)
    monkeypatch.setattr(pipeline, "_download_photos", no_photos)
    s = Settings(home=tmp_path, judge="cli")
    b = briefs.Brief.model_validate({
        "name": "saw", "title": "Saw", "kind": "general", "location": {"home": "Springfield, IL", "max_miles": 50},
        "price": {"max": 800}, "search": {"queries": ["table saw"], "price_bands": 1, "keep_days": 0},
    })
    return s, b, judged


def test_a_score_waiting_to_be_redone_stays_on_the_page(tmp_path, monkeypatch):
    s, b, judged = _setup(tmp_path, monkeypatch)
    asyncio.run(pipeline.run(s, b))
    assert set(judged) == {"a", "b"}  # screen, then the full read for contenders
    # The judge changes and this run can only re-score one listing.
    monkeypatch.setattr(judge_mod, "JUDGE_VERSION", judge_mod.JUDGE_VERSION + 1)
    b.judge.max_candidates = 1
    asyncio.run(pipeline.run(s, b))
    rows = {c.listing.id: c for c in Store(s.db_path).all("saw")}
    waiting = [c for c in rows.values() if c.facts.get("rescore_pending")]
    assert len(waiting) == 1 and waiting[0].evaluation is not None  # old score kept, marked
    # Next run picks it up: its stored key is the old one.
    asyncio.run(pipeline.run(s, b))
    rows = {c.listing.id: c for c in Store(s.db_path).all("saw")}
    assert not any(c.facts.get("rescore_pending") for c in rows.values())


def test_good_listings_are_kept_until_sold(tmp_path, monkeypatch):
    s, b, _ = _setup(tmp_path, monkeypatch)
    asyncio.run(pipeline.run(s, b))
    assert Store(s.db_path).kept("saw") == {"a", "b"}
    Market.listed = []  # both drop out of the search; keep_days is 0
    asyncio.run(pipeline.run(s, b))
    assert {c.listing.id for c in Store(s.db_path).all("saw") if not c.excluded} == {"a", "b"}
    Market.sold = {"a"}
    Market.reads = []
    # Force the daily re-read now.
    monkeypatch.setattr(pipeline, "RECHECK_SECONDS", -1)
    asyncio.run(pipeline.run(s, b))
    [sec] = sections_from_store(Store(s.db_path), [b])
    assert [c.listing.id for c in sec.gone] == ["a"]  # its own list, not "ruled out"
    assert "a" not in {c.listing.id for c in sec.candidates}
    Market.reads = []
    asyncio.run(pipeline.run(s, b))
    assert "a" not in Market.reads  # a sold listing is no longer re-read


def test_not_interested_is_hidden_and_never_reread(tmp_path, monkeypatch):
    s, b, judged = _setup(tmp_path, monkeypatch)
    asyncio.run(pipeline.run(s, b))
    Store(s.db_path).set_dismissed("saw", "b", True)
    Market.reads, judged[:] = [], []
    monkeypatch.setattr(pipeline, "RECHECK_SECONDS", -1)
    monkeypatch.setattr(judge_mod, "JUDGE_VERSION", judge_mod.JUDGE_VERSION + 1)
    asyncio.run(pipeline.run(s, b))
    assert "b" not in Market.reads and "b" not in judged  # even while still in the search results
    [sec] = sections_from_store(Store(s.db_path), [b])
    assert "b" not in {c.listing.id for c in sec.candidates} and [c.listing.id for c in sec.dismissed] == ["b"]
    html = render([sec], s.photos_dir, interactive=True)
    assert "1 marked not interested" in html and 'class="undismiss"' in html and 'class="dismiss"' in html


def test_dismiss_endpoint(tmp_path):
    import threading
    from http.server import ThreadingHTTPServer

    from market_agent.web import _Rerender, make_handler

    s = Settings(home=tmp_path, judge="none")
    s.ensure_dirs()
    briefs.new_brief("winter", s.briefs_dir, "winter-commuter")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(s, _Rerender(lambda: None, delay=0.01)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        def post(body):
            req = urllib.request.Request(f"http://127.0.0.1:{httpd.server_port}/api/dismissed",
                                         data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
            return json.loads(urllib.request.urlopen(req, timeout=5).read())
        assert post({"brief": "winter", "id": "1234567890123", "dismissed": True})["dismissed"] is True
        assert Store(s.db_path).dismissed("winter") == {"1234567890123"}
        post({"brief": "winter", "id": "1234567890123", "dismissed": False})
        assert Store(s.db_path).dismissed("winter") == set()
    finally:
        httpd.shutdown()
