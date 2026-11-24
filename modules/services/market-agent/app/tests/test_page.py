from pathlib import Path

from market_agent import brief as briefs
from market_agent.models import Candidate, Check, Evaluation, Listing
from market_agent.report import Section, render


def _brief():
    return briefs.Brief.model_validate({
        "name": "w", "title": "Winter car", "kind": "vehicle", "location": {"home": "Springfield, IL", "max_miles": 50},
        "price": {"min": 2500, "max": 6000}, "search": {"queries": ["x"]}, "vehicle": {"drivetrain": ["awd"]},
    })


def test_page_groups_and_photos(tmp_path: Path):
    from PIL import Image

    (tmp_path / "1").mkdir()
    Image.new("RGB", (1200, 900), (90, 120, 150)).save(tmp_path / "1" / "photo-01.jpg")
    ev = Evaluation(score=8.2, verdict="top", headline="Good car <b>", strengths=["clean"])
    top = Candidate(listing=Listing(id="1", title="2008 Honda CR-V", price_text="$4,200", price=4200), evaluation=ev)
    out = Candidate(listing=Listing(id="2", title="2006 CR-V FWD", price_text="$5,250", price=5250),
                    checks=[Check(name="drivetrain", status="fail", detail="FWD")])
    html = render([Section(_brief(), [top, out], None)], tmp_path)
    assert 'Worth contacting · <span class="count">1</span>' in html
    assert "data:image/jpeg;base64," in html
    assert "Good car &lt;b&gt;" in html  # escaped
    assert html.count('class="card"') == 1  # the excluded one is a list line, not a card


def test_bulletins_are_tags_and_recalls_are_not_double_counted(tmp_path: Path):
    from market_agent.report import _ref_chips

    c = Candidate(listing=Listing(id="1", title="2009 Honda CR-V", price_text="$5,000", price=5000), references=[
        {"type": "recall", "title": "Rear frame corrosion recall (NHTSA 23V-228)", "chip": "Frame-rust recall",
         "source": "https://example/r", "verified": True, "detail": "d", "ask": "a"},
        {"type": "bulletin", "title": "A/C compressor clutch wear (Honda SB 12-072)", "chip": "A/C clutch",
         "verified": True, "detail": "d", "ask": "a"},
        {"type": "pattern", "title": "Rear differential fluid", "chip": "Rear diff fluid", "verified": False},
        {"type": "nhtsa_recall", "title": "Recall 23V228000: STRUCTURE:FRAME", "verified": True},
        {"type": "nhtsa_recall", "title": "Recall 19V182000: AIR BAGS", "verified": True},
    ])
    chips = _ref_chips(c)
    assert 'class="chip recall"' in chips and "Frame-rust recall" in chips
    assert 'class="chip bulletin"' in chips and "Rear diff fluid?" in chips
    assert "1 NHTSA recalls" in chips  # 23V-228 already has its own tag


def test_screened_listing_has_no_full_evaluation(tmp_path: Path):
    from market_agent.report import card

    ev = Evaluation(score=4.0, headline="meh", depth="screen", standout=["new tires", "no records"])
    c = Candidate(listing=Listing(id="2", title="2010 Toyota RAV4", price_text="$4,000", price=4000), evaluation=ev)
    html = card(c, tmp_path, _brief(), starred=False, interactive=True, first_seen=1)
    assert "Full evaluation" not in html and "new tires" in html
    assert 'class="star"' in html and 'data-model="Toyota RAV4"' in html


def test_artifact_render_has_no_stars(tmp_path: Path):
    ev = Evaluation(score=8.0, verdict="top", headline="good")
    c = Candidate(listing=Listing(id="3", title="2008 Honda CR-V", price_text="$4,200", price=4200), evaluation=ev)
    assert 'class="star"' not in render([Section(_brief(), [c], None)], tmp_path, interactive=False)


def test_photos_as_files_gallery_and_sweep(tmp_path: Path):
    from PIL import Image
    from market_agent.report import Photos, card

    src = tmp_path / "src" / "9"
    src.mkdir(parents=True)
    for n in range(1, 9):  # eight photos on disk; the strip shows at most six
        Image.new("RGB", (960, 720), (n * 20, 90, 120)).save(src / f"photo-{n:02d}.jpg")
    out = tmp_path / "www" / "photos"
    (out.parent).mkdir()
    out.mkdir()
    (out / "stale-1-240.jpg").write_bytes(b"x")
    photos = Photos(tmp_path / "src", out)
    ev = Evaluation(score=6.0, headline="ok", depth="full", strengths=["a"])
    c = Candidate(listing=Listing(id="9", title="2011 Subaru Forester", price_text="$5,500", price=5500), evaluation=ev)
    html = card(c, photos, _brief(), starred=False, interactive=False, first_seen=1)
    photos.sweep()
    assert "data:image" not in html and 'src="photos/9-1-520.jpg"' in html
    assert html.count('class="shot') == 6 and 'data-full="photos/9-3-800.jpg"' in html
    assert not (out / "stale-1-240.jpg").exists()  # unreferenced photos are removed
    assert (out / "9-6-800.jpg").stat().st_mode & 0o044  # nginx can read them
