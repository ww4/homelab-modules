"""The request pace, learned across jobs: back off hard on a wall, recover gently.

Each watch job starts at the interval the last one ended on. A job that hit
the wall leaves the slowed interval behind (Transport doubles it per wall); a
clean job trims it by SPEEDUP, never below the configured base. Over a few
days the starting pace settles just under wherever Facebook's line is, and the
"_pace" rows in the runs table record each step for review.
"""

from __future__ import annotations

import time
from typing import Any

from .facebook.transport import RATE_LIMIT_MAX_INTERVAL_S, Pace
from .store import Store

KEY = "fb:pace"
RUNS_NAME = "_pace"
SPEEDUP = 0.9  # a clean job lets the next one go 10% faster


def load(store: Store, base: float) -> Pace:
    state = store.cache_get(KEY, max_age=None) or {}
    interval = min(max(float(state.get("interval") or base), base), RATE_LIMIT_MAX_INTERVAL_S)
    return Pace(interval=interval, job=time.strftime("%Y%m%d-%H%M%S"))


def next_interval(pace: Pace, base: float) -> float:
    if pace.walls:
        return min(pace.interval, RATE_LIMIT_MAX_INTERVAL_S)  # already slowed for each wall
    return max(base, pace.interval * SPEEDUP)


def save(store: Store, pace: Pace, base: float) -> dict[str, Any] | None:
    """Record this job's pacing and set the next job's start. No requests, no change."""
    if not pace.seq:
        return None
    nxt = next_interval(pace, base)
    counts = {row[0]: row[1] for row in store.db.execute(
        "SELECT outcome, count(*) FROM fb_requests WHERE job = ? GROUP BY outcome", (pace.job,))}
    summary = {
        "job": pace.job, "requests": pace.seq, "outcomes": counts,
        "start_interval": round(pace.start_interval, 3), "end_interval": round(pace.interval, 3),
        "next_interval": round(nxt, 3), "walls": pace.walls, "waited_s": pace.waited_s,
        "gave_up": pace.gave_up,
    }
    store.cache_put(KEY, {"interval": nxt, "job": pace.job})
    store.record_run(RUNS_NAME, summary)
    return summary
