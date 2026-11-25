import argparse
import asyncio
from pathlib import Path

from market_agent import cli
from market_agent.config import Settings
from market_agent.models import Candidate, Detail, Evaluation, Listing
from market_agent.pipeline import RunResult
from market_agent.store import Store


def test_watch_notifies_each_new_top_once(tmp_path: Path, monkeypatch):
    s = Settings(home=tmp_path, judge="none")
    s.ensure_dirs()
    from market_agent import brief as briefs

    b = briefs.load(briefs.new_brief("winter", s.briefs_dir, "winter-commuter"))
    top = Candidate(listing=Listing(id="9", title="2008 Honda CR-V", price_text="$4,200", price=4200),
                    evaluation=Evaluation(score=8.0, verdict="top", headline="good"),
                    detail=Detail(id="9", description="AWD, clean title"))
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


def test_read_cap_is_reported_not_silent():
    from market_agent.pipeline import RunResult

    r = RunResult(brief="w")
    r.unread = 7
    assert r.summary()["unread"] == 7


def test_watch_runs_only_requested_and_skips_paused(tmp_path, monkeypatch):
    s = Settings(home=tmp_path, judge="none")
    s.ensure_dirs()
    from market_agent import brief as briefs

    for n in ("a", "b"):
        briefs.new_brief(n, s.briefs_dir, "general")
    p = briefs.load(s.briefs_dir / "b.toml").model_copy(update={"paused": True})
    briefs.save(p, s.briefs_dir)
    ran = []

    async def fake_run(settings, brief, **_kw):
        ran.append(brief.name)
        return RunResult(brief=brief.name, searches=[{"query": "q", "ok": True, "count": 0, "source": "", "error": None}])

    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.setattr(cli, "_notify", lambda *a: None)
    args = argparse.Namespace(page=str(tmp_path / "www/index.html"), page_url=None, notify_cmd=None, title="t")
    cli.cmd_watch(s, args)
    assert ran == ["a"]  # b is paused
    (tmp_path / "requests").mkdir()
    (tmp_path / "requests" / "a").write_text("1")
    ran.clear()
    cli.cmd_watch(s, args)
    assert ran == ["a"] and not any((tmp_path / "requests").iterdir())  # request consumed


def test_watch_for_others_is_kept_but_not_notified(tmp_path: Path, monkeypatch):
    s = Settings(home=tmp_path, judge="none")
    s.ensure_dirs()
    from market_agent import brief as briefs

    b = briefs.load(briefs.new_brief("winter", s.briefs_dir, "winter-commuter"))
    briefs.save(b.model_copy(update={"for_others": True}), s.briefs_dir)
    assert briefs.load(s.briefs_dir / "winter.toml").for_others  # round-trips through the file
    top = Candidate(listing=Listing(id="9", title="2008 Honda CR-V", price_text="$4,200", price=4200),
                    evaluation=Evaluation(score=8.0, verdict="top", headline="good"),
                    detail=Detail(id="9", description="AWD, clean title"))
    st = Store(s.db_path)
    st.save(b.name, top, "k")
    st.close()
    ran = []

    async def fake_run(settings, brief, **_kw):
        ran.append(brief.name)
        return RunResult(brief=brief.name, searches=[{"query": "q", "ok": True, "count": 1, "source": "graphql", "error": None}])

    sent = []
    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.setattr(cli, "_notify", lambda cmd, title, body: sent.append(title))
    args = argparse.Namespace(page=str(tmp_path / "www/index.html"), page_url=None, notify_cmd="x", title="t")
    assert cli.cmd_watch(s, args) == 0
    assert ran == ["winter"] and sent == []  # still searched; nothing sent
    assert "CR-V" in (tmp_path / "www/index.html").read_text()
