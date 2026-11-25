"""The watch page's script must parse.

2026-11-24: one unescaped quote in a toast message was a syntax error, and a
browser runs none of a script that doesn't parse: no expanding cards, stars,
gallery, drawer or "not interested", while every Python test passed.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from market_agent.models import Candidate, Evaluation, Listing
from market_agent.report import Section, render
from test_page import _brief


@pytest.mark.parametrize("interactive", [True, False])
def test_page_script_parses(tmp_path: Path, interactive: bool):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available (the package build provides it)")
    c = Candidate(listing=Listing(id="1", title="2008 Honda CR-V", price_text="$4,200", price=4200),
                  evaluation=Evaluation(score=6.0, verdict="consider", headline="ok"))
    html = render([Section(_brief(), [c], None)], tmp_path, interactive=interactive)
    scripts = html.split("<script>")[1:]
    assert scripts, "no script on the page"
    for i, chunk in enumerate(scripts):
        js = tmp_path / f"page{i}.js"
        js.write_text(chunk.split("</script>")[0])
        r = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
