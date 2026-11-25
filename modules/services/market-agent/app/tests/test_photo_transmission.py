"""Transmission from the photos, and "manual" from the text only when it means the gearbox.

Cases from the 2026-11-24 car watch: a 2014 RAV4 ("a manual handbrake") and a
2006 Highlander ("original owners manual") were ruled out as manuals; neither
model was ever sold with one.
"""

import asyncio
from pathlib import Path

from market_agent.config import Settings
from market_agent.models import Candidate, Detail, Listing
from market_agent.report import _transmission_row
from market_agent.store import Store
from market_agent.vehicles import configs, photo_transmission as pt
from market_agent.vehicles.extract import vehicle_facts


def _facts(title, desc, vehicle=None):
    listing = Listing(id="1", title=title, price_text="$5000", price=5000)
    f = vehicle_facts(listing, Detail(id="1", title=title, description=desc, vehicle=vehicle or {}))
    configs.apply(f, title)
    return f


def test_manual_in_a_description_needs_transmission_words():
    for desc in ("tan cloth interior and a manual handbrake", "Also has it's original owners manual.",
                 "manual seats, manual windows", "automatic with manual shift mode"):
        assert _facts("2009 Toyota Tacoma", desc).get("transmission") != "manual", desc
    for desc in ("5-speed manual, new clutch", "manual transmission", "stick shift, runs great", "three pedals"):
        f = _facts("2009 Toyota Tacoma", desc)
        assert f["transmission"] == "manual" and f["transmission_source"] == "listing text", desc
    assert _facts("2005 Toyota Tacoma 4x4 manual", "")["transmission"] == "manual"  # a title means it


def test_epa_overrules_a_text_manual_that_never_existed():
    f = _facts("2014 Toyota RAV4 manual", "")  # no 2014 RAV4 had a manual
    assert f["transmission"] == "automatic" and f["transmission_source"] == "EPA model-year list"
    assert "none was offered" in f["transmission_note"]


def test_needs_check_only_when_a_manual_was_possible_and_unsettled():
    assert not pt.needs_check(_facts("2008 Honda CR-V EX", ""))  # never a manual
    assert pt.needs_check(_facts("2004 Honda CR-V EX", ""))  # manual offered, nothing stated
    assert pt.needs_check(_facts("2009 Toyota Tacoma", "5-speed manual"))  # text only: cross-check
    assert not pt.needs_check(_facts("2009 Toyota Tacoma", "", {"vehicle_transmission_type": "MANUAL"}))


def _raw(t, conf, n=8):
    return {"interior_photo": n, "shifter_visible": True, "transmission": t, "confidence": conf,
            "evidence": "H-pattern knob in a boot" if t == "manual" else "P R N D gate"}


def test_interpret_is_biased_toward_automatic():
    f = {"transmission": None}
    pt.interpret(f, _raw("manual", "low"))
    assert f["transmission"] is None  # low confidence never sets manual
    pt.interpret(f, _raw("manual", "medium"))
    assert f["transmission"] == "manual" and f["transmission_source"] == "photos" and f["transmission_photo"] == 8
    stated = {"transmission": "manual", "transmission_source": "listing text"}
    pt.interpret(stated, _raw("automatic", "medium"))
    assert stated["transmission"] == "manual" and "looks automatic" in stated["transmission_note"]
    pt.interpret(stated, _raw("automatic", "high"))
    assert stated["transmission"] == "automatic"


def test_apply_downloads_asks_once_and_caches(tmp_path: Path, monkeypatch):
    from market_agent import judge

    s = Settings(home=tmp_path, judge="cli")
    s.ensure_dirs()
    st = Store(s.db_path)
    c = Candidate(listing=Listing(id="9", title="2004 Honda CR-V", price_text="$4000"),
                  detail=Detail(id="9", images=[f"https://x/{i}.jpg" for i in range(10)]))
    c.facts = {"transmission": None}
    asked, fetched = [], []

    async def fake_ask(settings, **kw):
        asked.append(kw["photos"])
        return _raw("manual", "high", 9)

    async def download(cand, dest, n):
        fetched.append(n)
        dest.mkdir(parents=True, exist_ok=True)
        return [dest / f"photo-{i:02d}.jpg" for i in range(1, 11)]

    monkeypatch.setattr(judge, "ask", fake_ask)
    asyncio.run(pt.apply(c, s, st, download))
    assert fetched == [pt.MAX_PHOTOS] and len(asked[0]) == 10 and c.facts["transmission"] == "manual"
    c.facts = {"transmission": None}
    asyncio.run(pt.apply(c, s, st, download))
    assert len(asked) == 1 and c.facts["transmission"] == "manual"  # cached: no second call


def test_card_row_shows_the_known_transmission_and_where_it_was_seen():
    assert _transmission_row({"transmission": "manual", "transmission_source": "photos", "transmission_photo": 9,
                              "transmission_spec": "4-speed automatic or 5-speed manual"}) == [
        ("Transmission", "5-speed manual (seen in photo 9)")]
    assert _transmission_row({"transmission_spec": "4-speed automatic or 5-speed manual"}) == [
        ("Transmission", "4-speed automatic or 5-speed manual — not stated")]
    assert _transmission_row({"transmission": "automatic", "transmission_spec": "5-speed automatic"}) == [
        ("Transmission", "5-speed automatic")]
