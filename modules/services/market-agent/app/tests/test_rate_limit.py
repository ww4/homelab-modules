"""Rate limits: shared pacing across a job, a log of every request, and the probe."""

import asyncio
from pathlib import Path

import httpx

from market_agent import probe
from market_agent.config import Settings
from market_agent.facebook import transport as tr
from market_agent.models import Candidate, Listing
from market_agent.store import Store

LIMITED = {"errors": [{"message": "Rate limit exceeded", "severity": "CRITICAL"}]}
OK = {"data": {"ok": True}}


def _transport(handler, **kw) -> tr.Transport:
    t = tr.Transport(**kw)
    t._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return t


def _no_pacing(monkeypatch):
    async def instant(self):
        self.pace.seq += 1
        return self.pace.seq, None

    monkeypatch.setattr(tr.Transport, "_pace", instant)


def test_a_wall_slows_the_next_watch_and_spends_one_job_budget(monkeypatch):
    monkeypatch.setattr(tr, "RATE_LIMIT_WAITS_S", (60.0,))
    _no_pacing(monkeypatch)
    pace = tr.Pace(interval=1.5, backoff_left_s=0.0)  # budget already spent: no waiting
    first = _transport(lambda r: httpx.Response(200, json=LIMITED), pace=pace)
    try:
        asyncio.run(first.graphql("doc", {}))
        raise AssertionError("expected RateLimited")
    except tr.RateLimited:
        pass
    # The next watch in the same job shares the slowdown instead of starting fresh.
    second = _transport(lambda r: httpx.Response(200, json=OK), pace=pace)
    assert second.pace.interval == 3.0


def test_backoff_budget_runs_out_across_the_job(monkeypatch):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(tr.asyncio, "sleep", fake_sleep)
    _no_pacing(monkeypatch)
    pace = tr.Pace(interval=1.5)
    t = _transport(lambda r: httpx.Response(200, json=LIMITED), pace=pace)
    try:
        asyncio.run(t.graphql("doc", {}))
    except tr.RateLimited:
        pass
    assert sleeps == list(tr.RATE_LIMIT_WAITS_S)  # 1, 2, 4, 8, 16 minutes
    assert pace.backoff_left_s == 0
    sleeps.clear()
    try:
        asyncio.run(_transport(lambda r: httpx.Response(200, json=LIMITED), pace=pace).graphql("doc", {}))
    except tr.RateLimited:
        pass
    assert sleeps == []  # the job's budget is spent: give up at once


def test_concurrent_requests_wait_out_one_wall_together(monkeypatch):
    sleeps = []
    real_sleep = asyncio.sleep

    async def fake_sleep(s):
        sleeps.append(s)
        await real_sleep(0)  # let the other request reach the wall meanwhile

    monkeypatch.setattr(tr.asyncio, "sleep", fake_sleep)
    _no_pacing(monkeypatch)
    calls = []

    def handler(r):
        calls.append(1)
        return httpx.Response(200, json=LIMITED if len(calls) <= 2 else OK)

    t = _transport(handler)

    async def both():
        return await asyncio.gather(t.graphql("a", {}), t.graphql("b", {}))

    assert [x["data"] for x in asyncio.run(both())] == [{"ok": True}, {"ok": True}]
    assert sleeps == [tr.RATE_LIMIT_WAITS_S[0]]  # one wait, not one per request


def test_every_request_is_logged(tmp_path: Path, monkeypatch):
    _no_pacing(monkeypatch)
    monkeypatch.setattr(tr, "RATE_LIMIT_WAITS_S", ())
    st = Store(tmp_path / "db.sqlite")
    replies = iter([OK, LIMITED])
    t = _transport(lambda r: httpx.Response(200, json=next(replies)), pace=tr.Pace(job="j1"),
                   on_request=lambda rec: st.log_request("w", "proxy", rec))
    asyncio.run(t.graphql("doc", {}, kind="info"))
    try:
        asyncio.run(t.graphql("doc", {}, kind="photos"))
    except tr.RateLimited:
        pass
    rows = st.db.execute("SELECT job, seq, brief, via, kind, outcome, status FROM fb_requests ORDER BY ts").fetchall()
    assert [tuple(r) for r in rows] == [("j1", 1, "w", "proxy", "info", "ok", 200),
                                        ("j1", 2, "w", "proxy", "photos", "rate_limit", 200)]
    detail = st.db.execute("SELECT detail, latency_ms FROM fb_requests WHERE outcome = 'rate_limit'").fetchone()
    assert "Rate limit" in detail[0] and detail[1] >= 0
    assert st.last_request_ts() is not None


def _probe_settings(tmp_path: Path, proxy: str | None = "http://127.0.0.1:8888") -> Settings:
    s = Settings(home=tmp_path, judge="none", proxy=proxy)
    s.ensure_dirs()
    st = Store(s.db_path)
    for i in range(3):
        st.save("w", Candidate(listing=Listing(id=f"1000{i}", title="x", price_text="$1", price=1)), None)
    st.close()
    return s


