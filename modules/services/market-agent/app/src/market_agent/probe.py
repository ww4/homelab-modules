"""Find Facebook's rate-limit edges on purpose, so normal runs can stay under them.

One session, run while Facebook has been left alone for hours:

1. Burst: one listing-page request every `interval` seconds until the first
   refusal (or `max_requests`). How many got through is the allowance at that
   pace. Sessions at 1.5 s, 3 s and 6 s tell a fixed window (same count at any
   pace) from a refilling one (slower paces get further).
2. Recovery: single requests at growing waits after the wall (1, 2, 4 ... 64
   min). The first answer is how long a block lasts.
3. Re-burst: the same pace again until the wall: a full reset gets about the
   first count again, a partial one less.

Only the listing-page ("info") request is used, against listings already in the
store, so it is the same request a normal run makes. Anything other than an
answer or the rate-limit reply (a login wall, an HTTP error) stops the session:
that is Facebook escalating, and probing further would make it worse.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .config import Settings
from .facebook.queries import DETAIL_INFO_DOC_ID, detail_info_variables
from .facebook.transport import Pace, RefusalError, Transport
from .store import Store

log = logging.getLogger(__name__)

SCHEDULE = (1.5, 3.0, 6.0)  # one automatic session per pace, then stop
RECOVERY_WAITS_MIN = (1, 2, 4, 8, 16, 32, 64)
QUIET_HOURS = 3.0
MAX_ERRORS = 3  # network errors in a row before giving up (not a verdict on the limit)
PROBE = "probe"  # runs-table name for session summaries


@contextlib.contextmanager
def facebook_lock(home: Path, wait: bool = True) -> Iterator[bool]:
    """One thing talks to Facebook at a time: a watch run or a probe, never both.

    Yields False (without waiting) when wait=False and the lock is taken.
    """
    home.mkdir(parents=True, exist_ok=True)
    with open(home / "facebook.lock", "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def sessions_done(store: Store) -> list[dict[str, Any]]:
    import json

    rows = store.db.execute("SELECT summary FROM runs WHERE brief = ? ORDER BY ts", (PROBE,)).fetchall()
    return [s for s in (json.loads(r[0]) for r in rows) if s.get("completed")]


def listing_ids(store: Store, limit: int = 500) -> list[str]:
    rows = store.db.execute("SELECT DISTINCT id FROM listings ORDER BY last_seen DESC LIMIT ?", (limit,)).fetchall()
    return [r[0] for r in rows]


class _Escalated(Exception):
    pass


async def session(settings: Settings, store: Store, interval: float, *, max_requests: int = 400,
                  waits_min: tuple[int, ...] = RECOVERY_WAITS_MIN,
                  sleep=asyncio.sleep, clock=time.time) -> dict[str, Any]:
    ids = listing_ids(store)
    if not ids:
        return {"completed": False, "reason": "no stored listings to read"}
    label = f"probe@{interval:g}s"
    via = "proxy" if settings.proxy else "direct"
    t = Transport(proxy=settings.proxy, pace=Pace(interval=interval, job=f"{label}-{int(clock())}"),
                  on_request=lambda rec: store.log_request(label, via, rec))
    n = 0
    errors = 0

    async def ask() -> bool:
        nonlocal n, errors
        lid = ids[n % len(ids)]
        n += 1
        while True:
            try:
                ok = await t.request_once(DETAIL_INFO_DOC_ID, detail_info_variables(lid), kind="info")
                errors = 0
                return ok
            except RefusalError as e:
                raise _Escalated(str(e)) from e
            except Exception as e:  # noqa: BLE001 - network trouble: retry a few times
                errors += 1
                if errors >= MAX_ERRORS:
                    raise _Escalated(f"{MAX_ERRORS} request errors in a row: {e}") from e

    async def burst() -> tuple[int, float | None]:
        got = 0
        start = clock()
        for _ in range(max_requests):
            if not await ask():
                return got, clock() - start
            got += 1
        return got, None  # never hit the wall

    out: dict[str, Any] = {"interval_s": interval, "via": via, "started": clock(), "max_requests": max_requests}
    try:
        out["burst"], out["burst_seconds"] = await burst()
        if out["burst_seconds"] is None:
            out["completed"] = True
            out["note"] = f"no wall within {max_requests} requests"
            return out
        wall = clock()
        out["recovered_after_min"] = None
        for w in waits_min:
            delay = wall + w * 60 - clock()
            if delay > 0:
                await sleep(delay)
            if await ask():
                out["recovered_after_min"] = w
                break
        if out["recovered_after_min"] is None:
            out["completed"] = True
            out["note"] = f"still limited {waits_min[-1]} min after the wall"
            return out
        out["reburst"], out["reburst_seconds"] = await burst()
        out["completed"] = True
        return out
    except _Escalated as e:
        out["completed"] = False
        out["aborted"] = str(e)[:300]
        return out
    finally:
        out["requests"] = n
        out["finished"] = clock()
        await t.aclose()


def run_auto(settings: Settings, *, allow_direct: bool = False, interval: float | None = None,
             max_requests: int = 400) -> dict[str, Any]:
    """The timer's entry point: the next scheduled session, if it's safe to run one."""
    if not settings.proxy and not allow_direct:
        return {"skipped": "no proxy set; the probe only runs through the VPN"}
    with facebook_lock(settings.home, wait=False) as got:
        if not got:
            return {"skipped": "a watch run is talking to Facebook"}
        store = Store(settings.db_path)
        try:
            done = sessions_done(store)
            if interval is None:
                if len(done) >= len(SCHEDULE):
                    return {"skipped": f"schedule complete ({len(done)} sessions)"}
                interval = SCHEDULE[len(done)]
            last = store.last_request_ts()
            if last and time.time() - last < QUIET_HOURS * 3600:
                return {"skipped": f"Facebook was used {(time.time() - last) / 60:.0f} min ago; "
                                   f"needs {QUIET_HOURS:g} h of quiet"}
            log.info("probe session at one request per %gs", interval)
            res = asyncio.run(session(settings, store, interval, max_requests=max_requests))
            store.record_run(PROBE, res)
            return res
        finally:
            store.close()
