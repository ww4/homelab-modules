import argparse
import asyncio
from pathlib import Path

from market_agent import cli
from market_agent.config import Settings
from market_agent.models import Candidate, Evaluation, Listing
from market_agent.pipeline import RunResult
from market_agent.store import Store


def test_watch_notifies_each_new_top_once(tmp_path: Path, monkeypatch):
    s = Settings(home=tmp_path, judge="none")
    s.ensure_dirs()
    from market_agent import brief as briefs

    b = briefs.load(briefs.new_brief("winter", s.briefs_dir, "winter-commuter"))
    top = Candidate(listing=Listing(id="9", title="2008 Honda CR-V", price_text="$4,200", price=4200),
                    evaluation=Evaluation(score=8.0, verdict="top", headline="good"))
    st = Store(s.db_path)
    st.save(b.name, top, "k")
    st.close()

    async def fake_run(settings, brief, **_kw):
        return RunResult(brief=brief.name, searches=[{"query": "q", "ok": True, "count": 1, "source": "graphql", "error": None}])

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.setattr(cli, "_notify", lambda cmd, title, body: sent.append((title, body)))
    args = argparse.Namespace(page=str(tmp_path / "www/index.html"), page_url="https://example/", notify_cmd="x",
                              title="Marketplace Watch")
    assert cli.cmd_watch(s, args) == 0
    assert len(sent) == 1 and "CR-V" in sent[0][1] and "https://example/" in sent[0][1]
    assert (tmp_path / "www/index.html").stat().st_mode & 0o044  # nginx can read it
    sent.clear()
    assert cli.cmd_watch(s, args) == 0
    assert sent == []  # already notified


def test_watch_alerts_when_every_search_fails(tmp_path: Path, monkeypatch):
    s = Settings(home=tmp_path, judge="none")
    s.ensure_dirs()
    from market_agent import brief as briefs

    briefs.new_brief("winter", s.briefs_dir, "winter-commuter")

    async def broken_run(settings, brief, **_kw):
        return RunResult(brief=brief.name, searches=[{"query": "q", "ok": False, "count": 0, "source": "", "error": "x"}])

    sent = []
    monkeypatch.setattr(cli, "run", broken_run)
    monkeypatch.setattr(cli, "_notify", lambda cmd, title, body: sent.append(title))
    args = argparse.Namespace(page=str(tmp_path / "index.html"), page_url=None, notify_cmd="x", title="t")
    assert cli.cmd_watch(s, args) == 1
    assert any("failing" in t for t in sent)
