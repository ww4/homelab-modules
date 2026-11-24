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
    assert "Worth contacting · 1" in html
    assert "data:image/jpeg;base64," in html
    assert "Good car &lt;b&gt;" in html  # escaped
    assert html.count('class="card"') == 1  # the excluded one is a list line, not a card