def _fake_transport(monkeypatch, replies):
    """Probe transport whose requests answer from `replies` (True = ok, False = limited, Exception)."""
    seq = iter(replies)
    real = tr.Transport

    def make(**kw):
        def handler(r):
            nxt = next(seq)
            if isinstance(nxt, Exception):
                return httpx.Response(403)
            return httpx.Response(200, json=OK if nxt else LIMITED)

        t = real(**kw)
        t._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return t

    monkeypatch.setattr(probe, "Transport", make)
    _no_pacing(monkeypatch)


def test_probe_session_finds_burst_recovery_and_reburst(tmp_path: Path, monkeypatch):
    s = _probe_settings(tmp_path)
    # 5 answered, wall; still limited at +1 and +2 min, back at +4; then 3 more and the wall.
    _fake_transport(monkeypatch, [True] * 5 + [False, False, False, True] + [True] * 3 + [False])
    now = [1000.0]

    async def fake_sleep(d):
        now[0] += d

    st = Store(s.db_path)
    res = asyncio.run(probe.session(s, st, 1.5, sleep=fake_sleep, clock=lambda: now[0]))
    assert res["completed"] and res["burst"] == 5 and res["recovered_after_min"] == 4 and res["reburst"] == 3
    assert res["via"] == "proxy"
    logged = st.db.execute("SELECT count(*) FROM fb_requests WHERE brief = 'probe@1.5s'").fetchone()[0]
    assert logged == res["requests"] == 13


def test_probe_stops_when_facebook_escalates(tmp_path: Path, monkeypatch):
    s = _probe_settings(tmp_path)
    _fake_transport(monkeypatch, [True, True, RuntimeError("login wall")])
    st = Store(s.db_path)
    res = asyncio.run(probe.session(s, st, 1.5))
    assert res["completed"] is False and "403" in res["aborted"]


def test_probe_auto_refuses_without_proxy_and_while_a_watch_runs(tmp_path: Path, monkeypatch):
    assert "no proxy" in probe.run_auto(_probe_settings(tmp_path, proxy=None))["skipped"]
    s = _probe_settings(tmp_path)
    with probe.facebook_lock(s.home):
        assert "watch run" in probe.run_auto(s)["skipped"]
    # Recent Facebook traffic: wait for quiet.
    st = Store(s.db_path)
    st.log_request("w", "proxy", {"job": "j", "kind": "info", "outcome": "ok"})
    st.close()
    assert "quiet" in probe.run_auto(s)["skipped"]


def test_probe_schedule_advances_then_stops(tmp_path: Path, monkeypatch):
    s = _probe_settings(tmp_path)
    seen = []

    async def fake_session(settings, store, interval, **kw):
        seen.append(interval)
        return {"completed": True, "interval_s": interval}

    monkeypatch.setattr(probe, "session", fake_session)
    for _ in range(4):
        probe.run_auto(s)
    assert seen == list(probe.SCHEDULE)
    assert "complete" in probe.run_auto(s)["skipped"]


def test_jitter_never_a_metronome_and_pauses_sometimes(monkeypatch):
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(tr.asyncio, "sleep", fake_sleep)
    clock = [1000.0]
    monkeypatch.setattr(tr.time, "monotonic", lambda: clock[0])
    t = tr.Transport(pace=tr.Pace(interval=2.0))

    async def go():
        for _ in range(200):
            await t._pace()

    asyncio.run(go())
    gaps = [s for s in slept]  # the clock never moves, so each wait is the whole target gap
    assert len(set(round(g, 3) for g in gaps)) > 150  # not a metronome
    assert min(gaps) >= 2.0 * tr.JITTER[0] - 1e-9
    assert sum(1 for g in gaps if g > 2.0 * tr.JITTER[1]) >= 200 // tr.PAUSE_EVERY[1]  # reading pauses


def test_pace_is_learned_across_jobs(tmp_path: Path):
    from market_agent import pacing

    st = Store(tmp_path / "db.sqlite")
    p = pacing.load(st, 1.5)
    assert p.interval == 1.5
    p.seq, p.walls, p.interval = 40, 1, 3.0  # this job hit the wall once
    assert pacing.save(st, p, 1.5)["next_interval"] == 3.0
    p = pacing.load(st, 1.5)
    assert p.interval == 3.0  # the next job starts slow
    p.seq = 40  # a clean job
    pacing.save(st, p, 1.5)
    assert pacing.load(st, 1.5).interval == 2.7  # then speeds up gently
    q = pacing.load(st, 1.5)
    assert pacing.save(st, q, 1.5) is None  # no requests: no change
    for _ in range(30):
        q = pacing.load(st, 1.5)
        q.seq = 5
        pacing.save(st, q, 1.5)
    assert pacing.load(st, 1.5).interval == 1.5  # never faster than the base
    rows = st.db.execute("SELECT count(*) FROM runs WHERE brief = '_pace'").fetchone()[0]
    assert rows == 32
