import time

from market_agent import brief as briefs
from market_agent.brief import Vehicle
from market_agent.checks import dealer_signals, price_check, shared_text_signals, age_signal
from market_agent.models import Candidate, Detail, Listing
from market_agent.vehicles import knowledge
from market_agent.vehicles.checks import apply_knowledge, vehicle_checks
from market_agent.vehicles.extract import drivetrain_mentions, mileage_from_text, vehicle_facts, vin_from


def test_drivetrain_words():
    assert drivetrain_mentions("2009 Honda CR-V AWD")[0] == "awd"
    assert drivetrain_mentions("2000 toyota tacoma sr5 4x4")[0] == "4wd"
    assert drivetrain_mentions("Front-Wheel Drive model, well-maintained")[0] == "fwd"
    assert drivetrain_mentions("Has the 3.4 motor. It's a pre runner. No four wheel drive.")[0] in ("rwd", "2wd")
    assert drivetrain_mentions("Clean SUV, runs great")[0] is None


def test_mileage_from_text():
    assert mileage_from_text("Driven 166,945 miles") == 166945
    assert mileage_from_text("232k miles. heated leather") == 232000
    assert mileage_from_text("roughly 260xxx on the dash") == 260000
    assert mileage_from_text("Miles are 15x,xxx and one owner") == 150000
    assert mileage_from_text("timing belt done at 100k") is None  # no 'miles' -> not trusted


def test_vin_detection():
    assert vin_from("VIN: 5J6RE4H50AL000395 maintenance receipts", None) == "5J6RE4H50AL000395"
    assert vin_from("no vin here, call 5025551234", None) is None


def _cand(title, desc, vehicle=None, price=5000):
    listing = Listing(id="1", title=title, price_text=f"${price}", price=price)
    detail = Detail(id="1", title=title, description=desc, vehicle=vehicle or {})
    c = Candidate(listing=listing, detail=detail)
    c.facts = vehicle_facts(listing, detail)
    return c


def test_facebook_fields_win_and_are_labelled():
    c = _cand("2013 Honda CRV", "runs great", {
        "vehicle_odometer_data": {"unit": "MILES", "value": 188000},
        "vehicle_title_status": "CLEAN",
        "vehicle_transmission_type": "AUTOMATIC",
    })
    assert c.facts["mileage"] == 188000 and c.facts["mileage_source"] == "Facebook odometer field"
    assert c.facts["title_status"] == "clean"
    assert c.facts["year"] == 2013


def test_rebuilt_in_text_beats_clean_field():
    c = _cand("2014 Subaru Forester", "Runs & drives excellent. KY Rebuilt title", {"vehicle_title_status": "CLEAN"})
    assert c.facts["title_status"] == "branded"


def test_subaru_is_awd_by_generation_and_2011_forester_has_fb25_issue():
    kb = knowledge.load()
    c = _cand("2011 Subaru Forester 2.5X Premium", "Panoramic roof, heated seats")
    apply_knowledge(c, kb)
    assert c.facts["drivetrain"] == "awd"
    titles = [r["title"] for r in c.references]
    assert any("oil consumption" in t for t in titles)
    assert not any("head gasket" in t.lower() for t in titles)


def test_crv_2009_gets_recall_and_ac_bulletin_and_unknown_drive():
    kb = knowledge.load()
    c = _cand("2009 Honda CR-V", "clean, new tires")
    apply_knowledge(c, kb)
    assert c.facts["transmission_type"] == "conventional"
    titles = " | ".join(r["title"] for r in c.references)
    assert "23V-228" in titles and "12-072" in titles
    checks = {x.name: x.status for x in vehicle_checks(Vehicle(drivetrain=["awd", "4wd"]), c.facts)}
    assert checks["drivetrain"] == "unknown"


def test_outback_cvt_fails_conventional_rule():
    kb = knowledge.load()
    c = _cand("2014 Subaru Forester 2.5X", "AWD with automatic cvt", {"vehicle_transmission_type": "AUTOMATIC"})
    apply_knowledge(c, kb)
    checks = {x.name: x.status for x in vehicle_checks(Vehicle(transmission="conventional-automatic"), c.facts)}
    assert checks["transmission"] == "fail"


def test_fwd_fails_awd_brief():
    c = _cand("2006 Honda CRV", "Front-Wheel Drive model, well-maintained")
    checks = {x.name: x.status for x in vehicle_checks(Vehicle(drivetrain=["awd", "4wd"]), c.facts)}
    assert checks["drivetrain"] == "fail"


def test_dealer_template_detected():
    text = ("🚙 2009 HONDA CR-V AWD ✅ CLEAN TITLE ✅ ALL-WHEEL DRIVE ✅ NO RUST ✅ Great on Gas "
            "📍 Financing Available 🇪🇸 Hablamos Español")
    sig = dealer_signals(text)
    assert sig and "financing" in sig[0].detail


def test_shared_boilerplate_flags_both():
    a = _cand("2010 Toyota Highlander · Crome", "This vehicle is in great condition runs well no issues price is negotiable")
    b = _cand("2002 Toyota Tacoma · Crome", "This vehicle is in great condition runs well no issues price is negotiable")
    b.listing.id = "2"
    shared_text_signals([a, b])
    assert any(s.name in ("boilerplate text", "shared description") for s in a.signals)


def test_price_floor_and_age():
    b = briefs.Brief.model_validate({
        "name": "t", "title": "t", "location": {"home": "Springfield, IL"},
        "price": {"min": 2500, "max": 6000}, "search": {"queries": ["x"]},
    })
    assert price_check(b, 2781).status == "pass"
    assert price_check(b, 1500).status == "fail"
    assert price_check(b, 6500).status == "fail"
    assert age_signal(int(time.time()) - 255 * 86400) is not None


def test_examples_are_valid(tmp_path):
    for ex in briefs.EXAMPLES:
        path = briefs.new_brief(f"x-{ex}", tmp_path, ex)
        b = briefs.load(path)
        assert b.name == f"x-{ex}"
    commuter = briefs.load(tmp_path / "x-winter-commuter.toml")
    assert commuter.vehicle and commuter.vehicle.title == "clean"


def test_near_miss_band():
    from market_agent.models import Check
    from market_agent.pipeline import near_miss

    b = briefs.Brief.model_validate({
        "name": "t", "title": "t", "location": {"home": "Springfield, IL", "max_miles": 50},
        "price": {"max": 6000}, "search": {"queries": ["x"]},
    })
    c = _cand("2008 Honda CR-V EX-L 4WD", "")
    c.facts["miles_from_home"] = 58
    c.checks = [Check(name="distance", status="fail", detail="58 mi")]
    assert near_miss(b, c) and "58 mi" in near_miss(b, c)
    c.facts["miles_from_home"] = 80
    assert near_miss(b, c) is None
    c.facts["miles_from_home"] = 55
    c.checks.append(Check(name="title", status="fail", detail="rebuilt"))
    assert near_miss(b, c) is None  # a hard fact rules it out


def test_outback_25i_resolves_to_cvt():
    kb = knowledge.load()
    c = _cand("2013 Subaru Outback 2.5i Premium", "")
    apply_knowledge(c, kb)
    assert c.facts["transmission_type"] == "cvt"


# The listing that exposed three bugs at once (2026-11-24): a 2008 CR-V EX-L
# 45 mi from home as the crow flies, facts written as "Label: value", and no
# Facebook vehicle fields at all. It was excluded on an invented driving
# distance, its mileage and title read as unknown, and it was queued last.
CRV_2008_DESC = (
    "Selling my  2008 Honda CR-V. I've owned it for 3 years with zero major issues.\n"
    "Vehicle Overview:\n• Mileage: 193,000\n• Title: Clean (No accidents)\n"
    "• Engine/Fluids: Absolutely no leaks. Regular oil changes performed.\n"
    "• Recent Replacements:  new starter, alternator, windshield wipers, and intake manifold sensor.\n"
)


def test_labelled_facts_are_read():
    c = _cand("2008 Honda cr-v EXL 4WD", CRV_2008_DESC)
    assert c.facts["mileage"] == 193000
    assert c.facts["title_status"] == "clean"
    assert c.facts["drivetrain"] == "4wd"
    assert mileage_from_text("Odometer reads 128,404") == 128404
    assert mileage_from_text("miles: 150k") == 150000


def test_title_label_bad():
    c = _cand("2012 Forester", "Title: Rebuilt, runs great")
    assert c.facts["title_status"] == "branded"


def test_radius_is_the_default_distance():
    from market_agent.checks import distance_check

    b = briefs.load(briefs.new_brief("w", __import__("pathlib").Path(__import__("tempfile").mkdtemp()), "winter-commuter"))
    assert b.location.distance == "radius"
    assert distance_check(b, 45.2, "radius", "home").status == "pass"
    assert "45 mi from home" in distance_check(b, 45.2, "radius", "home").detail


def test_2008_crv_passes_the_winter_brief():
    kb = knowledge.load()
    c = _cand("2008 Honda cr-v EXL 4WD", CRV_2008_DESC, price=4200)
    apply_knowledge(c, kb)
    rules = Vehicle(drivetrain=["awd", "4wd"], title="clean", transmission="automatic", min_year=2000)
    statuses = {x.name: x.status for x in vehicle_checks(rules, c.facts)}
    assert statuses["drivetrain"] == "pass" and statuses["title"] == "pass"
    assert "fail" not in statuses.values()
